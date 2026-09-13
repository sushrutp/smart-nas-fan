# 🌀 smart-nas-fan admin — control center (`admin/`)

Neon web UI + API for the whole project on **`0.0.0.0:6767`**.
One screen: live flow map, RGB fan, per-drive temps, temp/fan/ntfy graph,
host gauges, array I/O, logs, CSV export, config editor, manual fan control.

**`admin/app.py` supports BOTH access modes** (same code, switch via env):

| Mode | Where the GUI runs | How it reaches Proxmox files | Env |
|---|---|---|---|
| **📍 local** (default) | On the Proxmox host (Docker service or `setup.sh`) | Direct local reads: `/sys`, `/var/log`, `/run`, config file | `PROXMOX_HOST` empty |
| **📍 remote** | Any VM/host with IP access | SSH key auth to Proxmox (persistent connection, auto-reconnect) | `PROXMOX_HOST` set (see below) |

TrueNAS API, MQTT check, ntfy and weather are network services — identical in both modes.
The header badge always shows which mode you're in. Fan control itself **never** lives
here: the controller script on Proxmox owns `/sys`; the GUI only observes files and
writes the self-expiring `override.json` for manual control.

## Components

| File | What it is |
|---|---|
| `app.py` | FastAPI backend: auth, live status APIs, SSE stream, config/manual/weather/metrics/export endpoints, local↔remote file layer |
| `index.html` | Single-page neon UI (no build step, no CDN required except fonts): flow map, RGB fan, charts, gauges, editors |
| `Dockerfile` | `python:3.12-slim` + `fastapi uvicorn pyyaml requests paramiko paho-mqtt websocket-client`, serves on `6767` |
| `README.md` | This file |

## How to run

### A. Docker on the Proxmox host (local mode)

```bash
cd smart-nas-fan
cp .env.example .env && nano .env   # ADMIN_PASS required — compose fails fast without it
docker compose up -d --build admin
# → http://<proxmox-ip>:6767  (login with ADMIN_USER / ADMIN_PASS from .env)
```

### B. Native on Proxmox, no Docker (local mode)

```bash
sudo bash setup.sh /opt/smart-nas-fan   # installs controller + watchdog + admin (venv + systemd unit)
# setup.sh writes secrets to /opt/smart-nas-fan/smart-nas-fan.env (0600), generates ADMIN_PASS if unset
sudo systemctl start smart-nas-fan-admin
```

### C. Docker on another VM/host (remote mode)

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_proxmox
ssh-copy-id -i ~/.ssh/id_proxmox root@192.168.1.2
# uncomment PROXMOX_* env + ~/.ssh mount in docker-compose.yml, then:
PROXMOX_HOST=192.168.1.2 PROXMOX_USER=root docker compose up -d --build admin
```

> First: `cp .env.example .env && nano .env` — `ADMIN_PASS` is required (compose
> fails fast without it); also fill `TRUENAS_API_KEY`, `MQTT_*`, `NTFY_URL`.

### D. Native on another VM/host, no Docker (remote mode)

```bash
apt install python3-fastapi python3-uvicorn   # + pip install paramiko
PROXMOX_HOST=192.168.1.2 PROXMOX_USER=root PROXMOX_CONFIG=/opt/smart-nas-fan/config.yaml \
  python3 -m uvicorn app:app --host 0.0.0.0 --port 6767 --app-dir ./admin
