#!/usr/bin/env python3
"""
nastemp-2 / fan_controller.py
Runs on PROXMOX host (has /sys/class/hwmon it87 pwm2).

What it does every interval:
 1. Query TrueNAS API (disk.query + disk.temperatures, HDD-ONLY, SSD ignored).
    Fallback to SSH smartctl if API unreachable (method: auto/api/ssh).
 2. Compute target PWM from config temps (floor 40% .. ceiling ~72%, emergency 255 only if critical).
 3. STEP toward target (fast up, slow down + cooldown) - no sudden noise jumps.
 4. Enforce MAX BOOST timer: never stay elevated > max_boost_sec (default 20min)
    without fresh temp justification -> force step-down.
 5. If TrueNAS unreachable (fail_threshold in a row) -> failsafe step-down to safe_pwm + ntfy.
 6. Write PWM, update heartbeat file, append local log + JSONL + SQLite DB,
    publish MQTT (HA graphs), ntfy on events.

Failsafe layers:
  L1 (here): API/SSH loss -> step down + ntfy. Stuck-elevated -> step down after 20 min.
  L2 (watchdog.py + systemd): if this process dies / heartbeat stale -> force 40%.
  L3 (systemd Restart=always): auto-restart this process.
"""
import glob
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

import yaml
import paramiko
import requests

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

CFG_PATH = os.environ.get("NASTEMP_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))

# ---------- helpers ----------

def load_cfg(path):
    with open(path) as f:
        return _expand_env(yaml.safe_load(f))

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

def log_line(cfg, msg):
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = f"{ts} {msg}"
    print(line, flush=True)
    try:
        with open(cfg["timing"]["log_file"], "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def log_jsonl(cfg, obj):
    try:
        with open(cfg["timing"]["jsonl_file"], "a") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception:
        pass

def init_db(cfg):
    """SQLite history DB: readings per cycle + per-drive temps + events. Stdlib only."""
    try:
        db = cfg["timing"].get("db_file", "/var/log/nastemp.db")
        d = os.path.dirname(db)
        if d:
            os.makedirs(d, exist_ok=True)
        con = sqlite3.connect(db)
        con.execute("""CREATE TABLE IF NOT EXISTS readings(
            ts TEXT PRIMARY KEY, source TEXT, max_temp REAL, avg_temp REAL,
            target INTEGER, pwm INTEGER, pct REAL, rpm INTEGER,
            action TEXT, why TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS drive_temps(
            ts TEXT, drive TEXT, temp REAL,
            PRIMARY KEY (ts, drive))""")
        con.execute("""CREATE TABLE IF NOT EXISTS events(
            ts TEXT PRIMARY KEY, event TEXT, max_temp REAL, pwm INTEGER, why TEXT)""")
        con.commit()
        con.close()
    except Exception as e:
        print(f"db init failed: {e}", flush=True)

def log_db(cfg, rec, drive_temps=None, event=None):
    try:
        db = cfg["timing"].get("db_file", "/var/log/nastemp.db")
        con = sqlite3.connect(db)
        con.execute("INSERT OR REPLACE INTO readings VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (rec.get("ts"), rec.get("source", "unknown"), rec.get("max_temp"),
                     rec.get("avg"), rec.get("target"), rec.get("pwm"), rec.get("pct"),
                     rec.get("rpm"), rec.get("action"), rec.get("why")))
        if drive_temps:
            for drv, tv in drive_temps.items():
                if tv is not None:
                    con.execute("INSERT OR REPLACE INTO drive_temps VALUES(?,?,?)",
                                (rec.get("ts"), drv, tv))
        if event:
            con.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?)",
                        (event.get("ts"), event.get("event"), event.get("max_temp"),
                         event.get("pwm"), event.get("why")))
        con.commit()
        con.close()
    except Exception:
        pass

