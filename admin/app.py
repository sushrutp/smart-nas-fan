#!/usr/bin/env python3
"""smart-nas-fan / admin center — neon control UI on :6767.

Backend: FastAPI. Serves the dashboard, live status APIs, config editor.
Auth: simple login form -> bearer token (ADMIN_USER / ADMIN_PASS env).
Fan control itself stays on Proxmox (fan_controller.py); this app only
monitors + edits config. No new inbound ports besides 6767.
"""
import glob
import asyncio
import io
import csv
import json
import os
import re
import secrets
import shlex
import socket
import sqlite3
import ssl
import time
from datetime import datetime, timezone

import requests
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.environ.get("NASTEMP_CONFIG", os.path.join(os.path.dirname(BASE), "config.yaml"))
try:
    with open(os.path.join(os.path.dirname(BASE), "VERSION")) as _vf:
        VERSION = _vf.read().strip()
except Exception:
    VERSION = "1.0"
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS") or ""  # NO default password — fail fast if unset
DEBUG = (os.environ.get("ADMIN_DEBUG", "") or os.environ.get("NASTEMP_DEBUG", "")) not in ("", "0", "no", "false")
TOKEN_TTL = 12 * 3600  # bearer tokens expire (limits blast radius if one leaks, e.g. access logs)
_tokens: dict[str, float] = {}
_auth = HTTPBearer(auto_error=False)

def _valid_token(tok):
    ts = _tokens.get(tok or "")
    if ts is None:
        return False
    if time.time() - ts > TOKEN_TTL:
        _tokens.pop(tok, None)
        return False
    return True

def debug(msg):
    if DEBUG:
        print(f"admin DEBUG: {msg}", flush=True)

def _mask(v):
    s = str(v or "")
    return "(empty)" if not s else f"***len{len(s)}"

_netlog: list = []          # ring buffer: last ~60 connection events (console + GUI)
_link_state: dict = {}      # subsystem -> "up"/"down" (transition-only logging)


def note(sub, msg):
    """Always-on console line for connection events (WS/MQTT fallbacks, reconnects).

    Unlike debug() this prints WITHOUT needing ADMIN_DEBUG, and is kept in a
    ring buffer served at GET /api/netlog so the GUI shows it too."""
    line = (f"{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')} "
            f"net[{sub}] {msg}")
    print(line, flush=True)
    with _netlog_lock:
        _netlog.append(line)
        del _netlog[:-60]


def mark(sub, ok, msg_ok="", msg_fail=""):
    """Log a subsystem transition once (no per-retry spam during long outages).

    Returns the new state ("up"/"down"). First call for a subsystem always logs,
    so a healthy boot still proves in the console which transport is live."""
    cur = "up" if ok else "down"
    if _link_state.get(sub) != cur:
        _link_state[sub] = cur
        note(sub, (msg_ok or "up") if ok else (msg_fail or "down"))
    return cur

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")

def _expand_env(obj):
    """Secrets live in env/.env, never in files: expands $VAR / ${VAR} / ${VAR:-default}."""
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str):
        def sub(m):
            if m.group(3) is not None:  # $VAR -> "" if unset/empty
                return os.environ.get(m.group(3), "")
            v = os.environ.get(m.group(1))  # ${VAR} / ${VAR:-default}
            if v:
                return v
            return m.group(2) if m.group(2) is not None else ""
        return _ENV_RE.sub(sub, obj)
    return obj

app = FastAPI(title="smart-nas-fan admin", docs_url=None, redoc_url=None, openapi_url=None)


def load_cfg():
    with open(cfg_path(), "r") as f:
        return _expand_env(yaml.safe_load(f))


def check_auth(creds: HTTPAuthorizationCredentials = Depends(_auth)):
    if creds is None or not _valid_token(creds.credentials):
        raise HTTPException(status_code=401, detail="login required")
    return True


# ---------- proxmox access: local files OR remote over SSH ----------
# Local mode (default): GUI runs on the Proxmox host, reads /sys + /var/log + /run directly.
# Remote mode: set PROXMOX_HOST -> the SAME files are fetched over SSH (key auth),
# so the GUI can live on any VM/host with IP access. TrueNAS/MQTT/ntfy/weather
# are network services and always queried directly.

def prox_cfg():
    return {"host": os.environ.get("PROXMOX_HOST", ""),
            "user": os.environ.get("PROXMOX_USER", "root"),
            "port": int(os.environ.get("PROXMOX_PORT", "22")),
            "key": os.environ.get("PROXMOX_KEY", "/root/.ssh/id_ed25519"),
            "config": os.environ.get("PROXMOX_CONFIG", "/opt/smart-nas-fan/config.yaml")}


def is_remote():
    return bool(prox_cfg()["host"])


def cfg_path():
    return prox_cfg()["config"] if is_remote() else CFG_PATH


def where():
    p = prox_cfg()
    return {"mode": "remote" if is_remote() else "local", "proxmox": p["host"] or None}


def _ssh_client():
    import paramiko  # lazy: only required in remote mode (pip install paramiko)
    p = prox_cfg()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=p["host"], port=p["port"], username=p["user"],
              key_filename=os.path.expanduser(p["key"]),
              timeout=8, banner_timeout=8, auth_timeout=8)
    return c


import threading as _th

_netlog_lock = _th.Lock()

_ssh_lock = _th.Lock()
_ssh_pooled = None
_ssh_pkey = None


def ssh_client():
    """Persistent SSH connection (reconnects on failure). One handshake, then ~RTT per command."""
    global _ssh_pooled, _ssh_pkey
    p = prox_cfg()
    key = (p["host"], p["port"], p["user"], p["key"])
    with _ssh_lock:
        if _ssh_pooled is None or _ssh_pkey != key:
            try:
                _ssh_pooled.close()
            except Exception:
                pass
            _ssh_pooled = _ssh_client()
            _ssh_pkey = key
        return _ssh_pooled


def _ssh_drop():
    global _ssh_pooled
    with _ssh_lock:
        _ssh_pooled = None


def prox_exec(cmd, timeout=10):
    p = prox_cfg()
    debug(f"SSH {p['user']}@{p['host']}:{p['port']}: {cmd[:160]}")
    try:
        _, out, _ = ssh_client().exec_command(cmd, timeout=timeout)
        data = out.read().decode(errors="ignore")
        debug(f"SSH <- {len(data)} bytes")
        return data
    except Exception:
        _ssh_drop()
        raise


def prox_read(path):
    return prox_exec("cat " + shlex.quote(path))


def prox_write(path, data: bytes, backup=True):
    try:
        c = ssh_client()
        if backup:
            c.exec_command(f"cp -f {shlex.quote(path)} {shlex.quote(path + '.bak')} 2>/dev/null", timeout=10)
        sftp = c.open_sftp()
        try:
            with sftp.file(path, "w") as f:
                f.write(data)
        finally:
            sftp.close()
    except Exception:
        _ssh_drop()
        raise


def prox_rm(path):
    try:
        ssh_client().exec_command("rm -f " + shlex.quote(path), timeout=10)
    except Exception:
        _ssh_drop()
        raise


def prox_get(remote, local):
    d = os.path.dirname(local)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        sftp = ssh_client().open_sftp()
        try:
            sftp.get(remote, local)
        finally:
            sftp.close()
    except Exception:
        _ssh_drop()
        raise


def read_text(path):
    if is_remote():
        return prox_read(path)
    with open(path) as f:
        return f.read()


def write_text(path, data, backup=True):
    if is_remote():
        prox_write(path, data.encode("utf-8"), backup=backup)
        return
    if backup and os.path.exists(path):
        os.rename(path, path + ".bak")
    with open(path, "w") as f:
        f.write(data)


def remove_file(path):
    if is_remote():
        prox_rm(path)
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


_db_cache_ts = 0


def open_db(db):
    """Local sqlite, or SFTP-downloaded copy in remote mode (re-pulled every 30s max)."""
    global _db_cache_ts
    if not is_remote():
        return sqlite3.connect(db)
    local = "/tmp/smart-nas-fan-remote.db"
    if time.time() - _db_cache_ts > 30 or not os.path.exists(local):
        prox_get(db, local)
        _db_cache_ts = time.time()
    return sqlite3.connect(local)


_cfg_cache = {"ts": 0, "data": None}


def load_cfg_cached(ttl=60):
    if time.time() - _cfg_cache["ts"] < ttl and _cfg_cache["data"] is not None:
        return _cfg_cache["data"]
    cfg = load_cfg()
    _cfg_cache.update(ts=time.time(), data=cfg)
    return cfg


def invalidate_cfg_cache():
    _cfg_cache.update(ts=0, data=None)


