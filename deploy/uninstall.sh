#!/usr/bin/env bash
# Reverse exactly what install.sh created. Leaves the application folder alone.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
OTHERS=(/etc/nginx/sites-available/snet-ai-apps /etc/nginx/sites-available/cfbuilder /etc/nginx/sites-available/coeo-transcripts)
fingerprint(){ for f in "${OTHERS[@]}"; do [ -f "$f" ] && sha256sum "$f"; done; }
BEFORE="$(fingerprint)"
systemctl disable --now auditly 2>/dev/null || true
rm -f /etc/systemd/system/auditly.service
systemctl daemon-reload
rm -f /etc/nginx/sites-enabled/auditly /etc/nginx/sites-available/auditly /etc/nginx/auditly-common.conf /etc/nginx/conf.d/auditly-limits.conf
nginx -t && systemctl reload nginx
command -v ufw >/dev/null && ufw status | grep -q '^Status: active' && ufw delete allow 8444/tcp >/dev/null 2>&1 || true
[ "$BEFORE" = "$(fingerprint)" ] || { echo "!!! a neighbouring site file changed" >&2; exit 1; }
echo "Auditly nginx site and service removed. The application folder was not touched."
