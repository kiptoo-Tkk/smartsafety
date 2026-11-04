#!/bin/bash
# Run with sudo. Auto-starts the website at boot and sets Wi-Fi policy:
# saved Wi-Fi networks in range are preferred; otherwise the Pi's own hotspot starts.
set -e
APPDIR=/home/smartsafety/motor-safety/pi

echo "== Hotspot (fallback) =="
nmcli radio wifi on
nmcli con delete SmartSafetyHotspot 2>/dev/null || true
nmcli con add type wifi ifname wlan0 con-name SmartSafetyHotspot autoconnect yes ssid smartsafety \
  802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk smartsafety01 connection.autoconnect-priority -10

echo "== Saved Wi-Fi networks: autoconnect, preferred over the hotspot =="
nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-11-wireless" && $1!="SmartSafetyHotspot"{print $1}' | while IFS= read -r n; do
  nmcli con modify "$n" connection.autoconnect yes connection.autoconnect-priority 10 && echo "  $n"
done

echo "== Website service (starts at boot, restarts if it crashes) =="
cat > /etc/systemd/system/smartsafety.service <<EOF
[Unit]
Description=Smart Safety motor portal
After=network.target

[Service]
User=smartsafety
SupplementaryGroups=dialout
WorkingDirectory=$APPDIR
ExecStart=$APPDIR/.venv/bin/python app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
pkill -f 'python.*app.py' || true
systemctl daemon-reload
systemctl enable --now smartsafety

if ! nmcli -t -f DEVICE,STATE dev | grep -q '^wlan0:connected'; then
  echo "== wlan0 not connected: starting hotspot now =="
  nmcli con up SmartSafetyHotspot || true
fi
sleep 3
echo "== Result =="
echo "service active: $(systemctl is-active smartsafety) | enabled at boot: $(systemctl is-enabled smartsafety)"
nmcli device status