# ---------- probes ----------

def _tn_ws_url(cfg):
    """wss://host/api/current derived from api_url/host (JSON-RPC 2.0, TrueNAS 25.04+)."""
    t = cfg.get("truenas", {})
    if t.get("ws_url"):
        return str(t["ws_url"]).rstrip("/")
    base = (t.get("api_url") or f"https://{t.get('host', '')}").rstrip("/")
    if base.startswith("https://"):
        ws = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws = "ws://" + base[len("http://"):]
    elif base.startswith(("wss://", "ws://")):
        ws = base
    else:
        ws = "wss://" + base
    for suffix in ("/api/current", "/api/v2.0", "/api", "/websocket"):
        if ws.endswith(suffix):
            ws = ws[:-len(suffix)]
    return ws.rstrip("/") + "/api/current"


def _tn_ws_recv(ws, req_id, timeout):
    deadline = time.time() + max(1, timeout)
    while True:
        if time.time() > deadline:
            raise TimeoutError(f"websocket response timeout for id={req_id}")
        msg = json.loads(ws.recv())
        if not isinstance(msg, dict) or msg.get("id") != req_id:
            continue  # skip collection_update / notify_unsubscribed notifications
        if msg.get("error") is not None:
            err = msg["error"]
            raise RuntimeError(f"truenas ws error {err.get('code')}: {err.get('message')}")
        return msg.get("result")


def _tn_ws_batch(cfg, calls, timeout=10):
    """Run [(method, params)] over one authenticated WS session. Returns [result]."""
    try:
        import websocket
    except ImportError:
        raise RuntimeError("websocket-client not installed")
    t = cfg.get("truenas", {})
    key = os.environ.get("TRUENAS_API_KEY") or t.get("api_key") or ""
    if not key:
        raise RuntimeError("api_key missing")
    url = _tn_ws_url(cfg)
    to = min(int(t.get("timeout_sec", 8)), timeout)
    verify = bool(t.get("verify_ssl", False))
    sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False} if not verify else {}
    ws = websocket.create_connection(url, timeout=to, sslopt=sslopt)
    try:
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                            "method": "auth.login_with_api_key", "params": [key]}))
        if _tn_ws_recv(ws, 1, to) is not True:
            raise RuntimeError("ws auth rejected")
        out, rid = [], 10
        for method, params in calls:
            ws.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                "method": method, "params": params or []}))
            out.append(_tn_ws_recv(ws, rid, to))
            rid += 1
        return out
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _tn_transport(cfg):
    return str(cfg.get("truenas", {}).get("api_transport", "auto") or "auto").lower()


def _tn_ws_query_then_temps(cfg, timeout=10):
    """disk.query + disk.temperatures over one WS session. Returns (disks, raw_temps)."""
    import websocket
    t = cfg.get("truenas", {})
    key = os.environ.get("TRUENAS_API_KEY") or t.get("api_key") or ""
    if not key:
        raise RuntimeError("api_key missing")
    url = _tn_ws_url(cfg)
    to = min(int(t.get("timeout_sec", 8)), timeout)
    verify = bool(t.get("verify_ssl", False))
    sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False} if not verify else {}
    ws = websocket.create_connection(url, timeout=to, sslopt=sslopt)
    try:
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                            "method": "auth.login_with_api_key", "params": [key]}))
        if _tn_ws_recv(ws, 1, to) is not True:
            raise RuntimeError("ws auth rejected")
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 10, "method": "disk.query",
                            "params": [[], {"select": ["name", "type", "rotationrate", "model"]}]}))
        disks = _tn_ws_recv(ws, 10, to)
        names = []
        for d in disks if isinstance(disks, list) else []:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            dtype = (d.get("type") or "").upper()
            if (t.get("hdd_only", True) and dtype == "HDD") or (not t.get("hdd_only", True)):
                if not (t.get("hdd_only", True) and d["name"].startswith("nvme")):
                    names.append(d["name"])
            elif dtype == "" and d.get("rotationrate") is not None:
                names.append(d["name"])
        if not names:
            return disks, {}
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 11, "method": "disk.temperatures",
                            "params": [names, False]}))
        raw = _tn_ws_recv(ws, 11, to)
        return disks, raw if isinstance(raw, dict) else {}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _tn_temps_from_results(cfg, disks, raw, t0, transport="ws"):
    """Build truenas_api() result dict from disk.query + disk.temperatures payloads."""
    t = cfg.get("truenas", {})
    names = []
    for d in disks if isinstance(disks, list) else []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        dtype = (d.get("type") or "").upper()
        if (t.get("hdd_only", True) and dtype == "HDD") or (not t.get("hdd_only", True)):
            if not (t.get("hdd_only", True) and d["name"].startswith("nvme")):
                names.append(d["name"])
        elif dtype == "" and d.get("rotationrate") is not None:
            names.append(d["name"])
    if not names:
        return {"ok": False, "error": "no HDDs from disk.query",
                "latency_ms": int((time.time() - t0) * 1000)}
    temps = {}
    for n in names:
        v = (raw or {}).get(n)
        tv = float(v) if isinstance(v, (int, float)) else (
            float(v["temperature"]) if isinstance(v, dict) and isinstance(v.get("temperature"), (int, float)) else None)
        if tv is not None:
            temps[f"/dev/{n}"] = round(tv, 1)
    ms = int((time.time() - t0) * 1000)
    if not temps:
        return {"ok": False, "error": "no temps returned", "latency_ms": ms}
    vals = list(temps.values())
    if transport == "ws":
        mark("tn:temps", True, "temps via websocket (JSON-RPC)")
    else:
        mark("tn:temps", False, "", "temps via REST fallback — websocket failed, see net log")
    debug(f"TrueNAS temps ok ({transport}): {len(vals)} HDDs max={max(vals)} in {ms}ms")
    return {"ok": True, "source": "api", "transport": transport, "max": max(vals), "avg": round(sum(vals) / len(vals), 1),
            "count": len(vals), "temps": temps, "latency_ms": ms}


def truenas_api(cfg):
    """HDD temps via TrueNAS API. Returns dict with ok/temps/error/latency."""
    t = cfg.get("truenas", {})
    key = os.environ.get("TRUENAS_API_KEY") or t.get("api_key") or ""
    if not key:
        return {"ok": False, "error": "api_key missing"}
    base = (t.get("api_url") or f"https://{t.get('host', '')}").rstrip("/")
    verify = bool(t.get("verify_ssl", False))
    if not verify:
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    to = min(int(t.get("timeout_sec", 8)), 10)
    t0 = time.time()
    debug(f"TrueNAS temps via {_tn_ws_url(cfg) if _tn_transport(cfg) != 'rest' else base} "
          f"key={_mask(key)} hdd_only={t.get('hdd_only', True)} transport={_tn_transport(cfg)}")
    try:
        ws_err = None
        if _tn_transport(cfg) in ("ws", "websocket", "auto"):
            try:
                disks, raw = _tn_ws_query_then_temps(cfg, to)
                return _tn_temps_from_results(cfg, disks, raw, t0, transport="ws")
            except Exception as e:
                ws_err = str(e)[:140]
                if _tn_transport(cfg) != "auto":
                    raise
                debug(f"WS failed ({ws_err}), falling back to REST")
        q = requests.post(f"{base}/api/v2.0/disk.query", json=[],
                          headers=hdr, timeout=to, verify=verify)
        q.raise_for_status()
        disks = q.json()
        names = []
        for d in disks if isinstance(disks, list) else []:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            dtype = (d.get("type") or "").upper()
            if (t.get("hdd_only", True) and dtype == "HDD") or \
               (not t.get("hdd_only", True)):
                if not (t.get("hdd_only", True) and d["name"].startswith("nvme")):
                    names.append(d["name"])
            elif dtype == "" and d.get("rotationrate") is not None:
                names.append(d["name"])
        if not names:
            return {"ok": False, "error": "no HDDs from disk.query", "latency_ms": int((time.time() - t0) * 1000)}
        r = requests.post(f"{base}/api/v2.0/disk.temperatures", json=[names, False],
                          headers=hdr, timeout=to, verify=verify)
        r.raise_for_status()
        raw = r.json() if isinstance(r.json(), dict) else {}
        temps = {}
        for n in names:
            v = raw.get(n)
            tv = float(v) if isinstance(v, (int, float)) else (
                float(v["temperature"]) if isinstance(v, dict) and isinstance(v.get("temperature"), (int, float)) else None)
            if tv is not None:
                temps[f"/dev/{n}"] = round(tv, 1)
        ms = int((time.time() - t0) * 1000)
        if not temps:
            err = "no temps returned" + (f" (ws also failed: {ws_err})" if ws_err else "")
            return {"ok": False, "error": err[:200], "latency_ms": ms}
        vals = list(temps.values())
        debug(f"TrueNAS temps ok (rest): {len(vals)} HDDs max={max(vals)} in {ms}ms")
        return {"ok": True, "source": "api", "transport": "rest", "max": max(vals), "avg": round(sum(vals) / len(vals), 1),
                "count": len(vals), "temps": temps, "latency_ms": ms}
    except Exception as e:
        debug(f"TrueNAS temps FAIL: {e}")
        mark("tn:temps", False, "", f"temps FAILED — ws: {ws_err or 'n/a'} / rest: {e}")
        err = str(e)[:160] + (f" (ws also failed: {ws_err})" if ws_err else "")
        return {"ok": False, "error": err[:220], "latency_ms": int((time.time() - t0) * 1000)}


