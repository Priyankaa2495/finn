# FINN: one container serving the API and the static mobile UI.
# Build:  docker build -t finn .
# Run:    docker run -p 8000:8000 -e FINN_ACCESS_TOKEN=change-me finn
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

# Dependencies first so code changes don't bust the pip layer.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

# Only what the service needs: no tests, eval sets, scripts or local data.
COPY backend/app backend/app
COPY backend/snapshot backend/snapshot
COPY frontend frontend

# Unprivileged user; the only writable path is the (ephemeral) data dir.
RUN useradd --create-home --uid 10001 finn \
 && mkdir -p backend/data \
 && chown -R finn:finn backend/data
USER finn
WORKDIR /app/backend

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8000'), timeout=4)" || exit 1

# Single worker by decision: the per-symbol fetch lock is per process (scale-out would use
# pg_advisory_xact_lock). Render injects $PORT; exec so uvicorn receives SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
