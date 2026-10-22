#!/usr/bin/env bash
# Smart City - Raspberry Pi tune-up: reliable all day, steadier wifi, faster boot.
#
#   Run once:  sudo bash pi_optimize.sh
#   Undo all:  sudo bash pi_optimize.sh --undo
#   Then:      sudo reboot
#
# What it does
#   1. Dashboard restarts by itself if it ever crashes.
#   2. Hardware watchdog: the Pi reboots itself if it freezes.
#   3. Fewer SD card writes (logs kept in RAM, swap off), so pulling the plug is safer.
#   4. Wifi: power-saving off, reconnects forever, and a 1-minute checker that resets
#      wifi if the Pi loses the network.
#   5. Faster boot: Bluetooth, modem manager, "wait for network" and background
#      update timers switched off.
# Note: with logs in RAM, old logs are gone after a reboot (journalctl still works).

set -u
if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:   sudo bash pi_optimize.sh"
  exit 1
fi

SERVICE="smartcity-dashboard"
STATE_DIR="/var/lib/smartcity-optimize"
CONFIG_TXT="/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="/boot/config.txt"
MARK_BEGIN="# >>> smartcity-optimize >>>"
MARK_END="# <<< smartcity-optimize <<<"
SERVICES_OFF="bluetooth.service hciuart.service ModemManager.service triggerhappy.service triggerhappy.socket
NetworkManager-wait-online.service apt-daily.timer apt-daily-upgrade.timer man-db.timer dphys-swapfile.service"

say() { echo; echo "==> $*"; }

remove_config_block() {
  [ -f "$CONFIG_TXT" ] && sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$CONFIG_TXT"
}

undo() {
  say "Putting everything back"
  rm -f "/etc/systemd/system/$SERVICE.service.d/smartcity-optimize.conf"
  rm -f /etc/systemd/system.conf.d/smartcity-watchdog.conf
  rm -f /etc/systemd/journald.conf.d/smartcity-ram.conf
  rm -f /etc/NetworkManager/conf.d/smartcity-wifi-powersave.conf
  systemctl disable --now smartcity-wifi-check.timer >/dev/null 2>&1
  rm -f /etc/systemd/system/smartcity-wifi-check.service /etc/systemd/system/smartcity-wifi-check.timer
  rm -f /usr/local/bin/smartcity-wifi-check.sh
  if [ -f "$STATE_DIR/disabled.txt" ]; then
    while read -r u; do
      [ -n "$u" ] && systemctl enable "$u" >/dev/null 2>&1 && echo "  re-enabled $u"
    done < "$STATE_DIR/disabled.txt"
  fi
  if [ -f "$STATE_DIR/wifi_connection.txt" ]; then
    nmcli connection modify "$(cat "$STATE_DIR/wifi_connection.txt")" 802-11-wireless.powersave 0 >/dev/null 2>&1
  fi
  remove_config_block
  rm -rf "$STATE_DIR"
  systemctl daemon-reload
  systemctl restart systemd-journald
  echo
  echo "Done. Now reboot:   sudo reboot"
  exit 0
}

[ "${1:-}" = "--undo" ] && undo

mkdir -p "$STATE_DIR"
touch "$STATE_DIR/disabled.txt"

say "Boot time right now (compare after the reboot with:  systemd-analyze)"
systemd-analyze 2>/dev/null | head -n 1 || echo "  (not available)"

# ---------------------------------------------------------------- 1. dashboard
say "1/5  Dashboard: restart automatically if it crashes"
if systemctl list-unit-files "$SERVICE.service" 2>/dev/null | grep -q "^$SERVICE.service"; then
  mkdir -p "/etc/systemd/system/$SERVICE.service.d"
  cat > "/etc/systemd/system/$SERVICE.service.d/smartcity-optimize.conf" <<'EOF'
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=2
Nice=-5
Environment=PYTHONUNBUFFERED=1
EOF
  echo "  ok"
