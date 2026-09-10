# nastemp-2 — Architecture

Smart HDD-temperature fan control: **Proxmox** drives chassis fans from **TrueNAS** HDD temperatures, with visibility in **Home Assistant (via MQTT)** and push alerts via **ntfy**, plus hard failsafes so fans never get stuck loud.

## 1. Big picture

```text
                    +------------------+
                    | TrueNAS (VM/host)|
                    | smartctl per-disk|
                    | °C  192.168.1.10 |
                    +--------+---------+
                             | SSH port 22 (paramiko, key auth)
                             | commands: ls /dev/sd? ; smartctl -A -j /dev/sdX
                             v
+-------------+    +----------------------+    +-------------------+
| /sys/class/ |<-->| PROXMOX HOST         |--->| Home Assistant    |
| hwmon it87  |    | fan_controller.py    |--->| Mosquitto broker  |
| pwm2 +      |    |  - control law       | MQTT 1883 | HA sensors  |
| pwm2_enable |    |  - stepping          |    +-------------------+
| fan2_input  |    |  - heartbeat         |
+-------------+    +----------+-----------+    +-------------------+
     ^                        | ntfy HTTP POST    | Phone / browser |
     |              +---------v-----------+    | ntfy server+topic |
     |              | watchdog.py         |--->| e.g. http://       |
     +--------------| forces PWM 102 if   |    | 192.168.1.5:8080/ |
                    | heartbeat stale     |    | nastemp           |
                    +---------------------+    +-------------------+
```

Two runtimes, same code:

| Mode | Controller | Watchdog | Hardware access |
|---|---|---|---|
| **A: native systemd (recommended on Proxmox)** | `nastemp-controller.service` → `/opt/nastemp/fan_controller.py` | `nastemp-watchdog.service` → `/opt/nastemp/watchdog.py` | Direct `/sys` writes, root |
| **B: Docker** | `nastemp-controller` container → `python fan_controller.py` | `nastemp-watchdog` container → `python watchdog.py` | `privileged:true` + `/sys/class/hwmon:rw` bind mount |

Both modes do the same logic. Only packaging differs (see §7).

## 2. Systems and responsibilities

| System | Role | Runs what | Needs |
|---|---|---|---|
| **Proxmox host** (`it87` / `pwm2`) | Actuator + compute. Owns fans. | `fan_controller.py` + `watchdog.py` (native or Docker). Arctic P12 Pro daisy-chain. | `it87` kernel module, Python deps, `config.yaml`, SSH key to TrueNAS |
| **TrueNAS** | Sensor. Owns HDD temps. No agent installed. | Stock `smartctl` only, queried remotely over SSH | SSH enabled, user with `smartctl` rights, key auth |
| **MQTT broker (Mosquitto, usually on/with HA)** | Telemetry bus. Live values + history source. | Mosquitto broker | IP/port/user/pass, `nastemp/#` topics |
| **Home Assistant** | Visualization + history DB. Read-only subscriber. | `ha_sensors.yaml` sensors + `ha_dashboard.yaml` cards + `recorder` | MQTT integration pointed at broker |
| **ntfy server** | Push alerts. Fire-and-forget HTTP. | Any ntfy server (self-hosted or ntfy.sh) + topic | URL + topic name |
| **Logs on Proxmox/container** | Independent text + JSON history DB (works even if HA/MQTT down) | `/var/log/nastemp.log` + `/var/log/nastemp.jsonl` (Docker: `./logs/`) | Disk space only |

No cloud, no extra API server, no database container.

## 3. Communication between systems (how + protocol + direction)

### 3.1 Proxmox → TrueNAS: HTTPS API first (HDD-only), SSH fallback

* **Who initiates:** `fan_controller.py:get_all_temps()` on Proxmox. `method:auto` (default) tries API, falls back to SSH; `api`/`ssh` force one path.
* **API path** (`get_temps_via_api`, `requests`, Bearer key): `POST {api_url}/api/v2.0/disk.query` → keep `type==HDD` / `rotationrate != null`, drop SSD/NVMe (extra `nvme*` skip); then `POST {api_url}/api/v2.0/disk.temperatures` with `[names, false]` → `{sda: 38}` or `{sda: {temperature: 38}}`. Temperatures are TrueNAS-cached (≤5 min) by design. First failure → ntfy (`on_api_fail`); `fail_threshold` consecutive total failures → failsafe + ntfy.
* **SSH path** (fallback): same as before (`ls` + `smartctl -A -j`, text fallback), but HDD-filtered via `lsblk -d -o NAME,ROTA` (`ROTA=1` kept) + `nvme` skip.

