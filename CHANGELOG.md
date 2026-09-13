# Changelog — smart-nas-fan

All notable changes, newest first. Format follows [Keep a Changelog](https://keepachangelog.com/).
The `VERSION` file + admin UI header badge always show the current release.
Releases go straight to `main`.

## [Unreleased]
### Added
- Poll cadence knobs: `truenas.poll_temps_sec` (default 15, clamped 5..300)
  and `truenas.poll_metrics_sec` (default 30, clamped 10..600), both in the
  GUI form. Controller loop rate stays `timing.interval_sec`.
### Changed
- GUI↔backend is one websocket now: `GET /ws?token=` pushes status (2s),
  manual (10s), host/plug/logs/netlog (12–15s), chart (30s), weather (60s)
  with an instant burst on connect. The old per-endpoint HTTP timers and SSE
  remain purely as fallback (they self-skip while the socket is live), so the
  dashboard costs ~1 connection instead of ~10 requests/15s.
- Needs the `websockets` package (admin Dockerfile, `setup.sh`, remote-mode
  pip line); without it the socket fails and HTTP fallback keeps working.
- NAS temps + metrics report which transport served them (`api(ws)` /
  `api(rest)` pill); temps failures chain the WS cause into the error.
- Weather geocoding moved to Nominatim/OSM (proper postcode support, e.g.
  `85276` Germany) with free-text retry; forecast stays Open-Meteo.
  `weather.latitude`/`longitude` still skips lookup entirely.
- Room/plug MQTT errors now carry `broker:port` plus the `nc -zv` check to run.
- Shared MQTT subscriber covers the room topic and the plug topic.
### Added
- 🔌 plug tile: live watts, meter total, today + yesterday kWh
  (`sensors.plug_topic/power_key/energy_key`), day baselines persisted to
  `<rundir>/z2m_plug.json`, `GET /api/plug`.
- Always-on connection log: admin prints `net[...] websocket/MQTT` lines on
  every transport transition (no `ADMIN_DEBUG` needed), kept in a ring buffer
  at `GET /api/netlog` and shown under 📜 logs; controller logs WS→REST
  fallback/recovery to console the same way (transition-only, no spam).
  GUI pill shows `api(ws)` / `api(rest)` so REST usage can't hide.

## [1.13] – 2026-09-13
### Changed
- TrueNAS access is WebSocket-first: controller uses JSON-RPC 2.0 on
  `/api/current` (`auth.login_with_api_key`, one session for
  `disk.query` + `disk.temperatures`); new `truenas.api_transport`
  (`auto`/`ws`/`rest`) with legacy REST as last resort. REST is deprecated
  since TrueNAS 25.04 (alerts since 25.10.1).
- Admin metrics go live: persistent WS hub subscribes to `reporting.realtime`
  (CPU/RAM/disk-IO ~2s frames, zero per-call handshake); WS `get_data` and
  legacy REST are failsafes and the card note shows `nas via <source>` or the
  chained `realtime | ws | rest` error (no more silent empty gauges).
- MQTT auth explicitly optional (empty user/pass = anonymous, e.g. Mosquitto
  `allow_anonymous`); controller publisher self-heals (throttled background
  reconnect, dead-client teardown, reconnect log line) instead of staying dead
  after a boot-time broker outage.
- `setup.sh` takes an install dir (default: current directory) with `--from`,
  `--no-apt`, `--no-systemd`, `--skip-deps`, `--dry-run`; every step narrates
  what it does + the exact command, secrets stay redacted, live config is never
  overwritten, units are backed up to `.bak`.
### Added
- Admin config editor renders env-expanded values (passwords masked), asks for
  confirmation before save, and keeps easy/raw panes in sync via dry-run
  `POST /api/config/preview`; both panes refresh from disk after save.
- Room sensor tile: background Zigbee2MQTT subscriber (`sensors.topic/keys`,
  broker creds reused) feeds `indoor` temp/humidity into `/api/weather`.
- Per-drive 60s trend arrows (⬆️/⬇️/➡️ + delta tooltip) on the temp chips;
  distinct golden-angle chart colors per drive.
- Weather `latitude`/`longitude` override (skips geocoding) + tolerant country
  match (two-way substring, ISO code, single-hit fallback).
- Manual slider latency: controller picks overrides up in ≤2s, GUI updates the
  pill optimistically and re-polls after each change; countdown labels render
  whole seconds in fixed-width digits (no flicker).
- Deps: `websocket-client` in `requirements.txt`; admin image + compose carry
  `paho-mqtt`, `websocket-client` and the MQTT/TRUENAS/NTFY env.

## [1.12] – 2026-09-10
### Changed
- Renamed the whole project `nastemp-2` → `smart-nas-fan`: paths
  (`/opt/smart-nas-fan`, `/var/log/smart-nas-fan.*`, `/run/smart-nas-fan`),
  systemd units, containers/images, MQTT base topic (`smart-nas-fan/#`),
  HA `unique_id`s (`smart_nas_fan_*`), ntfy titles, CSV names, all docs.
- `NASTEMP_*` env names and browser storage intentionally unchanged (compat).
- `INSTALL.md`: migration snippet for pre-1.12 installs.

## [1.11] – 2026-09-10
### Fixed
- Docs audit: `README.md` rewritten (was describing the SSH-only prototype);
  `ARCHITECTURE.md` diagram/services/ports/config/topics corrected;
  `INSTALL.md` + `admin/README.md` stale corners fixed.
- Real bug found by audit: watchdog container never received `NTFY_URL` in
  Docker — now passed through from `.env`.

## [1.10] – 2026-09-10
### Security
- Full history secret scan (clean); doc-example topic neutralized.
- Admin bearer tokens expire after 12h (pruned, `compare_digest` auth).
- XSS escaping on all dynamic UI strings.

## [1.9] – 2026-09-10
### Added
- Debug tracing everywhere: `debug: true` (controller), `NASTEMP_DEBUG=1`
  (watchdog), `ADMIN_DEBUG=1` (admin). Startup config dumps, every TrueNAS
  POST + latency, SSH commands, MQTT topics, ntfy status, login attempts.
  Secrets print masked (`***lenN`); URLs visible; logs stay local.

## [1.8] – 2026-09-10
### Security
- Secrets moved out of files into `.env` (Docker) / `nastemp.env` (native, 0600):
  `config.yaml` uses `${VAR}` / `${VAR:-default}` placeholders expanded at load.
- No default passwords: compose fails fast without `ADMIN_PASS`; `setup.sh`
  generates one and prints it once. Admin login returns 503 if unset.
- Watchdog also honors `NTFY_URL`. `.env.example` template added.

## [1.7] – 2026-09-10
### Added
- `admin/README.md`: setup, components, config reference, API table, remote vs local.

## [1.6] – 2026-09-10
### Added
- `PROXMOX_PORT` option for custom SSH ports.
- Remote mode proven end-to-end over a real SSH loopback (fake Proxmox + `sshd`).

## [1.5] – 2026-09-10
### Changed
- Update-proof native install: isolated venv (`/opt/smart-nas-fan-venv`),
  idempotent `setup.sh` (safe to re-run after Proxmox updates, never overwrites
  live config), recovery docs (`INSTALL.md` §3b).

## [1.4] – 2026-09-10
### Added
- Realtime SSH: pooled persistent connection (auto-reconnect), `/api/fast`
  1-second fan+heartbeat lane, 30s-cached DB pulls, cached remote CPU sampling.

## [1.3] – 2026-09-10
### Added
- Admin remote mode: GUI runs on any VM/host, reaches Proxmox files over SSH
  key auth (`PROXMOX_HOST/USER/KEY/CONFIG`); `📍 local/remote` header badge.

## [1.2] – 2026-09-10
### Added
- Native systemd unit for the admin UI (`setup.sh` installs all three services).

## [1.1] – 2026-09-09
### Added
- Host CPU/RAM gauges (Proxmox + TrueNAS via `reporting.get_data`), array
  read/write MB/s card, ntfy "Send Test Alert" button, `VERSION` file + UI badge.

## [1.0] – 2026-09-09 (initial commit)
### Added
- Core fan control on Proxmox: TrueNAS API-first HDD-only temps (`disk.query` +
  `disk.temperatures`), SSH/`smartctl` fallback, control law with hysteresis +
  cooldown + max-boost guard, failsafe step-down, independent watchdog,
  MQTT/HA sensors + dashboard, ntfy alerts + recovery notes, SQLite history DB,
  neon admin UI (flow map, RGB fan, graphs, config editor), Docker + native
  install, `ARCHITECTURE.md` / `INSTALL.md`.