```

## Configuration (all via environment — nothing hardcoded)

Secrets live in **`.env`** (root dir, gitignored — copy `.env.example`), native systemd
reads **`/opt/smart-nas-fan/smart-nas-fan.env`** (0600, written by `setup.sh`). `config.yaml` only
holds `${VAR}` / `${VAR:-default}` placeholders, expanded at load.

| Var | Default | Needed when | What it does |
|---|---|---|---|
| `ADMIN_USER` | `admin` | login name | Login form user |
| `ADMIN_PASS` | **none — required** | **always** | Login password. Compose fails fast without it; `setup.sh` generates one and prints it once |
| `NASTEMP_CONFIG` | `/config/config.yaml` (docker) / `../config.yaml` | local mode | Controller config file the editor reads/writes |
| `TRUENAS_API_KEY` | — (else `truenas.api_key` from config) | TrueNAS temps/metrics | Bearer token, safer than writing it in the file |
| `PROXMOX_HOST` | empty (= local mode) | remote mode | Proxmox IP — **this one switch** selects remote |
| `PROXMOX_USER` | `root` | remote mode | SSH user on Proxmox |
| `PROXMOX_PORT` | `22` | custom SSH port | SSH port on Proxmox |
| `PROXMOX_KEY` | `/root/.ssh/id_ed25519` | remote mode | Key file (mount `~/.ssh:/root/.ssh:ro` in Docker) |
| `PROXMOX_CONFIG` | `/opt/smart-nas-fan/config.yaml` | remote mode | Controller config **on Proxmox** (what the editor edits) |
| `TZ` | `UTC` | cosmetic | Timestamps in logs/UI |
| `ADMIN_DEBUG` (or `NASTEMP_DEBUG`) | off | debugging | Verbose probe trace, secrets masked (see below) |

Controller-side knobs the UI respects (in `config.yaml`, editable in the UI form):
`truenas.*` (API/SSH, `api_transport: auto/ws/rest`, HDD-only), `fan.*`, `temps_c.*`,
`timing.*`, `mqtt.*` (user/pass optional = anonymous), `ntfy.*` (`+on_api_fail`,
`on_recovery`), `weather.*` (postcode/country or latitude/longitude),
`sensors.*` (Zigbee2MQTT room topic/keys), `manual.*` (`enabled`, `max_sec`
auto-expire, `min_pwm` stall floor).

## API reference (all need `Authorization: Bearer <token>` except `/` + login)

| Endpoint | What |
|---|---|
| `POST /api/login` `{user, pass}` | → `{token}` |
| `GET /api/status` | Everything: temps, fan, heartbeat, MQTT, ntfy, DB, boost timer, `where` |
| `GET /stream?token=` | SSE push of status every 2s (UI uses this; polling fallback built in) |
| `GET /api/fast` | Sub-second lane: fan + heartbeat in one roundtrip (UI polls 1s in remote mode) |
| `GET /api/history?limit=` / `GET /api/drives?limit=` | Chart data: aggregate + per-drive series |
| `GET /api/config` / `POST /api/config` | Read (form renders env-expanded values, passwords masked) / save config (`{content}` raw or `{values}` dotted-keys form, asks confirm, `.bak` kept) |
| `POST /api/config/preview` | Dry-run conversion (writes nothing) — keeps easy/raw panes in sync across tab switches |
| `GET /api/manual` / `POST /api/manual` | Slider state / set `{pwm, seconds?, by?}` / release `{auto:true}` (picked up in ≤2s) |
| `GET /api/hostmetrics` | Proxmox CPU/RAM + TrueNAS CPU/RAM + array MB/s (live `realtime` WS feed; `source` field says `realtime`/`ws`/`rest`) |
| `POST /api/ntfy-test` | Send Test Alert push |
| `GET /api/export?kind=readings\|events` | CSV download (opens in Excel) |
| `GET /api/weather` | Outside temp + condition emoji (Nominatim geocode + Open-Meteo forecast, cached 10 min) + `indoor` room temp/humidity from Zigbee2MQTT |
| `GET /api/plug` | Sonoff plug: live W, meter kWh, today/yesterday kWh |
| `GET /api/logs?lines=` | Log tail |
| `GET /api/netlog?lines=` | Connection-event log (websocket vs REST, MQTT — with reasons, no debug flag needed) |

## Data freshness (remote mode, honest numbers)

Persistent SSH (one handshake, auto-reconnect) · fan fast lane ~1 RTT polled 1s ·
full status ~2s over SSE · history DB re-pulled ≤ every 30s · TrueNAS temps ~5 min
(by TrueNAS design) · NAS CPU/RAM/array-I/O live ~2s via the persistent WS
`reporting.realtime` subscription (WS `get_data` + legacy REST only as failsafes;
the card note shows `nas via <source>` or the chained error) · ms clock /
boosted-since / down-since timers tick client-side at 97ms on any transport
(countdown labels render whole seconds so they don't flicker).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `📍 remote` but fan shows `proxmox ssh: …` | Key not trusted: `ssh-copy-id`, check `PROXMOX_USER/PORT/KEY`, SSH port reachable |
| `No module named 'paramiko'` | Native remote needs it: `pip install paramiko` (Docker image already has it) |
| Login loops / 401s | Wrong password, or token expired (12h TTL — just log in again) |
| Edits don't affect fans | Editor saves the file — restart the **controller** (`docker compose restart controller`) |
| NAS gauges / array I/O empty | Card note shows the chain (`nas via realtime` vs `realtime: … \| ws: … \| rest: …`); API key needs `REPORTING_READ`; restart admin after key/config changes (hub + z2m threads bind at startup) |
| Room sensor shows an error | `sensors.topic` must match the Zigbee2MQTT topic (default `zigbee2mqtt/<friendly-name>`); the error names the `broker:port` it failed on — from the admin host run `nc -zv <broker> 1883` (use the Pi's LAN IP, never `localhost`, unless admin runs on the Pi itself); restart admin after broker/topic changes |
| Port in use | Another admin running: `ps aux \| grep uvicorn` / `docker ps` |

Security: keep `:6767` LAN-only behind your firewall, set a strong `ADMIN_PASS`,
prefer `TRUENAS_API_KEY` env over writing the key into the config file.

Threat model (public repo, homelab use): API keys/passwords live ONLY in `.env` /
`smart-nas-fan.env` (both gitignored — verified, no `.env` or key files are tracked, and the
full git history was scanned). Debug logs mask secrets (`***lenN`). Bearer tokens are
32-hex random, `compare_digest`-checked, and **expire after 12h** (SSE `?token=` URLs
land in access logs — expiry bounds that leak). UI escapes all dynamic strings.
Treat any authenticated UI session as root-equivalent (config write + fan override),
so guard `ADMIN_PASS` like one. SSH to Proxmox uses key auth + `AutoAddPolicy`
(TLS-grade inside your LAN; use `verify_ssl: true` with a real cert if you have one).

## Debug logging (what is it connecting to?)

Off by default. Turn on with `ADMIN_DEBUG=1` (or `NASTEMP_DEBUG=1`) on the admin,
`debug: true` in `config.yaml` for the controller, `NASTEMP_DEBUG=1` for the watchdog:

```bash
ADMIN_DEBUG=1 python3 -m uvicorn app:app --app-dir ./admin   # startup dump + probe trace
```

You get: full startup config dump (method/host/API URL/MQTT broker/ntfy URL/file paths),
every TrueNAS POST + latency, every SSH `user@host: command` + byte count, MQTT
connect/topics, ntfy POST + HTTP status, weather geocode/forecast URLs, login
attempts (user + ok/FAIL, never passwords), config/manual/export actions, watchdog
heartbeat age every 15s. Secrets print masked (`***len17`, never values); URLs do
appear — logs never leave your host.