### 3.2 Proxmox → TrueNAS (SSH detail, fallback path)

* **Who initiates:** `fan_controller.py:get_all_temps()` on Proxmox.
* **Library:** `paramiko` SSH client, key auth (`AutoAddPolicy` for host key).
* **Sequence per cycle:**
  1. `discover_drives()` → `ls -1 /dev/sd? /dev/hd? /dev/nvme?n1 ...; echo ---; ls -1 /dev/da?` (covers Linux `sd`/`nvme` + BSD `da`). Skipped if `truenas.drives:` is set explicitly.
  2. Per drive: `smartctl -A -j /dev/sdX` → parse JSON `temperature.current` (or `nvme_smart_health_information_log.temperature`). Fallback: `smartctl -A /dev/sdX | grep -iE 'Temperature|Airflow'` → last numeric token (handles `194 Temperature_Celsius ... 38`).
* **Failure handling:** any exception → `temps[dev]=None`. After `fail_threshold: 3` consecutive all-invalid cycles → failsafe step-down to `safe_pwm: 102` (never stuck high). Publishes `status` + `event:failsafe_step_down` + ntfy if enabled.
* **Ports:** TCP 22 Proxmox → TrueNAS. No inbound port on Proxmox needed for this path.

### 3.3 Proxmox → fans: sysfs PWM (local hardware write)

* **Who:** `PwmHw` class in `fan_controller.py`, and `find_pwm()/force_safe()` in `watchdog.py`. Same primitive as `test_fan.sh`.
* **Discovery:** glob `/sys/class/hwmon/hwmon*/name`, match `driver_match: "it87"`, use `pwm_channel: "pwm2"`. Fallback: first `hwmon*/pwm2`. RPM from `fan2_input` (number derived from channel).
* **Write:** `echo 1 > pwm2_enable` (manual mode, `pwm_enable_mode: 1`), then `echo <0-255> > pwm2`. Values: `102=40% floor`, `184=72% ceiling`, `255=100% emergency-only`.
* **Control law** (`target_for_temp()` + stepping in `main()`):
  * `≤cool (36°C)` → `floor_pwm (102)`; `≥hot (45°C)` → `ceiling_pwm (184)`; in-between linear ramp; `≥critical (52°C)` → `255` + urgent ntfy, ignores cooldown.
  * Stepping per `interval_sec: 30`: `+12 up` (fast, immediate on heat), `-6 down` (slow) **only after** `cooldown_down_sec: 300` **and** `hysteresis: 1.5°C` (at ceiling: temp must fall below `hot - hysteresis`). Prevents hunting/noise pumping.
  * `max_boost_sec: 1200` guard: elevated above floor >20 min without critical temp → forced double step-down + ntfy, timer re-armed.
* **Boot:** writes `safe_pwm: 102` immediately, logs path + result.

### 3.4 Controller → Watchdog: heartbeat file (local IPC)

* **Who:** `fan_controller.py:heartbeat()` writes `time.time()` to `timing.heartbeat_file` (`/run/nastemp/heartbeat`, Docker: shared `heartbeat` volume at same path) every loop start + end.
* **Who reads:** `watchdog.py` loop every `CHECK_SEC=15s`. If file missing (reboot/`/run` wiped) → force safe once per 10 min. If `age > STALE_SEC=300s` → `force_safe()` (write `SAFE_PWM=102` + `pwm_enable=1`) max once per 5 min + optional ntfy via `NASTEMP_NTFY`.
* **Why separate process/container:** Layer-2 failsafe if controller hangs/dies/SSH-blocks. Stdlib-only, no MQTT/config dependency.
* **Docker note:** both containers share named volume `heartbeat:/run/nastemp`. Native: tmpfs `/run/nastemp` created by `setup.sh`.

### 3.5 Controller → HA: MQTT publish (telemetry)

* **Who:** `Pub` class (`paho-mqtt`), `client_id=nastemp-controller`, LWT `nastemp/online=offline` (retained), `online=online` on connect.
* **Broker:** `mqtt.broker: 192.168.1.5`, `port: 1883`, optional `username/password`, `base: nastemp`, `retain: true` (aggregates) / `retain=false` (high-rate + events).
* **Topics published:**