def fan_hw(cfg=None):
    """Live PWM: local sysfs, or Proxmox sysfs over SSH in remote mode."""
    res = _fan_hw_local()
    if res.get("ok"):
        return res
    if is_remote():
        try:
            return _fan_hw_remote(cfg)
        except Exception as e:
            return {"ok": False, "error": f"proxmox ssh: {str(e)[:100]}"}
    return res


def _fan_hw_local():
    """Read live PWM from sysfs (works in container with :ro hwmon mount)."""
    for nf in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(nf) as f:
                if "it87" not in f.read():
                    continue
            base = os.path.dirname(nf)
            for chan in ("pwm2", "pwm1"):
                p = os.path.join(base, chan)
                if os.path.exists(p):
                    with open(p) as f:
                        pwm = int(f.read().strip())
                    rpm = None
                    num = "".join(c for c in chan if c.isdigit())
                    rp = os.path.join(base, f"fan{num}_input")
                    if os.path.exists(rp):
                        try:
                            with open(rp) as f:
                                rpm = int(f.read().strip())
                        except Exception:
                            pass
                    return {"ok": True, "pwm": pwm, "pct": round(pwm / 2.55, 1), "rpm": rpm}
        except Exception:
            continue
    return {"ok": False, "error": "no it87 pwm (native Proxmox only?)"}


def _fan_hw_remote(cfg=None):
    chan = (cfg or {}).get("fan", {}).get("pwm_channel", "pwm2") if isinstance(cfg, dict) else "pwm2"
    num = "".join(c for c in chan if c.isdigit()) or "2"
    base = None
    for nf in prox_exec("ls /sys/class/hwmon/hwmon*/name 2>/dev/null").split():
        if "it87" in prox_exec("cat " + shlex.quote(nf)):
            base = os.path.dirname(nf.strip())
            break
    if not base:
        raise RuntimeError("no it87 hwmon on proxmox host")
    pwm = int(prox_exec(f"cat {base}/{chan}").strip())
    rpm = None
    try:
        rpm = int(prox_exec(f"cat {base}/fan{num}_input").strip())
    except Exception:
        pass
    return {"ok": True, "pwm": pwm, "pct": round(pwm / 2.55, 1), "rpm": rpm}


def heartbeat_age(cfg):
    try:
        with open(cfg["timing"]["heartbeat_file"]) as f:
            return round(time.time() - float(f.read().strip()), 1)
    except Exception:
        if is_remote():
            try:
                return round(time.time() - float(prox_read(cfg["timing"]["heartbeat_file"]).strip()), 1)
            except Exception:
                pass
        return None


def tcp_ok(host, port, timeout=3):
    try:
        t0 = time.time()
        socket.create_connection((host, int(port)), timeout=timeout).close()
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def db_last(db, limit=60):
    try:
        con = open_db(db)
        rows = con.execute(
            "SELECT ts,max_temp,pwm,pct,action FROM readings ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
        ev = con.execute(
            "SELECT ts,event,max_temp,pwm FROM events ORDER BY ts DESC LIMIT 10").fetchall()
        con.close()
        return {"ok": True,
                "readings": [{"ts": r[0], "max": r[1], "pwm": r[2], "pct": r[3], "action": r[4]}
                             for r in reversed(rows)],
                "events": [{"ts": e[0], "event": e[1], "max": e[2], "pwm": e[3]} for e in ev]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "readings": [], "events": []}


def boost_info(db_path):
    """Last boosted (step_up/emergency) + last boosted-down (step_down/failsafe) for the ms timer."""
    try:
        con = open_db(db_path)
        up = con.execute(
            "SELECT ts,event,max_temp,pwm FROM events WHERE event IN "
            "('step_up','emergency','failsafe_maxboost_stepdown','maxboost_stepdown') "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        dn = con.execute(
            "SELECT ts,event,max_temp,pwm FROM events WHERE event IN "
            "('step_down','failsafe_step_down') ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        now_ms = int(time.time() * 1000)
        active = bool(up and (not dn or up[0] > dn[0]))
        return {"active": active,
                "since_ts": up[0] if active else None,
                "elapsed_ms": now_ms - int(datetime.fromisoformat(up[0]).timestamp() * 1000) if active else None,
                "last_up": {"ts": up[0], "event": up[1], "max": up[2], "pwm": up[3]} if up else None,
                "last_down": {"ts": dn[0], "event": dn[1], "max": dn[2], "pwm": dn[3]} if dn else None}
    except Exception:
        return {"active": False, "since_ts": None, "elapsed_ms": None, "last_up": None, "last_down": None}


WMO_EMOJI = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
             51: "🌦️", 53: "🌦️", 55: "🌦️", 56: "🌧️", 57: "🌧️",
             61: "🌧️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
             71: "❄️", 73: "❄️", 75: "❄️", 77: "❄️", 80: "🌧️", 81: "🌧️",
             82: "🌧️", 85: "❄️", 86: "❄️", 95: "⛈️", 96: "⛈️", 99: "⛈️"}
WMO_TEXT = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
            45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
            61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
            71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
            80: "light showers", 81: "showers", 82: "violent showers",
            85: "snow showers", 86: "snow showers", 95: "thunderstorm",
            96: "storm + hail", 99: "storm + hail"}
_weather_cache: dict = {}


def outside_weather(cfg):
    """Ambient outside temp. Geocode via Nominatim/OSM (proper postcode support),
    forecast via Open-Meteo (free, no key). Cached 10 min."""
    w = cfg.get("weather", {})
    if not w.get("enabled", True):
        return {"ok": None, "error": "disabled"}
    now = time.time()
    if _weather_cache.get("ts", 0) > now - 600 and _weather_cache.get("data"):
        debug("weather: cache hit")
        return _weather_cache["data"]
    try:
        pc, country = str(w.get("postcode", "33333")), w.get("country", "United States")
        lat, lon = w.get("latitude"), w.get("longitude")
        if lat is not None and lon is not None:
            # exact coordinates configured: skip geocoding entirely
            debug(f"weather: using configured lat={lat} lon={lon}")
            place = {"latitude": float(lat), "longitude": float(lon), "name": w.get("place") or pc}
        else:
            debug(f"weather: nominatim geocode postcode={pc} country={country}")
            g = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"postalcode": pc.strip(), "country": (country or "").strip(),
                                     "format": "jsonv2", "addressdetails": 0, "limit": 5},
                             headers={"User-Agent": "smart-nas-fan/1.0 (homelab weather tile)",
                                      "Accept": "application/json"},
                             timeout=10)
            g.raise_for_status()
            results = g.json() or []
            if not results and pc.strip().isdigit():
                # some regions index better as free-text query
                debug("weather: postalcode search empty, retrying as q=")
                g = requests.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": f"{pc.strip()}, {(country or '').strip()}",
                                         "format": "jsonv2", "addressdetails": 0, "limit": 5},
                                 headers={"User-Agent": "smart-nas-fan/1.0 (homelab weather tile)",
                                          "Accept": "application/json"},
                                 timeout=10)
                g.raise_for_status()
                results = g.json() or []
            if not results:
                return {"ok": False, "error": f"postcode {pc} not found in {country} "
                        f"— set weather.latitude/longitude (e.g. from openstreetmap.org search)"}
            top = results[0]
            place = {"latitude": float(top["lat"]), "longitude": float(top["lon"]),
                     "name": top.get("display_name", pc).split(",")[0]}  # short place name
        f = requests.get("https://api.open-meteo.com/v1/forecast",
                         params={"latitude": place["latitude"], "longitude": place["longitude"],
                                 "current": "temperature_2m,weather_code", "timezone": "auto"},
                         timeout=8).json()["current"]
        debug(f"weather: forecast lat={place['latitude']} lon={place['longitude']} -> "
              f"{f.get('temperature_2m')}C code={f.get('weather_code')}")
        code = int(f.get("weather_code", 3))
        data = {"ok": True, "temp": f.get("temperature_2m"),
                "emoji": WMO_EMOJI.get(code, "🌡️"), "text": WMO_TEXT.get(code, f"code {code}"),
                "place": place.get("name", pc), "postcode": pc}
        _weather_cache.update(ts=now, data=data)
        return data
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


