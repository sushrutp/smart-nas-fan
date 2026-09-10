#!/usr/bin/env python3
"""
nastemp-2 / watchdog.py  (Layer-2 failsafe)
If fan_controller.py dies / hangs / loses TrueNAS for too long,
heartbeat goes stale -> force fans to SAFE 40% so they NEVER get stuck high.

Run as separate systemd service. No MQTT required (stdlib only).
"""
import glob
import os
import subprocess
import time

HEARTBEAT = os.environ.get("NASTEMP_HEARTBEAT", "/run/nastemp/heartbeat")
SAFE_PWM = int(os.environ.get("NASTEMP_SAFE_PWM", "102"))
STALE_SEC = int(os.environ.get("NASTEMP_STALE_SEC", "300"))  # 5 min
CHECK_SEC = 15
DRV = "it87"
CHAN = os.environ.get("NASTEMP_PWM", "pwm2")
DBG = os.environ.get("NASTEMP_DEBUG", "") not in ("", "0", "no", "false")

def debug(msg):
    if DBG:
        print(f"WATCHDOG DEBUG: {msg}", flush=True)

def find_pwm():
    for nf in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(nf) as f:
                if DRV in f.read():
                    return os.path.join(os.path.dirname(nf), CHAN)
        except Exception:
            continue
    c = glob.glob(f"/sys/class/hwmon/hwmon*/{CHAN}")
    return c[0] if c else None

def force_safe(reason):
    p = find_pwm()
    if not p:
        print(f"WATCHDOG: {reason} but no pwm path found!", flush=True)
        return
    try:
        en = os.path.join(os.path.dirname(p), f"{CHAN}_enable")
        if os.path.exists(en):
            with open(en, "w") as f:
                f.write("1")
        with open(p, "w") as f:
            f.write(str(SAFE_PWM))
        print(f"WATCHDOG TRIGGER [{reason}]: forced {p} -> {SAFE_PWM} (40%)", flush=True)
    except Exception as e:
        print(f"WATCHDOG FAILED: {e}", flush=True)

def main():
    print(f"watchdog start: hb={HEARTBEAT} safe={SAFE_PWM} stale>{STALE_SEC}s chan={CHAN} drv={DRV}", flush=True)
    ntfy_url = os.environ.get("NASTEMP_NTFY") or os.environ.get("NTFY_URL") or ""
    debug(f"config ntfy={'set' if ntfy_url else '(empty, no push on trigger)'} check_every={CHECK_SEC}s "
          f"pwm_path={find_pwm() or '(not found yet)'}")
    last_forced = 0
    while True:
        try:
            if not os.path.exists(HEARTBEAT):
                # controller never ran / /run wiped on reboot -> ensure safe once
                debug("heartbeat file missing")
                if time.time() - last_forced > 600:
                    force_safe("no heartbeat file")
                    last_forced = time.time()
            else:
                with open(HEARTBEAT) as f:
                    ts = float(f.read().strip())
                age = time.time() - ts
                debug(f"heartbeat age={age:.1f}s (stale>{STALE_SEC}s)")
                if age > STALE_SEC and time.time() - last_forced > 300:
                    force_safe(f"heartbeat stale {int(age)}s")
                    last_forced = time.time()
                    # try ntfy if configured via env (optional)
                    import urllib.request
                    url = os.environ.get("NASTEMP_NTFY") or os.environ.get("NTFY_URL")
                    if url:
                        try:
                            debug(f"ntfy POST {url}")
                            req = urllib.request.Request(
                                url, data=f"Watchdog: controller stale {int(age)}s, fans forced to 40%".encode(),
                                headers={"Title": "nastemp watchdog", "Priority": "high"})
                            urllib.request.urlopen(req, timeout=8)
                            debug("ntfy sent")
                        except Exception as e:
                            print(f"watchdog ntfy failed: {e}", flush=True)
        except Exception as e:
            print(f"watchdog loop err: {e}", flush=True)
        time.sleep(CHECK_SEC)

if __name__ == "__main__":
    main()
