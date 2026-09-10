# nastemp-2 — Simple INSTALL.md

## 0. What goes where (placement)

| # | Component | Runs on | What to do there |
|---|---|---|---|
| 1 | `fan_controller.py` + `watchdog.py` (fan control, stays on Proxmox) | **Proxmox host** — native systemd (recommended) OR Docker on the Proxmox host | Install via `setup.sh` (native) or `docker compose up` (Docker). Needs `/sys/class/hwmon` with `it87/pwm2`. |
| 2 | Temps source (HDD-only) | **TrueNAS** — nothing to install, just enable API + SSH | Create API key + SSH user (see §1). |
| 3 | Telemetry bus | **MQTT broker** (Mosquitto, usually HA add-on) | Note IP/port/user/pass. |
| 4 | Graphs + history | **Home Assistant** | Paste `ha_sensors.yaml`, merge `ha_dashboard.yaml`. |
| 5 | Push alerts | **ntfy server** (self-hosted or ntfy.sh) + your phone/browser | Pick a topic URL (see §1). |
| 6 | History DB (local, no network) | **Proxmox** `/var/log/` (Docker: `./logs/`) | `nastemp.log` + `nastemp.jsonl` + `nastemp.db` — automatic. |

Fan control NEVER leaves Proxmox. Everything else is poll (outbound) or publish (outbound).

## 1. What you must generate BEFORE install (checklist)

| # | Needed | How to generate | Example | Where it goes |
|---|---|---|---|---|
| 1 | **TrueNAS API key** (HDD temps, primary) | TrueNAS UI → Credentials → API Keys → Add → name `nastemp-monitor` → Copy key ONCE (needs `REPORTING_READ` or admin) | `1-abc...xyz` | `config.yaml: truenas.api_url: "https://192.168.1.10"`, `truenas.api_key: "<key>"` (or env `TRUENAS_API_KEY=<key>` — safer for Docker) |
| 2 | **TrueNAS SSH key** (fallback when API down) | On Proxmox: `ssh-keygen -t ed25519 -N ""` then `ssh-copy-id admin@192.168.1.10` | `/root/.ssh/id_ed25519` | `config.yaml: truenas.host/user/key_path`. Docker: host `~/.ssh` auto-mounted to `/root/.ssh` |
| 3 | **MQTT broker address + login** | HA → Settings → Add-ons → Mosquitto broker → note IP/port/user/pass (or existing broker) | `192.168.1.5:1883` user/pass | `config.yaml: mqtt.broker/port/username/password` |
| 4 | **ntfy topic URL** (the "token" — secret URL) | Self-hosted: pick `http://192.168.1.5:8080/nastemp`. Public: `https://ntfy.sh/<pick-unguessable-name>`. Test: `curl -d "test" <URL>` → check phone/web app subscribed to same topic | `http://192.168.1.5:8080/nastemp` | `config.yaml: ntfy.url`. Subscribe same topic in ntfy phone app / web |
| 5 | (Optional) HA long history | Nothing to generate — just config | `purge_keep_days: 30` | HA `configuration.yaml` → `recorder:` (see README §6.3) |

No Proxmox token, no Docker token, no HA token needed.

## 2. Firewall rules you need (all OUTBOUND from Proxmox, nothing inbound)

| From → To | Port | Why | Allow? |
|---|---|---|---|
| Proxmox → TrueNAS | TCP **443** (HTTPS API) | `disk.query` + `disk.temperatures` (HDD-only) | yes (`method:auto/api`) |
| Proxmox → TrueNAS | TCP **22** (SSH) | `smartctl` fallback | yes (`method:auto/ssh`) |
| Proxmox → MQTT broker | TCP **1883** (or 8883 TLS) | publish `nastemp/#` | yes if `mqtt.enabled:true` |
| Proxmox → ntfy | TCP **80/443/8080** | push alerts | yes if `ntfy.enabled:true` |
| HA → MQTT broker | TCP **1883** | subscribe `nastemp/#` | yes (often same box) |
| You → Docker host | TCP **6767** | admin control center 🌀 (login required) | yes, LAN-only recommended |