_z2m_cache: dict = {"ts": 0, "temp": None, "humidity": None, "error": "subscriber not started"}
_plug_cache: dict = {"ts": 0, "power": None, "energy": None, "today_kwh": None,
                     "yesterday_kwh": None, "yesterday_day": None, "error": "subscriber not started"}
_plug_day: dict = {"day": None, "start": None, "last": None,
                   "yesterday": None, "yesterday_day": None, "saved_ts": 0}
_mqtt_started = False
_z2m_lock = _th.Lock()


def _mqtt_broker_cfg(cfg):
    m = cfg.get("mqtt", {})
    return ((m.get("broker") or "").strip(), int(m.get("port", 1883)),
            str(m.get("username") or "").strip(), str(m.get("password") or ""))


def _plug_state_path(cfg):
    try:
        hb = cfg["timing"]["heartbeat_file"]
        return os.path.join(os.path.dirname(hb), "z2m_plug.json")
    except Exception:
        return None


def _plug_state_load(cfg):
    p = _plug_state_path(cfg)
    if not p:
        return
    try:
        with open(p) as f:
            d = json.load(f)
        for k in ("day", "start", "last", "yesterday", "yesterday_day"):
            if k in d:
                _plug_day[k] = d[k]
        debug(f"plug day-state loaded from {p}: {_plug_day}")
    except Exception:
        pass


def _plug_state_save(cfg):
    p = _plug_state_path(cfg)
    if not p:
        return
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w") as f:
            json.dump({k: _plug_day.get(k) for k in
                       ("day", "start", "last", "yesterday", "yesterday_day")}, f)
        _plug_day["saved_ts"] = time.time()
    except Exception as e:
        debug(f"plug day-state save failed: {e}")


def _plug_accounting(cfg, energy, now_ts):
    """Daily kWh from cumulative energy: today = energy - midnight baseline,
    yesterday = last-of-yesterday - its baseline. Survives restarts via JSON."""
    if not isinstance(energy, (int, float)):
        return
    energy = float(energy)
    day = datetime.fromtimestamp(now_ts).astimezone().strftime("%Y-%m-%d")
    d = _plug_day
    if d["day"] != day:
        if d["day"] is not None and d["start"] is not None and d["last"] is not None:
            d["yesterday"] = round(max(0.0, d["last"] - d["start"]), 3)
            d["yesterday_day"] = d["day"]
        d["day"], d["start"] = day, energy  # re-baseline at first reading of the day
        _plug_state_save(cfg)
    d["last"] = energy
    if d["start"] is not None:
        _plug_cache["today_kwh"] = round(max(0.0, energy - d["start"]), 3)
    _plug_cache["yesterday_kwh"] = d["yesterday"]
    _plug_cache["yesterday_day"] = d["yesterday_day"]
    if time.time() - d.get("saved_ts", 0) > 300:
        _plug_state_save(cfg)


def _mqtt_ensure(cfg):
    """Start the shared MQTT subscriber thread once (daemon, auto-reconnect).

    Subscribes to the room-sensor topic AND the plug topic. Reuses the mqtt
    broker + credentials (anonymous when username is empty). Never raises:
    failures land in the caches with broker:port attached for the UI to show.
    """
    global _mqtt_started
    if _mqtt_started:
        return
    _mqtt_started = True
    s = cfg.get("sensors", {})
    if not s.get("enabled", True):
        _z2m_cache["error"] = _plug_cache["error"] = "disabled"
        return
    topics = [t for t in [str(s.get("topic") or "").strip(),
                          str(s.get("plug_topic") or "").strip()] if t]
    if not topics:
        _z2m_cache["error"] = _plug_cache["error"] = \
            "set sensors.topic (e.g. zigbee2mqtt/<friendly-name>)"
        return
    try:
        import paho.mqtt.client as pm
    except ImportError:
        _z2m_cache["error"] = _plug_cache["error"] = "paho-mqtt not installed"
        return
    broker, port, user, passwd = _mqtt_broker_cfg(cfg)
    if not broker:
        _z2m_cache["error"] = _plug_cache["error"] = "mqtt.broker not set"
        return
    _plug_state_load(cfg)
    t = _th.Thread(target=_mqtt_loop, args=(cfg, pm, broker, port, user, passwd), daemon=True)
    t.start()


def _mqtt_loop(cfg, pm, broker, port, user, passwd):
    """Blocking subscriber: caches room temp/humidity + plug power/energy."""
    s = cfg.get("sensors", {})
    topic = str(s.get("topic") or "").strip()
    plug_topic = str(s.get("plug_topic") or "").strip()
    tkey, hkey = str(s.get("temp_key") or "temperature"), str(s.get("humidity_key") or "humidity")
    pkey, ekey = str(s.get("power_key") or "power"), str(s.get("energy_key") or "energy")

    def on_msg(_c, _u, msg):
        try:
            p = json.loads(msg.payload.decode())
        except Exception as e:
            debug(f"mqtt bad payload on {msg.topic}: {e}")
            return
        now = time.time()
        if msg.topic == topic:
            t, h = p.get(tkey), p.get(hkey)
            with _z2m_lock:
                if isinstance(t, (int, float)):
                    _z2m_cache["temp"] = round(float(t), 1)
                if isinstance(h, (int, float)):
                    _z2m_cache["humidity"] = round(float(h), 1)
                _z2m_cache["ts"] = now
                _z2m_cache["error"] = None
        elif msg.topic == plug_topic:
            pw, en = p.get(pkey), p.get(ekey)
            with _z2m_lock:
                if isinstance(pw, (int, float)):
                    _plug_cache["power"] = round(float(pw), 1)
                if isinstance(en, (int, float)):
                    _plug_cache["energy"] = round(float(en), 3)
                _plug_cache["ts"] = now
                _plug_cache["error"] = None
            _plug_accounting(cfg, en if isinstance(en, (int, float)) else None, now)

    def where():
        return f"MQTT {broker}:{port} (topics: {', '.join([t for t in [topic, plug_topic] if t]) or 'none'})"

    while True:  # loop_forever reconnects on drops; outer loop survives fatal errors
        try:
            try:
                cbv = pm.CallbackAPIVersion.VERSION2
                c = pm.Client(callback_api_version=cbv, client_id="smart-nas-fan-admin-mqtt")
            except (AttributeError, TypeError, ValueError):
                c = pm.Client(client_id="smart-nas-fan-admin-mqtt")
            if user:
                c.username_pw_set(user, passwd)
            c.on_message = on_msg
            debug(f"mqtt connect {broker}:{port} user={(user or '(anonymous)')}")
            c.connect(broker, port, 60)
            for t in [topic, plug_topic]:
                if t:
                    c.subscribe(t, qos=0)
            with _z2m_lock:
                _z2m_cache["error"] = _plug_cache["error"] = None
            mark("mqtt:admin", True, f"MQTT {broker}:{port} connected ({', '.join([t for t in [topic, plug_topic] if t])})")
            debug(f"mqtt subscribed: {where()}")
            c.loop_forever(retry_first_connection=True)
        except Exception as e:
            err = (f"{where()} failed: {e} — is mqtt.broker reachable from this host? "
                   f"try: nc -zv {broker} {port}")[:200]
            with _z2m_lock:
                _z2m_cache["error"] = _plug_cache["error"] = err
            mark("mqtt:admin", False, "", f"MQTT {broker}:{port} FAILED: {e}")
            debug(f"mqtt loop failed ({e}), retry in 15s")
        time.sleep(15)


# backward-compat alias: indoor_sensor() still boots the (now shared) subscriber
def _z2m_ensure(cfg):
    _mqtt_ensure(cfg)


def indoor_sensor(cfg):
    """Latest Zigbee2MQTT temp/humidity reading (cached by the subscriber thread)."""
    s = cfg.get("sensors", {})
    if not s.get("enabled", True):
        return {"ok": None, "error": "disabled"}
    _mqtt_ensure(cfg)
    with _z2m_lock:
        snap = dict(_z2m_cache)
    if snap.get("temp") is None:
        return {"ok": False, "error": snap.get("error") or "waiting for first MQTT message",
                "topic": str(s.get("topic") or "")}
    return {"ok": True, "temp": snap["temp"], "humidity": snap.get("humidity"),
            "age_s": int(time.time() - snap["ts"]), "topic": str(s.get("topic") or "")}


