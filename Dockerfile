# Auditly as a public demo container (0.65.0): demo mode, no sign-in, fake providers, sample data.
# Runs as-is on Render (render.yaml), Hugging Face Spaces (set AUDITLY_PORT=7860) or any Docker host.
# Nothing here needs a key, and nothing in the image is a real call: .dockerignore keeps .env, the
# databases, the recordings and the certificates out of the build context.
FROM python:3.13-slim

WORKDIR /app
COPY . /app

# The demo's shape. Every value can be overridden by the host's own environment.
ENV AUDITLY_BIND=0.0.0.0 \
    AUDITLY_PORT=10000 \
    AUDITLY_TLS_PORT=0 \
    AUDITLY_DEMO=1 \
    AUDITLY_DEMO_HISTORY=1 \
    AUDITLY_OPEN_ACCESS=1 \
    ASK_ENABLED=1 \
    AUDITLY_MAX_UPLOAD_MB=10 \
    AUDITLY_RETENTION_DAYS=1 \
    AUDITLY_DB=/app/data/auditly-demo.db \
    AUDITLY_UPLOAD_DIR=/app/data/uploads \
    AUDITLY_DEMO_NOTE="Shared public sandbox: everyone is admin, it resets when the host restarts. Never upload a real call."

RUN mkdir -p /app/data && useradd --create-home --uid 10001 auditly && chown -R auditly:auditly /app
USER auditly

EXPOSE 10000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('AUDITLY_PORT','10000'), timeout=4)" || exit 1

CMD ["python3", "auditly_host.py", "--demo"]
