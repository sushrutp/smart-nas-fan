#!/usr/bin/env python3
"""nastemp-2 / admin center — neon control UI on :6767.

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
import secrets
import shlex
import socket
import sqlite3
import time
from datetime import datetime, timezone

import requests
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
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
ADMIN_PASS = os.environ.get("ADMIN_PASS", "nastemp")
_tokens: set[str] = set()
_auth = HTTPBearer(auto_error=False)

app = FastAPI(title="nastemp admin", docs_url=None, redoc_url=None, openapi_url=None)


def load_cfg():
    with open(cfg_path(), "r") as f:
        return yaml.safe_load(f)


def check_auth(creds: HTTPAuthorizationCredentials = Depends(_auth)):
    if creds is None or creds.credentials not in _tokens:
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
            "config": os.environ.get("PROXMOX_CONFIG", "/opt/nastemp/config.yaml")}


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
    try:
        _, out, _ = ssh_client().exec_command(cmd, timeout=timeout)
        return out.read().decode(errors="ignore")
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
    local = "/tmp/nastemp-remote.db"
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
    try:
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
            return {"ok": False, "error": "no temps returned", "latency_ms": ms}
        vals = list(temps.values())
        return {"ok": True, "source": "api", "max": max(vals), "avg": round(sum(vals) / len(vals), 1),
                "count": len(vals), "temps": temps, "latency_ms": ms}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "latency_ms": int((time.time() - t0) * 1000)}


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
    """Ambient outside temp via Open-Meteo (free, no key). Cached 10 min."""
    w = cfg.get("weather", {})
    if not w.get("enabled", True):
        return {"ok": None, "error": "disabled"}
    now = time.time()
    if _weather_cache.get("ts", 0) > now - 600 and _weather_cache.get("data"):
        return _weather_cache["data"]
    try:
        pc, country = w.get("postcode", "33333"), w.get("country", "United States")
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": pc, "count": 5, "language": "en", "format": "json"},
                         timeout=8).json()
        place = None
        for r in g.get("results", []):
            if country.lower() in (r.get("country") or "").lower():
                place = r
                break
        if not place:
            top = (g.get("results") or [{}])[0]
            return {"ok": False, "error": f"postcode {pc} not found in {country} "
                    f"(top hit: {top.get('name', '?')}, {top.get('country', '?')}) — fix weather.postcode/country"}
        f = requests.get("https://api.open-meteo.com/v1/forecast",
                         params={"latitude": place["latitude"], "longitude": place["longitude"],
                                 "current": "temperature_2m,weather_code", "timezone": "auto"},
                         timeout=8).json()["current"]
        code = int(f.get("weather_code", 3))
        data = {"ok": True, "temp": f.get("temperature_2m"),
                "emoji": WMO_EMOJI.get(code, "🌡️"), "text": WMO_TEXT.get(code, f"code {code}"),
                "place": place.get("name", pc), "postcode": pc}
        _weather_cache.update(ts=now, data=data)
        return data
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def build_status():
    cfg = load_cfg()
    t = cfg.get("truenas", {})
    tn = truenas_api(cfg)
    fan = fan_hw(cfg)
    hb = heartbeat_age(cfg)
    mq = cfg.get("mqtt", {})
    mqtt = tcp_ok(mq.get("broker", ""), mq.get("port", 1883)) if mq.get("enabled") else {"ok": None, "error": "disabled"}
    db_path = cfg["timing"].get("db_file", "/var/log/nastemp.db")
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
    """TrueNAS REST helper shared by metrics calls (api key + https + self-signed)."""
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
    return r.json()


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


def truenas_metrics(cfg):
    """TrueNAS CPU% + RAM% + array read/write MB/s via reporting.get_data. Cached 30s."""
    now = time.time()
    if _metrics_cache.get("ts", 0) > now - 30 and _metrics_cache.get("data"):
        return _metrics_cache["data"]
    try:
        out = _tn_req(cfg, "reporting/get_data", {
            "graphs": [{"name": "cpu"}, {"name": "memory"}, {"name": "disk"}],
            "reporting_query": {"unit": "HOUR", "page": 0, "aggregate": True}}, timeout=15)
        graphs = {g.get("name"): g for g in (out if isinstance(out, list) else [])}

        def vals(g):
            if not isinstance(g, dict):
                return [], []
            leg = [str(x).lower() for x in g.get("legend", [])]
            agg = (g.get("aggregations") or {}).get("mean") or []
            if agg:
                return leg, [float(v) for v in agg]
            data = g.get("data") or []
            return leg, [float(v) for v in data[-1]] if data else []

        res = {"ok": True}
        leg, v = vals(graphs.get("cpu"))
        if v and sum(v) > 0:
            idle = v[leg.index("idle")] if "idle" in leg else v[-1]
            res["cpu"] = round(100 * (sum(v) - idle) / sum(v), 1)
        leg, v = vals(graphs.get("memory"))
        if v and sum(v) > 0:
            used = v[leg.index("used")] if "used" in leg else v[0]
            res["ram"] = round(100 * used / sum(v), 1)
        leg, v = vals(graphs.get("disk"))
        data = (graphs.get("disk") or {}).get("data") or []
        last = [float(x) for x in data[-1]] if data else v  # live point, not hourly mean
        if last:
            ri = leg.index("read") if "read" in leg else 0
            wi = leg.index("write") if "write" in leg else (1 if len(last) > 1 else 0)
            res["read_mbs"] = round(max(0, last[ri]) / 1048576, 1)
            res["write_mbs"] = round(max(0, last[wi]) / 1048576, 1)
        if len(res) == 1:
            return {"ok": False, "error": "no metric parsed"}
        _metrics_cache.update(ts=now, data=res)
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)[:140]}


# ---------- routes ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "index.html"))


@app.post("/api/login")
async def login(req: Request):
    body = await req.json()
    if secrets.compare_digest(str(body.get("user", "")), ADMIN_USER) and \
       secrets.compare_digest(str(body.get("pass", "")), ADMIN_PASS):
        tok = secrets.token_hex(16)
        _tokens.add(tok)
        return {"token": tok}
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
    if token not in _tokens:
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
    return outside_weather(load_cfg())


@app.get("/api/history")
def history(limit: int = 120, _: bool = Depends(check_auth)):
    cfg = load_cfg()
    return db_last(cfg["timing"].get("db_file", "/var/log/nastemp.db"), limit=min(limit, 500))


@app.get("/api/drives")
def drives(limit: int = 120, _: bool = Depends(check_auth)):
    """Per-drive temp series for multi-HDD graphs: {series: {sda: [{ts,temp}]}}."""
    cfg = load_cfg()
    db = cfg["timing"].get("db_file", "/var/log/nastemp.db")
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
    return {"path": cfg_path(), "content": content, "parsed": yaml.safe_load(content)}


def _set_dotted(cfg, dotted, value):
    parts = dotted.split(".")
    node = cfg
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value


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
    write_text(cfg_path(), content if content.endswith("\n") else content + "\n")
    invalidate_cfg_cache()
    return {"ok": True, "note": "saved. Restart controller to apply: docker compose restart controller (or systemctl restart nastemp-controller)."}


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
        remove_file(ov_path(cfg))
        return {**manual_state(cfg), "active": False}
    M = cfg.get("manual", {})
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
    msg = str((body or {}).get("message") or "🔔 nastemp TEST alert — push pipeline OK ✅")
    t0 = time.time()
    try:
        requests.post(n["url"], data=msg.encode("utf-8"), timeout=8,
                      headers={"Title": "nastemp test", "Priority": "high", "Tags": "bell,test"})
        return {"ok": True, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:160])


@app.get("/api/export")
def export(kind: str = "readings", limit: int = 2000, _: bool = Depends(check_auth)):
    """CSV download (opens in Excel): kind=readings|events."""
    cfg = load_cfg()
    db = cfg["timing"].get("db_file", "/var/log/nastemp.db")
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
                    headers={"Content-Disposition": f"attachment; filename=nastemp-{kind}.csv"})


@app.get("/api/logs")
def logs(lines: int = 120, _: bool = Depends(check_auth)):
    cfg = load_cfg()
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=6767)
