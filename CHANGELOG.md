# Changelog — smart-nas-fan

All notable changes, newest first. Format follows [Keep a Changelog](https://keepachangelog.com/).
The `VERSION` file + admin UI header badge always show the current release.
Releases go straight to `main`.

## [Unreleased]

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
