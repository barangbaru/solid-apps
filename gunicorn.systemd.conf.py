"""Konfigurasi Gunicorn untuk systemd + Nginx di Ubuntu."""
import multiprocessing, os

# ─── Binding ───────────────────────────────────────────────────────────────────
# Unix socket dikelola oleh RuntimeDirectory systemd di /run/evaluasi/
bind = "unix:/run/evaluasi/evaluasi.sock"

# ─── Workers ───────────────────────────────────────────────────────────────────
# SQLite: 1 worker untuk hindari race condition tulis
workers = 1
worker_class = "sync"
threads = 4
timeout = 120
keepalive = 5

# ─── Logging ───────────────────────────────────────────────────────────────────
# Arahkan ke stdout/stderr agar journald yang tangkap (lihat service StandardOutput)
accesslog = "-"
errorlog  = "-"
loglevel  = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s"'

# ─── Process ───────────────────────────────────────────────────────────────────
daemon    = False
proc_name = "evaluasi_kinerja"
chdir     = os.path.dirname(os.path.abspath(__file__))

# ─── Security ──────────────────────────────────────────────────────────────────
limit_request_line       = 4096
limit_request_fields     = 100
limit_request_field_size = 8190