def heartbeat(cfg):
    try:
        hb = cfg["timing"]["heartbeat_file"]
        os.makedirs(os.path.dirname(hb), exist_ok=True)
        with open(hb, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass

class LinkWatch:
    """Per-link up/down tracker: first-failure timestamp (for 'down since' + ntfy),
    throttled alerts (once per outage), recovery notes. Keeps this app honest
    about what's broken — the core of the failsafe story."""
    def __init__(self, name):
        self.name = name
        self.ok = True
        self.since = None
        self.alerted = False

    def report(self, ok, detail=""):
        now = time.time()
        if ok:
            if not self.ok:
                down_for = now - (self.since or now)
                self.ok, self.since, self.alerted = True, None, False
                return "recovered", down_for
            return "ok", 0
        if self.ok:
            self.ok, self.since, self.alerted = False, now, False
        if not self.alerted:
            self.alerted = True
            ts = datetime.fromtimestamp(self.since).astimezone().isoformat(timespec="seconds")
            return "failed", f"{self.name} DOWN since {ts} ({detail})".strip()
        return "down", time.time() - (self.since or now)

    def down_for(self):
        return time.time() - self.since if self.since and not self.ok else 0

# ---------- manual override (admin UI slider) ----------

def override_path(cfg):
    return cfg["timing"].get("override_file") or os.path.join(
        os.path.dirname(cfg["timing"]["heartbeat_file"]), "override.json")

def load_override(cfg):
    """Returns {active, pwm, until, by, mtime} / {expired,...} / {active:False}."""
    try:
        with open(override_path(cfg)) as f:
            o = json.load(f)
        pwm = int(o.get("manual_pwm", 0))
        until = float(o.get("until", 0))
        by = str(o.get("by", "?"))[:40]
        mtime = os.path.getmtime(override_path(cfg))
        if time.time() >= until:
            return {"expired": True, "pwm": pwm, "by": by, "mtime": mtime}
        lo = int(cfg.get("manual", {}).get("min_pwm", cfg["fan"]["floor_pwm"]))
        pwm = max(lo, min(255, pwm))  # stall protection: never below floor
        return {"active": True, "pwm": pwm, "until": until, "by": by, "mtime": mtime}
    except Exception:
        return {"active": False}

def clear_override(cfg):
    try:
        os.remove(override_path(cfg))
    except Exception:
        pass

def override_sig(cfg):
    try:
        return os.path.getmtime(override_path(cfg))
    except Exception:
        return None

# ---------- PWM hardware ----------

class PwmHw:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pwm_path = None
        self.enable_path = None
        self.fan_input_path = None
        self.resolve()

    def resolve(self):
        drv = self.cfg["fan"]["driver_match"]
        chan = self.cfg["fan"]["pwm_channel"]  # e.g. pwm2
        num = "".join(c for c in chan if c.isdigit())
        for name_file in glob.glob("/sys/class/hwmon/hwmon*/name"):
            try:
                with open(name_file) as f:
                    if drv in f.read().strip():
                        base = os.path.dirname(name_file)
                        self.pwm_path = os.path.join(base, chan)
                        self.enable_path = os.path.join(base, f"{chan}_enable")
                        cand = os.path.join(base, f"fan{num}_input")
                        if os.path.exists(cand):
                            self.fan_input_path = cand
                        return
            except Exception:
                continue
        # fallback: first pwm2 found
        cands = glob.glob("/sys/class/hwmon/hwmon*/" + chan)
        if cands:
            self.pwm_path = cands[0]

    def ensure_manual(self):
        mode = self.cfg["fan"].get("pwm_enable_mode", 1)
        if self.enable_path and os.path.exists(self.enable_path):
            try:
                with open(self.enable_path, "w") as f:
                    f.write(str(mode))
            except Exception as e:
                return f"pwm_enable write failed: {e}"
        return None

    def read_pwm(self):
        try:
            with open(self.pwm_path) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def write_pwm(self, val):
        val = max(0, min(255, int(val)))
        if not self.pwm_path or not os.path.exists(self.pwm_path):
            return False, "pwm_path not found (is it87 loaded? check /sys/class/hwmon)"
        err = self.ensure_manual()
        try:
            with open(self.pwm_path, "w") as f:
                f.write(str(val))
            return True, err or "ok"
        except Exception as e:
            return False, str(e)

    def read_rpm(self):
        if self.fan_input_path and os.path.exists(self.fan_input_path):
            try:
                with open(self.fan_input_path) as f:
                    return int(f.read().strip())
            except Exception:
                return None
        return None

# ---------- TrueNAS API temps (HDD-only, preferred) ----------

def _api_key(cfg):
    return os.environ.get("TRUENAS_API_KEY") or cfg["truenas"].get("api_key") or ""

def api_post(cfg, method, params=None):
    """POST https://TRUENAS/api/v2.0/<method> with Bearer API key.
    method e.g. 'disk.query', 'disk.temperatures'. Raises on any failure."""
    t = cfg["truenas"]
    base = (t.get("api_url") or f"https://{t['host']}").rstrip("/")
    key = _api_key(cfg)
    if not key:
        raise RuntimeError("truenas api_key missing (config truenas.api_key or TRUENAS_API_KEY env)")
    url = f"{base}/api/v2.0/{method}"
    verify = bool(t.get("verify_ssl", False))
    if not verify:
        requests.packages.urllib3.disable_warnings(
            requests.packages.urllib3.exceptions.InsecureRequestWarning)
    r = requests.post(url, json={"method": method, "params": params or []} if False else (params or []),
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      timeout=t.get("timeout_sec", 8), verify=verify)
    # TrueNAS REST v2 expects raw params array as body for /api/v2.0/<method>.
    r.raise_for_status()
    return r.json()

def api_hdd_names(cfg):
    """disk.query -> names of spinning HDDs only (type==HDD, rotationrate not null).
    Respects hdd_only flag + explicit truenas.drives override."""
    t = cfg["truenas"]
    if t.get("drives"):
        return [d.replace("/dev/", "") for d in t["drives"]]
    disks = api_post(cfg, "disk.query", [[], {"select": ["name", "type", "rotationrate", "model"]}])
    names = []
    hdd_only = t.get("hdd_only", True)
    for d in disks:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        dtype = (d.get("type") or "").upper()
        rota = d.get("rotationrate")
        is_hdd = (dtype == "HDD") or (rota is not None)
        is_ssd = dtype in ("SSD", "NVM", "NVME") or (dtype == "" and rota is None and "nvme" in d["name"])
        if hdd_only and not is_hdd:
            continue
        if hdd_only and is_ssd:
            continue
        names.append(d["name"])
    # exclude nvme boot devices explicitly when hdd_only (extra safety)
    if hdd_only:
        names = [n for n in names if not n.startswith("nvme")]
    return names

def parse_api_temp(val):
    """disk.temperatures returns {sda: 38} or {sda: {temperature: 38, ...}}. Normalize to float/None."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        for k in ("temperature", "temp", "current"):
            if isinstance(val.get(k), (int, float)):
                return float(val[k])
    return None

def get_temps_via_api(cfg):
    names = api_hdd_names(cfg)
    if not names:
        raise RuntimeError("api disk.query returned no HDDs (hdd_only filter?)")
    # NOTE: TrueNAS caches disk.temperatures up to 5 min - same value repeats, by design.
    raw = api_post(cfg, "disk.temperatures", [names, False])
    temps, valid = {}, {}
    for n in names:
        dev = f"/dev/{n}"
        tv = parse_api_temp(raw.get(n)) if isinstance(raw, dict) else None
        temps[dev] = round(tv, 1) if tv is not None else None
        if tv is not None:
            valid[dev] = round(tv, 1)
    return names, temps, valid, "api"

# ---------- TrueNAS temps via SSH (fallback, HDD-only via lsblk) ----------

def ssh_exec(cfg, command):
    t = cfg["truenas"]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = t.get("key_path")
    kwargs = {"hostname": t["host"], "username": t["user"], "timeout": t.get("timeout_sec", 8),
              "banner_timeout": 8, "auth_timeout": 8}
    if key:
        kwargs["key_filename"] = os.path.expanduser(key)
    client.connect(**kwargs)
    try:
        _, stdout, _ = client.exec_command(command, timeout=t.get("timeout_sec", 8))
        out = stdout.read().decode(errors="ignore")
        return out
    finally:
        client.close()

def discover_drives(cfg):
    if cfg["truenas"].get("drives"):
        return list(cfg["truenas"]["drives"])
    out = ssh_exec(cfg, "ls -1 /dev/sd? /dev/hd? /dev/nvme?n1 2>/dev/null; echo ---; ls -1 /dev/da? 2>/dev/null")
    drives = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line == "---":
            continue
        if line.startswith("/dev/"):
            drives.append(line)
    # HDD-only: drop SSDs via lsblk ROTA (1=spinning, 0=ssd). Best-effort, keep all on failure.
    if cfg["truenas"].get("hdd_only", True):
        try:
            rota = ssh_exec(cfg, "lsblk -d -n -o NAME,ROTA 2>/dev/null")
            rotamap = {}
            for ln in rota.splitlines():
                p = ln.split()
                if len(p) >= 2:
                    rotamap[p[0]] = p[1]
            filt = [d for d in drives
                    if rotamap.get(d.replace("/dev/", "").rstrip("0123456789"), "1") == "1"
                    and "nvme" not in d]
            if filt:
                return filt
        except Exception:
            pass
        drives = [d for d in drives if "nvme" not in d]
    return drives

def parse_temp_json(j):
    # smartctl -j output: {"temperature": {"current": 38}, ...}
    try:
        d = json.loads(j)
        t = d.get("temperature", {}).get("current")
        if isinstance(t, (int, float)):
            return float(t)
        # NVMe path
        for k in ("nvme_smart_health_information_log",):
            if k in d and isinstance(d[k], dict) and "temperature" in d[k]:
                return float(d[k]["temperature"])
    except Exception:
        pass
    return None

def read_drive_temp(cfg, dev):
    # Prefer JSON, fallback to text
    try:
        out = ssh_exec(cfg, f"smartctl -A -j {dev} 2>/dev/null")
        t = parse_temp_json(out)
        if t is not None:
            return t, "json"
    except Exception:
        pass
    out = ssh_exec(cfg, f"smartctl -A {dev} 2>/dev/null | grep -iE 'Temperature|Airflow' | head -5")
    import re
    # pick first plausible 2-digit temp: e.g. "194 Temperature_Celsius ... 38 (Min/Max ...)"
    for line in out.splitlines():
        m = re.findall(r"\b(\d{2,3})\b", line)
        for cand in m:
            v = int(cand)
            if 10 <= v <= 80 and "194" not in line[:3] or True:
                # heuristic: last 2-digit number on ATA temp lines is usually current
                pass
        # simplest robust: take the token that looks like current temp = 4th+ numeric field
        toks = line.split()
        nums = [x for x in toks if x.lstrip("-").isdigit()]
        if nums:
            # ATA: value like "... 116 ... 38 ..." -> current is commonly nums[-4] or nums[-1]?
            # smartctl text: "194 Temperature_Celsius 0x0022 116 100 000 Old_age Always - 38"
            # -> last token is temp. NVMe similar.
            try:
                v = int(nums[-1])
                if 0 < v < 100:
                    return float(v), "text"
            except Exception:
                continue
    raise RuntimeError(f"no temp parsed for {dev}: {out[:200]}")

def get_all_temps(cfg):
    """Dispatcher: method auto/api/ssh. Returns (drives, temps, valid, source).
    auto = try API (HDD-only), fall back to SSH on any API error."""
    method = (cfg["truenas"].get("method") or "auto").lower()
    last_err = None
    if method in ("api", "auto"):
        try:
            return get_temps_via_api(cfg)
        except Exception as e:
            last_err = e
            if method == "api":
                raise
    # ssh fallback (also HDD-filtered via lsblk)
    drives = discover_drives(cfg)
    temps = {}
    for d in drives:
        try:
            t, src = read_drive_temp(cfg, d)
            temps[d] = round(float(t), 1)
        except Exception as e:
            temps[d] = None
    # drop Nones for control, but keep for reporting
    valid = {k: v for k, v in temps.items() if v is not None}
    if not valid and last_err is not None:
        raise RuntimeError(f"api failed ({last_err}) and ssh fallback got no temps")
    return drives, temps, valid, "ssh"

# ---------- control math ----------

def target_for_temp(cfg, max_temp):
    f = cfg["fan"]; t = cfg["temps_c"]
    if max_temp >= t["critical"]:
        return f["emergency_pwm"], "critical"
    if max_temp >= t["hot"]:
        return f["ceiling_pwm"], "hot"
    if max_temp <= t["cool"]:
        return f["floor_pwm"], "cool"
    # linear cool..hot -> floor..ceiling
    ratio = (max_temp - t["cool"]) / max(t["hot"] - t["cool"], 0.1)
    pwm = int(f["floor_pwm"] + ratio * (f["ceiling_pwm"] - f["floor_pwm"]))
    return max(f["floor_pwm"], min(f["ceiling_pwm"], pwm)), "ramp"

# ---------- MQTT + ntfy ----------

class Pub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.m = None
        mcfg = cfg.get("mqtt", {})
        if mcfg.get("enabled") and mqtt:
            try:
                self.m = mqtt.Client(client_id="nastemp-controller", clean_session=True)
                if mcfg.get("username"):
                    self.m.username_pw_set(mcfg["username"], mcfg.get("password", ""))
                base = mcfg.get("base", "nastemp")
                self.m.will_set(f"{base}/online", "offline", retain=True)
                self.m.connect(mcfg["broker"], int(mcfg.get("port", 1883)), 60)
                self.m.loop_start()
                self.m.publish(f"{base}/online", "online", retain=True)
            except Exception as e:
                print(f"MQTT connect failed: {e}", flush=True)
                self.m = None

    def pub(self, sub, val, retain=None):
        if not self.m:
            return
        base = self.cfg["mqtt"].get("base", "nastemp")
        try:
            self.m.publish(f"{base}/{sub}", str(val),
                           retain=self.cfg["mqtt"].get("retain", True) if retain is None else retain)
        except Exception:
            pass

def ntfy(cfg, msg, title="nastemp", priority="default", tags=""):
    n = cfg.get("ntfy", {})
    if not n.get("enabled") or not n.get("url"):
        return
    try:
        requests.post(n["url"], data=msg.encode("utf-8"), timeout=8,
                      headers={"Title": title, "Priority": priority, "Tags": tags})
    except Exception as e:
        print(f"ntfy failed: {e}", flush=True)

# ---------- main ----------

def main():
    cfg = load_cfg(CFG_PATH)
    init_db(cfg)
    hw = PwmHw(cfg)
    pub = Pub(cfg)
    F = cfg["fan"]; T = cfg["temps_c"]; TM = cfg["timing"]

    if not hw.pwm_path:
        print("FATAL: it87 pwm path not found. Load it87 module? (modprobe it87)", flush=True)
        sys.exit(2)

    # boot: safe default 40%
    ok, info = hw.write_pwm(F["safe_pwm"])
    log_line(cfg, f"boot pwm={F['safe_pwm']} ok={ok} info={info} path={hw.pwm_path} source={cfg['truenas'].get('method','auto')}")
    if not ok:
        ntfy(cfg, f"BOOT FAN WRITE FAILED: {info}. Check it87 module / pwm channel!",
             title="nastemp BOOT failure", priority="urgent", tags="rotating_light,fan")
    heartbeat(cfg)
    pub.pub("fan/pwm", F["safe_pwm"])
    pub.pub("fan/pct", round(F["safe_pwm"] / 2.55, 1))

    current = hw.read_pwm() or F["safe_pwm"]
    fails = 0
    api_fail_notified = False
    last_ov_sig = override_sig(cfg)
    tn_link = LinkWatch("TrueNAS")      # temps source (API/SSH)
    mqtt_link = LinkWatch("MQTT broker")  # HA telemetry path
    pwm_link = LinkWatch("fan PWM")     # local hardware writes
    cool_since = time.time()   # last time we were at/below target (for cooldown)
    boost_start = None         # when we first went above floor
    last_event = "init"

    ntfy(cfg, f"nastemp-2 started. src={cfg['truenas'].get('method','auto')} hdd_only={cfg['truenas'].get('hdd_only',True)} floor={F['floor_pwm']} ceiling={F['ceiling_pwm']} cool={T['cool']}C hot={T['hot']}C",
         title="nastemp started", tags="fan")

    while True:
        loop_start = time.time()
        heartbeat(cfg)
        rpm = hw.read_rpm()
        source = "unknown"

        try:
            drives, temps, valid, source = get_all_temps(cfg)
            if fails > 0:
                log_line(cfg, f"RECOVERED via {source} after {fails} fails")
            fails = 0
            api_fail_notified = False
            st, down_for = tn_link.report(True)
            if st == "recovered" and cfg["ntfy"].get("on_recovery", True):
                ntfy(cfg, f"TrueNAS recovered after {int(down_for)}s down. Resuming normal control.",
                     title="nastemp recovered", tags="white_check_mark,thermometer")
        except Exception as e:
            fails += 1
            valid = {}
            temps = {}
            drives = []
            source = "failed"
            log_line(cfg, f"TRUENAS FAIL {fails}/{cfg['truenas']['fail_threshold']}: {e}")
            # Immediate ntfy on 1st failure of the outage, with down-since stamp
            st, msg = tn_link.report(False, str(e)[:120])
            if st == "failed" and cfg["ntfy"].get("on_api_fail", True):
                ntfy(cfg, f"{msg}. Retrying, SSH fallback in auto mode.",
                     title="nastemp TrueNAS failed", priority="high", tags="warning,thermometer")
                api_fail_notified = True

        # MQTT link check (TCP, every cycle): HA blind is alert-worthy, control continues locally
        m = cfg.get("mqtt", {})
        if m.get("enabled"):
            try:
                socket.create_connection((m["broker"], int(m.get("port", 1883))), timeout=3).close()
                st, down_for = mqtt_link.report(True)
                if st == "recovered" and cfg["ntfy"].get("on_recovery", True):
                    ntfy(cfg, f"MQTT broker {m['broker']} recovered after {int(down_for)}s. HA graphs resume.",
                         title="nastemp recovered", tags="white_check_mark,antenna")
            except Exception as e:
                st, msg = mqtt_link.report(False, str(e)[:100])
                log_line(cfg, f"MQTT FAIL: {msg}")
                if st == "failed":
                    ntfy(cfg, f"{msg}. HA graphs blind — fan control continues locally on Proxmox.",
                         title="nastemp MQTT failed", priority="high", tags="warning,antenna")

        if not valid and fails >= cfg["truenas"].get("fail_threshold", 3):
            # FAILSAFE: TrueNAS lost -> step DOWN to safe, never stuck high
            reason = (f"truenas_lost fails={fails} down {int(tn_link.down_for())}s: "
                      f"stepping DOWN to safe {F['safe_pwm']}")
            if current > F["safe_pwm"]:
                current = max(F["safe_pwm"], current - TM["step_down_pwm"])
                hw.write_pwm(current)
                event = {"ts": datetime.now().isoformat(), "event": "failsafe_step_down",
                         "reason": reason, "pwm": current}
                pub.pub("event", json.dumps(event), retain=False)
                pub.pub("status", reason)
                log_line(cfg, f"EVENT failsafe_step_down pwm={current} {reason}")
                log_jsonl(cfg, event)
                log_db(cfg, {"ts": event["ts"], "source": "failed", "max_temp": None,
                             "avg": None, "target": F["safe_pwm"], "pwm": current,
                             "pct": round(current / 2.55, 1), "rpm": rpm,
                             "action": "failsafe_step_down", "why": reason},
                       None, {"ts": event["ts"], "event": "failsafe_step_down",
                              "max_temp": None, "pwm": current, "why": reason})
                if cfg["ntfy"].get("on_failsafe"):
                    ntfy(cfg, reason, title="nastemp failsafe", priority="high", tags="warning,fan")
                last_event = "failsafe"
            else:
                pub.pub("status", "failsafe holding at safe speed (truenas lost)")
            pub.pub("fan/pwm", current)
            pub.pub("fan/pct", round(current / 2.55, 1))
            if rpm is not None:
                pub.pub("fan/rpm", rpm)
            heartbeat(cfg)
            time.sleep(TM["interval_sec"])
            continue
        elif not valid:
            log_line(cfg, "no valid temps this cycle, holding")
            time.sleep(TM["interval_sec"])
            continue

        max_temp = max(valid.values())
        avg_temp = round(sum(valid.values()) / len(valid), 1)
        target, zone = target_for_temp(cfg, max_temp)

        # publish per-drive + aggregates (HA history comes from recorder)
        for dev, tv in temps.items():
            safe = dev.replace("/dev/", "")
            pub.pub(f"hdd/{safe}/temp", tv if tv is not None else "unknown", retain=False)
        pub.pub("hdd/max_temp", max_temp, retain=False)
        pub.pub("hdd/avg_temp", avg_temp, retain=False)
        pub.pub("hdd/count", len(valid), retain=False)
        pub.pub("hdd/source", source, retain=False)

        # ---- manual override (admin slider): direct target, auto-expires ----
        manual_on, mpwm, mov = False, None, {}
        if cfg.get("manual", {}).get("enabled", True):
            ov = load_override(cfg)
            if ov.get("expired"):
                clear_override(cfg)
                last_ov_sig = None
                msg = (f"EVENT manual_expired pwm was {ov.get('pwm')} by {ov.get('by')} "
                       f"-> back to AUTO")
                log_line(cfg, msg)
                log_jsonl(cfg, {"ts": datetime.now().isoformat(), "event": "manual_expired",
                                "pwm": ov.get("pwm"), "why": msg})
                if cfg["ntfy"].get("on_recovery", True):
                    ntfy(cfg, "Manual fan override expired. Back to automatic control.",
                         title="nastemp back to auto", tags="robot,fan")
            elif ov.get("active"):
                manual_on, mpwm, mov = True, ov["pwm"], ov
                if ov["mtime"] != last_ov_sig:
                    last_ov_sig = ov["mtime"]
                    left = int(ov["until"] - time.time())
                    msg = (f"EVENT manual_set by {ov['by']}: PWM {mpwm} "
                           f"({round(mpwm/2.55,1)}%) for {left}s")
                    log_line(cfg, msg)
                    log_jsonl(cfg, {"ts": datetime.now().isoformat(), "event": "manual_set",
                                    "pwm": mpwm, "why": msg})
                    ntfy(cfg, f"Manual fan override by {ov['by']}: PWM {mpwm} "
                              f"({round(mpwm/2.55,1)}%), auto-expires in {left//60}min.",
                         title="Manual fan control", tags="joystick,fan")

        now = time.time()
        action, why = "hold", f"zone={zone} target={target} current={current}"

        # MAX-BOOST guard: elevated longer than max_boost_sec without critical -> force step down
        if current > F["floor_pwm"]:
            if boost_start is None:
                boost_start = now
            elif (now - boost_start) > TM["max_boost_sec"] and max_temp < T["critical"]:
                current = max(target, F["safe_pwm"], current - TM["step_down_pwm"] * 2)
                hw.write_pwm(current)
                boost_start = now  # re-arm, will keep forcing down while unjustified
                action, why = "failsafe_maxboost_stepdown", \
                    f"elevated >{TM['max_boost_sec']}s without critical temp; forcing down to {current}"
                event = {"ts": datetime.now().isoformat(), "event": "maxboost_stepdown",
                         "max_temp": max_temp, "pwm": current, "reason": why}
                pub.pub("event", json.dumps(event), retain=False)
                log_line(cfg, f"EVENT {action} max={max_temp}C pwm={current} {why}")
                log_jsonl(cfg, event)
                log_db(cfg, {"ts": event["ts"], "source": source, "max_temp": max_temp,
                             "avg": avg_temp, "target": target, "pwm": current,
                             "pct": round(current / 2.55, 1), "rpm": rpm,
                             "action": action, "why": why}, temps, event)
                ntfy(cfg, f"Failsafe: boosted >{TM['max_boost_sec']//60}min, stepping down to {current} (temp {max_temp}C)",
                     title="nastemp max-boost guard", priority="high", tags="warning,fan")
        else:
            boost_start = None

        if manual_on and action == "hold":
            # Manual mode: go straight to the slider value (critical still wins below).
            # Cooldown/hysteresis/max-boost are skipped — expiry is the guardrail.
            if current != mpwm:
                current = mpwm
                hw.write_pwm(current)
            left = int(mov["until"] - time.time())
            action, why = "manual", f"MANUAL by {mov['by']} pwm={mpwm} expires in {left}s"
        elif action == "hold":
            if target > current:
                current = min(target, current + TM["step_up_pwm"])
                hw.write_pwm(current)
                cool_since = now  # still heating -> re-arm cooldown
                if boost_start is None:
                    boost_start = now
                action, why = "step_up", f"max={max_temp}C zone={zone} target={target} -> {current}"
            elif target < current:
                # Hysteresis gate: don't leave ceiling until temp drops below hot - hysteresis
                if current >= F["ceiling_pwm"] - 5 and max_temp > T["hot"] - T["hysteresis"]:
                    why = f"HOLD still hot {max_temp}C > {T['hot']-T['hysteresis']}C (hysteresis), pwm={current}"
                    cool_since = now  # still hot, keep cooldown fresh
                elif (now - cool_since) >= TM["cooldown_down_sec"]:
                    # cooldown satisfied -> step down every cycle from here (don't re-arm)
                    current = max(target, current - TM["step_down_pwm"])
                    hw.write_pwm(current)
                    action, why = "step_down", f"max={max_temp}C cooled {int(now-cool_since)}s, target={target} -> {current}"
                else:
                    why = f"cooling, cooldown {int(now-cool_since)}s/{TM['cooldown_down_sec']}s pwm={current}"
            else:
                # target == current and elevated -> still hot, keep cooldown fresh
                # target == current at floor -> irrelevant
                if current > F["floor_pwm"]:
                    cool_since = now
                why = f"HOLD current={current} target={target} zone={zone} max={max_temp}C"

        # critical override ignores cooldown
        if max_temp >= T["critical"] and current != F["emergency_pwm"]:
            current = F["emergency_pwm"]
            hw.write_pwm(current)
            action, why = "emergency", f"CRITICAL {max_temp}C -> 255"
            ntfy(cfg, f"CRITICAL HDD {max_temp}C! Fans 100% (emergency override)",
                 title="HDD CRITICAL", priority="urgent", tags="rotating_light,thermometer")

        actual = hw.read_pwm()
        st, msg = pwm_link.report(actual is not None, "sysfs readback failed")
        if st == "failed":
            log_line(cfg, f"PWM FAIL: {msg}")
            ntfy(cfg, f"{msg}. Fan control writes may be broken — check it87 module NOW!",
                 title="nastemp FAN CONTROL failure", priority="urgent", tags="rotating_light,fan")
        elif st == "recovered" and cfg["ntfy"].get("on_recovery", True):
            ntfy(cfg, "Fan PWM readback recovered. Hardware control OK.",
                 title="nastemp recovered", tags="white_check_mark,fan")
        if actual is None:
            actual = current
        elif abs(actual - current) > max(TM["step_up_pwm"] * 2, 24):
            log_line(cfg, f"PWM drift: expected {current}, hardware reads {actual} (external change?)")
        status = (f"{'[MANUAL] ' if manual_on else ''}{action} max={max_temp}C avg={avg_temp}C "
                  f"pwm={actual}({round(actual/2.55,1)}%) rpm={rpm} why:{why}")
        pub.pub("fan/pwm", actual, retain=False)
        pub.pub("fan/pct", round(actual / 2.55, 1), retain=False)
        if rpm is not None:
            pub.pub("fan/rpm", rpm, retain=False)
        pub.pub("fan/target", target, retain=False)
        pub.pub("fan/mode", "manual" if manual_on else "auto", retain=False)
        pub.pub("status", status, retain=False)

        rec = {"ts": datetime.now().isoformat(), "source": source, "max_temp": max_temp, "avg": avg_temp,
               "temps": temps, "target": target, "pwm": actual,
               "pct": round(actual / 2.55, 1), "rpm": rpm, "action": action, "why": why}
        log_jsonl(cfg, rec)

        if action in ("step_up", "emergency", "failsafe_maxboost_stepdown"):
            evt = {"ts": rec["ts"], "event": action, "max_temp": max_temp, "pwm": actual, "why": why}
            pub.pub("event", json.dumps(evt), retain=False)
            log_line(cfg, f"EVENT {action} max={max_temp}C pwm={actual} {why}")
            log_db(cfg, rec, temps, evt)
            if action == "step_up" and cfg["ntfy"].get("on_step_up"):
                ntfy(cfg, f"Step UP: {max_temp}C -> PWM {actual} ({round(actual/2.55,1)}%). {why}",
                     title="Fan step up", tags="fan,thermometer")
            last_event = action
        elif action == "step_down":
            evt = {"ts": rec["ts"], "event": action, "max_temp": max_temp, "pwm": actual, "why": why}
            pub.pub("event", json.dumps(evt), retain=False)
            log_line(cfg, f"EVENT {action} max={max_temp}C pwm={actual} {why}")
            log_db(cfg, rec, temps, evt)
            if cfg["ntfy"].get("on_step_down"):
                ntfy(cfg, f"Step DOWN: {max_temp}C -> PWM {actual}. {why}", title="Fan step down", tags="fan")
            last_event = action
            # NOTE: do NOT reset cool_since here - after initial cooldown,
            # keep stepping down every cycle until target reached.
        else:
            # light status log every cycle for investigation (goes to .log + jsonl + sqlite, not ntfy)
            log_line(cfg, status)
            log_db(cfg, rec, temps, None)

        heartbeat(cfg)
        # Chunked sleep: re-check the manual override every ≤5s so the UI
        # slider feels real-time instead of waiting out the 30s cycle.
        elapsed = time.time() - loop_start
        deadline = time.time() + max(5, TM["interval_sec"] - elapsed)
        while True:
            time.sleep(min(5, max(1, deadline - time.time())))
            if time.time() >= deadline:
                break
            if override_sig(cfg) != last_ov_sig:
                log_line(cfg, "override changed -> re-evaluating now")
                break

if __name__ == "__main__":
    main()
