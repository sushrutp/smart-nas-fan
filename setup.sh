#!/bin/bash
# smart-nas-fan / setup.sh — native systemd install.
#
# Usage:
#   sudo bash setup.sh [INSTALL_DIR] [options]
#   sudo -E bash setup.sh [INSTALL_DIR]   # -E passes TRUENAS_*/MQTT_*/NTFY_* env vars through
#
#   INSTALL_DIR   Folder to install the project into.
#                 Default: the current directory (i.e. where you run the script from).
#                 Example: sudo bash setup.sh /opt/smart-nas-fan
#
#   Options:
#     --from SRC_DIR   Project source folder (must contain fan_controller.py,
#                      watchdog.py, requirements.txt, config.yaml, admin/app.py).
#                      Default: the script's own folder, or the current directory
#                      if the script folder doesn't hold the sources.
#     --no-apt         Skip apt-get install (use when packages are already present).
#     --no-systemd     Skip writing/enabling systemd units (no daemon-reload, no enable).
#     --skip-deps      Skip venv creation + pip installs (use SYSTEM python as-is).
#     --dry-run        Print every step + command, change nothing.
#     -h, --help       Show this help and exit.
#
# Safety properties (safe to re-run any time, e.g. after Proxmox updates):
#   * `set -euo pipefail` — fail fast on any error, unset variable, or pipe failure.
#   * Every action is announced AND its exact command echoed before it runs (see run()).
#   * Secret VALUES are never echoed (only redacted placeholders like <hidden>).
#   * Your live config.yaml is NEVER overwritten (only created on first run).
#   * Existing systemd units are backed up to *.bak before being rewritten.
#   * Secrets are only appended when non-empty and never clobber existing keys.
#   * Copying a file onto itself is detected and skipped (needed when
#     INSTALL_DIR equals the source folder).
#   * `--dry-run` previews the whole run without touching anything.
set -euo pipefail

# ---------------------------------------------------------------- helpers ---

say() {
    # say "message" — section header: what we are about to do.
    printf '\n== %s ==\n' "$*"
}

run() {
    # run "description" cmd [args...] — announce WHAT, echo the exact COMMAND, then run it.
    local desc="$1"
    shift
    printf -- '-> %s\n' "$desc"
    printf -- '+ %s\n' "${*@Q}"
    if [ "${DRY_RUN:-0}" = "1" ]; then
        printf '   [dry-run: skipped]\n'
        return 0
    fi
    "$@"
}

run_secret() {
    # run_secret "description" "display-command-with-secrets-redacted" cmd [args...]
    # Same as run(), but the echoed command is the redacted DISPLAY string,
    # so secret values can never leak into logs/terminal scrollback.
    local desc="$1" display="$2"
    shift 2
    printf -- '-> %s\n' "$desc"
    printf -- '+ %s\n' "$display"
    if [ "${DRY_RUN:-0}" = "1" ]; then
        printf '   [dry-run: skipped]\n'
        return 0
    fi
    "$@"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

same_file() {
    # same_file A B — true when both paths exist and are the same inode.
    [ -n "${1:-}" ] && [ -n "${2:-}" ] && [ -e "$1" ] && [ -e "$2" ] && [ "$1" -ef "$2" ]
}

copy_file() {
    # copy_file SRC DST — cp with announcement; skipped when SRC and DST are identical.
    local src="$1" dst="$2"
    if same_file "$src" "$dst"; then
        printf -- '-> Copy %s -> %s\n' "$src" "$dst"
        printf '   [skipped: source and destination are the same file]\n'
        return 0
    fi
    run "Copy $src -> $dst" cp -f "$src" "$dst"
}

is_systemd_running() {
    [ -d /run/systemd/system ]
}

# ---------------------------------------------------------------- args ------

INSTALL_DIR=""
SRC_DIR=""
NO_APT=0
NO_SYSTEMD=0
SKIP_DEPS=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "${1:-}" in
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        --from)
            SRC_DIR="${2:-}"
            [ -n "$SRC_DIR" ] || die "--from needs a directory argument"
            shift 2
            ;;
        --from=*)
            SRC_DIR="${1#--from=}"
            shift
            ;;
        --no-apt)      NO_APT=1; shift ;;
        --no-systemd)  NO_SYSTEMD=1; shift ;;
        --skip-deps)   SKIP_DEPS=1; shift ;;
        --dry-run)     DRY_RUN=1; shift ;;
        --)            shift; break ;;
        -*)
            die "Unknown option: $1 (see --help)"
            ;;
        *)
            if [ -z "$INSTALL_DIR" ]; then
                INSTALL_DIR="$1"
            else
                die "Unexpected extra argument: $1 (only one INSTALL_DIR allowed, see --help)"
            fi
            shift
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CALLER_DIR="$(pwd)"

