# nastemp-2 — Smart HDD-Temperature Fan Control

Control Proxmox chassis fans from TrueNAS HDD temperatures. Quiet by default,
louder only when disks are hot, with hard failsafes so fans can never get
stuck at high speed.

- **Host:** Proxmox (Arctic P12 Pro daisy-chain on `it87` / `pwm2`)
  - Your known-good manual command is the hardware primitive:
    `echo 140 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')`
- **Temps:** TrueNAS HDDs only (SSD/NVMe ignored) — HTTPS API first
  (`disk.query` + `disk.temperatures`), SSH + `smartctl` fallback
- **Visibility:** Home Assistant via MQTT (Mosquitto) + ntfy push alerts +
  neon admin UI on `:6767` (local or remote, see `admin/README.md`)
- **Secrets:** never in files — `.env` (Docker) / `/opt/nastemp/nastemp.env` (native).
  Copy `.env.example` → `.env` first.
- **Policy:** default **40% (PWM 102)**, normal ceiling **~72% (PWM 184)** —
  never 100% in normal regulation. **255 only on critical (≥52 °C).**

Docs: `INSTALL.md` (setup, step-by-step) · `ARCHITECTURE.md` (how it all works) ·
`admin/README.md` (control center) · `VERSION` (current release).

---

## 1. How it works

```text
TrueNAS (HDD-only °C: API 443, fallback SSH 22)
   --poll--> Proxmox: fan_controller.py
                |-- computes target PWM (cool/warm/hot/critical + hysteresis)
                |-- STEPS toward target (fast up, slow down + cooldown)
                |-- manual override from admin UI (auto-expiring, critical wins)
                |-- writes /sys/class/hwmon/.../pwm2
                |-- heartbeat -> watchdog.py (forces 40% if stale)
                |-- MQTT -> Home Assistant (live + history graphs)
                |-- ntfy  -> phone/push on failures / step-up / critical / recovery
                |-- /var/log/nastemp.log + .jsonl + nastemp.db (SQLite history)
                                     |
                              admin UI :6767 (FastAPI, on Proxmox or any VM via SSH)
```

### Control law (all in `config.yaml`, no code edits)

| Max HDD temp | Target | Zone |
|---|---|---|
| ≤ 36 °C (`cool`) | 102 (40%) | cool |
| 36–45 °C | linear 102 → 184 | ramp |
| ≥ 45 °C (`hot`) | 184 (72%) | hot |
| ≥ 52 °C (`critical`) | 255 (100%) + urgent ntfy | critical |

Stepping per 30 s cycle: **+12 up / −6 down**. Step-down additionally requires
**5 min cooldown** and **1.5 °C hysteresis** (prevents fan hunting / noise pumping).

### Safety layers (why it won't burn or stay loud)

1. **Max-boost guard (your 20-min rule):** elevated above 40% for longer than
   `max_boost_sec: 1200` without critical temp → forced step-down.
2. **TrueNAS lost:** after `fail_threshold: 3` dead polls → step down to 40%,
   MQTT `status` + ntfy. Never stuck high.
3. **MQTT / fan-hardware failures:** own `LinkWatch` each — throttled ntfy
   (DOWN-since stamp) + recovery notes. Control continues locally if MQTT dies.
4. **Watchdog (separate process/container):** heartbeat stale > 5 min →
   forces 40% directly to hardware (+ ntfy if `NTFY_URL` set).
5. **Manual override can't stick:** slider input clamps to floor..255,
   auto-expires (`manual.max_sec`, default 30 min), ignored while blind,
   critical temps always win.
6. **systemd `Restart=always`** (native) or **`restart: unless-stopped`**
   (Docker) + boot defaults to 40%.

---

## 2. Files

| File | Where | Purpose |
|---|---|---|
| `config.yaml` | Proxmox / container `/config` | Tuning (placeholders `${VAR}` — secrets come from env, see `.env.example`) |
| `.env.example` | repo root (copy to `.env`, gitignored) | All secrets for Docker |
| `fan_controller.py` | Proxmox or `controller` container | main loop |
| `watchdog.py` | Proxmox or `watchdog` container | layer-2 failsafe |
| `admin/` (`app.py`, `index.html`, `Dockerfile`, `README.md`) | Proxmox or any VM (`:6767`) | neon control center, local + remote (SSH) mode |
| `Dockerfile` / `docker-compose.yml` | Docker host | container install (alternative to systemd) |
| `setup.sh` | Proxmox | native systemd install (venv + 3 units, idempotent) |
| `test_fan.sh` | Proxmox | safe manual HW test |
| `ha_sensors.yaml` | Home Assistant | MQTT sensors |
| `ha_dashboard.yaml` | Home Assistant | Lovelace cards |
| `requirements.txt` | build | controller deps (`paho-mqtt`, `paramiko`, `pyyaml`, `requests`) |
| `ARCHITECTURE.md` / `INSTALL.md` / `VERSION` | repo | deep dive / setup guide / release |

