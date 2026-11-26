#!/usr/bin/env bash
# systemd timers on the server: nightly backup, hourly log shipping to S3. Safe to run again.
set -euo pipefail
BIN=/opt/volley-hub/deploy/bin
unit() {  # name description command calendar
  cat > "/etc/systemd/system/volley-hub-$1.service" <<UNIT
[Unit]
Description=$2
[Service]
Type=oneshot
ExecStart=$3
UNIT
  cat > "/etc/systemd/system/volley-hub-$1.timer" <<UNIT
[Unit]
Description=$2
[Timer]
OnCalendar=$4
Persistent=true
[Install]
WantedBy=timers.target
UNIT
}
unit backup "volley-hub backup to S3" "$BIN/backup.sh" "*-*-* 03:30:00 UTC"
unit logs "volley-hub logs to S3" "$BIN/ship-logs.sh" "*-*-* *:05:00 UTC"
# Ship what is left before a shutdown or reboot.
cat > /etc/systemd/system/volley-hub-logs-final.service <<UNIT
[Unit]
Description=volley-hub logs to S3 before shutdown
After=docker.service network-online.target
Requires=docker.service
[Service]
Type=oneshot
RemainAfterExit=true
ExecStart=/bin/true
ExecStop=$BIN/ship-logs.sh
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now volley-hub-backup.timer volley-hub-logs.timer volley-hub-logs-final.service