| Topic (`nastemp/...`) | Payload | Retain | HA sensor |
|---|---|---|---|
| `hdd/<disk>/temp` e.g. `hdd/sda/temp` | `38.0` or `unknown` | false | per-drive (add manually, see `ha_sensors.yaml` comments) |
| `hdd/max_temp` | max valid °C | false | `sensor.nas_hdd_max_temp` |
| `hdd/avg_temp` | avg °C | false | `sensor.nas_hdd_avg_temp` |
| `hdd/count` | N valid HDDs | false | `sensor.nas_hdd_count` |
| `hdd/source` | `api`/`ssh`/`failed` | false | `sensor.nas_temp_source` |
| `fan/pwm` | 0–255 actual readback | false | `sensor.nas_fan_pwm` |
| `fan/pct` | `pwm/2.55` % | false | `sensor.nas_fan_speed` |
| `fan/rpm` | `fan2_input` or omitted | false | `sensor.nas_fan_rpm` |
| `fan/target` | computed target PWM | false | `sensor.nas_fan_target` |
| `status` | human line: `step_up max=43.2C avg=... pwm=150(58.8%) rpm=... why:...` | false | `sensor.nas_fan_status` |
| `event` | JSON `{ts,event:step_up\|step_down\|emergency\|failsafe_step_down\|maxboost_stepdown, max_temp, pwm, why}` | false | `sensor.nas_fan_event` (Logbook) |
| `online` | `online`/`offline` (LWT) | true | `sensor.nas_controller_online` |

* **HA side:** `ha_sensors.yaml` defines MQTT sensors; `ha_dashboard.yaml` gives Entities (Now) + history-graph (Temp vs Fan) + Logbook (Why?) + Markdown legend. Long-term history = HA `recorder` (`purge_keep_days: 30` suggested). Verify with `mosquitto_sub -h BROKER -t "nastemp/#" -v`.
* **Ports:** TCP 1883 (or 8883 for TLS, not default) Proxmox → broker. No inbound port on Proxmox.

### 3.6 Controller/Watchdog → ntfy: HTTP POST (alerts only, not telemetry)

* **Who:** `fan_controller.py:ntfy()` via `requests.post(url, data=msg, headers={Title,Priority,Tags})`; `watchdog.py` via `urllib` if `NASTEMP_NTFY` set.
* **When:** `started` (always), 1st TrueNAS failure (if `on_api_fail:true`, throttled once per outage, message carries **DOWN-since timestamp**), `step_up` (if `on_step_up:true`), `step_down` (if `on_step_down:false` by default — noisy), `failsafe` + `maxboost_stepdown` (if `on_failsafe:true`), `critical→255` (if `on_critical:true`, `priority=urgent`), **MQTT broker unreachable** (throttled, `priority=high` — control continues locally), **fan PWM readback failure** (`priority=urgent` — hardware control broken), **any recovery** (if `on_recovery:true`, includes downtime seconds). Watchdog stale event (only if `NASTEMP_NTFY` set). `LinkWatch` class tracks per-link down-since; admin UI shows live per-link down timers with ms precision.
* **Ports:** TCP 80/443 or custom (e.g. `8080`) Proxmox → ntfy server. One-way outbound.

### 3.7 Local logs + SQLite DB (no network)

* `timing.log_file: /var/log/nastemp.log` — human lines: `boot`, every-cycle `HOLD/...`, `EVENT step_up|step_down|emergency|failsafe...`.
* `timing.jsonl_file: /var/log/nastemp.jsonl` — machine history per cycle: `{ts,source,max_temp,avg,temps,target,pwm,pct,rpm,action,why}`. Query: `grep EVENT /var/log/nastemp.log`, `jq '{ts,max_temp,pwm,action}' /var/log/nastemp.jsonl`.
* `timing.db_file: /var/log/nastemp.db` — SQLite history DB (same data, queryable): `readings/drive_temps/events` tables. Fan control stays on Proxmox; DB is just local history.
* Docker maps all three to `./logs/` on the Docker host.

## 4. Tokens / credentials / APIs needed

You need 3 credentials + 1 optional topic secret:

| # | What | Where to create/get it | Where to put it | Code that uses it |
|---|---|---|---|---|
| 1 | **TrueNAS API key (HDD-only temps, preferred)** — Bearer token | TrueNAS UI: Credentials → API Keys → Add (needs `REPORTING_READ` or admin). Name e.g. `nastemp-monitor`. Copy once → password manager. Must use `https://` (TrueNAS **revokes** keys sent over plain HTTP). Test: `curl -k -H "Authorization: Bearer <KEY>" https://TRUENAS/api/v2.0/system/info`. Uses `disk.query` (type/rotationrate filter → HDD only) + `disk.temperatures` (cached ≤5 min by TrueNAS). | `config.yaml: truenas.api_url` (e.g. `https://192.168.1.10`), `truenas.api_key`, `truenas.verify_ssl:false` (self-signed) or `TRUENAS_API_KEY` env (Docker-safe, preferred). `truenas.method: auto/api/ssh`, `hdd_only:true`. | `fan_controller.py:api_post()/api_hdd_names()/get_temps_via_api()` |
| 1b | **TrueNAS SSH keypair (fallback)** — used when `method:auto/ssh` or API down | On Proxmox: `ssh-keygen -t ed25519 -N ""`, then `ssh-copy-id admin@TRUENAS_IP`. Verify: `ssh admin@TRUENAS_IP "smartctl -A /dev/sda \| head -20"`. SSDs excluded via `lsblk ROTA` + `nvme` skip. | `config.yaml: truenas.host`, `truenas.user`, `truenas.key_path`. Docker: host `~/.ssh` → `/root/.ssh:ro`. | `fan_controller.py:ssh_exec()/discover_drives()` |
| 2 | **MQTT broker user/pass** (if broker requires auth; else leave empty) | Mosquitto / HA add-on: note broker IP, port, username, password. HA side: Settings → Devices → MQTT → configure same broker. | `config.yaml: mqtt.broker`, `mqtt.port`, `mqtt.username`, `mqtt.password`, `mqtt.base`. | `fan_controller.py:Pub.__init__()` |
| 3 | **ntfy topic URL** (acts as bearer secret — anyone with URL can publish/read) | Self-hosted: `http://192.168.1.5:8080/nastemp`. Public: `https://ntfy.sh/YOUR-UNGUESSABLE-TOPIC-HERE` (pick unguessable suffix). No signup. Test: `curl -d "test" <URL>`. Sent on: 1st API fail (`on_api_fail`), failsafe (`on_failsafe`), step_up, critical. | `config.yaml: ntfy.url`, `ntfy.enabled`, `on_step_up/on_step_down/on_failsafe/on_critical/on_api_fail`. Watchdog (Docker): optional `NASTEMP_NTFY` env. | `fan_controller.py:ntfy()`, `watchdog.py` urllib block |
| — | HA `recorder` retention (optional, no token) | `configuration.yaml`: `recorder: purge_keep_days: 30` + include list from README | HA config only | — |

No Proxmox API token (local sysfs only), no Docker Hub token, no HA long-lived token (MQTT sensors only). Fan control always runs on Proxmox (native or Docker on the Proxmox host with `/sys` passthrough).

## 5. Connections / ports / firewall needed

| From → To | Port/proto | Purpose | Required? |
|---|---|---|---|
| Proxmox → TrueNAS | TCP 443 / HTTPS API | `disk.query` (HDD-only filter) + `disk.temperatures` polling | **yes** when `method:auto/api` |
| Proxmox → TrueNAS | TCP 22 / SSH | `smartctl` fallback polling (auto/ssh) | yes when `method:auto/ssh` |
| Proxmox → MQTT broker | TCP 1883 (default) / 8883 TLS | telemetry publish (`hdd/*`, `fan/*`, `status`, `event`, `hdd/source`) | yes if `mqtt.enabled:true` |
| Proxmox → ntfy server | TCP 80/443/8080 / HTTP POST | push alerts | yes if `ntfy.enabled:true` |
| HA → MQTT broker | TCP 1883 | subscribe `nastemp/#` | yes (usually localhost/same LAN if broker is HA add-on) |
| Admin laptop → Proxmox | SSH / Docker / `mosquitto_sub` | setup + `mosquitto_sub -h BROKER -t "nastemp/#" -v` debug | ops only |
| Proxmox → Internet (PyPI) | HTTPS | `pip install -r requirements.txt` / `docker build` (paho-mqtt, paramiko, pyyaml, requests) | build-time only |

No inbound ports opened on Proxmox by this project. All flows are outbound polls/publishes + local `/sys` writes.

## 6. Config surface (single file + env overrides)

