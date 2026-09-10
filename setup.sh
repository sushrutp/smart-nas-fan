#!/bin/bash
# nastemp-2 / setup.sh  (run on PROXMOX host as root)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "== nastemp-2 setup =="
echo "(safe to re-run after Proxmox updates — it rebuilds everything without touching your config)"
apt update && apt install -y python3-pip python3-venv smartmontools
# Isolated venv: immune to system-python changes, rebuilt from scratch every run.
VENV=/opt/nastemp-venv
python3 -m venv "$VENV" 2>/dev/null || true
if [ -x "$VENV/bin/pip" ]; then
  PY="$VENV/bin/python3"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r "$DIR/requirements.txt"
  "$VENV/bin/pip" install -q "fastapi>=0.115" "uvicorn>=0.30" paramiko
else
  echo "WARN: venv pip missing, falling back to system pip"
  PY="/usr/bin/python3"
  pip3 install --break-system-packages -r "$DIR/requirements.txt" 2>/dev/null || pip3 install -r "$DIR/requirements.txt"
  pip3 install --break-system-packages "fastapi>=0.115" "uvicorn>=0.30" paramiko 2>/dev/null || pip3 install "fastapi>=0.115" "uvicorn>=0.30" paramiko
fi
mkdir -p /run/nastemp /opt/nastemp /opt/nastemp-admin
cp "$DIR/fan_controller.py" /opt/nastemp/
cp "$DIR/watchdog.py" /opt/nastemp/
cp "$DIR/admin/app.py" "$DIR/admin/index.html" /opt/nastemp-admin/
cp "$DIR/VERSION" /opt/nastemp-admin/ 2>/dev/null || echo "1.0" > /opt/nastemp-admin/VERSION
cp "$DIR/config.yaml" /opt/nastemp/config.yaml.example
[ -f /opt/nastemp/config.yaml ] || cp "$DIR/config.yaml" /opt/nastemp/config.yaml
chmod +x /opt/nastemp/fan_controller.py /opt/nastemp/watchdog.py
touch /var/log/nastemp.log /var/log/nastemp.jsonl
# SQLite history DB (created automatically on first run if missing):
touch /var/log/nastemp.db 2>/dev/null || true

echo "1) Secrets: put them in the environment before running, or edit /opt/nastemp/nastemp.env after:"
echo "   TRUENAS_API_KEY, MQTT_USER, MQTT_PASSWORD, NTFY_URL, ADMIN_USER, ADMIN_PASS"
echo "   (ADMIN_PASS is auto-generated if you don't provide one — shown at the end)"
echo "2) SSH key: ssh-keygen -t ed25519; ssh-copy-id admin@TRUENAS_IP"
echo "3) HW test: bash $DIR/test_fan.sh 140"

# --- secrets: env in, single 0600 file out. Nothing hardcoded, nothing printed except once. ---
ENVF=/opt/nastemp/nastemp.env
touch "$ENVF"; chmod 600 "$ENVF"
set_kv() { # set_kv KEY value : write only if value non-empty and key absent (never clobber yours)
  if [ -n "${2:-}" ] && ! grep -q "^${1}=" "$ENVF" 2>/dev/null; then echo "${1}=${2}" >> "$ENVF"; fi
}
if [ -z "${ADMIN_PASS:-}" ] && ! grep -q "^ADMIN_PASS=" "$ENVF" 2>/dev/null; then
  ADMIN_PASS="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64)"
  echo "ADMIN_PASS=$ADMIN_PASS" >> "$ENVF"
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
chmod 600 "$ENVF"

cat > /etc/systemd/system/nastemp-controller.service <<EOF
[Unit]
Description=nastemp-2 smart fan controller (proxmox it87 pwm2)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
Environment=NASTEMP_CONFIG=/opt/nastemp/config.yaml
EnvironmentFile=-/opt/nastemp/nastemp.env
ExecStart=$PY /opt/nastemp/fan_controller.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/nastemp-watchdog.service <<EOF
[Unit]
Description=nastemp-2 watchdog (forces 40% if controller stale)
After=nastemp-controller.service
[Service]
Type=simple
User=root
Environment=NASTEMP_HEARTBEAT=/run/nastemp/heartbeat
Environment=NASTEMP_SAFE_PWM=102
Environment=NASTEMP_STALE_SEC=300
EnvironmentFile=-/opt/nastemp/nastemp.env
# Watchdog ntfy push (needs NTFY_URL in nastemp.env) when it force-resets fans.
ExecStart=$PY /opt/nastemp/watchdog.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/nastemp-admin.service <<EOF
[Unit]
Description=nastemp-2 admin control center (:6767, native, no docker)
After=network-online.target nastemp-controller.service
Wants=network-online.target
[Service]
Type=simple
User=root
Environment=NASTEMP_CONFIG=/opt/nastemp/config.yaml
EnvironmentFile=-/opt/nastemp/nastemp.env
ExecStart=$PY -m uvicorn app:app --host 0.0.0.0 --port 6767 --app-dir /opt/nastemp-admin
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable nastemp-controller nastemp-watchdog nastemp-admin
echo ""
echo "Done. Next:"
echo "  nano /opt/nastemp/nastemp.env   # secrets (0600) - NTFY_URL etc."
echo "  nano /opt/nastemp/config.yaml   # tuning (no secrets needed there)"
if [ "${GENERATED_PASS:-}" = "1" ]; then
  echo ""
  echo "  *** GENERATED ADMIN LOGIN (shown once, stored in /opt/nastemp/nastemp.env):"
  echo "      user: ${ADMIN_USER:-admin}"
  echo "      pass: $ADMIN_PASS"
  echo "  *** Change it any time: nano /opt/nastemp/nastemp.env + systemctl restart nastemp-admin"
fi
echo ""
echo "  systemctl start nastemp-controller nastemp-watchdog nastemp-admin"
echo "  journalctl -u nastemp-controller -f"
echo "  tail -f /var/log/nastemp.log"
echo "  UI: http://<proxmox-ip>:6767"