def plug_sensor(cfg):
    """Sonoff plug: live watts + cumulative energy + today/yesterday kWh."""
    s = cfg.get("sensors", {})
    if not s.get("enabled", True):
        return {"ok": None, "error": "disabled"}
    topic = str(s.get("plug_topic") or "").strip()
    if not topic:
        return {"ok": False, "error": "set sensors.plug_topic to the plug's zigbee2mqtt topic",
                "topic": ""}
    _mqtt_ensure(cfg)
    with _z2m_lock:
        snap = dict(_plug_cache)
    if snap.get("power") is None and snap.get("energy") is None:
        return {"ok": False, "error": snap.get("error") or "waiting for first MQTT message",
                "topic": topic}
    return {"ok": True, "power": snap.get("power"), "energy": snap.get("energy"),
            "today_kwh": snap.get("today_kwh"), "yesterday_kwh": snap.get("yesterday_kwh"),
            "yesterday_day": snap.get("yesterday_day"),
            "age_s": int(time.time() - snap["ts"]) if snap.get("ts") else None,
            "topic": topic}


def build_status():
    cfg = load_cfg()
    t = cfg.get("truenas", {})
    tn = truenas_api(cfg)
    fan = fan_hw(cfg)
    hb = heartbeat_age(cfg)
    mq = cfg.get("mqtt", {})
    mqtt = tcp_ok(mq.get("broker", ""), mq.get("port", 1883)) if mq.get("enabled") else {"ok": None, "error": "disabled"}
    db_path = cfg["timing"].get("db_file", "/var/log/smart-nas-fan.db")
    db = db_last(db_path, 60)
    # fan state for UI color/animation
    pct = fan.get("pct") if fan.get("ok") else (db["readings"][-1]["pct"] if db.get("readings") else None)
    state = "unknown"
    if pct is not None:
        if pct <= 1:
            state = "stopped"
        elif pct <= 45:
            state = "normal"
        elif pct < 90:
            state = "boost"
        else:
            state = "critical"
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "ts": now,
        "version": VERSION,
        "where": where(),
        "truenas": {**tn, "mode": t.get("method", "auto"), "host": t.get("host")},
        "fan": {**fan, "pct": pct, "state": state,
                "target": db["readings"][-1]["pwm"] if db.get("readings") else None},
        "controller": {"heartbeat_age_s": hb, "alive": hb is not None and hb < 300},
        "mqtt": {"ok": mqtt.get("ok"), "broker": mq.get("broker"),
                 "error": mqtt.get("error"), "latency_ms": mqtt.get("latency_ms")},
        "ntfy": {"enabled": cfg.get("ntfy", {}).get("enabled"),
                 "last_event": db["events"][0] if db.get("events") else None},
        "db": {"ok": db.get("ok"), "points": len(db.get("readings", []))},
        "boost": boost_info(db_path),
    }


def _tn_req(cfg, method, params, timeout=12):
    """TrueNAS helper: JSON-RPC over WS first, legacy REST fallback.

    REST is deprecated since 25.04 (alerts since 25.10.1). api_transport
    'ws' forces WS, 'rest' forces REST, 'auto' (default) tries WS then REST.
    """
    if _tn_transport(cfg) in ("ws", "websocket", "auto"):
        try:
            res = _tn_ws_batch(cfg, [(method, params)], timeout=timeout)
            mark(f"tn:{method}", True, f"{method} via websocket (JSON-RPC)")
            debug(f"TrueNAS {method} <- WS ok")
            return res[0]
        except Exception as e:
            if _tn_transport(cfg) != "auto":
                mark(f"tn:{method}", False, "", f"{method} websocket FAILED: {e}")
                raise
            mark(f"tn:{method}", False, "",
                 f"{method} websocket FAILED ({e}) — REST fallback (deprecated)")
            debug(f"TrueNAS {method} WS failed ({e}), REST fallback")
    t = cfg.get("truenas", {})
    key = os.environ.get("TRUENAS_API_KEY") or t.get("api_key") or ""
    if not key:
        raise RuntimeError("api_key missing")
    base = (t.get("api_url") or f"https://{t.get('host', '')}").rstrip("/")
    verify = bool(t.get("verify_ssl", False))
    if not verify:
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)
    r = requests.post(f"{base}/api/v2.0/{method}", json=params,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      timeout=timeout, verify=verify)
    r.raise_for_status()
    body = r.json()
    debug(f"TrueNAS {method} <- HTTP {r.status_code}, "
          f"{len(body) if isinstance(body, (list, dict)) else '?'} items")
    return body


def proxmox_host():
    """CPU% + RAM% of the Proxmox box: local /proc, or over SSH in remote mode."""
    if is_remote():
        try:
            return _proxmox_host_remote()
        except Exception as e:
            return {"ok": False, "error": f"proxmox ssh: {str(e)[:100]}"}
    try:
        return _proxmox_host_local()
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


def _parse_proc(stat1, stat2, meminfo, dt):
    a = list(map(int, stat1.split()[1:8]))
    b = list(map(int, stat2.split()[1:8]))
    idle = (b[3] + b[4]) - (a[3] + a[4])
    cpu = round(100 * (1 - idle / max(1, sum(b) - sum(a))), 1)
    mem = {}
    for ln in meminfo.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            mem[k] = int(v.split()[0])
    avail = mem.get("MemAvailable", mem.get("MemFree"))
    if avail is None or "MemTotal" not in mem:
        raise RuntimeError("unparseable /proc/meminfo")
    ram = round(100 * (1 - avail / mem["MemTotal"]), 1)
    return {"ok": True, "cpu": cpu, "ram": ram,
            "ram_used_gb": round((mem["MemTotal"] - avail) / 1048576, 1),
            "sample_s": round(dt, 2)}


def _proxmox_host_local():
    """Lightweight CPU% + RAM% of the box running this UI (Proxmox/Docker host). Stdlib only."""
    with open("/proc/stat") as f:
        s1 = f.readline()
    t0 = time.time()
    time.sleep(0.4)
    with open("/proc/stat") as f:
        s2 = f.readline()
    with open("/proc/meminfo") as f:
        mi = f.read()
    return _parse_proc(s1, s2, mi, time.time() - t0)


_stat_cache = {"t": 0, "raw": None}


def _proxmox_host_remote():
    s2 = prox_exec("cat /proc/stat | head -1")
    mi = prox_exec("cat /proc/meminfo")
    now = time.time()
    if _stat_cache["raw"] is not None and now - _stat_cache["t"] < 90:
        res = _parse_proc(_stat_cache["raw"], s2, mi, now - _stat_cache["t"])
    else:  # first sample: take a second one locally-spaced, then cache it
        time.sleep(0.4)
        s3 = prox_exec("cat /proc/stat | head -1")
        res = _parse_proc(s2, s3, mi, time.time() - now)
    _stat_cache.update(t=now, raw=s2)
    return res


_metrics_cache: dict = {}


# ---------- persistent TrueNAS WS hub: live reporting.realtime feed ----------
# One long-lived, auto-reconnecting WebSocket: auth once, subscribe once to
# `reporting.realtime` (TrueNAS pushes cpu/disks/memory every ~2s), cache the
# latest frame. Endpoints serve the cache = real-time data with zero per-call
# handshake. Polling (WS get_data) and legacy REST are only fallbacks.

_tnhub = {"started": False, "ts": 0, "fields": None, "error": "hub not started"}
_tnhub_lock = _th.Lock()


def _tnhub_open(cfg, timeout=10):
    """Open + authenticate one WS connection. Shared by hub and one-shot calls."""
    try:
        import websocket
    except ImportError:
        raise RuntimeError("websocket-client not installed")
    t = cfg.get("truenas", {})
    key = os.environ.get("TRUENAS_API_KEY") or t.get("api_key") or ""
    if not key:
        raise RuntimeError("api_key missing")
    url = _tn_ws_url(cfg)
    to = min(int(t.get("timeout_sec", 8)), timeout)
    verify = bool(t.get("verify_ssl", False))
    sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False} if not verify else {}
    ws = websocket.create_connection(url, timeout=to, sslopt=sslopt)
    try:
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                            "method": "auth.login_with_api_key", "params": [key]}))
        if _tn_ws_recv(ws, 1, to) is not True:
            raise RuntimeError("ws auth rejected")
        return ws
    except Exception:
        try:
            ws.close()
        except Exception:
            pass
        raise


def _tnhub_ensure(cfg):
    """Start the hub thread once. Never raises; status lands in _tnhub."""
    if _tnhub["started"]:
        return
    _tnhub["started"] = True
    t = _th.Thread(target=_tnhub_loop, args=(cfg,), daemon=True)
    t.start()