* **`config.yaml`** — all tuning, no code edits. Sections: `truenas:` (method/api_url/api_key/verify_ssl/hdd_only/host/user/key/drives/fail_threshold), `fan:` (channel/driver/floor/ceiling/emergency/safe), `temps_c:` (cool/warm/hot/critical/hysteresis), `timing:` (interval/step_up/step_down/cooldown/max_boost/heartbeat/log/jsonl/db paths), `mqtt:`, `ntfy:` (+`on_api_fail`). Defaults: floor `102 (40%)`, ceiling `184 (72%)`, `cool 36 / hot 45 / critical 52 / hysteresis 1.5`, `interval 30s, +12/-6, cooldown 300s, max_boost 1200s`, `method:auto, hdd_only:true`.
* **Env overrides:** `NASTEMP_CONFIG`, `TRUENAS_API_KEY` (preferred over config file for the TrueNAS key), `NASTEMP_HEARTBEAT`, `NASTEMP_SAFE_PWM` (default 102), `NASTEMP_STALE_SEC` (default 300), `NASTEMP_PWM` (default `pwm2`), `NASTEMP_NTFY` (watchdog only), `TZ`, `SAFE_PWM/STALE_SEC/PWM_CHANNEL` (compose → watchdog env).
* **HA:** paste `ha_sensors.yaml` into `configuration.yaml` (MQTT sensors incl. `hdd/source`, `hdd/count`), merge `ha_dashboard.yaml` via Dashboard Raw config. The key card is **"Temp spike -> Fan rise (SAME axis)"** — max/avg temp + fan % + target on one history-graph so a temp spike and the fan increase share timestamps. Optional `recorder:` + critical-temp automation (README §6.4).
* **SQLite history DB** (`timing.db_file`, default `/var/log/nastemp.db`, Docker `./logs/`): tables `readings(ts,source,max_temp,avg_temp,target,pwm,pct,rpm,action,why)`, `drive_temps(ts,drive,temp)`, `events(ts,event,max_temp,pwm,why)`. Query: `sqlite3 /var/log/nastemp.db "SELECT ts,max_temp,pwm,action FROM readings ORDER BY ts DESC LIMIT 20;"`. Complements `.jsonl` (machine) + `.log` (human) + HA recorder.

## 7. Where to run Docker and what it does

### 7.1 Where

* **Constraint:** the container that writes fans **must see the host's `it87 pwm2` sysfs**. So run Docker **on the Proxmox host itself** (install `docker.io` — Proxmox VE doesn't ship Docker) **or** any Linux host/VM/LXC that has `/sys/class/hwmon` with the `it87` device passed through.
* **If `/sys` writes are blocked** (some LXC/hosts mount `/sys` read-only and ignore `privileged`), Docker will fail with `permission denied` on `pwm2` → use **Option A native systemd** instead (`setup.sh`).
* **SSH/MQTT/ntfy reachability:** that Docker host must reach TrueNAS:443 (API) + :22 (SSH fallback), MQTT:1883, ntfy:80/8080. No special network mode needed (default bridge, outbound only). Put the API key in `TRUENAS_API_KEY` env rather than the mounted config if you prefer.

### 7.2 What `Dockerfile` + `docker-compose.yml` do

* **`Dockerfile`** (`python:3.12-slim`): installs `openssh-client`, `pip install -r requirements.txt` (`paho-mqtt, paramiko, pyyaml, requests`), copies `fan_controller.py` + `watchdog.py`, sets `NASTEMP_CONFIG=/config/config.yaml`, default `CMD=["python","fan_controller.py"]`.
* **`docker-compose.yml`** builds **one image** (`nastemp-2:latest`), runs it **twice**:
  * `controller` (`nastemp-controller`): `command: ["python","fan_controller.py"]`, `privileged:true`, `restart: unless-stopped`, `NASTEMP_CONFIG=/config/config.yaml`. Mounts: `./config.yaml:/config/config.yaml:ro`, `~/.ssh:/root/.ssh:ro` (TrueNAS key), `./logs:/var/log` (persisted logs), `heartbeat:/run/nastemp` (shared), `/sys/class/hwmon:/sys/class/hwmon:rw` (hardware).
  * `watchdog` (`nastemp-watchdog`): `command: ["python","watchdog.py"]`, same `privileged` + sysfs bind + `heartbeat` volume + `./logs`. Env: `NASTEMP_HEARTBEAT/SAFE_PWM/STALE_SEC/PWM` (overridable via `SAFE_PWM/STALE_SEC/PWM_CHANNEL/TZ` without editing compose).