Rule of thumb: allow Proxmox out to TrueNAS + broker + ntfy. Only inbound port is **6767** (admin UI) — keep it on LAN, behind your firewall, with a strong `ADMIN_PASS`.

## 3. Install — Option A: native on Proxmox (recommended)

```bash
cd nastemp-2
nano config.yaml   # fill §1 items: api_url, api_key, truenas.host, mqtt.broker, ntfy.url
sudo bash setup.sh
nano /opt/nastemp/config.yaml   # repeat your edits (installed copy)
sudo systemctl start nastemp-controller nastemp-watchdog
systemctl status nastemp-controller nastemp-watchdog
```

### 3b. Surviving Proxmox updates (no Docker needed, nothing to redo by hand)

- **Normal updates (`apt upgrade`):** `/opt/*`, `/etc/systemd/system`, `/var/log`, your SSH
  keys and your live `/opt/nastemp/config.yaml` all survive. Nothing to do.
- **If something breaks after an update** (new kernel/Python), recovery is one command —
  `setup.sh` is idempotent: it rebuilds the isolated venv + units from scratch and
  **never overwrites** your live config:
  ```bash
  cd ~/smart-nas-fan && git pull && sudo bash setup.sh
  sudo systemctl restart nastemp-controller nastemp-watchdog nastemp-admin
  modprobe it87   # only if `grep -H . /sys/class/hwmon/hwmon*/name` lost it87 after a kernel update
  ```
- **Keep a repo clone on Proxmox** (`git clone -b v1.5 git@github.com:sushrutp/smart-nas-fan.git ~/smart-nas-fan`)
  so recovery never depends on re-uploading files.
- **Back up two files** (everything else is regenerable): `/opt/nastemp/config.yaml`
  (your keys/settings) and `/var/log/nastemp.db` (history).
- **Major upgrades** (e.g. PVE 8→9): same recovery command, then verify `python3 --version`
  and re-check the HW test (`bash test_fan.sh 140`).
- **Zero-footprint alternative:** run only the 2 tiny `.py` scripts on Proxmox and the GUI
  on another VM via remote mode (§4c) — then Proxmox holds almost nothing of ours.

## 4. Install — Option B: Docker (on Proxmox host with `it87` visible)

```bash
cd nastemp-2
mkdir -p logs
nano config.yaml   # same §1 edits; key_path: "/root/.ssh/id_ed25519"
# safer: export TRUENAS_API_KEY="1-abc...xyz"  (instead of writing key in file)
docker compose up -d --build
docker compose logs -f controller
docker compose logs -f watchdog
```

Must be `privileged:true` + `/sys/class/hwmon:rw` (already in compose). If `permission denied` on `pwm2` → your platform blocks `/sys` writes → use Option A.

Before `docker compose down`, always park fans quiet:
```bash
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')
```

## 4b. Admin control center 🌀 (neon UI on `0.0.0.0:6767`)

Included as 3rd container (`admin/` → FastAPI). One screen shows: live flow map
(TrueNAS → Proxmox → Fan / MQTT→HA / ntfy / DB, neon = flowing, grey = broken),
RGB 🌀 fan (spins with real %, 🟢 normal / 🟠 boosted ⬆️ / 🔴 critical ⬆️⬆️ / ⏹️ stopped),
⚡ boosted-since timer (ms precision) + last ⬆️ / last ⬇️ from event log,
ntfy flash on new event, shared-axis chart (🌡️ temp + 🌀 fan% + 🔔 ntfy 0/1),
⬇️ CSV export (readings + events, opens in Excel), logs, and config editor
(🧪 easy form grouped by section + 📝 raw YAML).
- Header has **● live / ▶ demo switch** + clock with **milliseconds** — demo simulates
  3 HDDs with heat waves **plus random system faults** (🔥 banner, red lines) so you can
  see the whole show with no hardware.