MQTT topics (`base: nastemp`): `hdd/<disk>/temp`, `hdd/max_temp`,
`hdd/avg_temp`, `hdd/count`, `hdd/source` (api/ssh), `fan/pwm`, `fan/pct`,
`fan/rpm`, `fan/target`, `fan/mode` (auto/manual), `status`,
`event` (JSON, for Logbook), `online` (LWT).

---

## 3. Prerequisites

1. **Proxmox:** `it87` visible: `grep -H . /sys/class/hwmon/hwmon*/name`.
   If missing: `modprobe it87` (may need `acpi_enforce_resources=lax` on some boards).
2. **TrueNAS:** API key (Credentials → API Keys, needs `REPORTING_READ`) **and/or**
   SSH with a `smartctl`-capable user + key auth:
   ```bash
   curl -k -H "Authorization: Bearer <API-KEY>" https://TRUENAS/api/v2.0/system/info
   ssh-keygen -t ed25519 -N ""
   ssh-copy-id admin@TRUENAS_IP
   ```
3. **HA:** Mosquitto broker running. Note broker IP / user / pass.
4. **ntfy:** your server URL + topic, e.g. `http://192.168.1.5:8080/nastemp`.
5. **Secrets first:** `cp .env.example .env && nano .env`
   (`ADMIN_PASS` required, `TRUENAS_API_KEY`, `MQTT_*`, `NTFY_URL`).
   Native: `setup.sh` writes them to `/opt/nastemp/nastemp.env` (0600, generates
   `ADMIN_PASS` if you don't provide one).

Hardware sanity (Proxmox, as root):
```bash
sudo bash test_fan.sh 140   # your example value (~55%)
# back to quiet:
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')
```

---

## 4. Install — Option A: native systemd (recommended on Proxmox)

Direct hardware access, no container overhead. Survives updates (see `INSTALL.md` §3b).

```bash
cd nastemp-2
TRUENAS_API_KEY="..." NTFY_URL="..." ADMIN_PASS="..." sudo -E bash setup.sh
nano /opt/nastemp/config.yaml   # installed copy — non-secret tuning
sudo systemctl start nastemp-controller nastemp-watchdog nastemp-admin
systemctl status nastemp-controller nastemp-watchdog nastemp-admin
journalctl -u nastemp-controller -f
tail -f /var/log/nastemp.log
# UI: http://<proxmox-ip>:6767
```

---

## 5. Install — Option B: Docker

Yes, Docker is supported. The catch: fan control **writes to host `/sys`**,
so the controller/watchdog containers must run **privileged with
`/sys/class/hwmon` bind-mounted**. This works on any Docker host with that
sysfs visible (Proxmox host with `docker.io` installed, Debian VM/LXC with
hwmon passthrough, plain Linux box).

> Proxmox VE does not ship Docker. If you install Docker on the Proxmox host
> itself, Option B works. Otherwise run the containers on a host that has the
> `it87` hwmon device. If `/sys` writes are blocked on your platform, use
> Option A instead. (The **admin** container needs no privileged and can live
> on any VM — see `admin/README.md` remote mode.)

```bash
cd nastemp-2
mkdir -p logs
cp .env.example .env && nano .env   # secrets FIRST (compose fails fast without ADMIN_PASS)
nano config.yaml   # non-secret tuning only
# key_path inside container is /root/.ssh/... ; compose mounts ~/.ssh -> /root/.ssh

docker compose up -d --build
docker compose logs -f controller
docker compose logs -f watchdog

# live MQTT check (from any host with mosquitto-clients):
mosquitto_sub -h MQTT_BROKER -t "nastemp/#" -v
```

Useful overrides (no compose edit needed):

```bash
SAFE_PWM=102 STALE_SEC=300 PWM_CHANNEL=pwm2 TZ=Europe/Berlin docker compose up -d
```

What compose does:

- Builds the controller image (`Dockerfile`), runs it twice: `controller`
  (`fan_controller.py`) + `watchdog` (`watchdog.py`), plus `admin`
  (`admin/Dockerfile` → `:6767`).
- Shares heartbeat via named volume `heartbeat` (`/run/nastemp`).
- Persists logs to `./logs/` (`/var/log/nastemp.log`, `.jsonl`, `.db`).
- Mounts `./config.yaml` at `/config/config.yaml` (ro for controller, rw for admin editor).
- Mounts `~/.ssh` read-only for TrueNAS key auth.
- Secrets come from `.env` (interpolated into containers, expanded in `config.yaml`).

Stop / update:

```bash
docker compose down              # stops, fans STAY at last PWM!
# Always park fans quiet before stopping:
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')
docker compose up -d --build     # after config change
```

---

## 6. Home Assistant — visualization + history

You asked: *exactly when it kicked in, why, what temp, what fan speed, how
long until it stepped back down — visual + text + historic DB.*

### 6.1 Sensors

Paste `ha_sensors.yaml` content into HA `configuration.yaml` (or as an
included package), then reload MQTT / restart. This creates:

- `sensor.nas_hdd_max_temp`, `sensor.nas_hdd_avg_temp`, `sensor.nas_hdd_count`
- `sensor.nas_temp_source` (api/ssh — which path fed the loop)
- `sensor.nas_fan_speed` (%), `sensor.nas_fan_pwm`, `sensor.nas_fan_rpm`,
  `sensor.nas_fan_target`, `sensor.nas_fan_mode` (auto/manual)
- `sensor.nas_fan_status` (human line incl. reason), `sensor.nas_fan_event`
  (JSON per change), `sensor.nas_controller_online`

Per-drive: after first run, check MQTT (`nastemp/hdd/+/temp`), add one sensor
block per disk (template in `ha_sensors.yaml` comments).

### 6.2 Dashboard

Merge `ha_dashboard.yaml` cards (Dashboard → Edit → Raw config):

- **Entities “Now”** — current temp / fan / status.
- **History-graph “Temp vs Fan”** — overlay max_temp + fan % + PWM. This is
  where you *see* kick-in, plateau, and the step-down delay.
- **Logbook “Why?”** — `fan_event` + `status` shows e.g.
  `step_up max=43.2C → PWM 150 (58.8%) zone=ramp` then later
  `step_down max=37.1C → PWM 102`.
- **Markdown** — on-card legend for the household.

### 6.3 History database

HA `recorder` already stores every sensor above. For 30-day retention:

```yaml
recorder:
  purge_keep_days: 30
  include:
    entities:
      - sensor.nas_hdd_max_temp
      - sensor.nas_fan_speed
      - sensor.nas_fan_pwm
      - sensor.nas_fan_event
```

Second copy (independent of HA): Proxmox/container local files —
`/var/log/nastemp.log` (or `./logs/` in Docker):

```bash
grep EVENT /var/log/nastemp.log
jq '{ts, max_temp, pwm, action}' /var/log/nastemp.jsonl
sqlite3 /var/log/nastemp.db "SELECT ts,max_temp,pwm,action FROM readings ORDER BY ts DESC LIMIT 20;"
```

“How long after the temp dropped did fans step down?” = gap between the
temp fall and the first `EVENT step_down` on the graph; exact seconds are in
the log (`cooling, cooldown 210s/300s` → `EVENT step_down …`).

### 6.4 Optional critical automation (backup if ntfy missed)

```yaml
automation:
  - alias: "NAS HDD critical"
    trigger:
      - platform: numeric_state
        entity_id: sensor.nas_hdd_max_temp
        above: 52
    action:
      - service: notify.persistent_notification
        data:
          message: "HDD {{ states('sensor.nas_hdd_max_temp') }}C, fan {{ states('sensor.nas_fan_speed') }}%"
```

---

## 7. Tuning for noise (Arctic P12 Pro)

`pct = pwm / 2.55`: 102 → 40%, 140 → 55% (your example), 184 → 72%, 255 → 100%.
P12 Pro stalls below ~25–30%, so keep floor at 102.

| Goal | Change in `config.yaml` |
|---|---|
| Quieter | `ceiling_pwm: 160` (~63%), `step_up_pwm: 8`, `cooldown_down_sec: 420` |
| More aggressive | `warm: 38`, `hot: 43`, `step_up_pwm: 15` |
| Shorter loud periods | `max_boost_sec: 900` (15 min) |
| Calmer (fewer toggles) | `hysteresis: 2.0`, `cooldown_down_sec: 420` |

Restart controller after edits (systemd: `systemctl restart nastemp-controller`;
Docker: `docker compose up -d`). Or edit from the admin UI (config editor),
then restart the controller when it tells you to.

---

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `pwm_path not found` | `modprobe it87`; `ls /sys/class/hwmon/hwmon*/name` |
| API 401 / `api_key missing` | key valid? https only? `.env: TRUENAS_API_KEY` set? |
| `SSH FAIL` | key auth, TrueNAS SSH on, user can run `smartctl` |
| `no temp parsed` | on TrueNAS: `smartctl -A -j /dev/sda` (needs current smartmontools) |
| Fans stuck high | watchdog forces 40% ≤5 min; `systemctl status nastemp-controller` / `docker compose logs -f controller`; `cat /run/nastemp/heartbeat` |
| Docker `permission denied` on `pwm2` | must be `privileged: true` + `/sys/class/hwmon:rw`; else use native install |
| No HA data | `mosquitto_sub -h BROKER -t "nastemp/#" -v`; check `.env: MQTT_*` |
| No push | `curl -d test $NTFY_URL`; `ntfy.enabled` + `on_*` flags |
| Admin login fails | `ADMIN_PASS` set? compose fails fast without it; tokens expire after 12h — just log in again |
| Still confused | enable debug (`debug: true` / `ADMIN_DEBUG=1` / `NASTEMP_DEBUG=1`), `grep DEBUG` the logs — every URL + decision is traced, secrets masked |

Emergency park (always safe):

```bash
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')
```
