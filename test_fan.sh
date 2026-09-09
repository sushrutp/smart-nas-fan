#!/bin/bash
# nastemp-2 / test_fan.sh - safe manual hardware check on PROXMOX host.
# Usage: sudo bash test_fan.sh [pwm_value]
# Default 140 matches your known-good example.
set -u
WANT=${1:-140}
F=$(grep -l "it87" /sys/class/hwmon/hwmon*/name 2>/dev/null | sed 's/name/pwm2/' | head -1)
if [ -z "${F:-}" ]; then
  echo "FAIL: it87 pwm2 not found. Try: modprobe it87; ls /sys/class/hwmon/hwmon*/name"
  grep -H . /sys/class/hwmon/hwmon*/name 2>/dev/null || true
  exit 2
fi
echo "PWM file: $F"
EN=$(echo "$F" | sed 's/pwm2$/pwm2_enable/')
if [ -f "$EN" ]; then echo "enable before: $(cat $EN)  (setting 1=manual)"; echo 1 > "$EN" || echo "warn: cannot set enable"; fi
echo "pwm before: $(cat $F)"
echo "$WANT" > "$F" && echo "wrote $WANT OK"
sleep 1
echo "pwm after: $(cat $F)"
NUM=$(echo "$F" | grep -o '[0-9]*$')
BASE=$(dirname "$F")
if [ -f "$BASE/fan${NUM}_input" ]; then echo "fan RPM: $(cat $BASE/fan${NUM}_input)"; fi
echo "DONE. Restore with: echo 102 > $F   (40%)"