def _tnhub_loop(cfg):
    """Hold the connection forever: subscribe, cache realtime frames, reconnect."""
    rid = 100
    while True:
        try:
            ws = _tnhub_open(cfg, timeout=12)
            try:
                rid += 1
                ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "core.subscribe",
                                    "params": ["reporting.realtime"]}))
                sub_id = _tn_ws_recv(ws, rid, 12)
                mark("tn:hub", True, f"websocket connected + reporting.realtime subscribed ({sub_id})")
                debug(f"tnhub subscribed reporting.realtime -> {sub_id}")
                with _tnhub_lock:
                    _tnhub["error"] = None
                ws.settimeout(30)
                while True:
                    try:
                        raw = ws.recv()
                    except Exception:
                        # recv timeout doubles as a quiet-connection watchdog:
                        # a cheap ping proves the socket is still alive.
                        rid += 1
                        ws.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                            "method": "core.ping", "params": []}))
                        _tn_ws_recv(ws, rid, 12)
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    p = msg.get("params") or {}
                    # event notification: {"method":"collection_update","params":{
                    #   "collection":"reporting.realtime","fields":{...}}}
                    if (msg.get("method") == "collection_update"
                            and p.get("collection") == "reporting.realtime"
                            and isinstance(p.get("fields"), dict)):
                        with _tnhub_lock:
                            _tnhub["fields"] = p["fields"]
                            _tnhub["ts"] = time.time()
                            _tnhub["error"] = None
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
        except Exception as e:
            with _tnhub_lock:
                _tnhub["error"] = str(e)[:140]
            mark("tn:hub", False, "", f"websocket down: {e} — retrying")
            debug(f"tnhub loop failed ({e}), retry in 10s")
        time.sleep(10)


def truenas_realtime(cfg):
    """Live NAS cpu/ram/disk-IO from the hub cache (fresh < 15s). No API call."""
    _tnhub_ensure(cfg)
    with _tnhub_lock:
        snap = dict(_tnhub)
    age = time.time() - snap.get("ts", 0)
    f = snap.get("fields")
    if not isinstance(f, dict) or age > 15:
        err = snap.get("error") or "no realtime frame yet"
        mark("tn:realtime", False, "", f"realtime feed down: {err}")
        return {"ok": False, "error": f"realtime: {err}"[:140]}
    try:
        res = {"ok": True, "source": "realtime", "age_s": int(age)}
        cpu = (f.get("cpu") or {}).get("cpu") or {}
        if isinstance(cpu.get("usage"), (int, float)):
            res["cpu"] = round(float(cpu["usage"]), 1)
        mem = f.get("memory") or {}
        total, avail = mem.get("physical_memory_total"), mem.get("physical_memory_available")
        if isinstance(total, (int, float)) and total > 0 and isinstance(avail, (int, float)):
            res["ram"] = round(100 * (1 - avail / total), 1)
        dk = f.get("disks") or {}
        if isinstance(dk.get("read_bytes"), (int, float)):
            res["read_mbs"] = round(max(0, float(dk["read_bytes"])) / 1048576, 1)
        if isinstance(dk.get("write_bytes"), (int, float)):
            res["write_mbs"] = round(max(0, float(dk["write_bytes"])) / 1048576, 1)
        if len(res) <= 3:  # only ok/source/age_s -> nothing usable parsed
            mark("tn:realtime", False, "", "realtime: unparseable frame")
            return {"ok": False, "error": "realtime: unparseable frame"}
        mark("tn:realtime", True, "realtime feed live (websocket push, ~2s frames)")
        return res
    except Exception as e:
        return {"ok": False, "error": f"realtime parse: {e}"[:140]}


def _parse_reporting_graphs(out):
    """reporting.get_data payload -> {"ok":True,cpu,ram,read_mbs,write_mbs}.

    Accepts dict OR list shapes for aggregations.mean and data rows
    (they differ across TrueNAS versions). Raises RuntimeError if empty."""
    graphs = {g.get("name"): g for g in (out if isinstance(out, list) else [])}
    debug(f"reporting graphs: {sorted(graphs)}")

    def row_vals(leg, row):
        """One data row (dict OR list) -> values aligned to legend, or []."""
        if isinstance(row, dict):
            ordered = ([row[k] for k in leg if k in row]
                       or [v for k, v in row.items() if k != "timestamp"])
            try:
                return [float(v) for v in ordered]
            except (TypeError, ValueError):
                return []
        if isinstance(row, (list, tuple)) and row:
            try:
                return [float(v) for v in row]
            except (TypeError, ValueError):
                return []
        return []

    def vals(g):
        """(legend, values) from aggregations.mean (dict OR list),
        falling back to the last data row (dict OR list row)."""
        if not isinstance(g, dict):
            return [], []
        leg = [str(x) for x in (g.get("legend") or [])]
        mean = (g.get("aggregations") or {}).get("mean")
        if isinstance(mean, dict) and mean:
            ordered = ([mean[k] for k in leg if k in mean]
                       or [v for k, v in mean.items() if k in leg]
                       or list(mean.values()))
            try:
                return leg, [float(v) for v in ordered]
            except (TypeError, ValueError):
                pass
        if isinstance(mean, (list, tuple)) and mean:
            try:
                return leg, [float(v) for v in mean]
            except (TypeError, ValueError):
                pass
        rows = g.get("data") or []
        if rows:
            rv = row_vals(leg, rows[-1])
            if rv:
                return leg, rv
        return leg, []

    res = {"ok": True}
    leg, v = vals(graphs.get("cpu"))
    low = [x.lower() for x in leg]
    if v and sum(v) > 0:
        idle = v[low.index("idle")] if "idle" in low else v[-1]
        res["cpu"] = round(100 * (sum(v) - idle) / sum(v), 1)
    leg, v = vals(graphs.get("memory"))
    low = [x.lower() for x in leg]
    if v and sum(v) > 0:
        used = v[low.index("used")] if "used" in low else v[0]
        res["ram"] = round(100 * used / sum(v), 1)
    leg, v = vals(graphs.get("disk"))
    low = [x.lower() for x in leg]
    data = (graphs.get("disk") or {}).get("data") or []
    last = row_vals(leg, data[-1]) if data else []  # live point, not hourly mean
    if not last:
        last = v
    if last:
        ri = low.index("read") if "read" in low else 0
        wi = low.index("write") if "write" in low else (1 if len(last) > 1 else 0)
        res["read_mbs"] = round(max(0, last[ri]) / 1048576, 1)
        res["write_mbs"] = round(max(0, last[wi]) / 1048576, 1)
    if len(res) == 1:
        raise RuntimeError("no metric parsed")
    return res


def _tn_rest_reporting_legacy(cfg, timeout=15):
    """Last-resort legacy REST shape (pre-25.04 form): slash URL + dict body."""
    t = cfg.get("truenas", {})
    key = os.environ.get("TRUENAS_API_KEY") or t.get("api_key") or ""
    if not key:
        raise RuntimeError("api_key missing")
    base = (t.get("api_url") or f"https://{t.get('host', '')}").rstrip("/")
    verify = bool(t.get("verify_ssl", False))
    if not verify:
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)
    r = requests.post(f"{base}/api/v2.0/reporting/get_data",
                      json={"graphs": [{"name": "cpu"}, {"name": "memory"}, {"name": "disk"}],
                            "reporting_query": {"unit": "HOUR", "page": 1, "aggregate": True}},
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      timeout=timeout, verify=verify)
    r.raise_for_status()
    return _parse_reporting_graphs(r.json())


def truenas_metrics(cfg):
    """TrueNAS CPU% + RAM% + array read/write MB/s. Cached 30s.

    Chain: live WS hub (reporting.realtime, ~2s frames, no per-call cost)
    -> WS reporting.get_data poll -> legacy REST. Each stage's error is kept
    so the GUI note shows the REAL cause instead of only the last failure.
    """
    now = time.time()
    if _metrics_cache.get("ts", 0) > now - 30 and _metrics_cache.get("data"):
        return _metrics_cache["data"]
    errs = []
    rt = truenas_realtime(cfg)
    if rt.get("ok"):
        _metrics_cache.update(ts=now, data=rt)
        return rt
    errs.append(str(rt.get("error") or "realtime failed"))
    try:
        res = _parse_reporting_graphs(_tn_req(cfg, "reporting.get_data", [
            [{"name": "cpu"}, {"name": "memory"}, {"name": "disk"}],
            {"unit": "HOUR", "page": 1, "aggregate": True}], timeout=15))
        res["source"] = "ws"
        mark("tn:metrics", True, "metrics via websocket poll (get_data)")
        _metrics_cache.update(ts=now, data=res)
        return res
    except Exception as e:
        errs.append(f"ws: {e}"[:140])
        debug(f"truenas_metrics ws fallback failed: {e}")
    try:
        res = _tn_rest_reporting_legacy(cfg)
        res["source"] = "rest"
        mark("tn:metrics", True, "metrics via REST fallback (deprecated)")
        _metrics_cache.update(ts=now, data=res)
        return res
    except Exception as e:
        errs.append(f"rest: {e}"[:140])
    mark("tn:metrics", False, "", ("metrics FAILED: " + " | ".join(errs))[:180])
    return {"ok": False, "error": " | ".join(errs)[:220]}


