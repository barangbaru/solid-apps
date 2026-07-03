"""
conftest.py — Pytest shared fixtures for Hive Superapp automation tests.

Setiap test menggunakan SQLite in-memory sehingga tidak bergantung pada
database production maupun server aktif.
"""
import os
import sys
import pytest

# Pastikan folder root project ada di sys.path agar import app bisa ditemukan
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Paksa menggunakan SQLite in-memory saat testing
os.environ.setdefault('DB_TYPE', 'sqlite')
os.environ['DATABASE_PATH'] = ':memory:'
os.environ['SECRET_KEY'] = 'test-secret-key-hive-pytest'
os.environ['TESTING'] = '1'

import app as hive_app


@pytest.fixture(scope='session')
def application():
    """Buat instance Flask test dengan SQLite in-memory (shared per session)."""
    hive_app.app.config.update({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'SERVER_NAME': 'localhost',
    })
    # Override DB ke in-memory
    hive_app.DB_TYPE = 'sqlite'
    hive_app.DB_PATH = ':memory:'
    yield hive_app.app


@pytest.fixture(scope='session')
def _init_db(application):
    """Inisialisasi schema + seed data (dijalankan sekali per session)."""
    with application.app_context():
        hive_app.init_db()
    return True


@pytest.fixture()
def client(application, _init_db):
    """HTTP test client — fresh per test function."""
    with application.test_client() as c:
        yield c


@pytest.fixture()
def auth_client(client):
    """Client yang sudah login sebagai superadmin."""
    resp = client.post('/login', data={
        'username': 'superadmin',
        'password': 'Admin@123',
    }, follow_redirects=True)
    assert resp.status_code == 200
    yield client


@pytest.fixture()
def db_conn(application, _init_db):
    """
    Koneksi SQLite langsung untuk setup data di dalam test.
    Mengembalikan _DBWrapper. Perlu dipanggil di dalam app context.
    """
    with application.app_context():
        db = hive_app._get_raw_db()
        yield db
        db.close()