# Install folder: explicit arg, else the current directory.
if [ -z "$INSTALL_DIR" ]; then
    INSTALL_DIR="$CALLER_DIR"
    printf 'No INSTALL_DIR given, using current directory: %s\n' "$INSTALL_DIR"
else
    printf 'Installing into: %s\n' "$INSTALL_DIR"
fi

# Source folder: explicit --from, else script dir if it holds the project, else cwd.
if [ -z "$SRC_DIR" ]; then
    if [ -f "$SCRIPT_DIR/fan_controller.py" ]; then
        SRC_DIR="$SCRIPT_DIR"
    else
        SRC_DIR="$CALLER_DIR"
    fi
fi
printf 'Project sources from: %s\n' "$SRC_DIR"
if [ "$DRY_RUN" = "1" ]; then
    printf 'Mode: DRY-RUN (nothing will be changed)\n'
fi

# ---------------------------------------------------------- preconditions ---

say "Checking preconditions (root, tools, source files)"

if [ "$(id -u)" -ne 0 ]; then
    if [ "$NO_APT" = "1" ] && [ "$NO_SYSTEMD" = "1" ]; then
        printf -- '-> Not running as root, but --no-apt and --no-systemd are set.\n'
        printf '   System-wide steps (/var/log, /run, systemd) will be skipped with a warning.\n'
    else
        die "Not running as root. Re-run with sudo, or pass --no-apt --no-systemd to do a user-local install."
    fi
else
    printf -- '-> Running as root (uid 0): OK\n'
fi

run "Checking for python3 interpreter" command -v python3

for f in fan_controller.py watchdog.py requirements.txt config.yaml VERSION admin/app.py admin/index.html; do
    if [ -f "$SRC_DIR/$f" ]; then
        printf -- '-> Source file present: %s/%s: OK\n' "$SRC_DIR" "$f"
    else
        die "Source file missing: $SRC_DIR/$f — pass the project folder via --from DIR"
    fi
done

# ------------------------------------------------- resolve install paths ---

say "Resolving install paths"