# ---------- routes ----------

try:
    _c0 = load_cfg()
    _t0, _m0, _n0 = _c0.get("truenas", {}), _c0.get("mqtt", {}), _c0.get("ntfy", {})
    debug(f"startup v{VERSION} cfg={cfg_path()} where={where()} "
          f"truenas:{_t0.get('method')}@{_t0.get('host')} api={_t0.get('api_url')} key={_mask(_t0.get('api_key'))} "
          f"mqtt:{_m0.get('broker')}:{_m0.get('port')} user={_m0.get('username') or '(empty)'} "
          f"ntfy={_n0.get('url') or '(empty)'} weather={_c0.get('weather', {}).get('postcode')}")
except Exception as _e:
    debug(f"startup config not readable yet: {_e}")


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "index.html"))


@app.post("/api/login")
async def login(req: Request):
    if not ADMIN_PASS:
        raise HTTPException(status_code=503, detail="server misconfigured: set ADMIN_PASS env")
    body = await req.json()
    user = str(body.get("user", ""))
    if secrets.compare_digest(user, ADMIN_USER) and \
       secrets.compare_digest(str(body.get("pass", "")), ADMIN_PASS):
        tok = secrets.token_hex(16)
        _tokens[tok] = time.time()
        # prune expired so the set can't grow forever
        for t, ts in list(_tokens.items()):
            if time.time() - ts > TOKEN_TTL:
                _tokens.pop(t, None)
        debug(f"login ok user={user!r} tokens={len(_tokens)}")
        return {"token": tok}
    debug(f"login FAIL user={user!r}")
    raise HTTPException(status_code=403, detail="bad credentials")


@app.get("/api/status")
def status(_: bool = Depends(check_auth)):
    return build_status()


@app.get("/api/fast")
def fast(_: bool = Depends(check_auth)):
    """Sub-second lane: fan PWM + heartbeat only, ONE remote roundtrip (~RTT).
    Local mode answers instantly; remote mode batches everything into a single
    SSH exec so the fan gauge can refresh every second."""
    if not is_remote():
        cfg = load_cfg_cached()
        return {"fan": fan_hw(), "hb_age": heartbeat_age(cfg), "where": where()}
    try:
        cfg = load_cfg_cached()
        chan = cfg.get("fan", {}).get("pwm_channel", "pwm2")
        num = "".join(c for c in chan if c.isdigit()) or "2"
        hb = cfg["timing"]["heartbeat_file"]
        out = prox_exec(
            f"C={shlex.quote(chan)}; H=$(grep -l it87 /sys/class/hwmon/hwmon*/name 2>/dev/null | head -1); "
            f"B=$(dirname \"$H\" 2>/dev/null); echo P:$(cat \"$B/$C\" 2>/dev/null); "
            f"echo R:$(cat \"$B/fan{num}_input\" 2>/dev/null); echo H:$(cat {shlex.quote(hb)} 2>/dev/null)")
        vals = {}
        for ln in out.splitlines():
            if len(ln) > 2 and ln[1] == ":" and ln[0] in "PRH":
                vals[ln[0]] = ln[2:].strip()
        try:
            pwm = int(vals.get("P", ""))
            rpm = int(vals.get("R", "")) if vals.get("R", "").lstrip("-").isdigit() else None
            fan = {"ok": True, "pwm": pwm, "pct": round(pwm / 2.55, 1), "rpm": rpm}
        except Exception:
            fan = {"ok": False, "error": "no it87 pwm on proxmox"}
        try:
            hb_age = round(time.time() - float(vals.get("H", "")), 1)
        except Exception:
            hb_age = None
        return {"fan": fan, "hb_age": hb_age, "where": where()}
    except Exception as e:
        return {"fan": {"ok": False, "error": f"proxmox ssh: {str(e)[:100]}"},
                "hb_age": None, "where": where()}


@app.get("/stream")
async def stream(token: str = ""):
    """Realtime push over SSE (plain HTTP, auto-reconnect): status JSON every 2s."""
    if not _valid_token(token):
        raise HTTPException(status_code=403, detail="login required")
    async def gen():
        while True:
            try:
                data = await asyncio.to_thread(build_status)
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                yield "event: error\ndata: {}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/weather")
def weather(_: bool = Depends(check_auth)):
    cfg = load_cfg()
    out = outside_weather(cfg)
    out["indoor"] = indoor_sensor(cfg)  # same tile: room sensor next to outside temp
    return out


@app.get("/api/plug")
def plug(_: bool = Depends(check_auth)):
    return plug_sensor(load_cfg())


@app.get("/api/history")
def history(limit: int = 120, _: bool = Depends(check_auth)):
    cfg = load_cfg()
    return db_last(cfg["timing"].get("db_file", "/var/log/smart-nas-fan.db"), limit=min(limit, 500))


@app.get("/api/drives")
def drives(limit: int = 120, _: bool = Depends(check_auth)):
    """Per-drive temp series for multi-HDD graphs: {series: {sda: [{ts,temp}]}}."""
    cfg = load_cfg()
    db = cfg["timing"].get("db_file", "/var/log/smart-nas-fan.db")
    try:
        con = open_db(db)
        rows = con.execute(
            """SELECT ts, drive, temp FROM drive_temps WHERE ts IN
               (SELECT DISTINCT ts FROM drive_temps ORDER BY ts DESC LIMIT ?)
               ORDER BY ts ASC""", (min(limit, 500),)).fetchall()
        con.close()
        series: dict[str, list] = {}
        for ts, drv, temp in rows:
            series.setdefault(drv.replace("/dev/", ""), []).append({"ts": ts, "temp": temp})
        return {"ok": True, "series": series}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "series": {}}


@app.get("/api/config")
def get_config(_: bool = Depends(check_auth)):
    content = read_text(cfg_path())
    parsed = yaml.safe_load(content)
    # expanded: same file with $VAR/${VAR}/${VAR:-default} resolved from the
    # live environment (native: smart-nas-fan.env via systemd EnvironmentFile,
    # docker: compose env). The easy form renders from THIS so secrets and
    # backend-set values are visible; password fields stay type=password.
    try:
        expanded = _expand_env(parsed)
    except Exception:
        expanded = parsed
    return {"path": cfg_path(), "content": content, "parsed": parsed, "expanded": expanded}


def _set_dotted(cfg, dotted, value):
    parts = dotted.split(".")
    node = cfg
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value


