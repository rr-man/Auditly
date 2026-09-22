#!/usr/bin/env bash
# Install Auditly as its own nginx site + systemd service.
#
#   sudo "/path/to/Auditly/deploy/install.sh" \
#        --cert /etc/ssl/certs/snetcom-wildcard.pem \
#        --key  /etc/ssl/private/snetcom-wildcard.key
#
#   -> https://qa-server.example.com:8444/
#   -> https://auditly.example.com/            (once a DNS A record exists)
#
# Idempotent. Re-run to redeploy. Creates ONLY:
#   /etc/nginx/sites-available/auditly  (+ sites-enabled symlink)
#   /etc/nginx/auditly-common.conf
#   /etc/nginx/conf.d/auditly-limits.conf
#   /etc/systemd/system/auditly.service
#   ufw allow 8444/tcp, only if ufw is active
# It does not touch any other site and refuses to continue if it would.
set -euo pipefail

HERE0="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="$(dirname "$HERE0")"                     # the checkout this script lives in
APPUSER="$(stat -c %U "$APPDIR")"                 # the unit runs as the folder's owner
APPUSER="rmangune"
PORT_APP=8084
PORT_TLS=8444
SITE_AV="/etc/nginx/sites-available/auditly"
SITE_EN="/etc/nginx/sites-enabled/auditly"
COMMON_DST="/etc/nginx/auditly-common.conf"
LIMITS_DST="/etc/nginx/conf.d/auditly-limits.conf"
UNIT_DST="/etc/systemd/system/auditly.service"
HERE="$(cd "$(dirname "$0")" && pwd)"
OTHERS=(/etc/nginx/sites-available/snet-ai-apps /etc/nginx/sites-available/cfbuilder /etc/nginx/sites-available/coeo-transcripts)

CERT=""; KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --cert) CERT="$2"; shift 2;;
    --key)  KEY="$2";  shift 2;;
    *) echo "unknown argument: $1" >&2; exit 1;;
  esac
done
say(){ printf '==> %s\n' "$1"; }
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

if [ -z "$CERT" ] || [ -z "$KEY" ]; then
  cat >&2 <<'MSG'
!!! Refusing to install without TLS.
    This app carries passwords, session cookies and customer call recordings.
    Re-run with --cert /path/fullchain.pem --key /path/privkey.key
MSG
  exit 1
fi
[ -f "$CERT" ] || { echo "certificate not found: $CERT" >&2; exit 1; }
[ -f "$KEY"  ] || { echo "private key not found: $KEY" >&2; exit 1; }
[ -d "$APPDIR" ] || { echo "application not found: $APPDIR" >&2; exit 1; }
[ -f "$APPDIR/.env" ] || { echo "missing $APPDIR/.env -- copy .env.example and fill it in" >&2; exit 1; }
if grep -q '^AUDITLY_INSECURE_COOKIES=1' "$APPDIR/.env"; then
  echo "!!! .env has AUDITLY_INSECURE_COOKIES=1; set it to 0 before serving over TLS." >&2; exit 1
fi
if grep -q '^AUDITLY_OPEN_ACCESS=1' "$APPDIR/.env"; then
  echo "!!! .env has AUDITLY_OPEN_ACCESS=1 (no sign-in); set it to 0 before serving real recordings." >&2; exit 1
fi
command -v nginx >/dev/null || { echo "nginx is not installed" >&2; exit 1; }

# ── pre-flight: nothing else may own our ports or server names ────────────
if ss -ltn | awk '{print $4}' | grep -qE "[:.]${PORT_TLS}$"; then
  if ! grep -qs "listen ${PORT_TLS}" "$SITE_AV" 2>/dev/null; then
    echo "!!! something else already listens on ${PORT_TLS}" >&2; exit 1
  fi
fi
if ss -ltn | awk '{print $4}' | grep -qE "[:.]${PORT_APP}$"; then
  if ! systemctl is-active --quiet auditly 2>/dev/null; then
    echo "!!! something other than auditly.service listens on ${PORT_APP} (a dev server?). Stop it: fuser -k ${PORT_APP}/tcp" >&2; exit 1
  fi
