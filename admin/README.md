# 🌀 nastemp-2 admin — control center (`admin/`)

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
| `Dockerfile` | `python:3.12-slim` + `fastapi uvicorn pyyaml requests paramiko`, serves on `6767` |
| `README.md` | This file |

## How to run

### A. Docker on the Proxmox host (local mode)

```bash
cd nastemp-2
ADMIN_USER=me ADMIN_PASS='s3cret!' docker compose up -d --build admin
# → http://<proxmox-ip>:6767
```

### B. Native on Proxmox, no Docker (local mode)

```bash
sudo bash setup.sh   # installs controller + watchdog + admin (venv + systemd unit)
# setup.sh writes secrets to /opt/nastemp/nastemp.env (0600), generates ADMIN_PASS if unset
sudo systemctl start nastemp-admin
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
PROXMOX_HOST=192.168.1.2 PROXMOX_USER=root PROXMOX_CONFIG=/opt/nastemp/config.yaml \
  python3 -m uvicorn app:app --host 0.0.0.0 --port 6767 --app-dir ./admin
```

## Configuration (all via environment — nothing hardcoded)

Secrets live in **`.env`** (root dir, gitignored — copy `.env.example`), native systemd
reads **`/opt/nastemp/nastemp.env`** (0600, written by `setup.sh`). `config.yaml` only
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
| `PROXMOX_CONFIG` | `/opt/nastemp/config.yaml` | remote mode | Controller config **on Proxmox** (what the editor edits) |
| `TZ` | `UTC` | cosmetic | Timestamps in logs/UI |

Controller-side knobs the UI respects (in `config.yaml`, editable in the UI form):
`truenas.*` (API/SSH, HDD-only), `fan.*`, `temps_c.*`, `timing.*`, `mqtt.*`,
`ntfy.*` (+`on_api_fail`, `on_recovery`), `weather.*` (postcode/country),
`manual.*` (`enabled`, `max_sec` auto-expire, `min_pwm` stall floor).

## API reference (all need `Authorization: Bearer <token>` except `/` + login)

| Endpoint | What |
|---|---|
| `POST /api/login` `{user, pass}` | → `{token}` |
| `GET /api/status` | Everything: temps, fan, heartbeat, MQTT, ntfy, DB, boost timer, `where` |
| `GET /stream?token=` | SSE push of status every 2s (UI uses this; polling fallback built in) |
| `GET /api/fast` | Sub-second lane: fan + heartbeat in one roundtrip (UI polls 1s in remote mode) |
| `GET /api/history?limit=` / `GET /api/drives?limit=` | Chart data: aggregate + per-drive series |
| `GET /api/config` / `POST /api/config` | Read / save config (`{content}` raw or `{values}` dotted-keys form) |
| `GET /api/manual` / `POST /api/manual` | Slider state / set `{pwm, seconds?, by?}` / release `{auto:true}` |
| `GET /api/hostmetrics` | Proxmox CPU/RAM + TrueNAS CPU/RAM + array MB/s |
| `POST /api/ntfy-test` | Send Test Alert push |
| `GET /api/export?kind=readings\|events` | CSV download (opens in Excel) |
| `GET /api/weather` | Outside temp + condition emoji (Open-Meteo, cached 10 min) |
| `GET /api/logs?lines=` | Log tail |

## Data freshness (remote mode, honest numbers)

Persistent SSH (one handshake, auto-reconnect) · fan fast lane ~1 RTT polled 1s ·
full status ~2s over SSE · history DB re-pulled ≤ every 30s · TrueNAS temps ~5 min
(by TrueNAS design) · ms clock / boosted-since / down-since timers tick client-side
at 97ms on any transport.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `📍 remote` but fan shows `proxmox ssh: …` | Key not trusted: `ssh-copy-id`, check `PROXMOX_USER/PORT/KEY`, port 22 reachable |
| `No module named 'paramiko'` | Native remote needs it: `pip install paramiko` (Docker image already has it) |
| Login loops / 401s | `ADMIN_USER/PASS` differ between UI and server; token is per server start |
| Edits don't affect fans | Editor saves the file — restart the **controller** (`docker compose restart controller`) |
| Port in use | Another admin running: `ps aux \| grep uvicorn` / `docker ps` |

Security: keep `:6767` LAN-only behind your firewall, set a strong `ADMIN_PASS`,
prefer `TRUENAS_API_KEY` env over writing the key into the config file.