@app.post("/api/config/preview")
async def preview_config(req: Request, _: bool = Depends(check_auth)):
    """Dry-run conversion for tab sync (writes nothing):
    {values} -> {content} (form state serialized to YAML),
    {content} -> {parsed, expanded} (raw text parsed for the form).
    Lets the easy/raw panes stay in sync so one can't silently override the other."""
    body = await req.json()
    current = yaml.safe_load(read_text(cfg_path()))
    if "values" in body and isinstance(body["values"], dict):
        for k, v in body["values"].items():
            _set_dotted(current, k, v)
        content = yaml.safe_dump(current, sort_keys=False, default_flow_style=False)
        return {"ok": True, "content": content}
    content = body.get("content", "")
    try:
        parsed = yaml.safe_load(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid YAML: {e}")
    try:
        expanded = _expand_env(parsed)
    except Exception:
        expanded = parsed
    return {"ok": True, "parsed": parsed, "expanded": expanded}


@app.post("/api/config")
async def set_config(req: Request, _: bool = Depends(check_auth)):
    body = await req.json()
    current = yaml.safe_load(read_text(cfg_path()))
    if "values" in body and isinstance(body["values"], dict):
        # easy-form save: merge dotted keys into current yaml (structure preserved)
        for k, v in body["values"].items():
            _set_dotted(current, k, v)
        content = yaml.safe_dump(current, sort_keys=False, default_flow_style=False)
    else:
        # raw YAML save
        content = body.get("content", "")
        try:
            yaml.safe_load(content)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid YAML: {e}")
    debug(f"config save to {cfg_path()} ({len(content)} chars, {'form-values' if 'values' in body else 'raw'})")
    write_text(cfg_path(), content if content.endswith("\n") else content + "\n")
    invalidate_cfg_cache()
    return {"ok": True, "note": "saved. Restart controller to apply: docker compose restart controller (or systemctl restart smart-nas-fan-controller)."}


def ov_path(cfg):
    return cfg["timing"].get("override_file") or os.path.join(
        os.path.dirname(cfg["timing"]["heartbeat_file"]), "override.json")


def manual_state(cfg):
    F, M = cfg["fan"], cfg.get("manual", {})
    bounds = {"min": int(M.get("min_pwm", F["floor_pwm"])), "floor": F["floor_pwm"],
              "ceiling": F["ceiling_pwm"], "max_sec": int(M.get("max_sec", 1800)),
              "enabled": bool(M.get("enabled", True))}
    try:
        o = json.loads(read_text(ov_path(cfg)))
        pwm = max(bounds["min"], min(255, int(o.get("manual_pwm", 0))))
        until = float(o.get("until", 0))
        if time.time() >= until:
            return {**bounds, "active": False, "expired": True}
        return {**bounds, "active": True, "pwm": pwm, "pct": round(pwm / 2.55, 1),
                "until": until, "remaining_s": int(until - time.time()), "by": o.get("by", "?")}
    except Exception:
        return {**bounds, "active": False}


@app.get("/api/manual")
def get_manual(_: bool = Depends(check_auth)):
    return manual_state(load_cfg())


@app.post("/api/manual")
async def set_manual(req: Request, _: bool = Depends(check_auth)):
    """Slider/preset control: {pwm, seconds?, by?} or {auto:true} to release."""
    body = await req.json()
    cfg = load_cfg()
    if body.get("auto"):
        debug(f"manual release file={ov_path(cfg)}")
        remove_file(ov_path(cfg))
        return {**manual_state(cfg), "active": False}
    M = cfg.get("manual", {})
    debug(f"manual set req={ {k: v for k, v in body.items() if k != 'by'} } file={ov_path(cfg)}")
    lo = int(M.get("min_pwm", cfg["fan"]["floor_pwm"]))
    pwm = max(lo, min(255, int(body.get("pwm", lo))))
    secs = max(60, min(int(M.get("max_sec", 1800)), int(body.get("seconds", M.get("max_sec", 1800)))))
    p = ov_path(cfg)
    if not is_remote():
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w") as f:
            json.dump({"manual_pwm": pwm, "until": time.time() + secs,
                       "by": str(body.get("by", "admin-ui"))[:40]}, f)
    else:
        prox_write(p, json.dumps({"manual_pwm": pwm, "until": time.time() + secs,
                                  "by": str(body.get("by", "admin-ui"))[:40]}).encode(), backup=False)
    st = manual_state(cfg)
    st["note"] = (f"Manual PWM {pwm} ({round(pwm/2.55,1)}%) for {secs//60}min. "
                  "Auto-expires, critical temps still win.")
    return st


@app.get("/api/hostmetrics")
def hostmetrics(_: bool = Depends(check_auth)):
    cfg = load_cfg()
    return {"proxmox": proxmox_host(), "truenas": truenas_metrics(cfg)}


@app.post("/api/ntfy-test")
async def ntfy_test(req: Request, _: bool = Depends(check_auth)):
    """Send Test Alert button: verifies the whole push pipeline on demand."""
    body = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    cfg = load_cfg()
    n = cfg.get("ntfy", {})
    if not n.get("enabled") or not n.get("url"):
        raise HTTPException(status_code=400, detail="ntfy not configured")
    msg = str((body or {}).get("message") or "🔔 smart-nas-fan TEST alert — push pipeline OK ✅")
    debug(f"ntfy-test POST {n['url']}")
    t0 = time.time()
    try:
        requests.post(n["url"], data=msg.encode("utf-8"), timeout=8,
                      headers={"Title": "smart-nas-fan test", "Priority": "high", "Tags": "bell,test"})
        return {"ok": True, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:160])


@app.get("/api/export")
def export(kind: str = "readings", limit: int = 2000, _: bool = Depends(check_auth)):
    debug(f"export kind={kind} limit={limit}")
    """CSV download (opens in Excel): kind=readings|events."""
    cfg = load_cfg()
    db = cfg["timing"].get("db_file", "/var/log/smart-nas-fan.db")
    if kind == "events":
        header = ["ts", "event", "max_temp", "pwm", "why"]
        sql = "SELECT ts,event,max_temp,pwm,why FROM events ORDER BY ts DESC LIMIT ?"
    else:
        kind = "readings"
        header = ["ts", "source", "max_temp", "avg_temp", "target", "pwm", "pct", "rpm", "action", "why"]
        sql = "SELECT ts,source,max_temp,avg_temp,target,pwm,pct,rpm,action,why FROM readings ORDER BY ts DESC LIMIT ?"
    try:
        con = open_db(db)
        try:
            rows = con.execute(sql, (min(limit, 5000),)).fetchall()
        except sqlite3.OperationalError:
            rows = []  # DB exists but controller hasn't logged yet -> header-only CSV
        con.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:160])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=smart-nas-fan-{kind}.csv"})


def _logs_tail(cfg, lines=40):
    if is_remote():
        try:
            tail = prox_exec(f"tail -n {min(lines, 500)} " + shlex.quote(cfg["timing"]["log_file"])).splitlines(keepends=True)
            return {"ok": True, "lines": tail}
        except Exception as e:
            return {"ok": False, "error": f"proxmox ssh: {str(e)[:120]}", "lines": []}
    try:
        with open(cfg["timing"]["log_file"]) as f:
            tail = f.readlines()[-min(lines, 500):]
        return {"ok": True, "lines": tail}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "lines": []}


@app.get("/api/logs")
def logs(lines: int = 120, _: bool = Depends(check_auth)):
    return _logs_tail(load_cfg(), lines)


@app.get("/api/netlog")
def netlog(lines: int = 40, _: bool = Depends(check_auth)):
    """Connection-event log: websocket vs REST, MQTT and sensor failures with reasons."""
    with _netlog_lock:
        return {"ok": True, "lines": list(_netlog)[-min(max(lines, 1), 60):]}


_WS_CADENCE = (("status", 2), ("manual", 10), ("host", 15), ("plug", 15),
               ("logs", 12), ("netlog", 15), ("chart", 30), ("weather", 60))


def _ws_payload(cfg, kind):
    """Build one push frame payload. Blocking calls run in threads via to_thread."""
    if kind == "status":
        return build_status()
    if kind == "manual":
        return manual_state(cfg)
    if kind == "host":
        return {"proxmox": proxmox_host(), "truenas": truenas_metrics(cfg)}
    if kind == "plug":
        return plug_sensor(cfg)
    if kind == "logs":
        return _logs_tail(cfg, 40)
    if kind == "netlog":
        with _netlog_lock:
            return {"ok": True, "lines": list(_netlog)[-12:]}
    if kind == "chart":
        db = cfg["timing"].get("db_file", "/var/log/smart-nas-fan.db")
        h = db_last(db, 300)
        return {"readings": h.get("readings", []), "events": h.get("events", []),
                "series": drives(limit=300).get("series", {})}
    if kind == "weather":
        out = outside_weather(cfg)
        out["indoor"] = indoor_sensor(cfg)
        return out
    raise RuntimeError(f"unknown feed {kind}")


@app.websocket("/ws")
async def ws_feed(ws: WebSocket):
    """Single live socket GUI<->backend: pushes status/manual/host/plug/chart/
    weather/logs/netlog on cadence (initial burst on connect, then only due
    feeds). Auth via ?token= (same bearer as HTTP). Plain HTTP polling remains
    as fallback when the socket is down. Needs the `websockets` package."""
    if not _valid_token(ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
    await ws.accept()
    cfg, cfg_ts = load_cfg(), time.time()
    last = {k: 0.0 for k, _ in _WS_CADENCE}
    try:
        while True:
            now = time.time()
            if now - cfg_ts > 60:  # pick up config saves without reconnecting
                cfg, cfg_ts = load_cfg(), now
            for kind, every in _WS_CADENCE:
                if now - last[kind] < every:
                    continue
                last[kind] = now
                try:
                    data = await asyncio.to_thread(_ws_payload, cfg, kind)
                    await ws.send_json({"type": kind, "data": data})
                except Exception as e:
                    debug(f"ws feed {kind} skipped: {e}")
                    last[kind] = now - every + 5  # retry this feed in 5s, keep others
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                if isinstance(msg, dict) and "ping" in msg:
                    await ws.send_json({"type": "pong", "data": msg["ping"]})
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    except Exception as e:
        debug(f"ws feed closed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6767)