fi
for f in /etc/nginx/sites-enabled/*; do
  [ -e "$f" ] || continue
  [ "$(readlink -f "$f")" = "$SITE_AV" ] && continue
  if grep -q 'auditly.example.com' "$f"; then echo "!!! $f already claims auditly.example.com" >&2; exit 1; fi
done

fingerprint(){ for f in "${OTHERS[@]}"; do [ -f "$f" ] && sha256sum "$f"; done; }
BEFORE="$(fingerprint)"

# ── install, with rollback if nginx -t fails ──────────────────────────────
backup(){ [ -f "$1" ] && cp -p "$1" "$1.auditly-prev" || true; }
restore(){ if [ -f "$1.auditly-prev" ]; then mv "$1.auditly-prev" "$1"; else rm -f "$1"; fi; }
say "installing nginx files"
backup "$SITE_AV"; backup "$COMMON_DST"; backup "$LIMITS_DST"
sed -e "s|__CERT__|$CERT|g" -e "s|__KEY__|$KEY|g" "$HERE/nginx-auditly.conf" > "$SITE_AV"
install -m 644 "$HERE/auditly-common.conf" "$COMMON_DST"
install -m 644 "$HERE/auditly-limits.conf" "$LIMITS_DST"
ln -sfn "$SITE_AV" "$SITE_EN"
if ! nginx -t 2>/tmp/auditly-nginx-t.log; then
  cat /tmp/auditly-nginx-t.log >&2
  echo "!!! nginx -t failed; rolling back" >&2
  restore "$SITE_AV"; restore "$COMMON_DST"; restore "$LIMITS_DST"
  [ -f "$SITE_AV" ] || rm -f "$SITE_EN"
  exit 1
fi
rm -f "$SITE_AV.auditly-prev" "$COMMON_DST.auditly-prev" "$LIMITS_DST.auditly-prev"

say "installing systemd unit"
sed -e "s|__APPDIR__|$APPDIR|g" -e "s|__USER__|$APPUSER|g" "$HERE/auditly.service" > "$UNIT_DST" && chmod 644 "$UNIT_DST"   # the unit ships with placeholders
systemctl daemon-reload
systemctl enable --now auditly
systemctl restart auditly
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  ufw allow "${PORT_TLS}/tcp" >/dev/null && say "ufw: allowed ${PORT_TLS}/tcp"
fi

# ── post-install self-verification ────────────────────────────────────────
say "verifying"
fail(){ echo "!!! $1" >&2; echo "    leaving the install in place for inspection; 'sudo $HERE/uninstall.sh' removes it" >&2; exit 1; }
for i in $(seq 1 40); do curl -fsS "http://127.0.0.1:${PORT_APP}/health" >/dev/null 2>&1 && break; sleep 0.25; done
curl -fsS "http://127.0.0.1:${PORT_APP}/health" | grep -q '"ok": true' || fail "app /health did not answer on ${PORT_APP}"
curl -ksS -o /dev/null -w '%{http_code}' "https://127.0.0.1:${PORT_TLS}/health" | grep -q '^200$' || fail "nginx did not proxy /health on ${PORT_TLS}"
for p in /.env /.gitignore /README.md /docs/CHANGELOG.md /auditly.db /uploads/x.wav; do
  code="$(curl -ksS -o /dev/null -w '%{http_code}' "https://127.0.0.1:${PORT_TLS}$p")"
  [ "$code" = "404" ] || [ "$code" = "401" ] || fail "$p answered $code, expected 404/401"
done
AFTER="$(fingerprint)"
[ "$BEFORE" = "$AFTER" ] || fail "a neighbouring site file changed during install"

cat <<MSG

Auditly is installed.

  https://qa-server.example.com:${PORT_TLS}/
  https://auditly.example.com/           (needs a DNS A record)

  status:    systemctl status auditly
  logs:      journalctl -u auditly -f
  redeploy:  sudo $HERE/install.sh --cert $CERT --key $KEY
  remove:    sudo $HERE/uninstall.sh
  add user:  cd "$APPDIR" && python3 auditly_host.py --add-user you@example.com --role admin
MSG