* **Lifecycle:** `docker compose up -d --build` → `logs -f controller|watchdog`. Config change → `docker compose up -d --build`. Stop: park fans first — `echo 102 > $(grep -l it87 ...)` — then `docker compose down` (fans **stay** at last PWM on stop, they don't auto-reset).
* **Native equivalent** (`setup.sh` on Proxmox as root): `apt install python3-pip smartmontools`, pip install reqs, copy `.py` to `/opt/nastemp/`, create `/run/nastemp`, touch log files, write two systemd units (`Restart=always`, `RestartSec=10`), `daemon-reload + enable`. Then edit `/opt/nastemp/config.yaml`, `systemctl start nastemp-controller nastemp-watchdog`.
* **Admin center** (`admin/`, 3rd service): FastAPI on `0.0.0.0:6767` (no privileged, sysfs `:ro`, heartbeat volume `:rw` for the override file). Neon UI: flow map, animated 🌀, shared-axis chart, logs, config editor (saves `config.yaml`, keeps `.bak`). Auth: `ADMIN_USER`/`ADMIN_PASS` env (login form → bearer token). Only inbound port of the whole project — keep LAN-only.
* **Manual override** (`/run/nastemp/override.json`, admin slider): clamped to `manual.min_pwm..255`, auto-expires after `manual.max_sec` (default 30 min), ignored on the no-temps failsafe path, critical temps always win, `EVENT manual_set/manual_expired` in log+DB+ntfy, `fan/mode` auto|manual topic. Chunked sleep re-checks the file every ≤5s so the slider feels live.

## 8. End-to-end example (one heat event)

1. TrueNAS HDD hits `43.2°C` (SSD at 45°C ignored — `disk.query type==HDD` filter) → next poll `disk.temperatures[sda]` returns it; `max=43.2, zone=ramp, target=~167, source=api`.
2. `target(167) > current(102)` → `current=114`, write `pwm2` on Proxmox, `cool_since` re-armed, `EVENT step_up` → MQTT `fan/pwm`, `event` JSON, ntfy `Step UP`, `.log`+`.jsonl`+SQLite `readings+events` append.
3. HA **same-axis** history-graph shows max_temp rising + fan% stepping `40→45→...→59%`; Logbook `step_up max=43.2C → PWM 150 zone=ramp` timestamp matches the spike. This is the exact view you asked for.
4. Temp falls to `37.1°C` → `target≈110 < current`, but `cooldown 210s/300s` → log `cooling...`, hold. After 5 min cool → `step_down -6` per cycle until target. Gap on graph = step-down delay; exact seconds in log/DB.
5. If API dies → immediate ntfy (`on_api_fail`, 1st failure) + SSH fallback in `auto` mode. If all sources die 3× → `failsafe_step_down` to 102 + ntfy. If controller dies → heartbeat age >300s → watchdog forces 102 + optional ntfy. If elevated >20 min without critical → max-boost forced step-down.

## 9. Troubleshooting map

| Symptom | Which link | Check |
|---|---|---|
| `pwm_path not found` | Proxmox→sysfs | `modprobe it87`, `grep -H . /sys/class/hwmon/hwmon*/name`, `bash test_fan.sh 140` |
| `TRUENAS FAIL` / `api failed` | Proxmox→TrueNAS API | API key valid? `api_url` https? `verify_ssl:false` for self-signed? user role? test curl above; `method:auto` falls back to SSH; 1st fail → ntfy (`on_api_fail`) |
| `SSH FAIL` | Proxmox→TrueNAS SSH | key auth, TrueNAS SSH on, `smartctl` rights, `truenas.host/user/key_path`, port 22 (fallback path only) |
| `no HDDs` / empty valid | API filter | `hdd_only:true` + no spinning disks? check `disk.query` types; SSD/nvme correctly excluded; set `drives:` override to force |
| `no temp parsed` | smartctl format | on TrueNAS: `smartctl -A -j /dev/sda` (needs current smartmontools) |
| Fans stuck high | controller/watchdog | `cat /run/nastemp/heartbeat`, `systemctl status` / `docker compose logs -f controller`, watchdog forces 102 ≤5 min |
| Docker `permission denied` on `pwm2` | sysfs bind | must be `privileged:true` + `/sys/class/hwmon:rw`; else native install |
| No HA data | Proxmox→MQTT→HA | `mosquitto_sub -h BROKER -t "nastemp/#" -v`, `mqtt.broker/user/pass`, HA MQTT integration, `ha_sensors.yaml` loaded |
| No push | Proxmox→ntfy | `curl -d test <ntfy.url>`, `ntfy.enabled` + `on_*` flags |

Emergency park (always safe): `echo 102 > $(grep -l "it87" /sys/class/hwmon/hwmon*/name | sed 's/name/pwm2/')`.
