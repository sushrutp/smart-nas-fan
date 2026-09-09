# nastemp-2 — Smart HDD-Temperature Fan Control

Control Proxmox chassis fans from TrueNAS HDD temperatures. Quiet by default,
louder only when disks are hot, with hard failsafes so fans can never get
stuck at high speed.

- **Host:** Proxmox (Arctic P12 Pro daisy-chain on `it87` / `pwm2`)
  - Your known-good manual command is the hardware primitive:
    `echo 140 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')`
- **Temps:** TrueNAS VM (all HDDs, via SSH + `smartctl`)
- **Visibility:** Home Assistant via MQTT (Mosquitto) + ntfy push alerts
- **Policy:** default **40% (PWM 102)**, normal ceiling **~72% (PWM 184)** —
  never 100% in normal regulation. **255 only on critical (≥52 °C).**

---

## 1. How it works

```text
TrueNAS (smartctl per-disk °C)
   --SSH--> Proxmox: fan_controller.py
                |-- computes target PWM (cool/warm/hot/critical + hysteresis)
                |-- STEPS toward target (fast up, slow down + cooldown)
                |-- writes /sys/class/hwmon/.../pwm2
                |-- heartbeat -> watchdog.py (forces 40% if stale)
                |-- MQTT -> Home Assistant (live + history graphs)
                |-- ntfy  -> phone/push on step-up / failsafe / critical
                |-- /var/log/nastemp.log + .jsonl (text + history DB)
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
2. **TrueNAS lost:** after `fail_threshold: 3` SSH failures → step down to 40%,
   MQTT `status` + ntfy. Never stuck high.
3. **Watchdog (separate process/container):** heartbeat stale > 5 min →
   forces 40% directly to hardware.
4. **systemd `Restart=always`** (native) or **`restart: unless-stopped`**
   (Docker) + boot defaults to 40%.

---

## 2. Files

| File | Where | Purpose |
|---|---|---|
| `config.yaml` | Proxmox / container `/config` | ALL tuning: TrueNAS, PWM, temps, MQTT, ntfy |
| `fan_controller.py` | Proxmox or `controller` container | main loop |
| `watchdog.py` | Proxmox or `watchdog` container | layer-2 failsafe |
| `Dockerfile` / `docker-compose.yml` | Docker host | container install (alternative to systemd) |
| `setup.sh` | Proxmox | native systemd install |
| `test_fan.sh` | Proxmox | safe manual HW test |
| `ha_sensors.yaml` | Home Assistant | MQTT sensors |
| `ha_dashboard.yaml` | Home Assistant | Lovelace cards |
| `requirements.txt` | build | Python deps |

MQTT topics (`base: nastemp`): `hdd/<disk>/temp`, `hdd/max_temp`,
`hdd/avg_temp`, `hdd/count`, `fan/pwm`, `fan/pct`, `fan/rpm`,
`fan/target`, `status`, `event` (JSON, for Logbook), `online` (LWT).

---

## 3. Prerequisites

1. **Proxmox:** `it87` visible: `grep -H . /sys/class/hwmon/hwmon*/name`.
   If missing: `modprobe it87` (may need `acpi_enforce_resources=lax` on some boards).
2. **TrueNAS:** SSH enabled, a user with `smartctl` rights, key auth working:
   ```bash
   ssh-keygen -t ed25519 -N ""
   ssh-copy-id admin@TRUENAS_IP
   ssh admin@TRUENAS_IP "smartctl -A /dev/sda | head -20"
   ```
3. **HA:** Mosquitto broker running. Note broker IP / user / pass.
4. **ntfy:** your server URL + topic, e.g. `http://192.168.1.5:8080/nastemp`.
5. Edit `config.yaml`: `truenas.host/user`, `mqtt.broker`, `ntfy.url`.

Hardware sanity (Proxmox, as root):
```bash
sudo bash test_fan.sh 140   # your example value (~55%)
# back to quiet:
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')
```

---

## 4. Install — Option A: native systemd (recommended on Proxmox)

Direct hardware access, no container overhead.

```bash
cd nastemp-2
nano config.yaml
sudo bash setup.sh
nano /opt/nastemp/config.yaml   # installed copy — repeat your edits
sudo systemctl start nastemp-controller nastemp-watchdog
systemctl status nastemp-controller nastemp-watchdog
journalctl -u nastemp-controller -f
tail -f /var/log/nastemp.log
```

---

## 5. Install — Option B: Docker

Yes, Docker is supported. The catch: fan control **writes to host `/sys`**,
so the containers must run **privileged with `/sys/class/hwmon` bind-mounted**.
This works on any Docker host with that sysfs visible (Proxmox host with
`docker.io` installed, Debian VM/LXC with hwmon passthrough, plain Linux box).

> Proxmox VE does not ship Docker. If you install Docker on the Proxmox host
> itself, Option B works. Otherwise run the containers on a host that has the
> `it87` hwmon device. If `/sys` writes are blocked on your platform, use
> Option A instead.

```bash
cd nastemp-2
mkdir -p logs
nano config.yaml   # truenas.host, mqtt.broker, ntfy.url
# key_path inside container is /root/.ssh/... ; compose mounts ~/.ssh -> /root/.ssh
# Either set key_path: "/root/.ssh/id_ed25519" or leave default key discovery.

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

- Builds one image (`Dockerfile`), runs it twice: `controller`
  (`fan_controller.py`) + `watchdog` (`watchdog.py`).
- Shares heartbeat via named volume `heartbeat` (`/run/nastemp`).
- Persists logs to `./logs/` (`/var/log/nastemp.log`, `.jsonl`).
- Mounts `./config.yaml` read-only at `/config/config.yaml`.
- Mounts `~/.ssh` read-only for TrueNAS key auth.

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

- `sensor.nas_hdd_max_temp`, `sensor.nas_hdd_avg_temp`
- `sensor.nas_fan_speed` (%), `sensor.nas_fan_pwm`, `sensor.nas_fan_rpm`,
  `sensor.nas_fan_target`
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
Docker: `docker compose up -d`).

---

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `pwm_path not found` | `modprobe it87`; `ls /sys/class/hwmon/hwmon*/name` |
| `SSH FAIL` | key auth, TrueNAS SSH on, user can run `smartctl` |
| `no temp parsed` | on TrueNAS: `smartctl -A -j /dev/sda` (needs current smartmontools) |
| Fans stuck high | watchdog forces 40% ≤5 min; `systemctl status nastemp-controller` / `docker compose logs -f controller`; `cat /run/nastemp/heartbeat` |
| Docker `permission denied` on `pwm2` | must be `privileged: true` + `/sys/class/hwmon:rw`; else use native install |
| No HA data | `mosquitto_sub -h BROKER -t "nastemp/#" -v`; check `mqtt.broker/user/pass` |

Emergency park (always safe):

```bash
echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')
```