run "Creating install directory $INSTALL_DIR" mkdir -p "$INSTALL_DIR"
if [ "$DRY_RUN" = "1" ]; then
    # In dry-run the directory may not exist; resolve the path lexically.
    case "$INSTALL_DIR" in
        /*) ABS_INSTALL="$INSTALL_DIR" ;;
        *)  ABS_INSTALL="$CALLER_DIR/$INSTALL_DIR" ;;
    esac
else
    run "Resolving install directory to an absolute path" cd "$INSTALL_DIR"
    ABS_INSTALL="$(cd "$INSTALL_DIR" && pwd)"
fi
ADMIN_DIR="$ABS_INSTALL/admin"
VENV="${ABS_INSTALL}-venv"
ENVF="$ABS_INSTALL/smart-nas-fan.env"
CFGF="$ABS_INSTALL/config.yaml"
printf -- '-> Layout:\n'
printf '   app files : %s\n' "$ABS_INSTALL"
printf '   admin app : %s\n' "$ADMIN_DIR"
printf '   venv      : %s\n' "$VENV"
printf '   live conf : %s (never overwritten once created)\n' "$CFGF"
printf '   secrets   : %s (mode 0600)\n' "$ENVF"

# ------------------------------------------------------------ packages ------

if [ "$NO_APT" = "0" ]; then
    say "Installing OS packages (apt)"
    run "Refreshing apt package lists" apt-get update
    run "Installing python3-pip, python3-venv and smartmontools" \
        apt-get install -y python3-pip python3-venv smartmontools
else
    say "Skipping OS packages (--no-apt was given)"
fi

# ------------------------------------------------------- python venv/deps ---

PY="/usr/bin/python3"
if [ "$SKIP_DEPS" = "0" ]; then
    say "Setting up isolated Python venv at $VENV"
    if [ -x "$VENV/bin/pip" ]; then
        printf -- '-> Venv already exists with pip, reusing it (idempotent, nothing rebuilt).\n'
    else
        run "Creating venv (kept if it already exists)" python3 -m venv "$VENV"
    fi
    if [ "$DRY_RUN" = "1" ] || [ -x "$VENV/bin/pip" ]; then
        PY="$VENV/bin/python3"
        run "Upgrading pip inside the venv" "$VENV/bin/pip" install -q --upgrade pip
        run "Installing controller deps from requirements.txt" \
            "$VENV/bin/pip" install -q -r "$SRC_DIR/requirements.txt"
        run "Installing admin UI deps (fastapi, uvicorn, websockets) + paramiko" \
            "$VENV/bin/pip" install -q "fastapi>=0.115" "uvicorn>=0.30" "websockets>=13" paramiko
    else
        printf 'WARN: venv pip missing, falling back to system pip\n'
        PY="/usr/bin/python3"
        run "Installing controller deps with system pip" \
            pip3 install --break-system-packages -r "$SRC_DIR/requirements.txt"
        run "Installing admin UI deps with system pip" \
            pip3 install --break-system-packages "fastapi>=0.115" "uvicorn>=0.30" "websockets>=13" paramiko
    fi
else
    say "Skipping venv + pip installs (--skip-deps was given)"
    printf -- '-> Using system python: %s\n' "$PY"
fi
printf -- '-> Python for services: %s\n' "$PY"

# ---------------------------------------------------------- install files ---

say "Installing project files into $ABS_INSTALL"
run "Creating runtime directories" mkdir -p "$ABS_INSTALL" "$ADMIN_DIR" /run/smart-nas-fan

copy_file "$SRC_DIR/fan_controller.py" "$ABS_INSTALL/fan_controller.py"
copy_file "$SRC_DIR/watchdog.py" "$ABS_INSTALL/watchdog.py"
copy_file "$SRC_DIR/admin/app.py" "$ADMIN_DIR/app.py"
copy_file "$SRC_DIR/admin/index.html" "$ADMIN_DIR/index.html"
if [ -f "$SRC_DIR/VERSION" ]; then
    copy_file "$SRC_DIR/VERSION" "$ADMIN_DIR/VERSION"
elif [ ! -f "$ADMIN_DIR/VERSION" ]; then
    run "VERSION source missing, writing fallback 1.0" sh -c "echo 1.0 > '$ADMIN_DIR/VERSION'"
fi
copy_file "$SRC_DIR/config.yaml" "$ABS_INSTALL/config.yaml.example"
if [ -f "$CFGF" ]; then
    printf -- '-> Live config %s already exists: keeping yours (updating .example only).\n' "$CFGF"
else
    run "Creating live config from shipped default (first install only)" cp -f "$SRC_DIR/config.yaml" "$CFGF"
fi
run "Marking controller scripts executable" chmod +x "$ABS_INSTALL/fan_controller.py" "$ABS_INSTALL/watchdog.py"

if [ "$(id -u)" -eq 0 ]; then
    run "Creating log files" touch /var/log/smart-nas-fan.log /var/log/smart-nas-fan.jsonl
    run "Creating SQLite history DB placeholder (app creates schema on first run)" \
        touch /var/log/smart-nas-fan.db
else
    printf -- '-> Not root: skipping /var/log file creation (controller will create what it can).\n'
fi

say "Sanity-checking installed Python files compile"
run "Compiling fan_controller.py" "$PY" -m py_compile "$ABS_INSTALL/fan_controller.py"
run "Compiling watchdog.py" "$PY" -m py_compile "$ABS_INSTALL/watchdog.py"
run "Compiling admin app.py" "$PY" -m py_compile "$ADMIN_DIR/app.py"

say "Reminder: manual steps this script cannot do for you"
printf '1) Secrets: export them before running (sudo -E preserves env), or edit %s afterwards:\n' "$ENVF"
printf '   TRUENAS_API_KEY, MQTT_USER, MQTT_PASSWORD, NTFY_URL, ADMIN_USER, ADMIN_PASS\n'
printf '   (ADMIN_PASS is auto-generated below if you provide none — shown once at the end)\n'
printf '2) SSH key for fallback temps: ssh-keygen -t ed25519; ssh-copy-id admin@TRUENAS_IP\n'
printf '3) HW test (needs it87 pwm2): bash %s/test_fan.sh 140\n' "$SRC_DIR"

# ---------------------------------------------------------- secrets file ----

say "Writing secrets file $ENVF (mode 0600, values never echoed)"

run "Creating secrets file if missing" touch "$ENVF"
run "Locking secrets file down to owner-only" chmod 600 "$ENVF"

set_kv() {
    # set_kv KEY value — append KEY=value only when value is non-empty
    # and the key is not already present. Never clobbers, never prints values.
    local key="$1" val="${2:-}"
    printf -- '+ set_kv %s <hidden>\n' "$key"
    if [ "$DRY_RUN" = "1" ]; then
        printf '   [dry-run: skipped]\n'
        return 0
    fi
    if [ -n "$val" ] && ! grep -q "^${key}=" "$ENVF" 2>/dev/null; then
        printf '%s=%s\n' "$key" "$val" >> "$ENVF"
        printf '   [stored]\n'
    else
        printf '   [skipped: empty value or key already present]\n'
    fi
}

GENERATED_PASS=0
if [ -z "${ADMIN_PASS:-}" ] && ! grep -q "^ADMIN_PASS=" "$ENVF" 2>/dev/null; then
    printf -- '-> No ADMIN_PASS provided and none stored: generating a random one.\n'
    printf -- '+ generate ADMIN_PASS <hidden> (openssl rand, printed once at the end)\n'
    if [ "$DRY_RUN" = "0" ]; then
        ADMIN_PASS="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64)"
        printf '%s=%s\n' "ADMIN_PASS" "$ADMIN_PASS" >> "$ENVF"
        printf '   [stored]\n'
    else
        printf '   [dry-run: skipped]\n'
    fi
    GENERATED_PASS=1
fi
set_kv ADMIN_PASS "${ADMIN_PASS:-}"
set_kv ADMIN_USER "${ADMIN_USER:-admin}"
set_kv TRUENAS_HOST "${TRUENAS_HOST:-}"
set_kv TRUENAS_API_URL "${TRUENAS_API_URL:-}"
set_kv TRUENAS_API_KEY "${TRUENAS_API_KEY:-}"
set_kv TRUENAS_USER "${TRUENAS_USER:-}"
set_kv MQTT_BROKER "${MQTT_BROKER:-}"
set_kv MQTT_USER "${MQTT_USER:-}"
set_kv MQTT_PASSWORD "${MQTT_PASSWORD:-}"
set_kv NTFY_URL "${NTFY_URL:-}"
run "Re-asserting 0600 permissions on secrets file" chmod 600 "$ENVF"

# --------------------------------------------------------------- systemd ----

write_unit() {
    # write_unit PATH — backs up an existing unit to PATH.bak, then writes stdin to PATH.
    # Call as: write_unit PATH <<EOF ... EOF
    local unit="$1"
    if [ -f "$unit" ]; then
        run "Backing up existing unit $unit" cp -f "$unit" "$unit.bak"
    else
        printf -- '-> No existing unit at %s, creating fresh.\n' "$unit"
    fi
    printf -- '-> Writing systemd unit %s\n' "$unit"
    printf -- '+ cat > %s <<EOF (service definition)\n' "$unit"
    if [ "$DRY_RUN" = "1" ]; then
        printf '   [dry-run: skipped]\n'
        cat > /dev/null
        return 0
    fi
    cat > "$unit"
}

if [ "$NO_SYSTEMD" = "0" ] && [ "$(id -u)" -eq 0 ]; then
    say "Installing systemd services (old units backed up to *.bak)"

    write_unit /etc/systemd/system/smart-nas-fan-controller.service <<EOF
[Unit]
Description=smart-nas-fan smart fan controller (proxmox it87 pwm2)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
Environment=NASTEMP_CONFIG=$CFGF
EnvironmentFile=-$ENVF
ExecStart=$PY $ABS_INSTALL/fan_controller.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

    write_unit /etc/systemd/system/smart-nas-fan-watchdog.service <<EOF
[Unit]
Description=smart-nas-fan watchdog (forces 40% if controller stale)
After=smart-nas-fan-controller.service
[Service]
Type=simple
User=root
Environment=NASTEMP_HEARTBEAT=/run/smart-nas-fan/heartbeat
Environment=NASTEMP_SAFE_PWM=102
Environment=NASTEMP_STALE_SEC=300
EnvironmentFile=-$ENVF
# Watchdog ntfy push (needs NTFY_URL in smart-nas-fan.env) when it force-resets fans.
ExecStart=$PY $ABS_INSTALL/watchdog.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

    write_unit /etc/systemd/system/smart-nas-fan-admin.service <<EOF
[Unit]
Description=smart-nas-fan admin control center (:6767, native, no docker)
After=network-online.target smart-nas-fan-controller.service
Wants=network-online.target
[Service]
Type=simple
User=root
Environment=NASTEMP_CONFIG=$CFGF
EnvironmentFile=-$ENVF
ExecStart=$PY -m uvicorn app:app --host 0.0.0.0 --port 6767 --app-dir $ADMIN_DIR
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

    if is_systemd_running; then
        run "Reloading systemd manager configuration" systemctl daemon-reload
        run "Enabling services for boot (controller, watchdog, admin)" \
            systemctl enable smart-nas-fan-controller smart-nas-fan-watchdog smart-nas-fan-admin
    else
        printf 'WARN: systemd is not running here (no /run/systemd/system) — units written but not enabled.\n'
        printf '      On the Proxmox host, run: systemctl daemon-reload && systemctl enable --now smart-nas-fan-controller smart-nas-fan-watchdog smart-nas-fan-admin\n'
    fi
else
    say "Skipping systemd units ($([ "$NO_SYSTEMD" = "1" ] && echo "--no-systemd was given" || echo "not running as root"))"
fi

# ----------------------------------------------------------------- done -----

say "Setup complete"
printf '\nDone. Next:\n'
printf '  nano %s   # secrets (0600) - NTFY_URL etc.\n' "$ENVF"
printf '  nano %s   # tuning (no secrets needed there)\n' "$CFGF"
if [ "${GENERATED_PASS:-0}" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    printf '\n  *** GENERATED ADMIN LOGIN (shown once, stored in %s):\n' "$ENVF"
    printf '      user: %s\n' "${ADMIN_USER:-admin}"
    printf '      pass: %s\n' "${ADMIN_PASS:-<see $ENVF>}"
    printf '  *** Change it any time: nano %s + systemctl restart smart-nas-fan-admin\n' "$ENVF"
fi
printf '\n  systemctl start smart-nas-fan-controller smart-nas-fan-watchdog smart-nas-fan-admin\n'
printf '  journalctl -u smart-nas-fan-controller -f\n'
printf '  tail -f /var/log/smart-nas-fan.log\n'
printf '  UI: http://<proxmox-ip>:6767\n'
printf '\nInstalled from %s into %s (re-run any time; your config + secrets are preserved).\n' "$SRC_DIR" "$ABS_INSTALL"
