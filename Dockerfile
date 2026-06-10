# ── Stage 1: builder — install deps ke venv ──────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps minimal untuk compile wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements dulu (layer cache)
COPY requirements.txt .

RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip --quiet && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime — image final yang ringan ────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="MMI DevOps <devops@mmi-pt.com>" \
      app="evaluasi-kinerja" \
      version="1.0.0"

# Env dasar
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    DATABASE_PATH="/app/data/evaluasi.db" \
    SECRET_KEY="GANTI_SECRET_KEY_DI_PRODUCTION" \
    PORT=5000 \
    FLASK_DEBUG=0 \
    START_SCHEDULER=1 \
    TZ=Asia/Jakarta

# Install tzdata untuk timezone
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl \
    && rm -rf /var/lib/apt/lists/*

# Copy virtualenv dari builder
COPY --from=builder /venv /venv

# Buat user non-root untuk keamanan
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /bin/false appuser

WORKDIR /app

# Copy source code
COPY --chown=appuser:appuser . .

# Buat direktori data (untuk SQLite volume mount)
RUN mkdir -p /app/data && chown appuser:appuser /app/data

# Gunakan user non-root
USER appuser

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5000/login || exit 1

# Entry point via Gunicorn
CMD ["gunicorn", \
     "--config", "gunicorn.docker.conf.py", \
     "wsgi:app"]