- **🌍 outside weather** (Open-Meteo, free, no key): set `weather.postcode` (default
  `33333`) + `weather.country`, compare HDD max Δ vs outside with condition emoji
  (☀️🌧️❄️⛈️🌫️…).
- Live data pushes over **SSE `/stream`** (auto-reconnect, polling fallback); flow lines are
  **green = flowing, blue = idle, red glow = broken**, with **⚡ ping ms** on the
  TrueNAS + MQTT arrows.
- **🎛️ Manual fan control card**: slider (live ≤5s) + ⬆️/⬇️ ±10 + 🔥 boost (ceiling) +
  🟢 quiet (floor) + 🤖 auto-release. Safety: auto-expires (`manual.max_sec`),
  never below floor, 🌡️ critical always wins, `fan/mode` topic for HA. Disabled in demo.
- **🖥️ Host metrics + 💽 array I/O**: CPU/RAM gauges for Proxmox + TrueNAS (via
  `reporting.get_data`), live read/write MB/s (heavy scrub/backup explains temp spikes).
- **📨 Send Test Alert** button in the ntfy card verifies the push pipeline on demand.

```bash
ADMIN_USER=me ADMIN_PASS='s3cret!' docker compose up -d --build admin
# open: http://<docker-host-ip>:6767
```

- Login with those credentials (defaults `admin` / `nastemp` — change them!).
- Fan control stays in `controller`; after saving config in the UI, restart it:
  `docker compose restart controller` (native: `systemctl restart nastemp-controller`).
- The `admin` container needs no `privileged` (sysfs mounted `:ro` for the fan readout).

## 4c. Run the GUI on a different VM/host (remote mode)

The controller **must** stay on Proxmox (it touches `/sys`). The GUI can live anywhere
with IP access — it reads Proxmox files over **SSH key auth** instead of local mounts:

```bash
# on the other VM/host:
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_proxmox   # if you don't have a key yet
ssh-copy-id -i ~/.ssh/id_proxmox root@192.168.1.2  # trust it on Proxmox
# docker: uncomment the PROXMOX_* env + ~/.ssh mount in compose, then:
PROXMOX_HOST=192.168.1.2 PROXMOX_USER=root docker compose up -d --build admin
# native (no docker): apt install python3-fastapi python3-uvicorn + pip install paramiko, then:
PROXMOX_HOST=192.168.1.2 PROXMOX_USER=root PROXMOX_CONFIG=/opt/nastemp/config.yaml \
  python3 -m uvicorn app:app --host 0.0.0.0 --port 6767 --app-dir ./admin
# open: http://<this-vm-ip>:6767  (header shows 📍 remote → 192.168.1.2)
# custom SSH port: PROXMOX_PORT=2222. Key: PROXMOX_KEY=/root/.ssh/id_ed25519 (default).
```

In remote mode fan PWM, heartbeat, DB/graphs/CSV, logs, config editor, manual slider
and Proxmox CPU/RAM all go over SSH (`PROXMOX_CONFIG` = controller config **on Proxmox**).
TrueNAS/MQTT/ntfy/weather are network services and work identically. Empty
`PROXMOX_HOST` = local mode (current behavior, zero change).

**Freshness in remote mode (honest numbers):** SSH holds one persistent connection
(auto-reconnect); the fan gauge has a **1s fast lane** (`/api/fast` = fan+heartbeat in
a single roundtrip, ~RTT). Full status pushes every ~2s, history DB re-pulls every 30s
(data only changes every 30s anyway), TrueNAS temps refresh every ~5 min **by TrueNAS
design**. The ms clock, boosted-since and down-since timers always tick client-side
at 97ms regardless of transport.

## 5. Install — Home Assistant (graphs where spike = fan rise)

1. Paste `ha_sensors.yaml` into HA `configuration.yaml` (or package), reload MQTT / restart. New: `sensor.nas_temp_source` (api/ssh), `sensor.nas_hdd_count`.
2. Dashboard → Edit → Raw → merge `ha_dashboard.yaml`. Key card: **"Temp spike -> Fan rise (SAME axis)"** — temp + fan % share one graph so you see cause → effect.
3. Per-drive: after first run check `mosquitto_sub -h BROKER -t "nastemp/#" -v`, add one sensor block per `nastemp/hdd/<disk>/temp`.