else
  echo "  WARNING: $SERVICE.service not found, skipped"
fi

# ---------------------------------------------------------------- 2. watchdog
say "2/5  Watchdog: reboot by itself if the Pi freezes"
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/smartcity-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
EOF
echo "  ok"

# ---------------------------------------------------------------- 3. SD card
say "3/5  SD card: logs in RAM, swap off"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/smartcity-ram.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=30M
EOF
if command -v dphys-swapfile >/dev/null 2>&1; then
  dphys-swapfile swapoff >/dev/null 2>&1
fi
echo "  ok"

# ---------------------------------------------------------------- 4. wifi
say "4/5  Wifi: no power-saving, reconnect forever, auto-reset if the network is lost"
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/smartcity-wifi-powersave.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
iw dev wlan0 set power_save off >/dev/null 2>&1
CON=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: '$2=="wlan0" {print $1; exit}')
if [ -n "$CON" ]; then
  echo "$CON" > "$STATE_DIR/wifi_connection.txt"
  nmcli connection modify "$CON" connection.autoconnect yes connection.autoconnect-retries 0 \
        802-11-wireless.powersave 2 && echo "  wifi connection \"$CON\": ok"
else
  echo "  (no active wifi connection found, only the power-saving setting was changed)"
fi

cat > /usr/local/bin/smartcity-wifi-check.sh <<'EOF'
#!/bin/bash
# Resets wifi if the Pi can't reach the router (phone hotspot) or the internet
# two checks in a row (about 2 minutes).
GW=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
if { [ -n "$GW" ] && ping -c 3 -W 2 "$GW" >/dev/null 2>&1; } || ping -c 2 -W 2 1.1.1.1 >/dev/null 2>&1; then
  rm -f /run/smartcity-wifi-fail
  exit 0
fi
if [ -f /run/smartcity-wifi-fail ]; then
  logger -t smartcity-wifi "network lost, resetting wifi"
  rm -f /run/smartcity-wifi-fail
  nmcli radio wifi off
  sleep 3
  nmcli radio wifi on
else
  touch /run/smartcity-wifi-fail
fi
EOF
chmod +x /usr/local/bin/smartcity-wifi-check.sh
cat > /etc/systemd/system/smartcity-wifi-check.service <<'EOF'
[Unit]
Description=Smart City wifi check

[Service]
Type=oneshot
ExecStart=/usr/local/bin/smartcity-wifi-check.sh
EOF
cat > /etc/systemd/system/smartcity-wifi-check.timer <<'EOF'
[Unit]
Description=Smart City wifi check every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF

# ---------------------------------------------------------------- 5. boot
say "5/5  Faster boot: switching off things the model doesn't use"
for u in $SERVICES_OFF; do
  if [ "$(systemctl is-enabled "$u" 2>/dev/null)" = "enabled" ]; then
    if systemctl disable --now "$u" >/dev/null 2>&1; then
      grep -qx "$u" "$STATE_DIR/disabled.txt" || echo "$u" >> "$STATE_DIR/disabled.txt"
      echo "  off: $u"
    fi
  fi
done
remove_config_block
if [ -f "$CONFIG_TXT" ]; then
  cat >> "$CONFIG_TXT" <<EOF
$MARK_BEGIN
dtparam=watchdog=on
dtoverlay=disable-bt
disable_splash=1
boot_delay=0
$MARK_END
EOF
  echo "  $CONFIG_TXT: watchdog on, Bluetooth off, no splash"
else
  echo "  WARNING: config.txt not found, skipped"
fi

# ---------------------------------------------------------------- apply
systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now smartcity-wifi-check.timer >/dev/null 2>&1
systemctl restart "$SERVICE" >/dev/null 2>&1

echo
echo "All done. Reboot to finish:   sudo reboot"
echo "After it starts again, check: systemd-analyze"
echo "Undo everything any time:     sudo bash pi_optimize.sh --undo"
