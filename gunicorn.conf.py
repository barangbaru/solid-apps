"""Konfigurasi Gunicorn untuk production di aaPanel."""
import multiprocessing, os

# ─── Binding ───────────────────────────────────────────
# Gunakan Unix socket (lebih efisien dengan Nginx sebagai proxy)
bind = "unix:/tmp/evaluasi.sock"
# Atau pakai TCP jika lebih mudah:
# bind = "127.0.0.1:5000"

# ─── Workers ───────────────────────────────────────────
# 1 worker cukup untuk app dengan SQLite (hindari race condition DB)
workers = 1
worker_class = "sync"
threads = 4
timeout = 120
keepalive = 5

# ─── Logging ───────────────────────────────────────────
# Path disesuaikan di aaPanel saat deploy
accesslog = "/www/wwwlogs/evaluasi_access.log"
errorlog  = "/www/wwwlogs/evaluasi_error.log"
loglevel  = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s'

# ─── Process ───────────────────────────────────────────
daemon           = False   # Supervisor/aaPanel yang handle daemon
proc_name        = "evaluasi_kinerja"
chdir            = os.path.dirname(os.path.abspath(__file__))

# ─── Security ──────────────────────────────────────────
limit_request_line       = 4096
limit_request_fields     = 100
limit_request_field_size = 8190
