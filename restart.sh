#!/usr/bin/env bash
# Restart the Auditly server as a LAN link. No root needed -- 8084 and 8444 are above 1024.
#
#   ./restart.sh            plain HTTP on 8084 AND HTTPS on 8444 (self-signed, generated on first run)
#   ./restart.sh --no-tls   plain HTTP only
#   -> http://qa-server.example.com:8084/auditly/
#   -> https://qa-server.example.com:8444/         (voice input needs this one; browsers warn once)
#
# Same two properties as ~/cfbuilder-serve/restart.sh, for the same reasons
# (a broad `pkill -f "[p]ython3 .*serve\.py"` killed that server for ~19 hours
# on 2026-08-19):
#   - the server file name contains no "serve.py" substring
#   - this script only ever stops its own server: by port (fuser -k 8084/tcp,
#     the method CLAUDE.md prescribes), or by absolute path if fuser is absent.
#     Never a broad pkill -f.
# Keep both properties if you rename or move this. The @reboot crontab entry
# must name the same file and the same MODE, or a reboot silently undoes them.
#
# MODE="": real mode -- real transcription and scoring of each uploaded call.
#   Needs DEEPGRAM_API_KEY (or OPENAI_API_KEY) and OPENAI_API_KEY in .env and
#   AUDITLY_ALLOW_SPEND=1. First time: python3 auditly_host.py --seed-rubric
#   (sample rubric + name lists into auditly.db). For real customer calls prefer
#   deploy/install.sh (nginx, a real certificate) once IT issues one -- run this
#   script with --no-tls first so nginx can have 8444 (docs/RUNBOOK.md §2).
# MODE="--demo": fake providers, the canned sample call for EVERY upload,
#   no keys, nothing leaves the machine (auditly-demo.db).
MODE=""
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the folder this script lives in
SELF="$APPDIR/auditly_host.py"
PORT="${AUDITLY_PORT:-8084}"
TLS_PORT="${AUDITLY_TLS_PORT:-8444}"
# The name in the links and in the self-signed certificate: AUDITLY_HOST from the environment or from .env,
# else what the machine calls itself.
if [ -z "${AUDITLY_HOST:-}" ] && [ -f "$APPDIR/.env" ]; then
  AUDITLY_HOST="$(grep -E '^AUDITLY_HOST=' "$APPDIR/.env" | tail -1 | cut -d= -f2- | cut -d'#' -f1 | tr -d '[:space:]"')"
fi
HOST="${AUDITLY_HOST:-$(hostname -f 2>/dev/null || hostname)}"
[ "${1:-}" = "--no-tls" ] && TLS_PORT=0

cd "$APPDIR" || exit 1
[ -f "$SELF" ] || { echo "server file missing: $SELF"; exit 1; }
[ -f "$APPDIR/.env" ] || { echo "missing .env -- see docs/RUNBOOK.md §8"; exit 1; }

# The self-signed pair for the HTTPS listener (0.49.0). Generated once; a real certificate's fullchain
# and key can overwrite these two files (or AUDITLY_TLS_CERT/KEY in .env can point elsewhere) and the
# browser warning stops. EC P-256, 825 days, SANs for every name this box answers to.
ensure_cert() {
  local dir="$APPDIR/tls" crt="$APPDIR/tls/auditly.crt" key="$APPDIR/tls/auditly.key"
  [ -s "$crt" ] && [ -s "$key" ] && return 0
  command -v openssl >/dev/null 2>&1 || { echo "openssl not found; run with --no-tls or install it"; return 1; }
  mkdir -p "$dir" && chmod 700 "$dir"
  local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
    -keyout "$key" -out "$crt" -subj "/CN=${HOST}/O=Auditly" \
    -addext "subjectAltName=DNS:${HOST},DNS:${HOST%%.*},DNS:localhost,IP:127.0.0.1${ip:+,IP:$ip}" \
    >/dev/null 2>&1 || { echo "could not generate the certificate"; return 1; }
  chmod 600 "$key" "$crt"
  echo "generated a self-signed certificate in tls/ (browsers will warn once per browser)"
}

# Stop the previous instance (ours is the only thing that may use these ports).
stop_port() {
  if command -v fuser >/dev/null 2>&1; then
    fuser -k -TERM "$1/tcp" >/dev/null 2>&1 || true
  else
    for p in $(pgrep -f "[p]ython3 ${SELF}"); do kill "$p" 2>/dev/null; done
  fi
  for _ in $(seq 1 20); do
    ss -ltn | awk '{print $4}' | grep -qE "[:.]$1$" || break
    sleep 0.5
  done
}
stop_port "$PORT"
[ "$TLS_PORT" != "0" ] && stop_port "$TLS_PORT"

if [ "$TLS_PORT" != "0" ]; then
  ensure_cert || TLS_PORT=0
fi

# shellcheck disable=SC2086  # MODE is intentionally word-split (empty = no flag)
AUDITLY_TLS_PORT="$TLS_PORT" setsid nohup python3 "$SELF" $MODE >> server.log 2>&1 < /dev/null &

for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 0.25
done
if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "running on ${PORT}${MODE:+ (demo mode)}"
  echo "  http://${HOST}:${PORT}/auditly/"
  if [ "$TLS_PORT" != "0" ]; then
    for _ in $(seq 1 20); do
      curl -kfsS "https://127.0.0.1:${TLS_PORT}/health" >/dev/null 2>&1 && break
      sleep 0.25
    done
    if curl -kfsS "https://127.0.0.1:${TLS_PORT}/health" >/dev/null 2>&1; then
      echo "  https://${HOST}:${TLS_PORT}/   (same app; self-signed -> Advanced, Proceed, once per browser; voice input works here)"
    else
      echo "  https listener did not come up on ${TLS_PORT}; see server.log"
    fi
  fi
else
  echo "FAILED, see server.log"; tail -5 server.log; exit 1
fi
