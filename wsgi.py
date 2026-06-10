"""WSGI entry point untuk Gunicorn / production server."""
from app import app, init_db, start_scheduler
import os

# Inisialisasi database dan scheduler saat startup
init_db()

# Jalankan scheduler hanya sekali (bukan di tiap worker)
if os.environ.get('START_SCHEDULER', '1') == '1':
    start_scheduler()

application = app   # beberapa server WSGI butuh nama 'application'

if __name__ == '__main__':
    app.run()
