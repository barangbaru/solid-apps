"""Gunicorn config khusus Docker — TCP binding, log ke stdout."""
import os

# ─── Binding ───────────────────────────────────────────
bind    = f"0.0.0.0:{os.environ.get('PORT', '5000')}"

# ─── Workers ───────────────────────────────────────────
# Tetap 1 worker agar APScheduler & SQLite tidak race condition
workers     = 1
worker_class = "sync"
threads     = 4
timeout     = 120
keepalive   = 5

# ─── Logging ke stdout/stderr (Docker best practice) ───
accesslog  = "-"    # stdout
errorlog   = "-"    # stderr
loglevel   = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ─── Process ───────────────────────────────────────────
daemon   = False    # Docker handles daemonizing
proc_name = "evaluasi_kinerja"
chdir    = "/app"

# ─── Security ──────────────────────────────────────────
limit_request_line       = 4096
limit_request_fields     = 100
limit_request_field_size = 8190

# ─── Graceful shutdown ─────────────────────────────────
graceful_timeout = 30