## 6. How to CHECK the setup (in order, stop at first failure)

```bash
# 1. Fans/hardware (Proxmox as root):
grep -H . /sys/class/hwmon/hwmon*/name          # must show it87
sudo bash test_fan.sh 140                        # hear spin-up = OK
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')  # back to 40%

# 2. TrueNAS API (from Proxmox):
curl -k -H "Authorization: Bearer <API-KEY>" https://192.168.1.10/api/v2.0/system/info | head -c 200
# expect JSON. Empty/401 = wrong key. Key revoked? = you used http:// once, create new key, use https:// only.

# 3. TrueNAS SSH fallback (from Proxmox):
ssh admin@192.168.1.10 "smartctl -A /dev/sda | head -20"
# expect temp line. Fail = key auth / SSH service / smartctl rights.

# 4. Controller live log (Proxmox):
journalctl -u nastemp-controller -f               # native
# or: docker compose logs -f controller           # docker
tail -f /var/log/nastemp.log                     # expect: boot pwm=102 ... source=api, then HOLD/step_up lines
# API first-fail -> ntfy alert + SSH fallback (method:auto). 3 fails -> failsafe_step_down + ntfy.

# 5. MQTT (any box with mosquitto-clients):
mosquitto_sub -h 192.168.1.5 -t "nastemp/#" -v
# expect: nastemp/hdd/max_temp, nastemp/fan/pwm, nastemp/hdd/source api, nastemp/event ...

# 6. DB (Proxmox):
sqlite3 /var/log/nastemp.db "SELECT ts,source,max_temp,pwm,action FROM readings ORDER BY ts DESC LIMIT 5;"
sqlite3 /var/log/nastemp.db "SELECT ts,event,max_temp,pwm FROM events ORDER BY ts DESC LIMIT 5;"

# 7. ntfy (phone/web) — you get a push for EVERY failure + recovery:
# restart controller -> "nastemp started". Break TrueNAS key -> "TrueNAS failed ... DOWN since <ts>".
# Stop Mosquitto -> "MQTT failed ... HA graphs blind". Unplug pwm (it87 rmmod) -> urgent "FAN CONTROL failure".
# Fix it -> "recovered after Ns" pushes. Watchdog force-reset -> push ONLY if NASTEMP_NTFY is set
#   (native: uncomment Environment=NASTEMP_NTFY in nastemp-watchdog.service; docker: uncomment in compose).

# 8. HA:
# Developer Tools -> States: sensor.nas_hdd_max_temp, sensor.nas_fan_speed have values.
# Dashboard NAS Cooling: temp spike and fan % rise on SAME graph; Logbook event timestamps match spikes.

# 9. Admin UI down-timers (http://<host>:6767):
# break any link -> its pill turns 🔴 and a "🔴 <link> down HH:MM:SS.mmm" timer ticks under the flow map.
# Fix it -> timer clears, "🟢 all links up". Same works in ▶ demo mode (random faults).
```

## 7. If something is wrong (quick map)

| Symptom | Check |
|---|---|
| `pwm_path not found` | `modprobe it87` |
| API 401/empty | wrong key, or key revoked via http — new key, https only, `verify_ssl:false` for self-signed |
| `no HDDs` | `hdd_only:true` + only SSDs? SSDs correctly ignored; force with `truenas.drives:` |
| `SSH FAIL` | `ssh-copy-id`, TrueNAS SSH on, user can run smartctl |
| No MQTT in HA | broker IP/user/pass, HA MQTT integration, `ha_sensors.yaml` loaded |
| No push | `curl -d test <ntfy.url>`, `ntfy.enabled` + `on_*` flags, same topic subscribed on phone |
| Docker `permission denied` on pwm2 | use native install (Option A) |

Emergency park (always safe):
```bash
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')
```
