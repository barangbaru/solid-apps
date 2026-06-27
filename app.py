from flask import (Flask, render_template, request, redirect, url_for,
                   flash, g, session, jsonify, Response, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3, os, smtplib, json, secrets, requests as req_lib, io, base64, signal, subprocess
try:
    from cryptography.fernet import Fernet, InvalidToken as _FernetInvalidToken
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from seed_data import ALL_DIVISIONS, ABILITY_ITEMS
from version import VERSION, RELEASE_DATE, RELEASE_NOTES
import pyotp, qrcode

app = Flask(__name__)
_default_secret = os.environ.get('SECRET_KEY', '')
if not _default_secret:
    import warnings
    warnings.warn("SECRET_KEY env var tidak diset! Gunakan nilai acak yang kuat di production.")
    _default_secret = 'evalkey-2024-superadmin-secure!'
app.secret_key = _default_secret

# Google OAuth config (set via env vars or portal settings)
GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_AUTH_URL      = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL     = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL  = 'https://www.googleapis.com/oauth2/v3/userinfo'

# Agar request.host_url benar di balik nginx (ProxyFix)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

_default_db = os.path.join(os.path.dirname(__file__), 'evaluasi.db')
DB_PATH = os.environ.get('DATABASE_PATH', _default_db)
DB_TYPE = os.environ.get('DB_TYPE', 'sqlite').lower()
DIVISI_LIST = list(ALL_DIVISIONS.keys())

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_IMAGE_EXT = {'jpg', 'jpeg', 'png', 'webp', 'gif'}

def _save_upload(file_obj, subfolder=''):
    import uuid
    ext = file_obj.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return None
    folder = os.path.join(UPLOAD_FOLDER, subfolder)
    os.makedirs(folder, exist_ok=True)
    fname = uuid.uuid4().hex + '.' + ext
    file_obj.save(os.path.join(folder, fname))
    return f'/static/uploads/{subfolder}/{fname}' if subfolder else f'/static/uploads/{fname}'

def get_divisi_list(db):
    try:
        rows = db.execute('SELECT name FROM divisions WHERE is_active=1 ORDER BY sort_order, name').fetchall()
        return [r['name'] for r in rows] if rows else DIVISI_LIST
    except Exception:
        return DIVISI_LIST

# ─── DB Helpers ────────────────────────────────────────────────────────────────
# Thin wrapper sehingga kode SQLite (placeholder "?", row['col']) tetap bekerja
# tanpa perubahan saat menggunakan PostgreSQL (placeholder "%s", RealDictCursor).

class _DBWrapper:
    """Wrap koneksi SQLite atau psycopg2 agar keduanya terlihat sama dari luar."""
    def __init__(self, conn, is_pg=False):
        self._conn = conn
        self._is_pg = is_pg

    def _fix(self, sql):
        """Konversi SQL dialek SQLite → PostgreSQL."""
        if not self._is_pg:
            return sql
        import re
        sql_stripped = sql.strip()
        # INSERT OR REPLACE → ON CONFLICT DO UPDATE SET col=EXCLUDED.col, ...
        m_replace = re.match(
            r'INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)',
            sql_stripped, re.IGNORECASE | re.DOTALL)
        if m_replace:
            cols = [c.strip() for c in m_replace.group(2).split(',')]
            # Kolom pertama dijadikan conflict target; kolom lainnya di-update
            conflict_col = cols[0]
            update_cols  = cols[1:] if len(cols) > 1 else cols
            update_set   = ', '.join(f'{c}=EXCLUDED.{c}' for c in update_cols)
            sql = re.sub(r'\bINSERT\s+OR\s+REPLACE\s+INTO\b', 'INSERT INTO', sql, flags=re.IGNORECASE)
            sql = re.sub(r'\?', '%s', sql)
            sql = sql.rstrip().rstrip(';') + f' ON CONFLICT ({conflict_col}) DO UPDATE SET {update_set}'
            return sql
        # INSERT OR IGNORE → ON CONFLICT DO NOTHING (tandai agar execute tahu tidak perlu RETURNING)
        is_or_ignore = bool(re.search(r'\bINSERT\s+OR\s+IGNORE\b', sql, re.IGNORECASE))
        sql = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', sql, flags=re.IGNORECASE)
        # Placeholder ? → %s
        sql = re.sub(r'\?', '%s', sql)
        if is_or_ignore:
            sql = sql.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
        # ── Konversi fungsi SQLite → PostgreSQL ───────────────────────────────
        # julianday(col) - julianday('now') → (col::date - CURRENT_DATE)
        sql = re.sub(
            r"julianday\(([^)]+)\)\s*-\s*julianday\('now'\)",
            r"(\1::date - CURRENT_DATE)",
            sql, flags=re.IGNORECASE)
        # julianday('now') - julianday(col) → (CURRENT_DATE - col::date)
        sql = re.sub(
            r"julianday\('now'\)\s*-\s*julianday\(([^)]+)\)",
            r"(CURRENT_DATE - \1::date)",
            sql, flags=re.IGNORECASE)
        # julianday(col) sisa (standalone)
        sql = re.sub(r"julianday\(([^)]+)\)", r"\1::date", sql, flags=re.IGNORECASE)
        # GROUP_CONCAT(col, sep) → STRING_AGG(col::text, sep)
        sql = re.sub(
            r'\bGROUP_CONCAT\s*\(([^,)]+),\s*([^)]+)\)',
            lambda m: f"STRING_AGG({m.group(1).strip()}::text, {m.group(2).strip()})",
            sql, flags=re.IGNORECASE)
        # GROUP_CONCAT(col) tanpa separator → STRING_AGG(col::text, ',')
        sql = re.sub(
            r'\bGROUP_CONCAT\s*\(([^)]+)\)',
            lambda m: f"STRING_AGG({m.group(1).strip()}::text, ',')",
            sql, flags=re.IGNORECASE)
        # last_insert_rowid() → lastval()
        sql = re.sub(r'\blast_insert_rowid\s*\(\s*\)', 'lastval()', sql, flags=re.IGNORECASE)
        # date('now') → CURRENT_DATE  (harus sebelum pola date(col) di bawah)
        sql = re.sub(r"date\('now'\)", "CURRENT_DATE", sql, flags=re.IGNORECASE)
        # date(col_expr) → (col_expr)::date  — konversi SQLite date() ke PostgreSQL cast
        sql = re.sub(r"\bdate\(([^)]+)\)", r"(\1)::date", sql, flags=re.IGNORECASE)
        # datetime('now','localtime') → NOW()  (di query DML, bukan DDL)
        sql = re.sub(r"datetime\('now',\s*'localtime'\)", "NOW()", sql, flags=re.IGNORECASE)
        # datetime('now') → NOW()
        sql = re.sub(r"datetime\('now'\)", "NOW()", sql, flags=re.IGNORECASE)
        # strftime('%Y-%m-%d', col) → TO_CHAR(col::date, 'YYYY-MM-DD')
        sql = re.sub(
            r"strftime\('%Y-%m-%d',\s*([^)]+)\)",
            r"TO_CHAR(\1::date, 'YYYY-MM-DD')",
            sql, flags=re.IGNORECASE)
        return sql

    @property
    def _is_or_ignore(self):
        return False  # placeholder, dipakai di execute

    def execute(self, sql, params=()):
        if self._is_pg:
            import re
            # Cek apakah ini INSERT OR IGNORE (sebelum _fix mengubahnya)
            is_or_ignore = bool(re.search(r'\bINSERT\s+OR\s+IGNORE\b', sql, re.IGNORECASE))
            is_or_replace = bool(re.search(r'\bINSERT\s+OR\s+REPLACE\b', sql, re.IGNORECASE))
            fixed = self._fix(sql)
            cur = self._conn.cursor()
            # Untuk INSERT biasa, tambahkan RETURNING id agar lastrowid tersedia
            is_insert = bool(re.match(r'\s*INSERT\b', fixed, re.IGNORECASE))
            needs_returning = is_insert and not is_or_ignore and 'RETURNING' not in fixed.upper()
            fixed_exec = fixed
            if needs_returning:
                fixed_exec = fixed.rstrip().rstrip(';') + ' RETURNING id'
            try:
                cur.execute(fixed_exec, params if params else None)
            except Exception as e:
                if needs_returning and 'column "id" does not exist' in str(e):
                    # Tabel ini tidak punya kolom id (misal app_settings pakai key)
                    self._conn.rollback()
                    cur = self._conn.cursor()
                    cur.execute(fixed, params if params else None)
                    needs_returning = False
                else:
                    raise
            wrapper = _CursorWrapper(cur, is_pg=True)
            if needs_returning:
                row = cur.fetchone()
                wrapper._last_id = row[0] if row else None
            elif is_or_replace and 'RETURNING' not in fixed.upper():
                # ON CONFLICT DO UPDATE SET sudah di fixed, tambahkan RETURNING
                pass  # lastrowid tidak kritis untuk OR REPLACE
            return wrapper
        else:
            return self._conn.execute(sql, params)

    def executescript(self, sql):
        if self._is_pg:
            # PostgreSQL: eksekusi per-statement dengan retry multi-pass
            # agar FK ordering di schema tidak masalah (misal sc_ticket_history
            # di-CREATE sebelum sc_tickets karena SQLite tidak enforce FK order)
            stmts = [s.strip() for s in sql.split(';') if s.strip()]
            pending = stmts
            cur = self._conn.cursor()
            max_passes = len(stmts) + 1
            for _ in range(max_passes):
                if not pending:
                    break
                still_pending = []
                for stmt in pending:
                    try:
                        cur.execute(stmt)
                        self._conn.commit()
                    except Exception:
                        self._conn.rollback()
                        cur = self._conn.cursor()
                        still_pending.append(stmt)
                if len(still_pending) == len(pending):
                    # Tidak ada kemajuan — statement benar-benar error
                    # Eksekusi sekali lagi agar exception muncul dengan jelas
                    cur.execute(still_pending[0])
                pending = still_pending
            return cur
        return self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class _CursorWrapper:
    """Wrap psycopg2 cursor agar fetchone()/fetchall() mengembalikan RealDictRow
    yang dapat diakses dengan row['col'] DAN row[0] seperti sqlite3.Row."""
    def __init__(self, cur, is_pg=False):
        self._cur = cur
        self._is_pg = is_pg

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if self._is_pg:
            return _DictRow(row, self._cur.description)
        return row

    def fetchall(self):
        rows = self._cur.fetchall()
        if self._is_pg:
            desc = self._cur.description
            return [_DictRow(r, desc) for r in rows]
        return rows

    @property
    def lastrowid(self):
        if self._is_pg:
            return getattr(self, '_last_id', None)
        return self._cur.lastrowid

    # Proxy agar sqlite3.Cursor attrs seperti rowcount dll bisa diakses
    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        for row in self._cur:
            if self._is_pg:
                yield _DictRow(row, self._cur.description)
            else:
                yield row


class _DictRow:
    """Row psycopg2 yang bisa diakses via row['col'] atau row[0]."""
    __slots__ = ('_data', '_keys')

    def __init__(self, row, description):
        self._keys = [d[0] for d in description]
        self._data = list(row)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[key]
        return self._data[self._keys.index(key)]

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return self._keys

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, ValueError):
            return default


def _pg_connect():
    """Buat koneksi psycopg2 dari env vars PG_*."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get('PG_HOST', 'localhost'),
        port=int(os.environ.get('PG_PORT', 5432)),
        dbname=os.environ.get('PG_NAME', 'hive_db'),
        user=os.environ.get('PG_USER', 'hive'),
        password=os.environ.get('PG_PASS', ''),
        options='-c search_path=public',
    )


def _get_raw_db():
    """Buat koneksi DB baru (untuk background tasks / scheduler di luar request context)."""
    if DB_TYPE == 'postgresql':
        conn = _pg_connect()
        conn.autocommit = False
        return _DBWrapper(conn, is_pg=True)
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    return _DBWrapper(raw, is_pg=False)


def get_db():
    if 'db' not in g:
        if DB_TYPE == 'postgresql':
            conn = _pg_connect()
            conn.autocommit = False
            g.db = _DBWrapper(conn, is_pg=True)
        else:
            raw = sqlite3.connect(DB_PATH)
            raw.row_factory = sqlite3.Row
            raw.execute('PRAGMA foreign_keys = ON')
            g.db = _DBWrapper(raw, is_pg=False)
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

# ─── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    role TEXT DEFAULT 'admin',
    is_active INTEGER DEFAULT 1,
    last_login TEXT DEFAULT '',
    email TEXT DEFAULT '',
    google_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    jabatan TEXT DEFAULT '',
    divisi TEXT NOT NULL,
    level TEXT DEFAULT 'Staff',
    employment_type TEXT DEFAULT 'tetap',
    contract_start TEXT DEFAULT '',
    contract_end TEXT DEFAULT '',
    rate_mandays REAL DEFAULT NULL,
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    telegram_id TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    periode TEXT NOT NULL,
    status TEXT DEFAULT 'draft',
    pp_score INTEGER DEFAULT 0,
    pp_total REAL DEFAULT 0,
    hs_total REAL DEFAULT 0,
    ability_score REAL DEFAULT 0,
    competency_total REAL DEFAULT 0,
    final_total REAL DEFAULT 0,
    evaluator TEXT DEFAULT '',
    overall_assessment TEXT DEFAULT '',
    development_plan TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS project_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL,
    entry_type TEXT NOT NULL,
    project_name TEXT DEFAULT '',
    detail_task TEXT DEFAULT '',
    status TEXT DEFAULT 'DONE',
    notes TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    FOREIGN KEY(eval_id) REFERENCES evaluations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS skill_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    divisi TEXT NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS skill_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    bobot REAL DEFAULT 1.0,
    sort_order INTEGER DEFAULT 0,
    FOREIGN KEY(category_id) REFERENCES skill_categories(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS skill_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL,
    skill_item_id INTEGER NOT NULL,
    score INTEGER DEFAULT 0,
    UNIQUE(eval_id, skill_item_id),
    FOREIGN KEY(eval_id) REFERENCES evaluations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ability_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    divisi TEXT NOT NULL,
    name TEXT NOT NULL,
    desc_a TEXT DEFAULT '',
    desc_b TEXT DEFAULT '',
    desc_c TEXT DEFAULT '',
    desc_d TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ability_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL,
    ability_item_id INTEGER NOT NULL,
    level TEXT DEFAULT '',
    UNIQUE(eval_id, ability_item_id),
    FOREIGN KEY(eval_id) REFERENCES evaluations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS competency_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    divisi TEXT NOT NULL,
    point_measurement TEXT NOT NULL,
    bobot REAL DEFAULT 0.0,
    sort_order INTEGER DEFAULT 0,
    is_hardskill INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS competency_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL,
    competency_item_id INTEGER NOT NULL,
    rating INTEGER DEFAULT 0,
    UNIQUE(eval_id, competency_item_id),
    FOREIGN KEY(eval_id) REFERENCES evaluations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS peer_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL,
    slot INTEGER DEFAULT 1,
    reviewer_name TEXT DEFAULT '',
    feedback TEXT DEFAULT '',
    FOREIGN KEY(eval_id) REFERENCES evaluations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reminder_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER,
    channel TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    error_msg TEXT DEFAULT '',
    triggered_by TEXT DEFAULT 'auto',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eval_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL UNIQUE,
    token TEXT UNIQUE NOT NULL,
    email_sent_to TEXT DEFAULT '',
    sent_at TEXT DEFAULT '',
    accessed_at TEXT DEFAULT '',
    submitted_at TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    FOREIGN KEY(eval_id) REFERENCES evaluations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS eval_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL,
    reviewer_user_id INTEGER NOT NULL,
    reviewer_role TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    submitted_at TEXT DEFAULT '',
    FOREIGN KEY(eval_id) REFERENCES evaluations(id) ON DELETE CASCADE,
    FOREIGN KEY(reviewer_user_id) REFERENCES users(id),
    UNIQUE(eval_id, reviewer_user_id)
);
CREATE TABLE IF NOT EXISTS divisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    is_system INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_name TEXT NOT NULL,
    permission TEXT NOT NULL,
    PRIMARY KEY (role_name, permission)
);
CREATE TABLE IF NOT EXISTS employee_salary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    year INTEGER NOT NULL,
    base_salary REAL DEFAULT 0,
    al_001 REAL DEFAULT 0,
    al_002 REAL DEFAULT 0,
    al_003 REAL DEFAULT 0,
    al_004 REAL DEFAULT 0,
    increase_pct REAL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(employee_id, year),
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS superapp_apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'grid',
    color TEXT DEFAULT '#4da8da',
    bg_color TEXT DEFAULT '#e8f4fd',
    url TEXT DEFAULT '/',
    is_active INTEGER DEFAULT 1,
    is_coming_soon INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    required_permission TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS user_app_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    app_slug TEXT NOT NULL,
    app_role TEXT DEFAULT 'user',
    is_active INTEGER DEFAULT 1,
    granted_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(user_id, app_slug),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ─── SupportCore: Ticket History & Multi-Assignee ────────────────────────────
CREATE TABLE IF NOT EXISTS sc_ticket_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    section TEXT NOT NULL DEFAULT 'description',
    filename TEXT NOT NULL,
    original_name TEXT DEFAULT '',
    uploaded_by INTEGER,
    uploaded_by_name TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(ticket_id) REFERENCES sc_tickets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sc_ticket_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    changed_by INTEGER,
    changed_by_name TEXT DEFAULT '',
    action TEXT DEFAULT '',
    field_name TEXT DEFAULT '',
    old_value TEXT DEFAULT '',
    new_value TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(ticket_id) REFERENCES sc_tickets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sc_ticket_external_assignees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    role_note TEXT DEFAULT '',
    added_by INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(ticket_id) REFERENCES sc_tickets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS pc_task_external_assignees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    added_by INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(task_id) REFERENCES pc_tasks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sc_ticket_assignees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    divisi TEXT DEFAULT '',
    role_note TEXT DEFAULT '',
    assigned_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(ticket_id, employee_id),
    FOREIGN KEY(ticket_id) REFERENCES sc_tickets(id) ON DELETE CASCADE,
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
);
-- ─── SupportCore: Presales & POC ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sc_presales_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    req_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    request_type TEXT DEFAULT 'presales',
    subject TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    status_note TEXT DEFAULT '',
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(customer_id) REFERENCES sc_customers(id)
);
CREATE TABLE IF NOT EXISTS sc_presales_assignees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    divisi TEXT DEFAULT '',
    role_note TEXT DEFAULT '',
    UNIQUE(request_id, employee_id),
    FOREIGN KEY(request_id) REFERENCES sc_presales_requests(id) ON DELETE CASCADE,
    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS sc_presales_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    changed_by INTEGER,
    changed_by_name TEXT DEFAULT '',
    action TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(request_id) REFERENCES sc_presales_requests(id) ON DELETE CASCADE
);

-- ─── Audit Trail ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_slug TEXT NOT NULL DEFAULT 'portal',
    user_id INTEGER,
    username TEXT DEFAULT '',
    action TEXT NOT NULL,
    resource TEXT DEFAULT '',
    resource_id TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS audit_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_slug TEXT NOT NULL DEFAULT 'portal',
    user_id INTEGER,
    username TEXT DEFAULT '',
    url TEXT DEFAULT '',
    method TEXT DEFAULT '',
    error_code INTEGER DEFAULT 500,
    error_type TEXT DEFAULT '',
    error_msg TEXT DEFAULT '',
    traceback TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS audit_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_slug TEXT NOT NULL DEFAULT 'portal',
    channel TEXT DEFAULT '',
    recipient TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    message TEXT DEFAULT '',
    status TEXT DEFAULT 'sent',
    error_msg TEXT DEFAULT '',
    triggered_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ─── SupportCore ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sc_apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sc_modules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(app_id) REFERENCES sc_apps(id) ON DELETE CASCADE,
    UNIQUE(app_id, name)
);
CREATE TABLE IF NOT EXISTS sc_customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    address TEXT DEFAULT '',
    contact_person TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sc_services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sc_support_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sc_sla_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    priority TEXT DEFAULT 'Medium',
    response_time_hours REAL DEFAULT 4,
    resolution_time_hours REAL DEFAULT 24,
    description TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sc_contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    contract_value REAL DEFAULT 0,
    status TEXT DEFAULT 'active',
    notes TEXT DEFAULT '',
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(customer_id) REFERENCES sc_customers(id)
);
CREATE TABLE IF NOT EXISTS sc_contract_services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    UNIQUE(contract_id, service_id),
    FOREIGN KEY(contract_id) REFERENCES sc_contracts(id) ON DELETE CASCADE,
    FOREIGN KEY(service_id) REFERENCES sc_services(id)
);
CREATE TABLE IF NOT EXISTS sc_contract_support_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    support_type_id INTEGER NOT NULL,
    UNIQUE(contract_id, support_type_id),
    FOREIGN KEY(contract_id) REFERENCES sc_contracts(id) ON DELETE CASCADE,
    FOREIGN KEY(support_type_id) REFERENCES sc_support_types(id)
);
CREATE TABLE IF NOT EXISTS sc_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no TEXT NOT NULL UNIQUE,
    contract_id INTEGER DEFAULT NULL,
    customer_id INTEGER NOT NULL,
    support_type_id INTEGER NOT NULL,
    sla_category_id INTEGER DEFAULT NULL,
    subject TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    reported_by TEXT DEFAULT '',
    reported_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    responded_at TEXT DEFAULT NULL,
    resolved_at TEXT DEFAULT NULL,
    closed_at TEXT DEFAULT NULL,
    assigned_to INTEGER DEFAULT NULL,
    created_by INTEGER DEFAULT NULL,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(customer_id) REFERENCES sc_customers(id),
    FOREIGN KEY(contract_id) REFERENCES sc_contracts(id),
    FOREIGN KEY(support_type_id) REFERENCES sc_support_types(id),
    FOREIGN KEY(sla_category_id) REFERENCES sc_sla_categories(id)
);
CREATE TABLE IF NOT EXISTS bk_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'room',
    subtype TEXT DEFAULT '',
    capacity INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    location TEXT DEFAULT '',
    color TEXT DEFAULT '#d97706',
    icon TEXT DEFAULT 'door-open',
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS bk_bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    purpose TEXT DEFAULT '',
    booked_by INTEGER NOT NULL,
    attendees TEXT DEFAULT '',
    attendee_count INTEGER DEFAULT 0,
    start_dt TEXT NOT NULL,
    end_dt TEXT NOT NULL,
    status TEXT DEFAULT 'confirmed',
    notes TEXT DEFAULT '',
    is_recurring INTEGER DEFAULT 0,
    recurring_type TEXT DEFAULT '',
    recurring_days TEXT DEFAULT '',
    recurring_until TEXT DEFAULT '',
    parent_id INTEGER DEFAULT NULL,
    destination TEXT DEFAULT '',
    cancelled_at TEXT DEFAULT '',
    cancelled_by INTEGER DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS bk_resource_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES bk_resources(id) ON DELETE CASCADE,
    image TEXT NOT NULL,
    caption TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS bk_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'other',
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'box',
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bk_booking_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bk_bookings(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES bk_items(id),
    qty INTEGER DEFAULT 1,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ac_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    device_type TEXT DEFAULT 'Laptop',
    brand TEXT DEFAULT '',
    os TEXT DEFAULT '',
    os_license_type TEXT DEFAULT '',
    processor TEXT DEFAULT '',
    ram TEXT DEFAULT '',
    disk TEXT DEFAULT '',
    office_version TEXT DEFAULT '',
    asset_tag TEXT DEFAULT '',
    serial_number TEXT DEFAULT '',
    purchase_date TEXT DEFAULT '',
    condition TEXT DEFAULT 'Baik',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ac_asset_software (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER REFERENCES ac_assets(id) ON DELETE CASCADE,
    software_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ac_asset_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES ac_assets(id) ON DELETE CASCADE,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    manual_employee_name TEXT DEFAULT '',
    started_at TEXT DEFAULT '',
    ended_at TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ac_infrastructure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_type TEXT NOT NULL,
    brand TEXT DEFAULT '',
    model TEXT DEFAULT '',
    description TEXT DEFAULT '',
    serial_number TEXT DEFAULT '',
    nickname TEXT DEFAULT '',
    ups_group TEXT DEFAULT '',
    location TEXT DEFAULT '',
    status TEXT DEFAULT 'Aktif',
    condition_notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ac_licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    software_name TEXT NOT NULL,
    license_key TEXT DEFAULT '',
    license_type TEXT DEFAULT 'Perpetual',
    version TEXT DEFAULT '',
    year INTEGER DEFAULT NULL,
    max_seats INTEGER DEFAULT 1,
    is_active INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ac_license_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id INTEGER REFERENCES ac_licenses(id) ON DELETE CASCADE,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    seat_number INTEGER DEFAULT 1,
    assigned_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ac_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    category TEXT DEFAULT 'SaaS',
    billing_cycle TEXT DEFAULT 'Monthly',
    start_date TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    username TEXT DEFAULT '',
    password TEXT DEFAULT '',
    access_url TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ac_software_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    software_name TEXT NOT NULL,
    version TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'Pending',
    requested_at TEXT DEFAULT (datetime('now','localtime')),
    resolved_at TEXT DEFAULT '',
    resolved_by TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ac_maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT DEFAULT 'Lainnya',
    location TEXT DEFAULT '',
    description TEXT DEFAULT '',
    frequency TEXT DEFAULT 'Bulanan',
    last_maintenance TEXT DEFAULT '',
    next_maintenance TEXT DEFAULT '',
    vendor TEXT DEFAULT '',
    pic TEXT DEFAULT '',
    cost_estimate REAL DEFAULT NULL,
    is_active INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ac_maintenance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maintenance_id INTEGER NOT NULL REFERENCES ac_maintenance(id) ON DELETE CASCADE,
    done_at TEXT DEFAULT '',
    done_by TEXT DEFAULT '',
    cost REAL DEFAULT NULL,
    result TEXT DEFAULT 'OK',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS pc_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    client TEXT DEFAULT '',
    customer_id INTEGER DEFAULT NULL REFERENCES sc_customers(id) ON DELETE SET NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    start_date TEXT DEFAULT NULL,
    end_date TEXT DEFAULT NULL,
    pic_id INTEGER DEFAULT NULL REFERENCES employees(id) ON DELETE SET NULL,
    implementor_id INTEGER DEFAULT NULL REFERENCES employees(id) ON DELETE SET NULL,
    co_leader_id INTEGER DEFAULT NULL REFERENCES employees(id) ON DELETE SET NULL,
    pic_ext TEXT DEFAULT '',
    implementor_ext TEXT DEFAULT '',
    co_leader_ext TEXT DEFAULT '',
    color TEXT DEFAULT '#0ea5e9',
    created_by INTEGER DEFAULT NULL REFERENCES users(id),
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS pc_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pc_projects(id) ON DELETE CASCADE,
    employee_id INTEGER DEFAULT NULL REFERENCES employees(id) ON DELETE CASCADE,
    name_ext TEXT DEFAULT '',
    role TEXT DEFAULT 'developer'
);
CREATE TABLE IF NOT EXISTS pc_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pc_projects(id) ON DELETE CASCADE,
    issue_no TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    role TEXT DEFAULT '',
    menu TEXT DEFAULT '',
    stage TEXT DEFAULT '',
    solution_type TEXT DEFAULT '',
    priority TEXT DEFAULT 'Medium',
    severity TEXT DEFAULT 'Medium',
    difficulty TEXT DEFAULT 'Normal',
    issued_type TEXT DEFAULT 'Bugs',
    issued_date TEXT DEFAULT NULL,
    issued_by TEXT DEFAULT '',
    pic_programmer_id INTEGER DEFAULT NULL REFERENCES employees(id) ON DELETE SET NULL,
    pic_tester_id INTEGER DEFAULT NULL REFERENCES employees(id) ON DELETE SET NULL,
    md_days REAL DEFAULT NULL,
    plan_hours REAL DEFAULT NULL,
    bobot_plan REAL DEFAULT 0,
    bobot_actual REAL DEFAULT 0,
    status_programmer TEXT DEFAULT 'New',
    status_testing TEXT DEFAULT '',
    testing_date TEXT DEFAULT NULL,
    resolved_date TEXT DEFAULT NULL,
    notes TEXT DEFAULT '',
    redmine TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS pc_issue_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES pc_issues(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    old_value TEXT DEFAULT '',
    new_value TEXT DEFAULT '',
    changed_by INTEGER DEFAULT NULL REFERENCES users(id),
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS pc_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pc_projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    due_date TEXT NOT NULL,
    status TEXT DEFAULT 'upcoming',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS pc_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pc_projects(id) ON DELETE CASCADE,
    milestone_id INTEGER DEFAULT NULL REFERENCES pc_milestones(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'backlog',
    priority TEXT DEFAULT 'Medium',
    due_date TEXT DEFAULT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS pc_task_assignees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES pc_tasks(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    UNIQUE(task_id, employee_id)
);
CREATE TABLE IF NOT EXISTS pc_proposed_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pc_projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    module TEXT DEFAULT '',
    pekerjaan TEXT DEFAULT '',
    impact TEXT DEFAULT 'Medium',
    difficulty TEXT DEFAULT 'Normal',
    status TEXT DEFAULT 'proposed',
    tester TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS pc_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES pc_projects(id) ON DELETE CASCADE,
    phase_type TEXT DEFAULT 'custom',
    name TEXT NOT NULL,
    start_date TEXT DEFAULT NULL,
    end_date TEXT DEFAULT NULL,
    status TEXT DEFAULT 'planned',
    sort_order INTEGER DEFAULT 0,
    pic_id INTEGER DEFAULT NULL REFERENCES employees(id) ON DELETE SET NULL,
    pic_ext TEXT DEFAULT '',
    sign_off_date TEXT DEFAULT NULL,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- ─── Task Performance Config ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_perf_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    base_points REAL DEFAULT 0,
    priority_critical REAL DEFAULT 2.0,
    priority_high REAL DEFAULT 1.5,
    priority_medium REAL DEFAULT 1.0,
    priority_low REAL DEFAULT 0.7,
    ontime_mult REAL DEFAULT 1.1,
    late_mult REAL DEFAULT 0.9,
    sort_order INTEGER DEFAULT 0
);
"""

MIGRATIONS = [
    ('users', 'email',     "TEXT DEFAULT ''"),
    ('users', 'google_id', "TEXT DEFAULT ''"),
    ('employees', 'level',           "TEXT DEFAULT 'Staff'"),
    ('employees', 'employment_type', "TEXT DEFAULT 'tetap'"),
    ('employees', 'contract_start',  "TEXT DEFAULT ''"),
    ('employees', 'contract_end',    "TEXT DEFAULT ''"),
    ('employees', 'rate_mandays',    'REAL DEFAULT NULL'),
    ('employees', 'email',           "TEXT DEFAULT ''"),
    ('employees', 'phone',           "TEXT DEFAULT ''"),
    ('employees', 'telegram_id',     "TEXT DEFAULT ''"),
    ('employees', 'is_active',       "INTEGER DEFAULT 1"),
    ('employees', 'notes',           "TEXT DEFAULT ''"),
    ('employees', 'supervisor_id',   'INTEGER DEFAULT NULL'),
    ('employees', 'leader_id',       'INTEGER DEFAULT NULL'),
    ('employees', 'manager_id',      'INTEGER DEFAULT NULL'),
    ('employees', 'user_id',         'INTEGER DEFAULT NULL'),
    ('evaluations', 'self_notes',        "TEXT DEFAULT ''"),
    ('evaluations', 'self_achievements', "TEXT DEFAULT ''"),
    ('evaluations', 'self_improvements', "TEXT DEFAULT ''"),
    ('evaluations', 'review_status',     "TEXT DEFAULT 'draft'"),
    ('evaluations', 'reviewed_by',       'INTEGER DEFAULT NULL'),
    ('evaluations', 'reviewed_at',       "TEXT DEFAULT ''"),
    ('evaluations', 'review_notes',      "TEXT DEFAULT ''"),
    ('users',       'totp_secret',       "TEXT DEFAULT ''"),
    ('users',       'mfa_enabled',       "INTEGER DEFAULT 0"),
    ('users',       'email',             "TEXT DEFAULT ''"),
    ('users',       'phone',             "TEXT DEFAULT ''"),
    ('users',       'telegram_id',       "TEXT DEFAULT ''"),
    ('employees',   'salary',            "TEXT DEFAULT ''"),
    ('employee_salary', 'increase_date', "TEXT DEFAULT ''"),
    ('roles',              'app_slug',               "TEXT DEFAULT 'evaluasi'"),
    ('sc_contracts',       'pic_helpdesk_id',         'INTEGER DEFAULT NULL'),
    ('sc_customers',       'pic_helpdesk_id',         'INTEGER DEFAULT NULL'),
    ('sc_customers',       'pic_helpdesk_backup_id',  'INTEGER DEFAULT NULL'),
    ('sc_customers',       'pic_implementor_id',      'INTEGER DEFAULT NULL'),
    ('sc_customers',       'pic_coleader_id',         'INTEGER DEFAULT NULL'),
    ('sc_customers',       'telegram_group_id',       "TEXT DEFAULT ''"),
    ('sc_tickets',         'module_id',               'INTEGER DEFAULT NULL'),
    ('sc_tickets',         'assignee_id',             'INTEGER DEFAULT NULL'),
    ('sc_tickets',         'status_note',             "TEXT DEFAULT ''"),
    ('sc_tickets',         'mandays',                 'REAL DEFAULT NULL'),
    ('sc_tickets',         'pct_done',                'INTEGER DEFAULT 0'),
    ('sc_tickets',         'solution_type',           "TEXT DEFAULT ''"),
    ('sc_tickets',         'solution_note',           "TEXT DEFAULT ''"),
    ('sc_tickets',         'due_date',                "TEXT DEFAULT NULL"),
    ('sc_tickets',         'work_start_date',         "TEXT DEFAULT NULL"),
    ('sc_tickets',         'media_lapor',             "TEXT DEFAULT ''"),
    ('sc_tickets',         'priority',                "TEXT DEFAULT 'Medium'"),
    ('sc_customers',       'customer_type',           "TEXT DEFAULT 'aktif'"),
    ('sc_customers',       'pic_sales_id',            'INTEGER DEFAULT NULL'),
    ('sc_sla_categories',  'workaround_time_hours',   'REAL DEFAULT NULL'),
    ('sc_sla_categories',  'maintenance_type',        "TEXT DEFAULT 'corrective'"),
    ('sc_sla_categories',  'priority',                "TEXT DEFAULT 'Medium'"),
    ('ac_assets',            'manual_employee_name',    "TEXT DEFAULT ''"),
    ('ac_assets',            'status',                  "TEXT DEFAULT 'Aktif'"),
    ('ac_assets',            'started_using',           "TEXT DEFAULT ''"),
    ('ac_subscriptions',     'last_reminder_sent',      "TEXT DEFAULT ''"),
    ('ac_infrastructure',    'updated_at',              "TEXT DEFAULT ''"),
    ('ac_licenses',          'updated_at',              "TEXT DEFAULT ''"),
    ('ac_subscriptions',     'updated_at',              "TEXT DEFAULT ''"),
    ('ac_software_requests', 'updated_at',              "TEXT DEFAULT ''"),
    ('bk_resources',         'image',                   "TEXT DEFAULT ''"),
    ('bk_resources',         'facilities',              "TEXT DEFAULT ''"),
    ('bk_resources',         'notes',                   "TEXT DEFAULT ''"),
    ('pc_projects',          'customer_id',             'INTEGER DEFAULT NULL'),
    ('pc_projects',          'implementor_id',          'INTEGER DEFAULT NULL'),
    ('pc_projects',          'co_leader_id',            'INTEGER DEFAULT NULL'),
    ('pc_projects',          'pic_ext',                 "TEXT DEFAULT ''"),
    ('pc_projects',          'implementor_ext',         "TEXT DEFAULT ''"),
    ('pc_projects',          'co_leader_ext',           "TEXT DEFAULT ''"),
    ('pc_members',           'name_ext',                "TEXT DEFAULT ''"),
    ('pc_projects',          'deleted_at',              'TEXT DEFAULT NULL'),
    ('pc_phases',            'pic_ext',                 "TEXT DEFAULT ''"),
    ('pc_phases',            'sign_off_date',           "TEXT DEFAULT NULL"),
    ('pc_phases',            'app_id',                  'INTEGER DEFAULT NULL'),
    ('pc_phases',            'module_id',               'INTEGER DEFAULT NULL'),
    ('evaluations',          'task_score',              'REAL DEFAULT NULL'),
    ('evaluations',          'task_date_from',          "TEXT DEFAULT ''"),
    ('evaluations',          'task_date_to',            "TEXT DEFAULT ''"),
    ('evaluations',          'task_benchmark',          'REAL DEFAULT 100'),
]

SC_TICKET_PRIORITIES = [
    ('Critical', 'Critical', 'danger'),
    ('High',     'High',     'warning'),
    ('Medium',   'Medium',   'primary'),
    ('Low',      'Low',      'secondary'),
]

SC_TICKET_STATUSES = [
    ('new',         'Baru',        'secondary'),
    ('in_progress', 'In Progress', 'primary'),
    ('hold',        'Hold',        'warning'),
    ('resolved',    'Resolved',    'success'),
    ('feedback',    'Feedback',    'info'),
    ('closed',      'Closed',      'dark'),
    ('rejected',    'Rejected',    'danger'),
]

SC_SOLUTION_TYPES = [
    ('workaround_restart',    'Workaround — Restart Aplikasi'),
    ('workaround_db',         'Workaround — DB'),
    ('workaround_coding',     'Workaround — Coding'),
    ('workaround_suggestion', 'Workaround — Suggestion'),
    ('final_db',              'Final — DB'),
    ('final_coding',          'Final — Coding'),
    ('final_suggestion',      'Final — Suggestion'),
]

SC_MEDIA_LAPOR = [
    ('wa_helpdesk', 'WA ke Nomor Helpdesk'),
    ('email',       'Email'),
    ('telegram',    'Telegram'),
    ('wa_pic',      'WA Pribadi PIC Helpdesk'),
]

PC_ISSUE_STATUSES_PRG = [
    ('New',           'New',            'secondary'),
    ('In Progress',   'In Progress',    'primary'),
    ('Done',          'Done',           'success'),
    ('Ready to Test', 'Ready to Test',  'info'),
    ('Need Deploy',   'Need Deploy',    'warning'),
    ('Hold',          'Hold',           'dark'),
    ('Feedback',      'Feedback',       'danger'),
]
PC_ISSUE_STATUSES_TEST = [
    ('',         '—',        'light'),
    ('Testing',  'Testing',  'primary'),
    ('Done',     'Done',     'success'),
    ('Feedback', 'Feedback', 'warning'),
    ('Reject',   'Reject',   'danger'),
]
PC_PRIORITIES  = ['Low', 'Medium', 'High', 'Critical']
PC_SEVERITIES  = ['Low', 'Medium', 'High', 'Critical']
PC_DIFFICULTIES= ['Easy', 'Normal', 'Hard', 'Very Hard']
PC_ISSUED_TYPES= ['Bugs', 'New Feature', 'Enhancement', 'Change Request']
PC_SOLUTION_TYPES = ['Coding', 'Config', 'DB', 'Design', 'Documentation', 'Other']
PC_TASK_STATUSES = [
    ('backlog',     'Backlog',     '#6b7280'),
    ('todo',        'To Do',       '#3b82f6'),
    ('in_progress', 'In Progress', '#f59e0b'),
    ('review',      'Review',      '#8b5cf6'),
    ('done',        'Done',        '#10b981'),
]
PC_MILESTONE_STATUSES = [
    ('upcoming',     'Upcoming',     'secondary'),
    ('in_progress',  'In Progress',  'primary'),
    ('completed',    'Completed',    'success'),
    ('delayed',      'Delayed',      'danger'),
]
PC_PROPOSED_STATUSES = [
    ('proposed',    'Proposed',     'secondary'),
    ('approved',    'Approved',     'success'),
    ('in_progress', 'In Progress',  'primary'),
    ('done',        'Done',         'success'),
    ('rejected',    'Rejected',     'danger'),
    ('hold',        'Hold',         'warning'),
]
PC_PHASE_TYPES = [
    ('sit',     'SIT',                   '#3b82f6'),
    ('uat',     'UAT',                   '#8b5cf6'),
    ('bast',    'BAST',                  '#f59e0b'),
    ('promote', 'Promote to Production', '#ef4444'),
    ('golive',  'Go Live',               '#10b981'),
    ('custom',  'Custom',                '#6b7280'),
]
PC_PHASE_STATUSES = [
    ('planned',     'Planned',     'secondary'),
    ('in_progress', 'In Progress', 'primary'),
    ('done',        'Done',        'success'),
    ('skipped',     'Skipped',     'dark'),
]
PC_PROJECT_COLORS = [
    '#0ea5e9','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#0d9488','#6366f1',
]

DEFAULT_SETTINGS = {
    'smtp_host': '',
    'smtp_port': '587',
    'smtp_user': '',
    'smtp_password': '',
    'smtp_from': '',
    'smtp_ssl': '0',
    'telegram_bot_token': '',
    'telegram_default_chat_id': '',
    'reminder_days': '30,14,7,1',
    'reminder_enabled': '1',
    'app_name': 'TalentCore',
    'notification_emails': '',
    'notification_telegram_ids': '',
    'openwa_url': '',
    'openwa_api_key': '',
    'openwa_session_id': 'default',      # session fallback jika app tidak punya session sendiri
    'openwa_session_evaluasi': '',        # TalentCore
    'openwa_session_support': '',         # SupportCore
    'openwa_session_booking': '',         # BookingCore
    'openwa_session_aset': '',            # AssetCore
    'openwa_enabled': '0',
    'openwa_extra_phones': '',
    'app_url': '',  # URL publik aplikasi mis. https://evaluasi.perusahaan.com (kosong = auto-detect)
    'ac_sub_reminder_enabled': '1',
    'ac_sub_reminder_days': '30,14,7,1',
    'ac_notification_emails': '',
    'ac_notification_telegram_ids': '',
    'ac_notification_wa_phones': '',
    # Update Center
    'update_check_enabled':  '1',           # cek update otomatis
    'update_notify_roles':   'superadmin,admin',  # role yang dapat notifikasi
    'update_trigger_roles':  'superadmin',  # role yang bisa trigger update
    'update_available':      '0',
    'update_latest_version': '',
    'update_latest_tag':     '',
    'update_release_notes':  '',
    'update_check_last':     '',
    'github_repo':           'barangbaru/solid-apps',
    # AI Chatbot
    'chatbot_enabled':       '0',
    'chatbot_roles':         'superadmin,admin,user',
    'ai_provider':           'anthropic',      # anthropic | openai | openai_compat
    'ai_api_key':            '',
    'ai_model':              '',               # kosong = pakai default per provider
    'ai_base_url':           '',               # hanya untuk openai_compat
    # backward compat
    'anthropic_api_key':     '',
}

LEVEL_CHOICES = ['Staff', 'Senior Staff', 'Co-Leader', 'Leader', 'Manager', 'Senior Manager', 'General Manager', 'Director']

# Permissions per-app. Portal-level (manage_users, manage_roles) dikelola via superadmin.
APP_PERMISSIONS = {
    'evaluasi': {
        'manage_settings':    'Pengaturan notifikasi sistem',
        'manage_template':    'Edit template evaluasi (skill/kompetensi/ability)',
        'manage_employees':   'Tambah/edit/hapus data karyawan',
        'manage_divisions':   'Kelola divisi',
        'manage_evaluations': 'Buat/hapus evaluasi karyawan',
        'view_evaluations':   'Lihat hasil evaluasi',
        'send_reminders':     'Kirim reminder kontrak',
        'view_salary':        'Lihat data gaji karyawan',
        'manage_salary':      'Edit data gaji karyawan',
    },
    'aset': {
        'ac_view':            'Lihat data AssetCore',
        'ac_manage_assets':   'Kelola inventaris laptop/PC',
        'ac_manage_infra':    'Kelola inventaris infrastruktur',
        'ac_manage_licenses': 'Kelola lisensi software',
        'ac_manage_subs':     'Kelola subscription & ISP',
        'ac_manage_requests': 'Kelola request software',
    },
    'support': {
        'sc_view':             'Lihat data SupportCore',
        'sc_manage_customers': 'Kelola master customer',
        'sc_manage_services':  'Kelola master layanan/jasa',
        'sc_manage_types':     'Kelola master tipe support',
        'sc_manage_sla':       'Kelola master kategori SLA',
        'sc_manage_apps':      'Kelola master aplikasi & modul',
        'sc_manage_contracts': 'Kelola kontrak support tahunan',
        'sc_manage_tickets':   'Buat/update tiket support',
        'sc_manage_presales':  'Kelola request Presales & POC',
        'sc_view_reports':     'Lihat laporan & monitoring SLA',
    },
}
# Backward-compat: ALL_PERMISSIONS = gabungan semua app + portal perms
ALL_PERMISSIONS = {
    'manage_users':  'Kelola pengguna (tambah/edit/hapus)',
    'manage_roles':  'Kelola role dan permission',
}
for _perms in APP_PERMISSIONS.values():
    ALL_PERMISSIONS.update(_perms)

# Permission yang hanya bisa diset oleh superadmin (admin tidak boleh assign ke role)
CRITICAL_PERMISSIONS = {'view_salary', 'manage_salary', 'manage_users', 'manage_roles'}

SYSTEM_ROLE_DEFAULTS = {
    'superadmin': list(ALL_PERMISSIONS.keys()),
    'admin': ['manage_employees','manage_divisions','manage_evaluations',
              'view_evaluations','send_reminders','manage_template',
              'sc_view','sc_manage_customers','sc_manage_services','sc_manage_types','sc_manage_sla',
              'sc_manage_apps','sc_manage_contracts','sc_manage_tickets','sc_manage_presales','sc_view_reports',
              'ac_view','ac_manage_assets','ac_manage_infra','ac_manage_licenses','ac_manage_subs','ac_manage_requests'],
    'viewer': ['view_evaluations','sc_view','sc_view_reports','ac_view'],
}

def _pg_adapt_schema(schema):
    """Konversi DDL SQLite → PostgreSQL."""
    import re
    s = schema
    # AUTOINCREMENT → SERIAL (untuk kolom id)
    s = re.sub(r'\bINTEGER PRIMARY KEY AUTOINCREMENT\b', 'SERIAL PRIMARY KEY', s, flags=re.IGNORECASE)
    # datetime('now','localtime') → NOW()
    s = re.sub(r"datetime\('now',\s*'localtime'\)", 'NOW()', s, flags=re.IGNORECASE)
    # SQLite CREATE INDEX IF NOT EXISTS sudah kompatibel dengan PG
    return s


def _pg_column_exists(db, table, col):
    """Cek apakah kolom ada di PostgreSQL via information_schema."""
    row = db.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, col)).fetchone()
    return row is not None


def init_db():
    if DB_TYPE == 'postgresql':
        conn = _pg_connect()
        conn.autocommit = False
        db = _DBWrapper(conn, is_pg=True)
    else:
        raw = sqlite3.connect(DB_PATH)
        db = _DBWrapper(raw, is_pg=False)

    schema_sql = _pg_adapt_schema(SCHEMA) if DB_TYPE == 'postgresql' else SCHEMA
    db.executescript(schema_sql)
    db.commit()

    # Migrations
    for table, col, col_def in MIGRATIONS:
        if DB_TYPE == 'postgresql':
            # Konversi tipe kolom SQLite → PostgreSQL jika perlu
            col_def_pg = col_def.replace('INTEGER', 'INTEGER').replace(
                "datetime('now','localtime')", 'NOW()')
            if not _pg_column_exists(db, table, col):
                db.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_def_pg}')
        else:
            existing = [r[1] for r in db._conn.execute(f'PRAGMA table_info({table})').fetchall()]
            if col not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_def}')
    db.commit()
    # Seed evaluation data
    if db.execute('SELECT COUNT(*) FROM skill_categories').fetchone()[0] == 0:
        seed_db(db)
    # Default settings
    for k, v in DEFAULT_SETTINGS.items():
        db.execute('INSERT OR IGNORE INTO app_settings(key, value) VALUES(?,?)', (k, v))
    db.commit()
    # Default superadmin
    if db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        db.execute('''INSERT INTO users(username, password_hash, full_name, role)
                      VALUES(?,?,?,?)''',
                   ('superadmin', generate_password_hash('Admin@123'), 'Super Administrator', 'superadmin'))
        db.commit()
        print("=" * 55)
        print(" SUPERADMIN DIBUAT:")
        print("   Username : superadmin")
        print("   Password : Admin@123")
        print(" !! Segera ganti password setelah login !!")
        print("=" * 55)
    # Migrate old level name
    db.execute("UPDATE employees SET level='Leader' WHERE level='Team Lead'")
    # Seed divisions
    if db.execute('SELECT COUNT(*) FROM divisions').fetchone()[0] == 0:
        for i, name in enumerate(ALL_DIVISIONS.keys()):
            db.execute('INSERT OR IGNORE INTO divisions(name, sort_order) VALUES(?,?)', (name, i))
    # Seed superapp apps registry
    _apps = [
        ('evaluasi', 'TalentCore', 'Penilaian & review kinerja karyawan',
         'clipboard2-check', '#4da8da', '#e8f4fd', '/', 1, 0, 0, ''),
        ('aset', 'AssetCore', 'Pencatatan & tracking aset perusahaan',
         'box-seam', '#6f42c1', '#f0ecff', '/aset/', 1, 0, 1, 'ac_view'),
        ('support', 'SupportCore', 'Monitoring technical support, SLA & presales',
         'headset', '#0d9488', '#e6faf8', '/support/', 1, 0, 2, ''),
        ('booking', 'BookingCore', 'Pemesanan & penjadwalan ruangan, kendaraan & aset',
         'calendar2-check', '#d97706', '#fff8e1', '/booking/', 1, 0, 3, ''),
        ('project', 'ProjectCore', 'Manajemen proyek, task & timeline tim',
         'kanban', '#0ea5e9', '#e0f2fe', '/project/', 1, 0, 4, ''),
        ('docs', 'DocsCore', 'Pengelolaan dokumen, SOP & knowledge base perusahaan',
         'file-earmark-richtext', '#10b981', '#d1fae5', '/docs/', 1, 1, 5, ''),
        ('finance', 'FinanceCore', 'Pencatatan keuangan, anggaran & laporan finansial',
         'cash-coin', '#f59e0b', '#fef3c7', '/finance/', 1, 1, 6, ''),
        ('helpdesk', 'HelpdeskCore', 'Helpdesk internal karyawan & manajemen tiket IT',
         'headset', '#8b5cf6', '#ede9fe', '/helpdesk/', 1, 1, 7, ''),
    ]
    for slug, name, desc, icon, color, bg, url, active, soon, sort, perm in _apps:
        db.execute('''INSERT INTO superapp_apps
            (slug,name,description,icon,color,bg_color,url,is_active,is_coming_soon,sort_order,required_permission)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                icon=excluded.icon,
                color=excluded.color,
                bg_color=excluded.bg_color,
                url=excluded.url,
                is_active=excluded.is_active,
                is_coming_soon=excluded.is_coming_soon,
                sort_order=excluded.sort_order,
                required_permission=excluded.required_permission''',
            (slug, name, desc, icon, color, bg, url, active, soon, sort, perm))
    db.commit()
    # Seed system roles sebagai global (app_slug='')
    for rname, rdesc, rsys in [('superadmin','Super Administrator',1),('admin','Administrator',1),('viewer','Viewer Read-Only',1)]:
        db.execute('INSERT OR IGNORE INTO roles(name,description,is_system,app_slug) VALUES(?,?,?,?)', (rname, rdesc, rsys, ''))
    # Migrate existing system roles agar global
    db.execute("UPDATE roles SET app_slug='' WHERE is_system=1 AND (app_slug='evaluasi' OR app_slug IS NULL)")
    for rname, perms in SYSTEM_ROLE_DEFAULTS.items():
        for perm in perms:
            db.execute('INSERT OR IGNORE INTO role_permissions(role_name,permission) VALUES(?,?)', (rname, perm))
    # Seed default support types
    for st_code, st_name, st_desc in [
        ('CORRECTIVE', 'Corrective Support', 'Penanganan gangguan/kerusakan sistem yang sudah terjadi'),
        ('PREVENTIVE', 'Preventive Support', 'Pemeliharaan rutin untuk mencegah kerusakan/gangguan'),
        ('ONSITE',     'Onsite Support',     'Kunjungan langsung ke lokasi customer'),
    ]:
        db.execute('INSERT OR IGNORE INTO sc_support_types(code,name,description) VALUES(?,?,?)',
                   (st_code, st_name, st_desc))
    db.commit()
    # Seed SLA Categories best practice (INSERT OR IGNORE — aman dijalankan ulang)
    _sla_seed = [
        # code, name, priority, response_h, workaround_h, resolution_h, mtype, description
        # ── CORRECTIVE ──────────────────────────────────────────────────────────────────
        ('COR-P1', 'Corrective P1 — Critical (System Down)',
         'Critical', 1, 4, 24, 'corrective',
         'Sistem tidak dapat diakses sama sekali / data corrupt. Berdampak ke seluruh user & operasional. '
         'Workaround wajib dalam 4 jam, solusi final dalam 1 hari kerja. Eskalasi immediate ke manajemen.'),
        ('COR-P2', 'Corrective P2 — High (Major Feature Broken)',
         'High', 2, 8, 72, 'corrective',
         'Fitur utama tidak berfungsi, operasional terganggu signifikan namun sistem masih bisa diakses sebagian. '
         'Workaround dalam 8 jam, solusi final dalam 3 hari kerja.'),
        ('COR-P3', 'Corrective P3 — Medium (Minor Feature Impaired)',
         'Medium', 4, 24, 120, 'corrective',
         'Fitur minor tidak berfungsi namun ada workaround manual yang dapat digunakan user. '
         'Workaround dalam 1 hari kerja, solusi final dalam 5 hari kerja.'),
        ('COR-P4', 'Corrective P4 — Low (Cosmetic / Minor Bug)',
         'Low', 8, 48, 240, 'corrective',
         'Bug minor / kosmetik, tidak mengganggu operasional. Antrian normal sesuai sprint. '
         'Workaround dalam 2 hari kerja, solusi final dalam 10 hari kerja.'),
        # ── PREVENTIVE ──────────────────────────────────────────────────────────────────
        ('PREV-CRIT', 'Preventive — Emergency Security Patch (Zero-Day)',
         'Critical', 2, None, 24, 'preventive',
         'Patch darurat untuk kerentanan zero-day atau exploit aktif yang mengancam keamanan data. '
         'Respons dalam 2 jam, patch diterapkan dalam 1 hari kerja meskipun di luar jam operasional.'),
        ('PREV-PATCH', 'Preventive — Security Patch & Update (Terjadwal)',
         'High', 8, None, 48, 'preventive',
         'Patch keamanan rutin, update library, atau upgrade versi minor. Dilakukan terjadwal dengan persetujuan customer. '
         'Konfirmasi jadwal dalam 8 jam, selesai dalam 2 hari kerja.'),
        ('PREV-STD', 'Preventive — Pemeliharaan Rutin',
         'Medium', 24, None, 72, 'preventive',
         'Pemeliharaan terjadwal: optimasi database, pembersihan log, cek performa, backup verification, monitoring review. '
         'Terjadwal bulanan/triwulan. Konfirmasi dalam 1 hari, selesai dalam 3 hari kerja.'),
        ('PREV-OPT', 'Preventive — Optimasi & Refactor Minor',
         'Low', 48, None, 168, 'preventive',
         'Optimasi performa non-urgent, refactor kode minor, update dokumentasi teknis, atau peningkatan kecil. '
         'Direncanakan dalam sprint berikutnya. Selesai dalam 7 hari kerja.'),
        # ── ONSITE ──────────────────────────────────────────────────────────────────────
        ('ONS-CRIT', 'Onsite — Critical Emergency',
         'Critical', 2, None, 4, 'onsite',
         'Kunjungan onsite darurat untuk gangguan kritikal infrastruktur / hardware yang tidak dapat diselesaikan remote sama sekali. '
         'Tim berangkat dalam 2 jam, target selesai dalam 4 jam sejak tiba di lokasi.'),
        ('ONS-URG', 'Onsite — Urgent',
         'High', 4, None, 8, 'onsite',
         'Kunjungan onsite urgen untuk masalah yang berdampak signifikan dan tidak dapat diselesaikan remote. '
         'Tim onsite di lokasi dalam 4 jam, penyelesaian dalam 8 jam sejak tiba.'),
        ('ONS-STD', 'Onsite — Standard',
         'Medium', 8, None, 24, 'onsite',
         'Kunjungan onsite terjadwal: training user, setup perangkat, instalasi, atau pendampingan rutin. '
         'Konfirmasi jadwal dalam 8 jam, pelaksanaan selesai dalam 1 hari kerja.'),
        ('ONS-PLAN', 'Onsite — Planned / Scheduled Visit',
         'Low', 24, None, 72, 'onsite',
         'Kunjungan onsite terencana: audit sistem, demo fitur baru, workshop, atau review berkala. '
         'Penjadwalan dalam 1 hari kerja, pelaksanaan disesuaikan kalender customer, selesai dalam 3 hari kerja.'),
    ]
    for code, name, prio, resp, wta, reso, mtype, desc in _sla_seed:
        db.execute('''INSERT OR IGNORE INTO sc_sla_categories
            (code,name,priority,response_time_hours,workaround_time_hours,resolution_time_hours,maintenance_type,description)
            VALUES(?,?,?,?,?,?,?,?)''', (code, name, prio, resp, wta, reso, mtype, desc))
    db.commit()
    # Seed default task_perf_config
    _tpc_seed = [
        # (task_type, label, base_pts, crit, high, med, low, ontime, late, sort)
        ('project_lead',      'Project (PIC/Lead)',       15, 2.0, 1.5, 1.0, 0.7, 1.1, 0.9, 1),
        ('project_impl',      'Project (Implementor)',    10, 2.0, 1.5, 1.0, 0.7, 1.1, 0.9, 2),
        ('project_member',    'Project (Member Tim)',      7, 2.0, 1.5, 1.0, 0.7, 1.1, 0.9, 3),
        ('project_issue',     'Issue Project (Programmer)',3, 2.0, 1.5, 1.0, 0.5, 1.1, 0.9, 4),
        ('project_task',      'Task ProjectCore (Done)',   2, 2.0, 1.5, 1.0, 0.7, 1.1, 0.9, 5),
        ('poc_presales',      'POC / Presales',            5, 2.0, 1.5, 1.0, 0.7, 1.1, 0.9, 6),
        ('support_ticket',    'Tiket Support (Closed)',    2, 2.0, 1.5, 1.0, 0.7, 1.1, 0.9, 7),
    ]
    for row in _tpc_seed:
        db.execute('''INSERT OR IGNORE INTO task_perf_config
            (task_type,label,base_points,priority_critical,priority_high,priority_medium,priority_low,ontime_mult,late_mult,sort_order)
            VALUES(?,?,?,?,?,?,?,?,?,?)''', row)
    db.commit()

    # Pastikan superadmin punya akses ke semua app (hanya jika belum ada)
    sa = db.execute("SELECT id FROM users WHERE role='superadmin' LIMIT 1").fetchone()
    if sa:
        for _slug in ['evaluasi', 'aset', 'support', 'booking']:
            db.execute('''INSERT OR IGNORE INTO user_app_access(user_id,app_slug,app_role,is_active)
                VALUES(?,?,?,1)''', (sa[0], _slug, 'superadmin'))
    db.commit()
    # Database indexes untuk kolom yang sering di-WHERE/JOIN
    _indexes = [
        ('idx_employees_is_active',       'employees',              'is_active'),
        ('idx_evaluations_employee_id',   'evaluations',            'employee_id'),
        ('idx_evaluations_review_status', 'evaluations',            'review_status'),
        ('idx_eval_tokens_eval_id',       'eval_tokens',            'eval_id'),
        ('idx_reminder_logs_employee',    'reminder_logs',          'employee_id'),
        ('idx_audit_created_at',          'audit_activity',         'created_at'),
        ('idx_audit_app_slug',            'audit_activity',         'app_slug'),
        ('idx_user_app_access_user',      'user_app_access',        'user_id, app_slug'),
        ('idx_sc_tickets_customer',       'sc_tickets',             'customer_id'),
        ('idx_sc_tickets_status',         'sc_tickets',             'status'),
        ('idx_sc_tickets_reported_at',    'sc_tickets',             'reported_at'),
        ('idx_sc_assignees_ticket',       'sc_ticket_assignees',    'ticket_id'),
        ('idx_sc_attachments_ticket',     'sc_ticket_attachments',  'ticket_id'),
        ('idx_sc_ext_assignees_ticket',   'sc_ticket_external_assignees', 'ticket_id'),
        ('idx_pc_ext_assignees_task',     'pc_task_external_assignees',   'task_id'),
        ('idx_sc_presales_assignees',     'sc_presales_assignees',  'request_id'),
        ('idx_bk_bookings_resource',      'bk_bookings',            'resource_id, start_dt'),
        ('idx_bk_bookings_booked_by',     'bk_bookings',            'booked_by'),
        ('idx_bk_bookings_parent',        'bk_bookings',            'parent_id'),
    ]
    for idx_name, tbl, cols in _indexes:
        db.execute(f'CREATE INDEX IF NOT EXISTS {idx_name} ON {tbl}({cols})')
    db.commit()
    # Hapus duplikat bk_resources — pertahankan baris dengan id terkecil per nama
    db.execute('''DELETE FROM bk_resources WHERE id NOT IN (
        SELECT MIN(id) FROM bk_resources GROUP BY name)''')
    # Seed booking resources hanya jika belum ada data
    if db.execute('SELECT COUNT(*) FROM bk_resources').fetchone()[0] == 0:
        _resources = [
            ('Big Meeting Room', 'room', 'meeting', 20, 'Ruang rapat utama kapasitas besar', 'Lantai 3', '#0d6efd', 'people-fill', 1),
            ('Small Meeting Room A', 'room', 'meeting', 8, 'Ruang rapat kecil A', 'Lantai 2', '#6f42c1', 'door-open', 2),
            ('Small Meeting Room B', 'room', 'meeting', 8, 'Ruang rapat kecil B', 'Lantai 2', '#20c997', 'door-open', 3),
            ('Lounge Room', 'room', 'lounge', 15, 'Area lounge untuk diskusi santai', 'Lantai 1', '#fd7e14', 'cup-hot-fill', 4),
            ('Mobil Operasional', 'vehicle', 'car', 6, 'Kendaraan operasional perusahaan', 'Parkir Basement', '#d97706', 'car-front-fill', 5),
        ]
        for name, rtype, subtype, cap, desc, loc, color, icon, sort in _resources:
            db.execute('''INSERT INTO bk_resources(name,type,subtype,capacity,description,location,color,icon,sort_order)
                VALUES(?,?,?,?,?,?,?,?,?)''', (name, rtype, subtype, cap, desc, loc, color, icon, sort))
    # Seed booking items (minuman, makanan) hanya jika belum ada
    if db.execute('SELECT COUNT(*) FROM bk_items').fetchone()[0] == 0:
        _items = [
            ('Air Mineral', 'minuman', 'Botol air mineral 600ml', 'droplet-fill', 1),
            ('Kopi', 'minuman', 'Kopi hitam / kopi susu', 'cup-hot-fill', 2),
            ('Teh', 'minuman', 'Teh hangat / teh manis', 'cup-hot', 3),
            ('Jus Buah', 'minuman', 'Jus buah segar', 'cup-straw', 4),
            ('Makanan Ringan', 'makanan', 'Snack / kue ringan', 'bag', 5),
            ('Lunch Box', 'makanan', 'Makan siang kotak', 'box2', 6),
            ('Nasi Box', 'makanan', 'Nasi kotak lengkap', 'grid-3x2', 7),
            ('Buah Potong', 'makanan', 'Buah segar potong', 'basket', 8),
        ]
        for name, cat, desc, icon, sort in _items:
            db.execute('INSERT INTO bk_items(name,category,description,icon,sort_order) VALUES(?,?,?,?,?)',
                       (name, cat, desc, icon, sort))
    db.commit()
    db.close()

def seed_db(db):
    for divisi, (skill_cats, competency_items) in ALL_DIVISIONS.items():
        for order, (cat_name, items) in enumerate(skill_cats):
            cur = db.execute(
                'INSERT INTO skill_categories(divisi, name, sort_order) VALUES(?,?,?)',
                (divisi, cat_name, order))
            cat_id = cur.lastrowid
            for iorder, (name, desc, bobot) in enumerate(items):
                db.execute(
                    'INSERT INTO skill_items(category_id, name, description, bobot, sort_order) VALUES(?,?,?,?,?)',
                    (cat_id, name, desc, bobot, iorder))
        for order, (measurement, bobot, is_hs) in enumerate(competency_items):
            db.execute(
                'INSERT INTO competency_items(divisi, point_measurement, bobot, sort_order, is_hardskill) VALUES(?,?,?,?,?)',
                (divisi, measurement, bobot, order, is_hs))
        for order, (name, a, b, c, d) in enumerate(ABILITY_ITEMS):
            db.execute(
                'INSERT INTO ability_items(divisi, name, desc_a, desc_b, desc_c, desc_d, sort_order) VALUES(?,?,?,?,?,?,?)',
                (divisi, name, a, b, c, d, order))
    db.commit()

# ─── Settings Helpers ──────────────────────────────────────────────────────────

def get_settings(db):
    rows = db.execute('SELECT key, value FROM app_settings').fetchall()
    return {r['key']: r['value'] for r in rows}

def save_setting(db, key, value):
    db.execute('INSERT OR REPLACE INTO app_settings(key, value) VALUES(?,?)', (key, value))

def get_notification_emails(settings, emp_email=''):
    """Gabungkan email karyawan + daftar email statis dari pengaturan."""
    recipients = []
    if emp_email and emp_email.strip():
        recipients.append(emp_email.strip())
    for e in settings.get('notification_emails', '').split(','):
        e = e.strip()
        if e and e not in recipients:
            recipients.append(e)
    return recipients

def normalize_telegram_id(value):
    """Normalisasi chat_id Telegram.
    - Angka (misal 123456789 atau -100123456789) → tetap
    - Username tanpa @ (misal 'grupku') → '@grupku'
    - Sudah ada @ → tetap
    """
    v = value.strip()
    if not v:
        return v
    if v.startswith('@'):
        return v
    if v.lstrip('-').isdigit():
        return v
    return '@' + v

def get_notification_telegram_ids(settings, emp_tg_id='', default_chat=''):
    """Gabungkan telegram_id karyawan + daftar statis + default chat, semua dinormalisasi."""
    ids = []
    if emp_tg_id and emp_tg_id.strip():
        ids.append(normalize_telegram_id(emp_tg_id))
    for t in settings.get('notification_telegram_ids', '').split(','):
        t = normalize_telegram_id(t)
        if t and t not in ids:
            ids.append(t)
    if not ids and default_chat:
        ids.append(normalize_telegram_id(default_chat))
    return ids

def get_notification_wa_phones(settings, emp_phone=''):
    """Gabungkan nomor HP karyawan + daftar extra phones dari pengaturan."""
    phones = []
    if emp_phone and emp_phone.strip():
        phones.append(emp_phone.strip())
    for p in settings.get('openwa_extra_phones', '').split(','):
        p = p.strip()
        if p and p not in phones:
            phones.append(p)
    return phones

def get_openwa_session(settings, app_slug=''):
    """Dapatkan OpenWA session ID untuk app tertentu.
    Prioritas: per-app session → global openwa_session_id → 'default'."""
    if app_slug:
        specific = settings.get(f'openwa_session_{app_slug}', '').strip()
        if specific:
            return specific
    return settings.get('openwa_session_id', 'default').strip() or 'default'

def _parse_list(raw):
    """Parse newline/comma-separated string jadi list string non-kosong."""
    return [x.strip() for x in raw.replace('\n', ',').split(',') if x.strip()]

def get_ac_notification_emails(settings):
    return _parse_list(settings.get('ac_notification_emails', ''))

def get_ac_notification_telegram_ids(settings):
    return [normalize_telegram_id(t) for t in _parse_list(settings.get('ac_notification_telegram_ids', ''))]

def get_ac_notification_wa_phones(settings):
    return _parse_list(settings.get('ac_notification_wa_phones', ''))

# ─── RBAC Helpers ─────────────────────────────────────────────────────────────

def get_role_permissions(db, role_name):
    rows = db.execute('SELECT permission FROM role_permissions WHERE role_name=?', (role_name,)).fetchall()
    return {r['permission'] for r in rows}

def has_permission(role_name, permission, db=None):
    if role_name == 'superadmin':
        return True
    if db is None:
        return False
    return permission in get_role_permissions(db, role_name)

def permission_required(perm):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            db = get_db()
            if not has_permission(session.get('user_role', ''), perm, db):
                flash(f'Akses ditolak — permission "{perm}" diperlukan', 'danger')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated
    return decorator

# ─── MFA Helpers ──────────────────────────────────────────────────────────────

MFA_CHALLENGE_TTL = 900  # 15 menit

def generate_totp_secret():
    return pyotp.random_base32()

def get_totp_uri(secret, username, issuer='TalentCore'):
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)

def verify_totp(secret, code):
    return pyotp.TOTP(secret).verify((code or '').strip(), valid_window=1)

def qr_png_base64(uri):
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

def mfa_session_valid(session_key):
    ts = session.get(session_key, 0)
    return (datetime.now().timestamp() - ts) < MFA_CHALLENGE_TTL

def mfa_challenge_required(session_key='mfa_verified'):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            db = get_db()
            user = db.execute('SELECT mfa_enabled FROM users WHERE id=?', (session['user_id'],)).fetchone()
            if user and user['mfa_enabled'] and not mfa_session_valid(session_key):
                qs = request.query_string.decode()
                session['mfa_return_to']   = request.path + ('?' + qs if qs else '')
                session['mfa_session_key'] = session_key
                return redirect(url_for('mfa_challenge'))
            return f(*args, **kwargs)
        return decorated
    return decorator

def get_chain_contacts(db, emp):
    """Ambil data co-leader (supervisor), leader, dan manager dari employee record.
    Returns list of sqlite3.Row, masing-masing unik dan tidak null."""
    chain_ids = []
    for col in ('supervisor_id', 'leader_id', 'manager_id'):
        try:
            val = emp[col]
        except (IndexError, KeyError):
            val = None
        if val and val not in chain_ids and val != emp['id']:
            chain_ids.append(val)
    if not chain_ids:
        return []
    placeholders = ','.join('?' * len(chain_ids))
    rows = db.execute(
        f'SELECT * FROM employees WHERE id IN ({placeholders}) AND is_active=1',
        chain_ids
    ).fetchall()
    seen, result = set(), []
    for r in rows:
        if r['id'] not in seen:
            seen.add(r['id'])
            result.append(r)
    return result

# ─── Review / Self-Assessment Helpers ─────────────────────────────────────────

def get_or_create_eval_token(db, eval_id):
    row = db.execute('SELECT token FROM eval_tokens WHERE eval_id=?', (eval_id,)).fetchone()
    if row:
        return row['token']
    token = secrets.token_urlsafe(32)
    db.execute('INSERT INTO eval_tokens(eval_id, token) VALUES(?,?)', (eval_id, token))
    db.commit()
    return token

def get_user_emp_id(db, user_id):
    """Return the employee.id linked to this user, or None."""
    row = db.execute('SELECT id FROM employees WHERE user_id=?', (user_id,)).fetchone()
    return row['id'] if row else None

def get_user_contacts(db, user):
    """Return merged contact dict: user fields take priority, fallback ke linked employee."""
    uid = user['id'] if hasattr(user, 'keys') else user.get('id')
    emp = db.execute(
        'SELECT email, phone, telegram_id, name FROM employees WHERE user_id=? AND is_active=1',
        (uid,)
    ).fetchone()
    email    = (user['email']       or '').strip() or (emp['email']       if emp else '')
    phone    = (user['phone']       or '').strip() or (emp['phone']       if emp else '')
    telegram = (user['telegram_id'] or '').strip() or (emp['telegram_id'] if emp else '')
    return {
        'email':       email,
        'phone':       phone,
        'telegram_id': telegram,
        'emp_name':    emp['name'] if emp else '',
        'linked_emp':  emp is not None,
    }

def can_review_eval(db, user_id, user_role, eval_id):
    if user_role == 'superadmin':
        return True
    eid = get_user_emp_id(db, user_id)
    if not eid:
        return False
    row = db.execute('''SELECT emp.supervisor_id, emp.leader_id, emp.manager_id
                        FROM evaluations e JOIN employees emp ON emp.id = e.employee_id
                        WHERE e.id=?''', (eval_id,)).fetchone()
    if not row:
        return False
    return eid in [row['supervisor_id'], row['leader_id'], row['manager_id']]

def get_pending_review_count(db, user_id, user_role):
    if user_role == 'superadmin':
        return db.execute(
            "SELECT COUNT(*) FROM evaluations WHERE review_status='self_filled'"
        ).fetchone()[0]
    eid = get_user_emp_id(db, user_id)
    if not eid:
        return 0
    return db.execute('''
        SELECT COUNT(*) FROM evaluations e
        JOIN employees emp ON emp.id = e.employee_id
        WHERE e.review_status='self_filled'
        AND (emp.supervisor_id=? OR emp.leader_id=? OR emp.manager_id=?)
    ''', (eid, eid, eid)).fetchone()[0]

# ─── Auth Helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Silakan login terlebih dahulu', 'warning')
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated

def superadmin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('user_role') != 'superadmin':
            flash('Akses ditolak — fitur ini hanya untuk Superadmin', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

def _get_update_badge(sess):
    """Return info update jika tersedia dan user berhak lihat, else None."""
    role = sess.get('user_role', '')
    if not role:
        return None
    try:
        db = get_db()
        settings = get_settings(db)
        notify_roles = [r.strip() for r in settings.get('update_notify_roles', 'superadmin,admin').split(',')]
        if role not in notify_roles:
            return None
        if settings.get('update_available', '0') != '1':
            return None
        return {
            'latest_version': settings.get('update_latest_version', ''),
            'latest_tag':     settings.get('update_latest_tag', ''),
        }
    except Exception:
        return None


@app.context_processor
def inject_globals():
    pending    = 0
    divisi_list = DIVISI_LIST
    user_perms  = set()
    portal_apps = []
    if 'user_id' in session:
        try:
            db = get_db()
            pending     = get_pending_review_count(db, session['user_id'], session.get('user_role', ''))
            divisi_list = get_divisi_list(db)
            user_perms  = get_role_permissions(db, session.get('user_role', ''))
            if session.get('user_role') == 'superadmin':
                user_perms = set(ALL_PERMISSIONS.keys())
            portal_apps = db.execute(
                'SELECT * FROM superapp_apps WHERE is_active=1 ORDER BY sort_order, name'
            ).fetchall()
            bk_resources = db.execute(
                'SELECT * FROM bk_resources WHERE is_active=1 ORDER BY sort_order, name'
            ).fetchall()
            try:
                _chatbot_on = get_settings(db).get('chatbot_enabled','0') == '1'
            except Exception:
                _chatbot_on = False
        except Exception:
            bk_resources = []
            _chatbot_on = False
    else:
        bk_resources = []
        _chatbot_on = False
    return {
        'current_user': {
            'id':       session.get('user_id'),
            'username': session.get('username'),
            'name':     session.get('user_name'),
            'role':     session.get('user_role'),
        } if 'user_id' in session else None,
        'now_year':        date.today().year,
        'today_date':      date.today().isoformat(),
        'pending_reviews': pending,
        'divisi_list':     divisi_list,
        'level_choices':   LEVEL_CHOICES,
        'user_perms':      user_perms,
        'ALL_PERMISSIONS': ALL_PERMISSIONS,
        'portal_apps':      portal_apps,
        'bk_resources':     bk_resources,
        'current_app_slug':    session.get('active_app') or 'portal',
        'app_version':         VERSION,
        'app_release_date':    RELEASE_DATE,
        'update_badge':        _get_update_badge(session),
        'chatbot_enabled':     _chatbot_on,
    }

# ─── Auto-set active_app dari URL path ────────────────────────────────────────

_SCANNER_PATHS = (
    '/wp-', '/wordpress', '/wp-login', '/wp-admin', '/xmlrpc',
    '/.env', '/.git', '/.htaccess', '/.htpasswd', '/config',
    '/admin/config', '/phpmyadmin', '/pma', '/mysql', '/myadmin',
    '/manager/', '/administrator', '/shell', '/cmd', '/eval',
    '/cgi-bin', '/cgi/', '/api/v1/version', '/actuator', '/debug',
    '/vendor/', '/composer', '/package.json', '/package-lock',
    '/node_modules', '/proc/', '/etc/', '/usr/', '/var/log',
    '/.aws', '/.ssh', '/id_rsa', '/credentials',
    '/setup.php', '/install.php', '/update.php', '/upgrade.php',
    '/backup', '/dump', '/db.sql', '/database.sql',
)
_SCANNER_EXTENSIONS = (
    '.php', '.asp', '.aspx', '.jsp', '.cgi', '.pl', '.rb',
    '.bak', '.sql', '.tar', '.gz', '.zip', '.rar', '.7z',
    '.log', '.cfg', '.conf', '.ini', '.yaml', '.yml', '.toml',
    '.pem', '.key', '.crt', '.p12', '.pfx',
)

@app.before_request
def block_scanner():
    path = request.path.lower()
    if request.path.startswith('/static/'):
        return None
    if any(path.startswith(p) for p in _SCANNER_PATHS):
        return ('', 404)
    if any(path.endswith(ext) for ext in _SCANNER_EXTENSIONS):
        return ('', 404)
    if '../' in path or '%2e%2e' in path.lower():
        return ('', 404)


@app.before_request
def add_security_headers():
    pass  # headers ditambahkan via after_request


@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options']  = 'nosniff'
    response.headers['X-Frame-Options']          = 'SAMEORIGIN'
    response.headers['X-XSS-Protection']         = '1; mode=block'
    response.headers['Referrer-Policy']           = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy']        = 'geolocation=(), microphone=(), camera=()'
    response.headers['Server']                    = 'Hive'
    return response


@app.before_request
def auto_set_active_app():
    path = request.path
    skip = ('/login', '/logout', '/static', '/mfa', '/portal/open')
    if any(path.startswith(p) for p in skip):
        # Halaman MFA & login tetap pakai konteks portal agar sidebar tidak salah
        if path.startswith('/mfa'):
            session['active_app'] = 'portal'
        return
    if path.startswith('/support'):
        session['active_app'] = 'support'
    elif path.startswith('/portal'):
        session['active_app'] = 'portal'
    elif path.startswith('/booking'):
        session['active_app'] = 'booking'
    elif path.startswith('/aset'):
        session['active_app'] = 'aset'
    elif path.startswith('/project'):
        session['active_app'] = 'project'
    else:
        session['active_app'] = 'evaluasi'

# ─── Enforce app-level access dari user_app_access ──────────────────────────────

# Prefix path → app_slug. Urutan penting: lebih spesifik di atas.
_APP_PATH_MAP = [
    ('/support',  'support'),
    ('/portal',   'portal'),
    ('/booking',  'booking'),
    ('/aset',     'aset'),
]
# Path yang bebas diakses tanpa cek app_access
_APP_ACCESS_EXEMPT = {
    '/login', '/logout', '/static', '/mfa', '/portal/open',
    '/reset-password', '/set-password',
}

@app.before_request
def enforce_app_access():
    """Pastikan user hanya bisa akses app yang ada di user_app_access mereka.
    Superadmin selalu boleh. Evaluasi (TalentCore) juga dicek.
    """
    if 'user_id' not in session:
        return
    path = request.path
    if any(path.startswith(p) for p in _APP_ACCESS_EXEMPT):
        return
    if request.path.startswith('/static'):
        return
    # Tentukan slug app berdasarkan path
    app_slug = 'evaluasi'
    for prefix, slug in _APP_PATH_MAP:
        if path.startswith(prefix):
            app_slug = slug
            break
    # Superadmin bebas
    if session.get('user_role') == 'superadmin':
        return
    # Portal management hanya untuk admin/superadmin (sudah dijaga is_portal_admin)
    if app_slug == 'portal':
        return
    try:
        db  = get_db()
        row = db.execute(
            'SELECT id FROM user_app_access WHERE user_id=? AND app_slug=? AND is_active=1',
            (session['user_id'], app_slug)
        ).fetchone()
        if not row:
            flash(f'Anda tidak memiliki akses ke aplikasi ini.', 'danger')
            return redirect(url_for('portal'))
    except Exception:
        pass

# ─── Force MFA setup untuk user non-Google ─────────────────────────────────────

@app.before_request
def enforce_mfa_setup():
    # MFA tidak mandatory — hanya set flag reminder untuk banner di base.html
    pass

# ─── Score Helpers ──────────────────────────────────────────────────────────────

def calc_hs_total(db, eval_id, divisi):
    rows = db.execute('''
        SELECT si.bobot, COALESCE(ss.score, 0) AS score
        FROM skill_categories sc
        JOIN skill_items si ON si.category_id = sc.id
        LEFT JOIN skill_scores ss ON ss.skill_item_id = si.id AND ss.eval_id = ?
        WHERE sc.divisi = ?
    ''', (eval_id, divisi)).fetchall()
    if not rows: return 0.0
    sum_bobot = sum(r['bobot'] for r in rows)
    if sum_bobot == 0: return 0.0
    return round(sum((r['score'] / 4.0) * r['bobot'] for r in rows) / sum_bobot * 100, 4)

def calc_competency_total(db, eval_id, divisi, hs_total):
    items = db.execute('''
        SELECT ci.bobot, ci.is_hardskill, COALESCE(cs.rating, 0) AS rating
        FROM competency_items ci
        LEFT JOIN competency_scores cs ON cs.competency_item_id = ci.id AND cs.eval_id = ?
        WHERE ci.divisi = ? ORDER BY ci.sort_order
    ''', (eval_id, divisi)).fetchall()
    return round(sum(
        hs_total * i['bobot'] if i['is_hardskill'] else i['rating'] * 20.0 * i['bobot']
        for i in items
    ), 4)

def calc_ability_score(db, eval_id, divisi):
    rows = db.execute('''
        SELECT COALESCE(abs.level, '') AS level FROM ability_items ai
        LEFT JOIN ability_scores abs ON abs.ability_item_id = ai.id AND abs.eval_id = ?
        WHERE ai.divisi = ?
    ''', (eval_id, divisi)).fetchall()
    if not rows: return 0.0
    lv = {'A':1,'B':2,'C':3,'D':4}
    scored = [lv[r['level']] for r in rows if r['level'] in lv]
    return round(sum(scored) / len(scored) * 25, 2) if scored else 0.0

def recalc(db, eval_id):
    ev = db.execute('''SELECT e.*, emp.divisi FROM evaluations e
                       JOIN employees emp ON emp.id = e.employee_id WHERE e.id=?''', (eval_id,)).fetchone()
    if not ev: return
    pp_total = ev['pp_score'] * 20.0
    hs_total = calc_hs_total(db, eval_id, ev['divisi'])
    ability  = calc_ability_score(db, eval_id, ev['divisi'])
    comp     = calc_competency_total(db, eval_id, ev['divisi'], hs_total)
    final    = round(pp_total * 0.3 + comp * 0.7, 4)
    db.execute('''UPDATE evaluations SET pp_total=?,hs_total=?,ability_score=?,
                  competency_total=?,final_total=? WHERE id=?''',
               (pp_total, hs_total, ability, comp, final, eval_id))
    db.commit()

def get_eval_or_404(db, eval_id):
    return db.execute('''SELECT e.*, emp.name AS emp_name, emp.jabatan, emp.divisi
                         FROM evaluations e JOIN employees emp ON emp.id = e.employee_id
                         WHERE e.id=?''', (eval_id,)).fetchone()

# ─── Notification Helpers ───────────────────────────────────────────────────────

def send_email(settings, to_email, subject, html_body):
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = settings.get('smtp_from') or settings.get('smtp_user', '')
        msg['To']      = to_email
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))
        use_ssl = settings.get('smtp_ssl', '0') == '1'
        host, port = settings.get('smtp_host',''), int(settings.get('smtp_port', 587))
        server = smtplib.SMTP_SSL(host, port) if use_ssl else smtplib.SMTP(host, port)
        if not use_ssl:
            server.starttls()
        server.login(settings.get('smtp_user',''), settings.get('smtp_password',''))
        server.send_message(msg)
        server.quit()
        return True, None
    except Exception as e:
        return False, str(e)

def normalize_phone_wa(phone):
    """Konversi nomor HP ke format WhatsApp chat ID: 628xxx@c.us"""
    p = ''.join(c for c in (phone or '') if c.isdigit())
    if not p:
        return ''
    if p.startswith('0'):
        p = '62' + p[1:]
    elif not p.startswith('62'):
        p = '62' + p
    return f'{p}@c.us'

def send_whatsapp(openwa_url, api_key, session_id, phone, message):
    """Kirim pesan WhatsApp via OpenWA REST API (rmyndharis/OpenWA)."""
    try:
        chat_id = normalize_phone_wa(phone)
        if not chat_id:
            return False, 'Nomor HP tidak valid'
        sid = (session_id or 'default').strip()
        url = openwa_url.rstrip('/') + f'/api/sessions/{sid}/messages/send-text'
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['X-API-Key'] = api_key
        r = req_lib.post(url, json={'chatId': chat_id, 'text': message},
                         headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json() if r.text else {}
        if isinstance(data, dict) and data.get('error'):
            return False, str(data['error'])
        return True, None
    except Exception as e:
        return False, str(e)

def send_telegram(bot_token, chat_id, message, _log_subject='', _app_slug=None):
    try:
        r = req_lib.post(
            f'https://api.telegram.org/bot{bot_token}/sendMessage',
            json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'},
            timeout=10)
        r.raise_for_status()
        audit_notif('telegram', chat_id, _log_subject or message[:60], message, True, app_slug=_app_slug)
        return True, None
    except Exception as e:
        audit_notif('telegram', chat_id, _log_subject or message[:60], message, False, str(e), app_slug=_app_slug)
        return False, str(e)

def _real_ip():
    """Ambil IP asli client — baca X-Forwarded-For / X-Real-IP dari reverse proxy."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.headers.get('X-Real-IP', '') or request.remote_addr or ''

def audit_log(action, resource='', resource_id='', detail='', app_slug=None):
    """Catat aktivitas user ke audit_activity. Dipanggil dari route mana saja."""
    try:
        db = get_db()
        slug = app_slug or session.get('active_app') or 'portal'
        db.execute('''INSERT INTO audit_activity(app_slug,user_id,username,action,resource,resource_id,detail,ip,user_agent)
                      VALUES(?,?,?,?,?,?,?,?,?)''',
                   (slug, session.get('user_id'), session.get('user_name',''),
                    action, resource, str(resource_id), detail,
                    _real_ip(), request.user_agent.string[:200] if request.user_agent else ''))
        db.commit()
    except Exception:
        pass

def audit_notif(channel, recipient, subject, message, ok, err='', triggered_by='', app_slug=None):
    """Catat log notifikasi (email/telegram/wa)."""
    try:
        db = get_db()
        slug = app_slug or session.get('active_app') or 'portal'
        db.execute('''INSERT INTO audit_notifications(app_slug,channel,recipient,subject,message,status,error_msg,triggered_by)
                      VALUES(?,?,?,?,?,?,?,?)''',
                   (slug, channel, str(recipient), subject, message[:1000],
                    'sent' if ok else 'failed', err or '', triggered_by))
        db.commit()
    except Exception:
        pass

def log_reminder(db, emp_id, channel, subject, message, ok, err, triggered_by='auto'):
    db.execute('''INSERT INTO reminder_logs(employee_id, channel, subject, message, status, error_msg, triggered_by)
                  VALUES(?,?,?,?,?,?,?)''',
               (emp_id, channel, subject, message,
                'sent' if ok else 'failed', err or '', triggered_by))

def compose_contract_message(emp, days_left):
    status = 'berakhir hari ini' if days_left == 0 else \
             f'berakhir dalam <b>{days_left} hari</b>' if days_left > 0 else \
             f'<b>sudah berakhir {abs(days_left)} hari lalu</b>'
    return f"""<h3>⚠️ Reminder Kontrak Karyawan</h3>
<table>
  <tr><td><b>Nama</b></td><td>: {emp['name']}</td></tr>
  <tr><td><b>Jabatan</b></td><td>: {emp['jabatan'] or '-'}</td></tr>
  <tr><td><b>Divisi</b></td><td>: {emp['divisi']}</td></tr>
  <tr><td><b>Tipe Kontrak</b></td><td>: Kontrak</td></tr>
  <tr><td><b>Akhir Kontrak</b></td><td>: {emp['contract_end']}</td></tr>
  <tr><td><b>Status</b></td><td>: Kontrak {status}</td></tr>
</table>
<p>Mohon segera tindak lanjut perpanjangan atau pemutusan kontrak.</p>"""

def compose_telegram_message(emp, days_left):
    icon = '🔴' if days_left <= 7 else '🟡' if days_left <= 30 else '🟢'
    status = 'Berakhir HARI INI!' if days_left == 0 else \
             f'Berakhir dalam {days_left} hari' if days_left > 0 else \
             f'SUDAH BERAKHIR {abs(days_left)} hari lalu!'
    return (f"{icon} <b>Reminder Kontrak Karyawan</b>\n\n"
            f"👤 <b>{emp['name']}</b>\n"
            f"🏢 {emp['divisi']} — {emp['jabatan'] or '-'}\n"
            f"📅 Akhir kontrak: <b>{emp['contract_end']}</b>\n"
            f"⏳ {status}\n\n"
            f"<i>Segera tindak lanjut perpanjangan / pemutusan kontrak.</i>")

def compose_contract_wa_message(emp, days_left):
    icon = '🔴' if days_left <= 7 else '🟡' if days_left <= 30 else '🟢'
    status = 'Berakhir HARI INI!' if days_left == 0 else \
             f'Berakhir dalam {days_left} hari' if days_left > 0 else \
             f'SUDAH BERAKHIR {abs(days_left)} hari lalu!'
    return (f"{icon} *Reminder Kontrak Karyawan*\n\n"
            f"👤 *{emp['name']}*\n"
            f"🏢 {emp['divisi']} — {emp['jabatan'] or '-'}\n"
            f"📅 Akhir kontrak: *{emp['contract_end']}*\n"
            f"⏳ {status}\n\n"
            f"_Segera tindak lanjut perpanjangan / pemutusan kontrak._")

def compose_contract_chain_email(emp, days_left, recipient_role=''):
    status = 'berakhir hari ini' if days_left == 0 else \
             f'berakhir dalam <b>{days_left} hari</b>' if days_left > 0 else \
             f'<b>sudah berakhir {abs(days_left)} hari lalu</b>'
    role_note = f' (sebagai {recipient_role})' if recipient_role else ''
    return (f"<h3>⚠️ Info Kontrak Karyawan — Notifikasi Atasan{role_note}</h3>"
            f"<table>"
            f"<tr><td><b>Nama</b></td><td>: {emp['name']}</td></tr>"
            f"<tr><td><b>Jabatan</b></td><td>: {emp['jabatan'] or '-'}</td></tr>"
            f"<tr><td><b>Divisi</b></td><td>: {emp['divisi']}</td></tr>"
            f"<tr><td><b>Akhir Kontrak</b></td><td>: {emp['contract_end']}</td></tr>"
            f"<tr><td><b>Status</b></td><td>: Kontrak {status}</td></tr>"
            f"</table>"
            f"<p>Mohon segera tindak lanjut perpanjangan atau pemutusan kontrak karyawan di atas.</p>")

def compose_contract_chain_tg(emp, days_left, recipient_role=''):
    icon = '🔴' if days_left <= 7 else '🟡' if days_left <= 30 else '🟢'
    status = 'Berakhir HARI INI!' if days_left == 0 else \
             f'Berakhir dalam {days_left} hari' if days_left > 0 else \
             f'SUDAH BERAKHIR {abs(days_left)} hari lalu!'
    role_note = f' [{recipient_role}]' if recipient_role else ''
    return (f"{icon} <b>Info Kontrak — Notifikasi Atasan{role_note}</b>\n\n"
            f"👤 <b>{emp['name']}</b>\n"
            f"🏢 {emp['divisi']} — {emp['jabatan'] or '-'}\n"
            f"📅 Akhir kontrak: <b>{emp['contract_end']}</b>\n"
            f"⏳ {status}\n\n"
            f"<i>Mohon segera tindak lanjut perpanjangan / pemutusan kontrak.</i>")

def compose_contract_chain_wa(emp, days_left, recipient_role=''):
    icon = '🔴' if days_left <= 7 else '🟡' if days_left <= 30 else '🟢'
    status = 'Berakhir HARI INI!' if days_left == 0 else \
             f'Berakhir dalam {days_left} hari' if days_left > 0 else \
             f'SUDAH BERAKHIR {abs(days_left)} hari lalu!'
    role_note = f' [{recipient_role}]' if recipient_role else ''
    return (f"{icon} *Info Kontrak — Notifikasi Atasan{role_note}*\n\n"
            f"👤 *{emp['name']}*\n"
            f"🏢 {emp['divisi']} — {emp['jabatan'] or '-'}\n"
            f"📅 Akhir kontrak: *{emp['contract_end']}*\n"
            f"⏳ {status}\n\n"
            f"_Mohon segera tindak lanjut perpanjangan / pemutusan kontrak._")

def _send_contract_notification(db, emp, days_left, settings, triggered_by='auto'):
    """Kirim notifikasi kontrak ke staff + chain (co-leader/leader/manager) + extra WA.
    Returns (sent, failed)."""
    sent = failed = 0
    subject   = f"[Reminder] Kontrak {emp['name']} — {days_left} hari lagi"
    html      = compose_contract_message(emp, days_left)
    tg_msg    = compose_telegram_message(emp, days_left)
    wa_msg    = compose_contract_wa_message(emp, days_left)

    bot_token    = settings.get('telegram_bot_token', '').strip()
    default_chat = settings.get('telegram_default_chat_id', '').strip()
    wa_url       = settings.get('openwa_url', '').strip()
    wa_key       = settings.get('openwa_api_key', '').strip()
    wa_session   = get_openwa_session(settings, 'evaluasi')
    wa_enabled   = settings.get('openwa_enabled', '0') == '1'

    # ── Staff: Email ──
    if settings.get('smtp_host', '').strip():
        for to_email in get_notification_emails(settings, emp['email'] or ''):
            ok, err = send_email(settings, to_email, subject, html)
            log_reminder(db, emp['id'], 'email', subject, html, ok, err, triggered_by)
            if ok: sent += 1
            else:  failed += 1

    # ── Staff: Telegram ──
    if bot_token:
        for chat_id in get_notification_telegram_ids(settings, emp['telegram_id'] or '', default_chat):
            ok, err = send_telegram(bot_token, chat_id, tg_msg)
            log_reminder(db, emp['id'], 'telegram', subject, tg_msg, ok, err, triggered_by)
            if ok: sent += 1
            else:  failed += 1

    # ── Staff: WhatsApp ──
    if wa_enabled and wa_url and emp['phone']:
        ok, err = send_whatsapp(wa_url, wa_key, wa_session, emp['phone'], wa_msg)
        log_reminder(db, emp['id'], 'whatsapp', subject, wa_msg, ok, err, triggered_by)
        if ok: sent += 1
        else:  failed += 1

    # ── Chain: Manajerial / Leader / Manager ──
    col_role_map = [('supervisor_id', 'Manajerial'), ('leader_id', 'Leader'), ('manager_id', 'Manager')]
    notified_chain_ids = set()
    for col, role_label in col_role_map:
        try:
            chain_emp_id = emp[col]
        except (IndexError, KeyError):
            chain_emp_id = None
        if not chain_emp_id or chain_emp_id in notified_chain_ids:
            continue
        chain_emp = db.execute('SELECT * FROM employees WHERE id=? AND is_active=1',
                               (chain_emp_id,)).fetchone()
        if not chain_emp:
            continue
        notified_chain_ids.add(chain_emp_id)
        c_html = compose_contract_chain_email(emp, days_left, role_label)
        c_tg   = compose_contract_chain_tg(emp, days_left, role_label)
        c_wa   = compose_contract_chain_wa(emp, days_left, role_label)
        c_subj = f"[Info Kontrak] {emp['name']} — {days_left} hari lagi ({role_label})"

        if settings.get('smtp_host', '').strip() and chain_emp['email']:
            ok, err = send_email(settings, chain_emp['email'], c_subj, c_html)
            log_reminder(db, emp['id'], 'email', c_subj, c_html, ok, err, triggered_by)
            if ok: sent += 1
            else:  failed += 1

        if bot_token and chain_emp['telegram_id']:
            tg_id = normalize_telegram_id(chain_emp['telegram_id'])
            ok, err = send_telegram(bot_token, tg_id, c_tg)
            log_reminder(db, emp['id'], 'telegram', c_subj, c_tg, ok, err, triggered_by)
            if ok: sent += 1
            else:  failed += 1

        if wa_enabled and wa_url and chain_emp['phone']:
            ok, err = send_whatsapp(wa_url, wa_key, wa_session, chain_emp['phone'], c_wa)
            log_reminder(db, emp['id'], 'whatsapp', c_subj, c_wa, ok, err, triggered_by)
            if ok: sent += 1
            else:  failed += 1

    # ── Extra WA phones (diluar orang terkait) ──
    if wa_enabled and wa_url:
        extra_wa_msg = compose_contract_wa_message(emp, days_left)
        for phone in settings.get('openwa_extra_phones', '').split(','):
            phone = phone.strip()
            if not phone:
                continue
            ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone, extra_wa_msg)
            log_reminder(db, emp['id'], 'whatsapp', subject, extra_wa_msg, ok, err, triggered_by)
            if ok: sent += 1
            else:  failed += 1

    return sent, failed

def run_contract_reminders(triggered_by='auto'):
    """Check contracts and send reminders. Call directly (not in request context)."""
    db = _get_raw_db()
    try:
        settings = get_settings(db)
        if settings.get('reminder_enabled', '1') != '1' and triggered_by == 'auto':
            return 0, 0
        reminder_days = [int(d.strip()) for d in settings.get('reminder_days', '30,14,7,1').split(',')
                         if d.strip().isdigit()]
        sent = failed = 0
        today = date.today()
        emps = db.execute('''SELECT * FROM employees
                             WHERE employment_type IN ('kontrak','staff_worker') AND contract_end != ''
                             AND contract_end IS NOT NULL AND is_active=1''').fetchall()
        for emp in emps:
            try:
                end_date = date.fromisoformat(emp['contract_end'])
            except Exception:
                continue
            days_left = (end_date - today).days
            if days_left not in reminder_days and triggered_by == 'auto':
                continue
            s, f = _send_contract_notification(db, emp, days_left, settings, triggered_by)
            sent += s
            failed += f
        db.commit()
        return sent, failed
    finally:
        db.close()

# ─── AssetCore: Subscription Reminders ────────────────────────────────────────

def compose_sub_email(sub, days_left):
    icon = '🔴' if days_left <= 7 else ('🟡' if days_left <= 14 else '🟢')
    cat  = sub['category'] or 'Subscription'
    status = 'berakhir <b>HARI INI</b>' if days_left == 0 else \
             f'berakhir dalam <b>{days_left} hari</b>' if days_left > 0 else \
             f'<b>sudah berakhir {abs(days_left)} hari lalu</b>'
    return (f"<h3>{icon} Reminder {cat}: {sub['provider']}</h3>"
            f"<table>"
            f"<tr><td><b>Layanan</b></td><td>: {sub['provider']}</td></tr>"
            f"<tr><td><b>Kategori</b></td><td>: {cat}</td></tr>"
            f"<tr><td><b>Billing</b></td><td>: {sub['billing_cycle'] or '-'}</td></tr>"
            f"<tr><td><b>Tgl Berakhir</b></td><td>: {sub['end_date']}</td></tr>"
            f"<tr><td><b>Status</b></td><td>: Subscription {status}</td></tr>"
            f"</table>"
            f"<p>Mohon segera lakukan perpanjangan atau evaluasi layanan ini.</p>")

def compose_sub_telegram(sub, days_left):
    icon = '🔴' if days_left <= 7 else ('🟡' if days_left <= 14 else '🟢')
    cat  = sub['category'] or 'Subscription'
    status = 'Berakhir HARI INI!' if days_left == 0 else \
             f'Berakhir dalam {days_left} hari' if days_left > 0 else \
             f'SUDAH BERAKHIR {abs(days_left)} hari lalu!'
    return (f"{icon} <b>Reminder {cat}</b>\n\n"
            f"📦 <b>{sub['provider']}</b>\n"
            f"💳 Billing: {sub['billing_cycle'] or '-'}\n"
            f"📅 Berakhir: <b>{sub['end_date']}</b>\n"
            f"⏳ {status}\n\n"
            f"<i>Segera lakukan perpanjangan atau evaluasi layanan ini.</i>")

def compose_sub_wa(sub, days_left):
    icon = '🔴' if days_left <= 7 else ('🟡' if days_left <= 14 else '🟢')
    cat  = sub['category'] or 'Subscription'
    status = 'Berakhir HARI INI!' if days_left == 0 else \
             f'Berakhir dalam {days_left} hari' if days_left > 0 else \
             f'SUDAH BERAKHIR {abs(days_left)} hari lalu!'
    return (f"{icon} *Reminder {cat}*\n\n"
            f"📦 *{sub['provider']}*\n"
            f"💳 Billing: {sub['billing_cycle'] or '-'}\n"
            f"📅 Berakhir: *{sub['end_date']}*\n"
            f"⏳ {status}\n\n"
            f"_Segera lakukan perpanjangan atau evaluasi layanan ini._")

# ─── AssetCore: Asset User Change Notifications ───────────────────────────────

def _asset_user_display(db, asset):
    """Ambil nama pengguna saat ini dari asset (linked / manual / kosong)."""
    if asset['employee_id']:
        row = db.execute('SELECT name FROM employees WHERE id=?', (asset['employee_id'],)).fetchone()
        return row['name'] if row else f'ID#{asset["employee_id"]}'
    return (asset['manual_employee_name'] or '').strip()

def _compose_asset_notif(asset, event, old_user, new_user, reason, fmt='email'):
    """
    event: 'assigned' | 'released' | 'end'
    fmt:   'email' | 'telegram' | 'wa'
    """
    name  = f"{asset['brand'] or ''} {asset['device_type'] or ''}".strip()
    tag   = asset['asset_tag'] or '—'
    today = date.today().isoformat()

    if event == 'assigned':
        icon, title = '🔄', 'Perubahan Pengguna Asset'
        line = f'Dari: {old_user or "—"}  →  Ke: {new_user or "—"}'
    elif event == 'released':
        icon, title = '📦', 'Asset Menjadi Available (Stok)'
        line = f'Pengguna sebelumnya: {old_user or "—"}'
    else:
        icon, title = '❌', 'Asset Ditandai End'
        line = f'Pengguna terakhir: {old_user or "—"}'

    if fmt == 'email':
        return (f"<h3>{icon} {title}</h3>"
                f"<table>"
                f"<tr><td><b>Asset</b></td><td>: {name}</td></tr>"
                f"<tr><td><b>Asset Tag</b></td><td>: {tag}</td></tr>"
                f"<tr><td><b>S/N</b></td><td>: {asset['serial_number'] or '—'}</td></tr>"
                f"<tr><td><b>Info</b></td><td>: {line}</td></tr>"
                f"<tr><td><b>Alasan</b></td><td>: {reason or '—'}</td></tr>"
                f"<tr><td><b>Tanggal</b></td><td>: {today}</td></tr>"
                f"</table>")
    elif fmt == 'telegram':
        return (f"{icon} <b>{title}</b>\n\n"
                f"💻 <b>{name}</b>  |  Tag: <code>{tag}</code>\n"
                f"👤 {line}\n"
                f"📋 Alasan: {reason or '—'}\n"
                f"📅 {today}")
    else:
        return (f"{icon} *{title}*\n\n"
                f"💻 *{name}*  |  Tag: {tag}\n"
                f"👤 {line}\n"
                f"📋 Alasan: {reason or '—'}\n"
                f"📅 {today}")

def _notify_asset_change(db, asset, event, old_user, new_user, reason):
    """Kirim notifikasi perubahan pengguna/status asset ke semua channel AC."""
    try:
        settings   = get_settings(db)
        ac_emails  = get_ac_notification_emails(settings)
        ac_tg_ids  = get_ac_notification_telegram_ids(settings)
        ac_phones  = get_ac_notification_wa_phones(settings)
        if not (ac_emails or ac_tg_ids or ac_phones):
            return

        name = f"{asset['brand'] or ''} {asset['device_type'] or ''}".strip()
        tag  = asset['asset_tag'] or ''
        ev_label = ('Perubahan Pengguna' if event == 'assigned'
                    else ('Asset Available' if event == 'released' else 'Asset End'))
        subj = f"[AssetCore] {ev_label}: {name} {tag}".strip()

        email_body = _compose_asset_notif(asset, event, old_user, new_user, reason, 'email')
        tg_msg     = _compose_asset_notif(asset, event, old_user, new_user, reason, 'telegram')
        wa_msg     = _compose_asset_notif(asset, event, old_user, new_user, reason, 'wa')

        bot_token  = settings.get('telegram_bot_token', '').strip()
        wa_url     = settings.get('openwa_url', '').strip()
        wa_key     = settings.get('openwa_api_key', '').strip()
        wa_session = get_openwa_session(settings, 'aset')
        wa_enabled = settings.get('openwa_enabled', '0') == '1'

        if settings.get('smtp_host', '').strip() and ac_emails:
            for to_email in ac_emails:
                ok, err = send_email(settings, to_email, subj, email_body)
                audit_notif('email', to_email, subj, email_body, ok, err, 'manual', app_slug='aset')

        if bot_token and ac_tg_ids:
            for chat_id in ac_tg_ids:
                send_telegram(bot_token, chat_id, tg_msg, _log_subject=subj, _app_slug='aset')

        if wa_enabled and wa_url and ac_phones:
            for phone in ac_phones:
                ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone, wa_msg)
                audit_notif('whatsapp', phone, subj, wa_msg, ok, err, 'manual', app_slug='aset')
    except Exception:
        pass  # Jangan ganggu flow utama jika notifikasi gagal


def run_subscription_reminders(triggered_by='auto'):
    """Cek ac_subscriptions yang mendekati expired dan kirim notifikasi.
    Dipanggil dari scheduler (auto) atau route manual (manual)."""
    db = _get_raw_db()
    try:
        settings = get_settings(db)
        if settings.get('ac_sub_reminder_enabled', '1') != '1' and triggered_by == 'auto':
            return 0, 0

        reminder_days_raw = settings.get('ac_sub_reminder_days', '30,14,7,1')
        reminder_days = [int(d.strip()) for d in reminder_days_raw.split(',') if d.strip().isdigit()]

        bot_token  = settings.get('telegram_bot_token', '').strip()
        wa_url     = settings.get('openwa_url', '').strip()
        wa_key     = settings.get('openwa_api_key', '').strip()
        wa_session = get_openwa_session(settings, 'aset')
        wa_enabled = settings.get('openwa_enabled', '0') == '1'

        # Gunakan daftar penerima khusus AssetCore
        ac_emails  = get_ac_notification_emails(settings)
        ac_tg_ids  = get_ac_notification_telegram_ids(settings)
        ac_phones  = get_ac_notification_wa_phones(settings)

        sent = failed = 0
        today = date.today()
        today_str = today.isoformat()

        subs = db.execute(
            "SELECT * FROM ac_subscriptions WHERE is_active=1 AND end_date!='' AND end_date IS NOT NULL"
        ).fetchall()

        for sub in subs:
            try:
                end_date = date.fromisoformat(sub['end_date'])
            except Exception:
                continue
            days_left = (end_date - today).days

            if triggered_by == 'auto':
                if days_left not in reminder_days:
                    continue
                if sub['last_reminder_sent'] == today_str:
                    continue

            subj   = f"[AssetCore] Reminder {sub['provider']} — {'berakhir dalam '+str(days_left)+' hari' if days_left > 0 else 'BERAKHIR HARI INI'}"
            html   = compose_sub_email(sub, days_left)
            tg_msg = compose_sub_telegram(sub, days_left)
            wa_msg = compose_sub_wa(sub, days_left)

            # Email — daftar AC penerima
            if settings.get('smtp_host', '').strip() and ac_emails:
                for to_email in ac_emails:
                    ok, err = send_email(settings, to_email, subj, html)
                    audit_notif('email', to_email, subj, html, ok, err, triggered_by, app_slug='aset')
                    if ok: sent += 1
                    else:  failed += 1

            # Telegram — daftar AC penerima
            if bot_token and ac_tg_ids:
                for chat_id in ac_tg_ids:
                    ok, err = send_telegram(bot_token, chat_id, tg_msg,
                                            _log_subject=subj, _app_slug='aset')
                    if ok: sent += 1
                    else:  failed += 1

            # WhatsApp — daftar AC penerima
            if wa_enabled and wa_url and ac_phones:
                for phone in ac_phones:
                    ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone, wa_msg)
                    audit_notif('whatsapp', phone, subj, wa_msg, ok, err, triggered_by, app_slug='aset')
                    if ok: sent += 1
                    else:  failed += 1

            db.execute("UPDATE ac_subscriptions SET last_reminder_sent=? WHERE id=?",
                       (today_str, sub['id']))

        db.commit()
        return sent, failed
    finally:
        db.close()

# ─── APScheduler ───────────────────────────────────────────────────────────────

# ─── Update Center ─────────────────────────────────────────────────────────────

UPDATE_TRIGGER_FILE = '/tmp/hive_update_trigger'
UPDATE_LOG_FILE     = '/tmp/hive_update.log'
UPDATE_DONE_MARKER  = 'HIVE_DEPLOY_DONE'
UPDATE_FAIL_MARKER  = 'HIVE_DEPLOY_FAILED'


def check_for_updates():
    """Cek GitHub untuk release terbaru. Dipanggil dari scheduler."""
    db = _get_raw_db()
    try:
        settings = get_settings(db)
        if settings.get('update_check_enabled', '1') != '1':
            return
        repo = settings.get('github_repo', 'barangbaru/solid-apps')
        resp = req_lib.get(
            f'https://api.github.com/repos/{repo}/tags',
            headers={'Accept': 'application/vnd.github.v3+json'},
            timeout=10)
        if resp.status_code != 200:
            return
        tags = resp.json()
        if not tags:
            return
        # Ambil tag terbaru (urutan dari API sudah descending by creation)
        latest_tag  = tags[0].get('name', '')
        latest_ver  = latest_tag.lstrip('v')
        now_str     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Fetch semua releases sekaligus (lebih efisien dari per-tag)
        import json
        rel_all_resp = req_lib.get(
            f'https://api.github.com/repos/{repo}/releases?per_page=50',
            headers={'Accept': 'application/vnd.github.v3+json'},
            timeout=10)
        releases_by_tag = {}
        if rel_all_resp.status_code == 200:
            for r in rel_all_resp.json():
                releases_by_tag[r.get('tag_name', '')] = r.get('body', '') or ''

        release_notes = releases_by_tag.get(latest_tag, '')

        # Gabungkan tags + release notes jadi satu struktur JSON
        all_tags_data = []
        for t in tags:
            tname = t.get('name', '')
            if tname.startswith('v'):
                all_tags_data.append({
                    'tag':   tname,
                    'notes': releases_by_tag.get(tname, ''),
                })

        is_newer = _version_gt(latest_ver, VERSION)
        for key, val in [
            ('update_check_last',     now_str),
            ('update_latest_version', latest_ver),
            ('update_latest_tag',     latest_tag),
            ('update_release_notes',  release_notes),
            ('update_available',      '1' if is_newer else '0'),
            ('update_all_tags',       json.dumps(all_tags_data)),
        ]:
            db.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))
        db.commit()
        print(f" Cek update: {'update tersedia ' + latest_tag if is_newer else 'sudah terbaru'} (terpasang v{VERSION})")
    except Exception as e:
        print(f" Cek update gagal: {e}")
    finally:
        db.close()


def _version_gt(a, b):
    """Return True jika versi a > b (semantic versioning)."""
    def parts(v):
        try:
            return [int(x) for x in v.strip().split('.')]
        except Exception:
            return [0]
    return parts(a) > parts(b)


def calc_task_perf(db, emp_id, date_from='', date_to='', benchmark_per_month=100.0):
    """Hitung skor kinerja task otomatis dari semua sumber data.

    Return dict:
      breakdown: list of {type, label, count, raw_pts}
      total_raw: total raw points
      task_score: normalized 0-100
      detail: list of individual tasks (untuk tabel detail)
      months: durasi periode dalam bulan
    """
    # Load config bobot
    cfg_rows = db.execute('SELECT * FROM task_perf_config ORDER BY sort_order').fetchall()
    cfg = {r['task_type']: r for r in cfg_rows}

    def _mult_priority(c, priority):
        p = (priority or 'Medium').lower()
        if p in ('critical', 'blocker', 'kritis'):   return float(c['priority_critical'])
        if p in ('high', 'tinggi'):                  return float(c['priority_high'])
        if p in ('low', 'rendah'):                   return float(c['priority_low'])
        return float(c['priority_medium'])

    def _mult_ontime(c, due_date, done_date):
        if not due_date or not done_date:
            return 1.0
        return float(c['ontime_mult']) if done_date <= due_date else float(c['late_mult'])

    # Helper: base WHERE clause untuk filter tanggal
    def _date_where(col_done, params, df=date_from, dt=date_to):
        clauses = []
        if df:
            clauses.append(f"{col_done} >= ?")
            params.append(df)
        if dt:
            clauses.append(f"{col_done} <= ?")
            params.append(dt)
        return (' AND ' + ' AND '.join(clauses)) if clauses else ''

    # Hitung durasi bulan (min 1)
    months = 1.0
    if date_from and date_to:
        try:
            from datetime import datetime as _dt
            d0 = _dt.strptime(date_from, '%Y-%m-%d')
            d1 = _dt.strptime(date_to,   '%Y-%m-%d')
            months = max(1.0, (d1 - d0).days / 30.44)
        except Exception:
            months = 1.0

    detail = []
    summary = {}

    def _add(task_type, label, source, pts, count=1):
        summary.setdefault(task_type, {'type': task_type, 'label': label, 'count': 0, 'raw_pts': 0.0})
        summary[task_type]['count']   += count
        summary[task_type]['raw_pts'] += pts
        detail.append({'type': task_type, 'label': label, 'source': source, 'pts': pts})

    # ── 1. Project (sebagai PIC/Lead) ──────────────────────────────────────────
    if 'project_lead' in cfg:
        c = cfg['project_lead']
        p = [emp_id]
        dw = _date_where('p.end_date', p)
        rows = db.execute(f'''
            SELECT p.code, p.name, p.end_date, p.status
            FROM pc_projects p
            WHERE p.pic_id=? AND p.deleted_at IS NULL
              AND p.status IN ('done','closed','selesai'){dw}
        ''', p).fetchall()
        for r in rows:
            pts = round(float(c['base_points']) * _mult_ontime(c, r['end_date'], r['end_date']), 2)
            _add('project_lead', c['label'], f"Project: {r['name']}", pts)

    # ── 2. Project (sebagai Implementor) ────────────────────────────────────────
    if 'project_impl' in cfg:
        c = cfg['project_impl']
        p = [emp_id, emp_id]
        dw = _date_where('p.end_date', p)
        rows = db.execute(f'''
            SELECT p.code, p.name, p.end_date
            FROM pc_projects p
            WHERE (p.implementor_id=? OR p.co_leader_id=?)
              AND p.pic_id != ? AND p.deleted_at IS NULL
              AND p.status IN ('done','closed','selesai'){dw}
        ''', p + [emp_id]).fetchall()
        for r in rows:
            pts = round(float(c['base_points']) * _mult_ontime(c, r['end_date'], r['end_date']), 2)
            _add('project_impl', c['label'], f"Project: {r['name']}", pts)

    # ── 3. Project (sebagai Member Tim) ─────────────────────────────────────────
    if 'project_member' in cfg:
        c = cfg['project_member']
        p = [emp_id]
        dw = _date_where('p.end_date', p)
        rows = db.execute(f'''
            SELECT p.code, p.name, p.end_date
            FROM pc_members m
            JOIN pc_projects p ON p.id=m.project_id
            WHERE m.employee_id=? AND p.deleted_at IS NULL
              AND p.status IN ('done','closed','selesai')
              AND p.pic_id != ? AND p.implementor_id != ?
              AND (p.co_leader_id IS NULL OR p.co_leader_id != ?){dw}
        ''', p + [emp_id, emp_id, emp_id]).fetchall()
        for r in rows:
            pts = round(float(c['base_points']) * _mult_ontime(c, r['end_date'], r['end_date']), 2)
            _add('project_member', c['label'], f"Project: {r['name']}", pts)

    # ── 4. Project Issues (sebagai Programmer) ──────────────────────────────────
    if 'project_issue' in cfg:
        c = cfg['project_issue']
        difficulty_map = {'hard': 2.0, 'sulit': 2.0, 'normal': 1.0, 'easy': 0.5, 'mudah': 0.5}
        p = [emp_id]
        dw = _date_where('i.resolved_date', p)
        rows = db.execute(f'''
            SELECT i.issue_no, i.title, i.difficulty, i.priority, i.resolved_date, p.name AS proj_name
            FROM pc_issues i
            JOIN pc_projects p ON p.id=i.project_id
            WHERE i.pic_programmer_id=?
              AND i.status_programmer IN ('done','closed','resolved'){dw}
        ''', p).fetchall()
        for r in rows:
            diff_mult = difficulty_map.get((r['difficulty'] or 'Normal').lower(), 1.0)
            pts = round(float(c['base_points']) * diff_mult *
                        _mult_ontime(c, r['resolved_date'], r['resolved_date']), 2)
            _add('project_issue', c['label'], f"Issue {r['issue_no']}: {r['title'][:40]} ({r['proj_name']})", pts)

    # ── 5. Project Tasks (done, assignee) ───────────────────────────────────────
    if 'project_task' in cfg:
        c = cfg['project_task']
        p = [emp_id]
        dw = _date_where('t.due_date', p)
        rows = db.execute(f'''
            SELECT t.title, t.priority, t.due_date, t.status, p.name AS proj_name
            FROM pc_task_assignees ta
            JOIN pc_tasks t ON t.id=ta.task_id
            JOIN pc_projects p ON p.id=t.project_id
            WHERE ta.employee_id=? AND t.status IN ('done','closed'){dw}
        ''', p).fetchall()
        for r in rows:
            pts = round(float(c['base_points']) * _mult_priority(c, r['priority']), 2)
            _add('project_task', c['label'], f"Task: {r['title'][:40]} ({r['proj_name']})", pts)

    # ── 6. POC / Presales ───────────────────────────────────────────────────────
    if 'poc_presales' in cfg:
        c = cfg['poc_presales']
        p = [emp_id]
        dw = _date_where('r.created_at', p)
        rows = db.execute(f'''
            SELECT r.req_no, r.subject, r.request_type, r.status, r.created_at
            FROM sc_presales_assignees pa
            JOIN sc_presales_requests r ON r.id=pa.request_id
            WHERE pa.employee_id=? AND r.status IN ('done','closed','approved'){dw}
        ''', p).fetchall()
        for r in rows:
            pts = round(float(c['base_points']), 2)
            _add('poc_presales', c['label'], f"{r['request_type'].upper()} {r['req_no']}: {r['subject'][:40]}", pts)

    # ── 7. Support Tickets (assignee) ───────────────────────────────────────────
    if 'support_ticket' in cfg:
        c = cfg['support_ticket']
        p = [emp_id]
        # Gunakan work_start_date sebagai proxy tanggal penyelesaian jika resolved_at tidak ada
        dw = _date_where('t.reported_at', p)
        rows = db.execute(f'''
            SELECT t.ticket_no, t.subject, t.priority, t.due_date, t.reported_at, t.status
            FROM sc_ticket_assignees ta
            JOIN sc_tickets t ON t.id=ta.ticket_id
            WHERE ta.employee_id=? AND t.status IN ('resolved','closed'){dw}
        ''', p).fetchall()
        for r in rows:
            pts = round(float(c['base_points']) * _mult_priority(c, r['priority'] or 'Medium')
                        * _mult_ontime(c, r['due_date'], r['reported_at']), 2)
            _add('support_ticket', c['label'], f"Tiket {r['ticket_no']}: {r['subject'][:40]}", pts)

    total_raw = round(sum(v['raw_pts'] for v in summary.values()), 2)
    task_score = round(min(total_raw / (benchmark_per_month * months) * 100, 100), 1)

    return {
        'breakdown': list(summary.values()),
        'total_raw': total_raw,
        'task_score': task_score,
        'months': round(months, 1),
        'benchmark': benchmark_per_month,
        'detail': detail,
    }


def calc_task_analytics(db, emp_id, date_from='', date_to=''):
    """Analitik detail per karyawan: timeliness, concurrency, breakdown tipe.

    Return dict:
      tasks_all      : list semua task (done & open) dengan info lengkap
      done_ontime    : task selesai tepat/sebelum due date
      done_delay     : task selesai setelah due date
      done_no_due    : task selesai tanpa due date
      open_ontime    : masih open, due date belum lewat (atau tanpa due)
      open_overtime  : masih open, due date sudah lewat!
      concurrent_max : maks task aktif bersamaan di satu titik waktu
      concurrent_avg : rata-rata task aktif bersamaan per hari
      by_type        : {type: {done, delay, overtime, ontime, open}} count
      total_done     : jumlah task selesai
      total_open     : jumlah task masih open
      ontime_rate    : pct ontime dari yang punya due_date
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td

    today = _date.today().isoformat()

    def _in_period(d):
        if not d:
            return True
        if date_from and d < date_from:
            return False
        if date_to and d > date_to:
            return False
        return True

    def _timeliness(due, done_date):
        """Return: 'ontime'|'delay'|'no_due'|'open_ontime'|'open_overtime'"""
        is_done = bool(done_date)
        if not due:
            return 'done_no_due' if is_done else 'open_ontime'
        if is_done:
            return 'done_ontime' if done_date <= due else 'done_delay'
        return 'open_ontime' if today <= due else 'open_overtime'

    tasks_all = []

    def _add(task_type, label, source, due, start, done, pts, priority='Medium', project=''):
        tl = _timeliness(due, done)
        tasks_all.append({
            'type': task_type, 'label': label, 'source': source,
            'due': due, 'start': start, 'done': done,
            'timeliness': tl, 'pts': pts,
            'priority': priority, 'project': project,
            'is_done': bool(done),
        })

    # Load config bobot
    cfg_rows = db.execute('SELECT * FROM task_perf_config ORDER BY sort_order').fetchall()
    cfg = {r['task_type']: r for r in cfg_rows}

    def _bpts(ctype):
        return float(cfg[ctype]['base_points']) if ctype in cfg else 0

    def _pmult(ctype, priority):
        if ctype not in cfg:
            return 1.0
        c = cfg[ctype]
        p = (priority or 'Medium').lower()
        if p in ('critical', 'blocker', 'kritis'): return float(c['priority_critical'])
        if p in ('high', 'tinggi'):                return float(c['priority_high'])
        if p in ('low', 'rendah'):                 return float(c['priority_low'])
        return float(c['priority_medium'])

    def _omult(ctype, due, done):
        if ctype not in cfg or not due or not done:
            return 1.0
        c = cfg[ctype]
        return float(c['ontime_mult']) if done <= due else float(c['late_mult'])

    def _dw(col, params):
        cl = []
        if date_from: cl.append(f'{col} >= ?'); params.append(date_from)
        if date_to:   cl.append(f'{col} <= ?'); params.append(date_to)
        return (' AND ' + ' AND '.join(cl)) if cl else ''

    # ── Project tasks sebagai PIC ───────────────────────────────────────────
    p = [emp_id]
    rows = db.execute(f'''
        SELECT p.name, p.start_date, p.end_date, p.status, p.code
        FROM pc_projects p WHERE p.pic_id=? AND p.deleted_at IS NULL{_dw("p.start_date", p)}
    ''', p).fetchall()
    for r in rows:
        done_date = r['end_date'] if r['status'] in ('done','closed','selesai') else None
        pts = round(_bpts('project_lead') * _omult('project_lead', r['end_date'], done_date), 2)
        _add('project_lead', 'Project PIC/Lead', f"Project: {r['name']}",
             r['end_date'], r['start_date'], done_date, pts, project=r['name'])

    # ── Project sebagai Implementor ─────────────────────────────────────────
    p = [emp_id, emp_id]
    rows = db.execute(f'''
        SELECT p.name, p.start_date, p.end_date, p.status
        FROM pc_projects p
        WHERE (p.implementor_id=? OR p.co_leader_id=?)
          AND p.pic_id != ? AND p.deleted_at IS NULL{_dw("p.start_date", p)}
    ''', p + [emp_id]).fetchall()
    for r in rows:
        done_date = r['end_date'] if r['status'] in ('done','closed','selesai') else None
        pts = round(_bpts('project_impl') * _omult('project_impl', r['end_date'], done_date), 2)
        _add('project_impl', 'Project Implementor', f"Project: {r['name']}",
             r['end_date'], r['start_date'], done_date, pts, project=r['name'])

    # ── Project Member ──────────────────────────────────────────────────────
    p = [emp_id]
    rows = db.execute(f'''
        SELECT p.name, p.start_date, p.end_date, p.status
        FROM pc_members m JOIN pc_projects p ON p.id=m.project_id
        WHERE m.employee_id=? AND p.deleted_at IS NULL
          AND p.pic_id != ? AND p.implementor_id != ?
          AND (p.co_leader_id IS NULL OR p.co_leader_id != ?){_dw("p.start_date", p)}
    ''', p + [emp_id, emp_id, emp_id]).fetchall()
    for r in rows:
        done_date = r['end_date'] if r['status'] in ('done','closed','selesai') else None
        pts = round(_bpts('project_member') * _omult('project_member', r['end_date'], done_date), 2)
        _add('project_member', 'Project Member', f"Project: {r['name']}",
             r['end_date'], r['start_date'], done_date, pts, project=r['name'])

    # ── Project Issues ──────────────────────────────────────────────────────
    difficulty_map = {'hard':2.0,'sulit':2.0,'normal':1.0,'easy':0.5,'mudah':0.5}
    p = [emp_id]
    rows = db.execute(f'''
        SELECT i.issue_no, i.title, i.difficulty, i.priority,
               i.resolved_date, i.created_at, i.status_programmer, p.name AS proj
        FROM pc_issues i JOIN pc_projects p ON p.id=i.project_id
        WHERE i.pic_programmer_id=?{_dw("i.created_at", p)}
    ''', p).fetchall()
    for r in rows:
        dm = difficulty_map.get((r['difficulty'] or 'Normal').lower(), 1.0)
        is_done = r['status_programmer'] in ('done','closed','resolved')
        done_date = r['resolved_date'] if is_done else None
        pts = round(_bpts('project_issue') * dm
                    * _pmult('project_issue', r['priority'])
                    * _omult('project_issue', done_date, done_date), 2)
        _add('project_issue', f"Issue ({r['difficulty'] or 'Normal'})",
             f"#{r['issue_no']} {r['title'][:35]} ({r['proj']})",
             done_date, r['created_at'][:10] if r['created_at'] else None,
             done_date, pts, r['priority'] or 'Medium', r['proj'])

    # ── Project Tasks ───────────────────────────────────────────────────────
    p = [emp_id]
    rows = db.execute(f'''
        SELECT t.title, t.priority, t.due_date, t.status, t.created_at, p.name AS proj
        FROM pc_task_assignees ta JOIN pc_tasks t ON t.id=ta.task_id
        JOIN pc_projects p ON p.id=t.project_id
        WHERE ta.employee_id=?{_dw("t.created_at", p)}
    ''', p).fetchall()
    for r in rows:
        is_done = r['status'] in ('done','closed')
        done_date = r['due_date'] if is_done else None  # proxy: done on due_date
        pts = round(_bpts('project_task') * _pmult('project_task', r['priority']), 2)
        _add('project_task', 'Task Project', f"{r['title'][:40]} ({r['proj']})",
             r['due_date'], r['created_at'][:10] if r['created_at'] else None,
             done_date if is_done else None, pts, r['priority'] or 'Medium', r['proj'])

    # ── POC / Presales ──────────────────────────────────────────────────────
    p = [emp_id]
    rows = db.execute(f'''
        SELECT r.req_no, r.subject, r.status, r.request_type, r.created_at
        FROM sc_presales_assignees pa JOIN sc_presales_requests r ON r.id=pa.request_id
        WHERE pa.employee_id=?{_dw("r.created_at", p)}
    ''', p).fetchall()
    for r in rows:
        is_done = r['status'] in ('done','closed','approved')
        done_date = r['created_at'][:10] if (is_done and r['created_at']) else None
        pts = round(_bpts('poc_presales'), 2)
        _add('poc_presales', f"POC/Presales",
             f"{r['request_type'].upper()} {r['req_no']}: {r['subject'][:35]}",
             None, r['created_at'][:10] if r['created_at'] else None,
             done_date, pts, 'Medium')

    # ── Support Tickets ─────────────────────────────────────────────────────
    p = [emp_id]
    rows = db.execute(f'''
        SELECT t.ticket_no, t.subject, t.priority, t.due_date, t.reported_at, t.status
        FROM sc_ticket_assignees ta JOIN sc_tickets t ON t.id=ta.ticket_id
        WHERE ta.employee_id=?{_dw("t.reported_at", p)}
    ''', p).fetchall()
    for r in rows:
        is_done = r['status'] in ('resolved','closed')
        done_date = r['due_date'] if is_done else None  # proxy
        pts = round(_bpts('support_ticket') * _pmult('support_ticket', r['priority'] or 'Medium')
                    * _omult('support_ticket', r['due_date'], done_date), 2)
        _add('support_ticket', 'Tiket Support',
             f"#{r['ticket_no']}: {r['subject'][:40]}",
             r['due_date'],
             r['reported_at'][:10] if r['reported_at'] else None,
             done_date, pts, r['priority'] or 'Medium')

    # ── Hitung timeliness ───────────────────────────────────────────────────
    def _cnt(tl): return sum(1 for t in tasks_all if t['timeliness'] == tl)
    done_ontime   = _cnt('done_ontime')
    done_delay    = _cnt('done_delay')
    done_no_due   = _cnt('done_no_due')
    open_ontime   = _cnt('open_ontime')
    open_overtime = _cnt('open_overtime')
    total_done    = done_ontime + done_delay + done_no_due
    total_open    = open_ontime + open_overtime
    total_with_due = done_ontime + done_delay
    ontime_rate   = round(done_ontime / total_with_due * 100, 1) if total_with_due else None

    # ── Concurrency (max & avg task aktif bersamaan) ─────────────────────────
    # Pakai event scan: setiap task punya [start, end]. Scan timeline, hitung max tumpang tindih.
    events = []
    for t in tasks_all:
        s = t['start'] or t['due'] or today
        e = t['done'] or (today if not t['is_done'] else t['done']) or today
        if s and e and s <= e:
            events.append((s, +1))
            events.append((e, -1))
    events.sort(key=lambda x: (x[0], x[1]))
    cur = mx = 0
    for _, d in events:
        cur += d
        if cur > mx: mx = cur
    concurrent_max = mx

    # Avg concurrent: total task-days / period days
    total_task_days = 0
    for t in tasks_all:
        s = t['start'] or t['due'] or today
        e = t['done'] or today
        try:
            d0 = _dt.strptime(s[:10], '%Y-%m-%d').date()
            d1 = _dt.strptime(e[:10], '%Y-%m-%d').date()
            total_task_days += max(1, (d1 - d0).days + 1)
        except Exception:
            total_task_days += 1
    period_days = 1
    if date_from and date_to:
        try:
            period_days = max(1, (_dt.strptime(date_to, '%Y-%m-%d').date()
                                  - _dt.strptime(date_from, '%Y-%m-%d').date()).days + 1)
        except Exception:
            pass
    concurrent_avg = round(total_task_days / period_days, 1) if period_days else 0

    # ── By type summary ──────────────────────────────────────────────────────
    by_type = {}
    for t in tasks_all:
        tp = t['type']
        if tp not in by_type:
            by_type[tp] = {'label': t['label'], 'done': 0, 'ontime': 0,
                           'delay': 0, 'overtime': 0, 'open': 0}
        tl = t['timeliness']
        if 'done' in tl:
            by_type[tp]['done'] += 1
            if tl == 'done_ontime': by_type[tp]['ontime'] += 1
            elif tl == 'done_delay': by_type[tp]['delay'] += 1
        else:
            by_type[tp]['open'] += 1
            if tl == 'open_overtime': by_type[tp]['overtime'] += 1

    total_raw = round(sum(t['pts'] for t in tasks_all if t['is_done']), 2)

    return {
        'tasks_all':      tasks_all,
        'done_ontime':    done_ontime,
        'done_delay':     done_delay,
        'done_no_due':    done_no_due,
        'open_ontime':    open_ontime,
        'open_overtime':  open_overtime,
        'total_done':     total_done,
        'total_open':     total_open,
        'ontime_rate':    ontime_rate,
        'concurrent_max': concurrent_max,
        'concurrent_avg': concurrent_avg,
        'by_type':        by_type,
        'total_raw':      total_raw,
    }


def start_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(lambda: run_contract_reminders('auto'),
                          'cron', hour=8, minute=0,
                          id='contract_reminder', replace_existing=True)
        scheduler.add_job(lambda: run_subscription_reminders('auto'),
                          'cron', hour=8, minute=5,
                          id='sub_reminder', replace_existing=True)
        scheduler.add_job(check_for_updates,
                          'interval', hours=6,
                          id='update_check', replace_existing=True)
        scheduler.start()
        import atexit
        atexit.register(scheduler.shutdown)
        print(" Scheduler aktif: cek kontrak 08:00, cek subscription 08:05, cek update setiap 6 jam")
        # Cek update saat startup (non-blocking, delay 30 detik)
        import threading
        threading.Timer(30, check_for_updates).start()
    except Exception as e:
        print(f" Scheduler gagal: {e}")

# ─── Auth Routes ───────────────────────────────────────────────────────────────

def _verify_recaptcha(token, secret_key):
    """Verify reCAPTCHA v3 token, return score (0.0–1.0) or None on failure."""
    try:
        resp = req_lib.post('https://www.google.com/recaptcha/api/siteverify',
                            data={'secret': secret_key, 'response': token}, timeout=5)
        data = resp.json()
        if data.get('success'):
            return data.get('score', 0.0)
    except Exception:
        pass
    return None


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    db = get_db()
    cfg = get_settings(db)
    recaptcha_enabled  = cfg.get('recaptcha_enabled') == '1'
    recaptcha_site_key = cfg.get('recaptcha_site_key', '').strip()
    recaptcha_secret   = cfg.get('recaptcha_secret_key', '').strip()
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        # reCAPTCHA v3 check
        if recaptcha_enabled and recaptcha_secret:
            rc_token = request.form.get('g-recaptcha-response', '')
            score = _verify_recaptcha(rc_token, recaptcha_secret)
            if score is None or score < 0.5:
                flash('Verifikasi bot gagal. Coba lagi.', 'danger')
                return render_template('login.html',
                    google_oauth_enabled=(cfg.get('google_oauth_enabled') == '1' and bool(cfg.get('google_client_id') or GOOGLE_CLIENT_ID)),
                    recaptcha_enabled=recaptcha_enabled,
                    recaptcha_site_key=recaptcha_site_key)
        user = db.execute('SELECT * FROM users WHERE username=? AND is_active=1',
                          (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            if user['mfa_enabled'] and user['totp_secret']:
                session['pending_mfa_user_id']   = user['id']
                _next_mfa = request.args.get('next') or ''
                if _next_mfa and (not _next_mfa.startswith('/') or _next_mfa.startswith('//')):
                    _next_mfa = ''
                session['pending_mfa_next'] = _next_mfa or url_for('portal')
                return redirect(url_for('login_mfa'))
            session['user_id']   = user['id']
            session['username']  = user['username']
            session['user_name'] = user['full_name'] or user['username']
            session['user_role'] = user['role']
            session['show_mfa_prompt'] = not bool(user['mfa_enabled'])
            db.execute('UPDATE users SET last_login=? WHERE id=?',
                       (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
            db.commit()
            audit_log('login', 'users', user['id'], f"Login berhasil via form", app_slug='portal')
            flash(f'Selamat datang, {session["user_name"]}!', 'success')
            _next = request.args.get('next') or ''
            if _next and (not _next.startswith('/') or _next.startswith('//')):
                _next = ''
            return redirect(_next or url_for('portal'))
        flash('Username atau password salah', 'danger')
    google_on = (cfg.get('google_oauth_enabled') == '1' and bool(cfg.get('google_client_id') or GOOGLE_CLIENT_ID))
    return render_template('login.html',
        google_oauth_enabled=google_on,
        recaptcha_enabled=recaptcha_enabled,
        recaptcha_site_key=recaptcha_site_key)

@app.route('/login/mfa', methods=['GET', 'POST'])
def login_mfa():
    pending_id = session.get('pending_mfa_user_id')
    if not pending_id:
        return redirect(url_for('login'))
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        db   = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (pending_id,)).fetchone()
        if user and verify_totp(user['totp_secret'], code):
            session.pop('pending_mfa_user_id', None)
            next_url = session.pop('pending_mfa_next', url_for('portal'))
            session['user_id']   = user['id']
            session['username']  = user['username']
            session['user_name'] = user['full_name'] or user['username']
            session['user_role'] = user['role']
            session['show_mfa_prompt'] = False  # sudah MFA, tidak perlu prompt
            db.execute('UPDATE users SET last_login=? WHERE id=?',
                       (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
            db.commit()
            audit_log('login', 'users', user['id'], 'Login berhasil via MFA', app_slug='portal')
            flash(f'Selamat datang, {session["user_name"]}!', 'success')
            return redirect(next_url)
        flash('Kode MFA salah atau kadaluarsa. Coba lagi.', 'danger')
    return render_template('mfa_login.html')

def _google_callback_url(settings):
    """Bangun redirect URI yang konsisten: utamakan app_url dari settings."""
    base = (settings.get('app_url') or '').rstrip('/')
    if base:
        return base + '/login/google/callback'
    return url_for('login_google_callback', _external=True)

@app.route('/login/google')
def login_google():
    db = get_db()
    settings = get_settings(db)
    client_id = settings.get('google_client_id') or GOOGLE_CLIENT_ID
    oauth_on  = settings.get('google_oauth_enabled', '0') == '1' or GOOGLE_CLIENT_ID
    if not client_id or not oauth_on:
        flash('Login Google belum dikonfigurasi. Hubungi administrator.', 'warning')
        return redirect(url_for('login'))
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    callback_url = _google_callback_url(settings)
    params = {
        'client_id':     client_id,
        'redirect_uri':  callback_url,
        'response_type': 'code',
        'scope':         'openid email profile',
        'state':         state,
        'prompt':        'select_account',
        'access_type':   'online',
    }
    from urllib.parse import urlencode
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")

@app.route('/login/google/callback')
def login_google_callback():
    db = get_db()
    settings = get_settings(db)
    client_id     = settings.get('google_client_id')     or GOOGLE_CLIENT_ID
    client_secret = settings.get('google_client_secret') or GOOGLE_CLIENT_SECRET

    error = request.args.get('error')
    if error:
        flash(f'Login Google dibatalkan: {error}', 'warning')
        return redirect(url_for('login'))

    state = request.args.get('state', '')
    if state != session.pop('oauth_state', None):
        flash('State tidak valid. Coba lagi.', 'danger')
        return redirect(url_for('login'))

    code = request.args.get('code')
    if not code:
        flash('Tidak mendapat kode otorisasi dari Google.', 'danger')
        return redirect(url_for('login'))

    callback_url = _google_callback_url(settings)
    try:
        token_resp = req_lib.post(GOOGLE_TOKEN_URL, data={
            'code':          code,
            'client_id':     client_id,
            'client_secret': client_secret,
            'redirect_uri':  callback_url,
            'grant_type':    'authorization_code',
        }, timeout=10)
        token_data = token_resp.json()
        access_token = token_data.get('access_token')
        if not access_token:
            raise ValueError(token_data.get('error_description', 'Tidak ada access token'))

        user_resp = req_lib.get(GOOGLE_USERINFO_URL,
                                headers={'Authorization': f'Bearer {access_token}'}, timeout=10)
        ginfo = user_resp.json()
    except Exception as e:
        flash(f'Gagal menghubungi Google: {e}', 'danger')
        return redirect(url_for('login'))

    google_email = ginfo.get('email', '').lower().strip()
    google_id    = ginfo.get('sub', '')
    google_name  = ginfo.get('name', '')
    verified     = ginfo.get('email_verified', False)

    if not google_email or not verified:
        flash('Email Google tidak terverifikasi.', 'danger')
        return redirect(url_for('login'))

    # Allowed domain check (optional — jika ada setting google_workspace_domain)
    allowed_domain = (settings.get('google_workspace_domain') or '').strip().lower()
    if allowed_domain:
        domain = google_email.split('@')[-1]
        if domain != allowed_domain:
            flash(f'Hanya email @{allowed_domain} yang diizinkan masuk.', 'danger')
            audit_log('login_google_rejected', 'users', 0,
                      f'Email ditolak (domain): {google_email}', app_slug='portal')
            return redirect(url_for('login'))

    # Cari user berdasarkan google_id dulu, lalu email
    user = db.execute("SELECT * FROM users WHERE google_id=? AND is_active=1",
                      (google_id,)).fetchone()
    if not user:
        user = db.execute("SELECT * FROM users WHERE LOWER(email)=? AND is_active=1",
                          (google_email,)).fetchone()

    emp = None
    if not user:
        # Cek apakah ada karyawan dengan email yang sama dan sudah punya user manual
        emp = db.execute("SELECT * FROM employees WHERE LOWER(email)=? AND is_active=1",
                         (google_email,)).fetchone()
        if emp and emp['user_id']:
            # Merge: hubungkan google_id ke user manual yang sudah ada
            existing = db.execute('SELECT * FROM users WHERE id=? AND is_active=1',
                                  (emp['user_id'],)).fetchone()
            if existing:
                db.execute("UPDATE users SET google_id=?, email=? WHERE id=?",
                           (google_id, google_email, existing['id']))
                db.commit()
                user = db.execute('SELECT * FROM users WHERE id=?', (existing['id'],)).fetchone()
                audit_log('merge_google', 'users', user['id'],
                          f'Akun manual digabung dengan Google ({google_email})', app_slug='portal')

    if not user:
        # Auto-create user baru tanpa akses app — admin harus grant via Akses Aplikasi
        username_base = google_email.split('@')[0].lower().replace('.', '_')
        username = username_base
        suffix = 1
        while db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            username = f'{username_base}{suffix}'
            suffix += 1
        cur = db.execute(
            'INSERT INTO users(username,password_hash,full_name,role,email,google_id,is_active) VALUES(?,?,?,?,?,?,1)',
            (username, generate_password_hash(secrets.token_hex(32), method='pbkdf2:sha256'),
             google_name, 'user', google_email, google_id))
        db.commit()
        new_uid = cur.lastrowid
        # Auto-link ke karyawan jika email cocok dan belum punya user
        if emp and not emp['user_id']:
            db.execute('UPDATE employees SET user_id=? WHERE id=?', (new_uid, emp['id']))
            db.commit()
        user = db.execute('SELECT * FROM users WHERE id=?', (new_uid,)).fetchone()
        audit_log('register_google', 'users', user['id'],
                  f'Akun baru via Google ({google_email})', app_slug='portal')

    # Update google_id dan last_login
    db.execute("UPDATE users SET google_id=?, last_login=? WHERE id=?",
               (google_id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
    db.commit()

    session['user_id']   = user['id']
    session['username']  = user['username']
    session['user_name'] = user['full_name'] or google_name or user['username']
    session['user_role'] = user['role']
    session['show_mfa_prompt'] = not bool(user['mfa_enabled'])
    audit_log('login_google', 'users', user['id'],
              f'Login via Google ({google_email})', app_slug='portal')
    flash(f'Selamat datang, {session["user_name"]}!', 'success')
    return redirect(url_for('portal'))

@app.route('/mfa/challenge', methods=['GET', 'POST'])
@login_required
def mfa_challenge():
    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        db   = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if user and verify_totp(user['totp_secret'], code):
            sk = session.pop('mfa_session_key', 'mfa_verified')
            session[sk] = datetime.now().timestamp()
            return redirect(session.pop('mfa_return_to', url_for('index')))
        flash('Kode MFA salah', 'danger')
    return render_template('mfa_challenge.html')

@app.route('/profile/mfa/setup', methods=['GET', 'POST'])
@login_required
def mfa_setup():
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'generate':
            secret = generate_totp_secret()
            session['pending_totp_secret'] = secret
            uri    = get_totp_uri(secret, user['username'])
            qr     = qr_png_base64(uri)
            return render_template('mfa_setup.html', user=user, secret=secret, qr=qr, step='verify')
        elif action == 'verify':
            secret = session.get('pending_totp_secret', '')
            code   = request.form.get('code', '').strip()
            if secret and verify_totp(secret, code):
                db.execute('UPDATE users SET totp_secret=?, mfa_enabled=1 WHERE id=?',
                           (secret, user['id']))
                db.commit()
                session.pop('pending_totp_secret', None)
                flash('Google Authenticator MFA berhasil diaktifkan! Silakan pilih aplikasi.', 'success')
                return redirect(url_for('portal'))
            flash('Kode verifikasi salah. Scan ulang QR dan coba lagi.', 'danger')
            uri = get_totp_uri(secret, user['username'])
            qr  = qr_png_base64(uri)
            return render_template('mfa_setup.html', user=user, secret=secret, qr=qr, step='verify')
        elif action == 'disable':
            code = request.form.get('code', '').strip()
            if user['totp_secret'] and verify_totp(user['totp_secret'], code):
                db.execute("UPDATE users SET totp_secret='', mfa_enabled=0 WHERE id=?", (user['id'],))
                db.commit()
                flash('MFA berhasil dinonaktifkan.', 'warning')
                return redirect(url_for('mfa_setup'))
            flash('Kode Authenticator salah. MFA tidak dinonaktifkan.', 'danger')
    mfa_on = bool(user['mfa_enabled'] and user['totp_secret'])
    if mfa_on:
        session['show_mfa_prompt'] = False
    return render_template('mfa_setup.html', user=user, step='intro', mfa_on=mfa_on)

@app.route('/mfa/dismiss-prompt', methods=['POST'])
@login_required
def mfa_dismiss_prompt():
    """User pilih 'Aktifkan Nanti' — sembunyikan prompt untuk sesi ini."""
    session['show_mfa_prompt'] = False
    return ('', 204)

@app.route('/admin/users/<int:uid>/send-reset', methods=['POST'])
@superadmin_required
def admin_send_reset(uid):
    """Superadmin kirim link reset password ke user tertentu."""
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=? AND is_active=1', (uid,)).fetchone()
    if not user:
        flash('User tidak ditemukan atau tidak aktif.', 'danger')
        return redirect(url_for('users_list'))
    # Hapus token lama
    db.execute('DELETE FROM password_reset_tokens WHERE user_id=?', (uid,))
    token   = secrets.token_urlsafe(48)
    expires = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    db.execute('INSERT INTO password_reset_tokens(user_id, token, expires_at) VALUES(?,?,?)',
               (uid, token, expires))
    db.commit()
    settings   = get_settings(db)
    reset_link = f"{get_base_url(settings)}/reset-password/{token}"
    sent = _send_reset_notifications(dict(user), reset_link, settings, db)
    if sent:
        flash(f'Link reset password dikirim ke {user["username"]} via: {", ".join(sent)}.', 'success')
    else:
        flash(f'Tidak ada kontak terdaftar untuk {user["username"]}. '
              f'Salin link ini dan kirim manual: {reset_link}', 'warning')
    return redirect(url_for('users_list'))

@app.route('/admin/users/<int:uid>/mfa-reset', methods=['POST'])
@superadmin_required
def admin_mfa_reset(uid):
    db = get_db()
    db.execute("UPDATE users SET totp_secret='', mfa_enabled=0 WHERE id=?", (uid,))
    db.commit()
    flash('MFA user berhasil direset. User harus setup ulang saat login berikutnya.', 'warning')
    return redirect(url_for('users_list'))

# ─── Forgot / Reset Password ───────────────────────────────────────────────────

def get_base_url(settings=None):
    """Kembalikan URL publik aplikasi. Prioritas: setting app_url > ProxyFix request.host_url."""
    if settings:
        configured = (settings.get('app_url') or '').strip().rstrip('/')
        if configured:
            return configured
    return request.host_url.rstrip('/')

RESET_TOKEN_TTL = 3600  # 1 jam

def _send_reset_notifications(user, reset_link, settings, db=None):
    """Kirim link reset password via email, Telegram, dan WhatsApp.
    Kontak diambil dari user fields; jika kosong, fallback ke data karyawan yang terhubung."""
    sent = []
    # Resolve contacts — merge user fields + linked employee
    if db is not None:
        contacts = get_user_contacts(db, user)
    else:
        contacts = {
            'email':       (user.get('email') or '').strip(),
            'phone':       (user.get('phone') or '').strip(),
            'telegram_id': (user.get('telegram_id') or '').strip(),
        }

    display_name = user['full_name'] or user['username']
    subject  = 'Reset Password — Aplikasi TalentCore'
    body_html = f'''
<p>Halo <b>{display_name}</b>,</p>
<p>Anda (atau seseorang) meminta reset password untuk akun <b>{user['username']}</b>.</p>
<p><a href="{reset_link}" style="background:#1a7a3a;color:#fff;padding:10px 20px;border-radius:6px;
   text-decoration:none;font-weight:bold">Klik di sini untuk reset password</a></p>
<p>Link ini hanya berlaku <b>1 jam</b> dan <b>langsung kadaluarsa setelah dibuka satu kali</b>.</p>
<p>Jika bukan Anda yang meminta, abaikan email ini.</p>
<hr><p style="color:#888;font-size:12px">Aplikasi TalentCore</p>
'''

    # Email
    if contacts.get('email'):
        ok, _ = send_email(settings, contacts['email'], subject, body_html)
        if ok:
            sent.append('email')

    # Telegram
    bot_token = settings.get('telegram_bot_token','').strip()
    if bot_token and contacts.get('telegram_id'):
        tg_msg = (f'🔐 *Reset Password*\n\nHalo {display_name},\n'
                  f'Klik link berikut untuk reset password akun `{user["username"]}`:\n\n'
                  f'{reset_link}\n\n_Link berlaku 1 jam dan sekali pakai._')
        ok, _ = send_telegram(bot_token, contacts['telegram_id'], tg_msg)
        if ok:
            sent.append('telegram')

    # WhatsApp
    wa_url     = settings.get('openwa_url','').strip()
    wa_key     = settings.get('openwa_api_key','').strip()
    wa_session = get_openwa_session(settings)  # portal reset password: pakai global session
    if wa_url and contacts.get('phone'):
        wa_msg = (f'🔐 Reset Password\n\nHalo {display_name},\n'
                  f'Link reset password akun *{user["username"]}*:\n\n{reset_link}\n\n'
                  f'Link berlaku 1 jam dan langsung kadaluarsa setelah dibuka.')
        ok, _ = send_whatsapp(wa_url, wa_key, wa_session, contacts['phone'], wa_msg)
        if ok:
            sent.append('whatsapp')

    return sent


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE (username=? OR email=?) AND is_active=1",
            (identifier, identifier)
        ).fetchone()
        if user:
            # Invalidate old tokens
            db.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (user['id'],))
            token    = secrets.token_urlsafe(48)
            expires  = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            db.execute(
                "INSERT INTO password_reset_tokens(user_id, token, expires_at) VALUES(?,?,?)",
                (user['id'], token, expires)
            )
            db.commit()
            settings   = get_settings(db)
            reset_link = f"{get_base_url(settings)}/reset-password/{token}"
            sent = _send_reset_notifications(dict(user), reset_link, settings, db)
            contacts = get_user_contacts(db, dict(user))
            if sent:
                flash(f'Link reset password dikirim via: {", ".join(sent)}. '
                      f'Cek email/Telegram/WhatsApp Anda.', 'success')
            elif not any([contacts.get('email'), contacts.get('phone'), contacts.get('telegram_id')]):
                flash('Tidak ada kontak (email/WA/Telegram) yang terdaftar pada akun ini. '
                      'Hubungi admin untuk reset password.', 'danger')
            else:
                flash('Gagal mengirim notifikasi — periksa konfigurasi email/WA/Telegram di pengaturan. '
                      'Hubungi admin untuk mendapatkan link reset.', 'warning')
        else:
            # Pesan generik agar tidak bisa enumerate user
            flash('Jika username/email terdaftar, link reset akan dikirim ke kontak yang tersimpan.', 'info')
        return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if 'user_id' in session:
        return redirect(url_for('index'))
    db  = get_db()
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=?", (token,)
    ).fetchone()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if not row or row['used'] or row['expires_at'] < now:
        flash('Link reset tidak valid atau sudah kadaluarsa. Silakan minta link baru.', 'danger')
        return redirect(url_for('forgot_password'))

    user = db.execute("SELECT * FROM users WHERE id=?", (row['user_id'],)).fetchone()
    if not user:
        flash('Akun tidak ditemukan.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_pass  = request.form.get('password', '').strip()
        new_pass2 = request.form.get('password2', '').strip()
        if len(new_pass) < 6:
            flash('Password minimal 6 karakter.', 'danger')
        elif new_pass != new_pass2:
            flash('Konfirmasi password tidak cocok.', 'danger')
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (generate_password_hash(new_pass, method='pbkdf2:sha256'), user['id']))
            # Mark token as used — link langsung kadaluarsa setelah dipakai
            db.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?", (token,))
            db.commit()
            flash('Password berhasil diubah. Silakan login.', 'success')
            return redirect(url_for('login'))
        return render_template('reset_password.html', token=token, user=user)

    # GET: mark token as used agar link tidak bisa dibuka ulang
    # Tapi jangan invalidate dulu — baru invalidate setelah POST sukses
    # (agar form bisa disubmit dari halaman yang sama)
    return render_template('reset_password.html', token=token, user=user)

@app.route('/logout')
def logout():
    audit_log('logout', 'users', session.get('user_id',''), 'User logout', app_slug='portal')
    session.clear()
    flash('Anda telah logout', 'info')
    return redirect(url_for('login'))

# ─── User Management (Superadmin) ─────────────────────────────────────────────

@app.route('/users')
@superadmin_required
def users_list():
    db    = get_db()
    users = db.execute('SELECT * FROM users ORDER BY role, username').fetchall()
    # Map user_id → linked employee contact (untuk ditampilkan di tabel)
    emps  = db.execute(
        'SELECT user_id, email, phone, telegram_id, name FROM employees WHERE user_id IS NOT NULL AND is_active=1'
    ).fetchall()
    linked_emps = {e['user_id']: e for e in emps}
    return render_template('users.html', users=users, linked_emps=linked_emps)

@app.route('/users/add', methods=['GET', 'POST'])
@superadmin_required
def user_add():
    if request.method == 'POST':
        username  = request.form['username'].strip()
        full_name = request.form.get('full_name','').strip()
        password  = request.form.get('password','')
        role      = request.form.get('role','admin')
        email     = request.form.get('email','').strip()
        phone     = request.form.get('phone','').strip()
        telegram  = request.form.get('telegram_id','').strip()
        if not username or not password:
            flash('Username dan password wajib diisi', 'danger')
        else:
            db = get_db()
            try:
                db.execute(
                    'INSERT INTO users(username,password_hash,full_name,role,email,phone,telegram_id) VALUES(?,?,?,?,?,?,?)',
                    (username, generate_password_hash(password, method='pbkdf2:sha256'),
                     full_name, role, email, phone, telegram))
                db.commit()
                flash(f'User {username} berhasil dibuat', 'success')
                return redirect(url_for('users_list'))
            except sqlite3.IntegrityError:
                flash('Username sudah digunakan', 'danger')
    return render_template('user_form.html', user=None)

@app.route('/users/<int:uid>/edit', methods=['GET', 'POST'])
@superadmin_required
def user_edit(uid):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user:
        flash('User tidak ditemukan', 'danger')
        return redirect(url_for('users_list'))
    if request.method == 'POST':
        full_name  = request.form.get('full_name','').strip()
        role       = request.form.get('role', user['role'])
        is_active  = 1 if request.form.get('is_active') else 0
        email      = request.form.get('email','').strip()
        phone      = request.form.get('phone','').strip()
        telegram   = request.form.get('telegram_id','').strip()
        new_pass   = request.form.get('password','').strip()
        if new_pass:
            db.execute(
                'UPDATE users SET full_name=?,role=?,is_active=?,email=?,phone=?,telegram_id=?,password_hash=? WHERE id=?',
                (full_name, role, is_active, email, phone, telegram,
                 generate_password_hash(new_pass, method='pbkdf2:sha256'), uid))
        else:
            db.execute(
                'UPDATE users SET full_name=?,role=?,is_active=?,email=?,phone=?,telegram_id=? WHERE id=?',
                (full_name, role, is_active, email, phone, telegram, uid))
        db.commit()
        flash('User diperbarui', 'success')
        if uid == session['user_id']:
            session['user_name'] = full_name or session['username']
            session['user_role'] = role
        return redirect(url_for('users_list'))
    linked_emp = db.execute(
        'SELECT name, email, phone, telegram_id FROM employees WHERE user_id=? AND is_active=1',
        (uid,)
    ).fetchone()
    return render_template('user_form.html', user=user, linked_emp=linked_emp)

@app.route('/users/<int:uid>/delete', methods=['POST'])
@superadmin_required
def user_delete(uid):
    if uid == session['user_id']:
        flash('Tidak bisa menghapus akun sendiri', 'danger')
        return redirect(url_for('users_list'))
    db = get_db()
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()
    flash('User dihapus', 'warning')
    return redirect(url_for('users_list'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if request.method == 'POST':
        full_name = request.form.get('full_name','').strip()
        old_pass  = request.form.get('old_password','')
        new_pass  = request.form.get('new_password','').strip()
        if new_pass:
            if not check_password_hash(user['password_hash'], old_pass):
                flash('Password lama salah', 'danger')
                return render_template('profile.html', user=user)
            db.execute('UPDATE users SET full_name=?,password_hash=? WHERE id=?',
                       (full_name, generate_password_hash(new_pass), session['user_id']))
        else:
            db.execute('UPDATE users SET full_name=? WHERE id=?',
                       (full_name, session['user_id']))
        db.commit()
        session['user_name'] = full_name or session['username']
        flash('Profil diperbarui', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=user)

# ─── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/portal')
@login_required
def portal():
    session.pop('active_app', None)   # clear app saat kembali ke portal
    db   = get_db()
    uid  = session.get('user_id')
    role = session.get('user_role', '')
    all_apps = db.execute('SELECT * FROM superapp_apps WHERE is_active=1 ORDER BY sort_order, name').fetchall()
    if role == 'superadmin':
        accessible = {a['slug'] for a in all_apps}
    else:
        rows = db.execute(
            'SELECT app_slug FROM user_app_access WHERE user_id=? AND is_active=1', (uid,)
        ).fetchall()
        accessible = {r['app_slug'] for r in rows}
    return render_template('portal.html', apps=all_apps, accessible=accessible)

@app.route('/portal/open/<slug>')
@login_required
def portal_open(slug):
    db  = get_db()
    app_row = db.execute('SELECT * FROM superapp_apps WHERE slug=? AND is_active=1', (slug,)).fetchone()
    if not app_row:
        flash('Aplikasi tidak ditemukan.', 'danger')
        return redirect(url_for('portal'))
    # Cek akses (superadmin bypass)
    if session.get('user_role') != 'superadmin':
        acc = db.execute('SELECT 1 FROM user_app_access WHERE user_id=? AND app_slug=? AND is_active=1',
                         (session['user_id'], slug)).fetchone()
        if not acc:
            flash('Anda tidak memiliki akses ke aplikasi ini.', 'danger')
            return redirect(url_for('portal'))
    if app_row['is_coming_soon']:
        flash('Aplikasi ini belum tersedia.', 'info')
        return redirect(url_for('portal'))
    session['active_app'] = slug
    audit_log('open_app', 'superapp_apps', slug, f"Buka aplikasi: {app_row['name']}", app_slug='portal')
    return redirect(app_row['url'])

@app.route('/portal/settings', methods=['GET', 'POST'])
@login_required
def portal_settings():
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal'))
    db    = get_db()
    users = db.execute('SELECT id, username, full_name, role, email, google_id FROM users WHERE is_active=1 ORDER BY role DESC, username').fetchall()
    apps  = db.execute('SELECT * FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()

    if request.method == 'POST':
        # Hapus semua akses non-superadmin lalu rebuild dari form
        db.execute("DELETE FROM user_app_access WHERE user_id IN (SELECT id FROM users WHERE role != 'superadmin')")
        for u in users:
            if u['role'] == 'superadmin':
                continue
            for a in apps:
                key    = f"access_{u['id']}_{a['slug']}"
                role_k = f"role_{u['id']}_{a['slug']}"
                if request.form.get(key):
                    app_role = request.form.get(role_k, 'user')
                    db.execute('''INSERT INTO user_app_access(user_id,app_slug,app_role,is_active)
                                  VALUES(?,?,?,1)
                                  ON CONFLICT(user_id,app_slug) DO UPDATE SET app_role=excluded.app_role,is_active=1''',
                               (u['id'], a['slug'], app_role))
        db.commit()
        flash('Pengaturan akses berhasil disimpan.', 'success')
        return redirect(url_for('portal_settings'))

    # Baca akses saat ini
    access_map = {}
    rows = db.execute('SELECT user_id, app_slug, app_role, is_active FROM user_app_access').fetchall()
    for r in rows:
        access_map[(r['user_id'], r['app_slug'])] = {'role': r['app_role'], 'active': r['is_active']}

    # Roles per app — include global roles (app_slug='') untuk setiap app
    all_roles    = db.execute("SELECT name, description, app_slug FROM roles ORDER BY is_system DESC, name").fetchall()
    global_roles = [r for r in all_roles if r['app_slug'] == '']
    roles_by_app = {}
    for a in apps:
        app_specific = [r for r in all_roles if r['app_slug'] == a['slug']]
        roles_by_app[a['slug']] = global_roles + app_specific

    return render_template('portal_settings.html', users=users, apps=apps,
                           access_map=access_map, roles_by_app=roles_by_app)

def is_portal_admin():
    """True jika user adalah superadmin atau admin (dapat akses portal management)."""
    return session.get('user_role') in ('superadmin', 'admin')

def is_superadmin():
    return session.get('user_role') == 'superadmin'

# ─── Portal: Kelola User ───────────────────────────────────────────────────────

@app.route('/portal/api/employees-search')
@login_required
def portal_emp_search():
    q   = request.args.get('q', '').strip()
    db  = get_db()
    if len(q) < 2:
        return jsonify([])
    rows = db.execute('''
        SELECT id, name, jabatan, divisi, email, phone, telegram_id
        FROM employees WHERE is_active=1 AND user_id IS NULL
        AND (name LIKE ? OR jabatan LIKE ? OR divisi LIKE ?)
        ORDER BY name LIMIT 20
    ''', (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/portal/users')
@login_required
def portal_users():
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal'))
    db    = get_db()
    users = db.execute('SELECT * FROM users ORDER BY role, username').fetchall()
    emps  = db.execute(
        'SELECT user_id, email, phone, telegram_id, name FROM employees WHERE user_id IS NOT NULL AND is_active=1'
    ).fetchall()
    linked_emps = {e['user_id']: e for e in emps}
    apps = db.execute('SELECT slug, name FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()
    # Akses per user
    access_rows = db.execute('SELECT user_id, app_slug, app_role FROM user_app_access WHERE is_active=1').fetchall()
    user_access = {}
    for r in access_rows:
        user_access.setdefault(r['user_id'], {})[r['app_slug']] = r['app_role']
    # Deteksi semua duplikat email — termasuk email dari tabel employees
    from collections import defaultdict
    # effective email: users.email jika ada, fallback ke linked employee email
    def eff_email(u):
        e = (u['email'] or '').strip().lower()
        if not e:
            emp = linked_emps.get(u['id'])
            if emp:
                e = (emp['email'] or '').strip().lower()
        return e

    email_groups = defaultdict(list)
    for u in users:
        if not u['is_active']:
            continue
        em = eff_email(u)
        if em:
            email_groups[em].append(u)
    duplicate_emails = {email: grp for email, grp in email_groups.items() if len(grp) > 1}
    # merge_candidates: google_uid -> manual_uid
    merge_candidates = {}
    for grp in duplicate_emails.values():
        google_u = next((u for u in grp if u['google_id']), None)
        manual_u = next((u for u in grp if not u['google_id']), None)
        if google_u and manual_u:
            merge_candidates[google_u['id']] = manual_u['id']
    return render_template('portal_users.html', users=users, linked_emps=linked_emps,
                           apps=apps, user_access=user_access,
                           merge_candidates=merge_candidates,
                           duplicate_count=len(duplicate_emails))

@app.route('/portal/users/merge-all-duplicates', methods=['POST'])
@login_required
def portal_user_merge_all():
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal_users'))
    db = get_db()
    users = db.execute('SELECT * FROM users WHERE is_active=1').fetchall()
    # Bangun map user_id -> employee email untuk fallback
    emp_emails = {r['user_id']: (r['email'] or '').strip().lower()
                  for r in db.execute('SELECT user_id, email FROM employees WHERE user_id IS NOT NULL AND is_active=1').fetchall()}
    from collections import defaultdict
    email_groups = defaultdict(list)
    for u in users:
        em = (u['email'] or '').strip().lower() or emp_emails.get(u['id'], '')
        if em:
            email_groups[em].append(dict(u))
    merged = 0
    for email, grp in email_groups.items():
        if len(grp) < 2:
            continue
        # Prioritas: user dengan google_id jadi "donor", user tanpa google_id jadi "penerima"
        # Jika keduanya manual/google, pilih yang lebih lama (id lebih kecil) sebagai penerima
        google_users = [u for u in grp if u['google_id']]
        manual_users = [u for u in grp if not u['google_id']]
        if not google_users:
            # Dua user manual: nonaktifkan yang lebih baru
            grp_sorted = sorted(grp, key=lambda u: u['id'])
            keep, drop = grp_sorted[0], grp_sorted[1]
        else:
            keep  = manual_users[0] if manual_users else sorted(google_users, key=lambda u: u['id'])[0]
            donor = google_users[0]
            # Pastikan email tersimpan di users.email untuk keep
            keep_email = (keep.get('email') or '').strip() or emp_emails.get(keep['id'], '')
            if keep_email and not keep.get('email'):
                db.execute("UPDATE users SET email=? WHERE id=?", (keep_email, keep['id']))
            # Pindahkan google_id ke keep
            db.execute("UPDATE users SET google_id=? WHERE id=?", (donor['google_id'], keep['id']))
            # Pindahkan akses app
            for a in db.execute('SELECT * FROM user_app_access WHERE user_id=?', (donor['id'],)).fetchall():
                db.execute('INSERT OR IGNORE INTO user_app_access(user_id,app_slug,app_role,is_active) VALUES(?,?,?,?)',
                           (keep['id'], a['app_slug'], a['app_role'], a['is_active']))
            # Update link karyawan
            db.execute("UPDATE employees SET user_id=? WHERE user_id=?", (keep['id'], donor['id']))
            drop = donor
        db.execute("UPDATE users SET is_active=0, email='' WHERE id=?", (drop['id'],))
        audit_log('merge_user_bulk', 'users', keep['id'],
                  f'Duplikat email {email}: akun {drop["username"]} digabung ke {keep["username"]}',
                  app_slug='portal')
        merged += 1
    db.commit()
    flash(f'{merged} duplikat email berhasil digabungkan.', 'success')
    return redirect(url_for('portal_users'))

@app.route('/portal/users/<int:google_uid>/merge/<int:manual_uid>', methods=['POST'])
@login_required
def portal_user_merge(google_uid, manual_uid):
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal_users'))
    db = get_db()
    g_user = db.execute('SELECT * FROM users WHERE id=?', (google_uid,)).fetchone()
    m_user = db.execute('SELECT * FROM users WHERE id=?', (manual_uid,)).fetchone()
    if not g_user or not m_user:
        flash('User tidak ditemukan.', 'danger')
        return redirect(url_for('portal_users'))
    # Pindahkan google_id ke user manual
    db.execute("UPDATE users SET google_id=?, email=COALESCE(NULLIF(email,''),?) WHERE id=?",
               (g_user['google_id'], g_user['email'], manual_uid))
    # Pindahkan akses app dari Google user ke user manual (INSERT OR IGNORE)
    access = db.execute('SELECT * FROM user_app_access WHERE user_id=?', (google_uid,)).fetchall()
    for a in access:
        db.execute('''INSERT OR IGNORE INTO user_app_access(user_id,app_slug,app_role,is_active)
                      VALUES(?,?,?,?)''', (manual_uid, a['app_slug'], a['app_role'], a['is_active']))
    # Nonaktifkan akun Google duplikat
    db.execute("UPDATE users SET is_active=0 WHERE id=?", (google_uid,))
    # Update link karyawan jika ada
    db.execute("UPDATE employees SET user_id=? WHERE user_id=?", (manual_uid, google_uid))
    db.commit()
    audit_log('merge_user', 'users', manual_uid,
              f'Akun Google {g_user["username"]} digabung ke {m_user["username"]}', app_slug='portal')
    flash(f'Akun Google {g_user["username"]} berhasil digabung ke {m_user["username"]}.', 'success')
    return redirect(url_for('portal_users'))

@app.route('/portal/users/add', methods=['GET', 'POST'])
@login_required
def portal_user_add():
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal'))
    db   = get_db()
    apps = db.execute('SELECT slug, name, icon FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()
    all_roles    = db.execute("SELECT name, description, app_slug FROM roles ORDER BY is_system DESC, name").fetchall()
    global_roles = [r for r in all_roles if r['app_slug'] == '']
    roles_by_app = {}
    for a in apps:
        specific = [r for r in all_roles if r['app_slug'] == a['slug']]
        roles_by_app[a['slug']] = global_roles + specific

    if request.method == 'POST':
        username  = request.form.get('username', '').strip()
        full_name = request.form.get('full_name', '').strip()
        password  = request.form.get('password', '')
        # role portal: hanya set jika access_portal dicentang, default viewer
        role      = request.form.get('role', 'viewer') if request.form.get('access_portal') else 'viewer'
        email     = request.form.get('email', '').strip()
        phone     = request.form.get('phone', '').strip()
        telegram  = request.form.get('telegram_id', '').strip()
        emp_id    = request.form.get('emp_id', type=int)
        if not username or not password:
            flash('Username dan password wajib diisi', 'danger')
        elif email and db.execute("SELECT id FROM users WHERE LOWER(email)=? AND is_active=1", (email.lower(),)).fetchone():
            flash(f'Email {email} sudah digunakan oleh user lain.', 'danger')
        else:
            try:
                cur = db.execute(
                    'INSERT INTO users(username,password_hash,full_name,role,email,phone,telegram_id) VALUES(?,?,?,?,?,?,?)',
                    (username, generate_password_hash(password, method='pbkdf2:sha256'),
                     full_name, role, email, phone, telegram))
                new_uid = cur.lastrowid
                if emp_id:
                    db.execute('UPDATE employees SET user_id=? WHERE id=? AND user_id IS NULL', (new_uid, emp_id))
                for a in apps:
                    if request.form.get(f'access_{a["slug"]}'):
                        app_role = request.form.get(f'role_{a["slug"]}', 'admin')
                        db.execute('''INSERT INTO user_app_access(user_id,app_slug,app_role,is_active) VALUES(?,?,?,1)
                                      ON CONFLICT(user_id,app_slug) DO UPDATE SET app_role=excluded.app_role,is_active=1''',
                                   (new_uid, a['slug'], app_role))
                db.commit()
                flash(f'User {username} berhasil dibuat', 'success')
                return redirect(url_for('portal_users'))
            except sqlite3.IntegrityError:
                flash('Username sudah digunakan', 'danger')
    free_emps = db.execute(
        'SELECT id,name,jabatan,divisi FROM employees WHERE is_active=1 AND user_id IS NULL ORDER BY name'
    ).fetchall()
    return render_template('portal_user_form.html', user=None, apps=apps, free_emps=free_emps,
                           global_roles=global_roles, roles_by_app=roles_by_app, user_access={})

@app.route('/portal/users/<int:uid>/edit', methods=['GET', 'POST'])
@login_required
def portal_user_edit(uid):
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal'))
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user:
        flash('User tidak ditemukan', 'danger')
        return redirect(url_for('portal_users'))
    apps = db.execute('SELECT slug, name, icon FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()
    all_roles    = db.execute("SELECT name, description, app_slug FROM roles ORDER BY is_system DESC, name").fetchall()
    global_roles = [r for r in all_roles if r['app_slug'] == '']
    roles_by_app = {}
    for a in apps:
        specific = [r for r in all_roles if r['app_slug'] == a['slug']]
        roles_by_app[a['slug']] = global_roles + specific

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        role      = request.form.get('role', 'viewer') if request.form.get('access_portal') else 'viewer'
        is_active = 1 if request.form.get('is_active') else 0
        email     = request.form.get('email', '').strip()
        phone     = request.form.get('phone', '').strip()
        telegram  = request.form.get('telegram_id', '').strip()
        new_pass  = request.form.get('password', '').strip()
        if email:
            dup = db.execute("SELECT id FROM users WHERE LOWER(email)=? AND is_active=1 AND id!=?",
                             (email.lower(), uid)).fetchone()
            if dup:
                flash(f'Email {email} sudah digunakan oleh user lain.', 'danger')
                linked_emp = db.execute('SELECT id,name,email,phone,telegram_id FROM employees WHERE user_id=? AND is_active=1', (uid,)).fetchone()
                user_access = {r['app_slug']: r['app_role'] for r in
                               db.execute('SELECT app_slug,app_role FROM user_app_access WHERE user_id=? AND is_active=1',(uid,)).fetchall()}
                return render_template('portal_user_form.html', user=user, apps=apps,
                                       linked_emp=linked_emp, free_emps=[],
                                       global_roles=global_roles, roles_by_app=roles_by_app,
                                       user_access=user_access)
        if new_pass:
            db.execute('UPDATE users SET full_name=?,role=?,is_active=?,email=?,phone=?,telegram_id=?,password_hash=? WHERE id=?',
                       (full_name, role, is_active, email, phone, telegram,
                        generate_password_hash(new_pass, method='pbkdf2:sha256'), uid))
        else:
            db.execute('UPDATE users SET full_name=?,role=?,is_active=?,email=?,phone=?,telegram_id=? WHERE id=?',
                       (full_name, role, is_active, email, phone, telegram, uid))
        # Update per-app access: hapus lama, insert baru
        db.execute('DELETE FROM user_app_access WHERE user_id=?', (uid,))
        for a in apps:
            if request.form.get(f'access_{a["slug"]}'):
                app_role = request.form.get(f'role_{a["slug"]}', 'admin')
                db.execute('INSERT INTO user_app_access(user_id,app_slug,app_role,is_active) VALUES(?,?,?,1)',
                           (uid, a['slug'], app_role))
        db.commit()
        if uid == session['user_id']:
            session['user_name'] = full_name or session['username']
            session['user_role'] = role
        flash('User diperbarui', 'success')
        return redirect(url_for('portal_users'))
    linked_emp  = db.execute(
        'SELECT id,name,email,phone,telegram_id FROM employees WHERE user_id=? AND is_active=1', (uid,)
    ).fetchone()
    user_access = {r['app_slug']: r['app_role'] for r in
                   db.execute('SELECT app_slug,app_role FROM user_app_access WHERE user_id=? AND is_active=1',(uid,)).fetchall()}
    return render_template('portal_user_form.html', user=user, apps=apps,
                           linked_emp=linked_emp, free_emps=[],
                           global_roles=global_roles, roles_by_app=roles_by_app,
                           user_access=user_access)

@app.route('/portal/users/<int:uid>/delete', methods=['POST'])
@login_required
def portal_user_delete(uid):
    if not is_portal_admin():
        return jsonify({'error': 'forbidden'}), 403
    if uid == session['user_id']:
        flash('Tidak bisa menonaktifkan akun sendiri', 'danger')
        return redirect(url_for('portal_users'))
    db = get_db()
    u = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    db.execute('UPDATE users SET is_active=0 WHERE id=?', (uid,))
    db.commit()
    flash(f'User {u["username"] if u else uid} dinonaktifkan', 'warning')
    return redirect(url_for('portal_users'))

@app.route('/portal/users/<int:uid>/remove', methods=['POST'])
@login_required
def portal_user_remove(uid):
    """Hapus permanen — hanya superadmin."""
    if session.get('user_role') != 'superadmin':
        flash('Hanya Superadmin yang bisa menghapus user secara permanen.', 'danger')
        return redirect(url_for('portal_users'))
    if uid == session['user_id']:
        flash('Tidak bisa menghapus akun sendiri.', 'danger')
        return redirect(url_for('portal_users'))
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        flash('User tidak ditemukan.', 'danger')
        return redirect(url_for('portal_users'))
    if u['role'] == 'superadmin':
        # Pastikan masih ada superadmin lain
        cnt = db.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND is_active=1 AND id!=?",
                         (uid,)).fetchone()[0]
        if cnt == 0:
            flash('Tidak bisa menghapus satu-satunya Superadmin.', 'danger')
            return redirect(url_for('portal_users'))
    # Cleanup data terkait
    db.execute('DELETE FROM user_app_access WHERE user_id=?', (uid,))
    db.execute('UPDATE employees SET user_id=NULL WHERE user_id=?', (uid,))
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()
    audit_log('remove_user', 'users', uid,
              f'User {u["username"]} ({u["email"] or "-"}) dihapus permanen', app_slug='portal')
    flash(f'User {u["username"]} berhasil dihapus secara permanen.', 'success')
    return redirect(url_for('portal_users'))

@app.route('/portal/users/<int:uid>/send-reset', methods=['POST'])
@login_required
def portal_user_send_reset(uid):
    if not is_portal_admin():
        return redirect(url_for('portal'))
    # Delegasi ke fungsi existing
    from flask import current_app
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if user:
        settings = get_settings(db)
        token    = secrets.token_urlsafe(48)
        expires  = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute('DELETE FROM password_reset_tokens WHERE user_id=?', (user['id'],))
        db.execute('INSERT INTO password_reset_tokens(user_id, token, expires_at) VALUES(?,?,?)',
                   (user['id'], token, expires))
        db.commit()
        reset_link = f"{get_base_url(settings)}/reset-password/{token}"
        sent = _send_reset_notifications(dict(user), reset_link, settings, db)
        if sent:
            flash(f'Link reset dikirim via: {", ".join(sent)}.', 'success')
        else:
            flash(f'Tidak ada kontak — salin link manual: {reset_link}', 'warning')
    return redirect(url_for('portal_users'))

@app.route('/portal/users/<int:uid>/mfa-reset', methods=['POST'])
@login_required
def portal_user_mfa_reset(uid):
    if not is_portal_admin():
        return redirect(url_for('portal'))
    db = get_db()
    db.execute('UPDATE users SET totp_secret="", mfa_enabled=0 WHERE id=?', (uid,))
    db.commit()
    flash('MFA user berhasil direset', 'warning')
    return redirect(url_for('portal_users'))

# ─── Portal: Role & Permission ─────────────────────────────────────────────────

@app.route('/portal/roles', methods=['GET', 'POST'])
@login_required
def portal_roles():
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal'))
    db         = get_db()
    active_app = request.args.get('app', 'evaluasi')
    superadmin = is_superadmin()

    if request.method == 'POST':
        action   = request.form.get('action')
        app_slug = request.form.get('app_slug', 'evaluasi')
        if action == 'add':
            if not superadmin:
                flash('Hanya superadmin yang dapat menambah role.', 'danger')
            else:
                name = request.form.get('name', '').strip().lower().replace(' ', '_')
                desc = request.form.get('description', '').strip()
                if name:
                    try:
                        db.execute('INSERT INTO roles(name,description,is_system,app_slug) VALUES(?,?,0,?)',
                                   (name, desc, app_slug))
                        db.commit()
                        flash(f'Role "{name}" ditambahkan', 'success')
                    except Exception:
                        flash('Nama role sudah ada', 'danger')
        elif action == 'delete':
            if not superadmin:
                flash('Hanya superadmin yang dapat menghapus role.', 'danger')
            else:
                rname  = request.form.get('role_name', '')
                is_sys = db.execute('SELECT is_system FROM roles WHERE name=?', (rname,)).fetchone()
                if is_sys and is_sys['is_system']:
                    flash('Role sistem tidak bisa dihapus', 'danger')
                else:
                    db.execute('DELETE FROM roles WHERE name=?', (rname,))
                    db.execute('DELETE FROM role_permissions WHERE role_name=?', (rname,))
                    db.commit()
                    flash(f'Role "{rname}" dihapus', 'warning')
        elif action == 'save_perms':
            rname     = request.form.get('role_name', '')
            selected  = set(request.form.getlist('permissions'))
            app_perms = APP_PERMISSIONS.get(app_slug, {})
            # Admin tidak boleh assign critical permissions
            if not superadmin:
                selected -= CRITICAL_PERMISSIONS
            db.execute('DELETE FROM role_permissions WHERE role_name=?', (rname,))
            for perm in selected:
                if perm in app_perms or perm in ('manage_users', 'manage_roles'):
                    db.execute('INSERT OR IGNORE INTO role_permissions(role_name,permission) VALUES(?,?)',
                               (rname, perm))
            db.commit()
            flash(f'Permission role "{rname}" diperbarui', 'success')
        return redirect(url_for('portal_roles', app=active_app))

    apps_list     = db.execute('SELECT slug, name FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()
    roles         = db.execute("SELECT * FROM roles WHERE app_slug=? OR app_slug='' ORDER BY is_system DESC, name",
                               (active_app,)).fetchall()
    perms_by_role = {r['name']: get_role_permissions(db, r['name']) for r in roles}
    app_perms     = APP_PERMISSIONS.get(active_app, {})
    return render_template('portal_roles.html', roles=roles, perms_by_role=perms_by_role,
                           app_perms=app_perms, apps_list=apps_list, active_app=active_app,
                           critical_permissions=CRITICAL_PERMISSIONS, is_superadmin=superadmin)

PORTAL_SYSTEM_KEYS = [
    'app_url',
    'smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from', 'smtp_ssl',
    'telegram_bot_token', 'telegram_default_chat_id',
    'openwa_url', 'openwa_api_key', 'openwa_session_id', 'openwa_enabled',
    'openwa_session_evaluasi', 'openwa_session_support', 'openwa_session_booking', 'openwa_session_aset',
    'google_client_id', 'google_client_secret', 'google_workspace_domain', 'google_oauth_enabled',
    'recaptcha_site_key', 'recaptcha_secret_key', 'recaptcha_enabled',
    'chatbot_enabled', 'chatbot_roles',
    'ai_provider', 'ai_api_key', 'ai_model', 'ai_base_url',
    'anthropic_api_key',  # backward compat
]

@app.route('/portal/system-settings', methods=['GET', 'POST'])
@login_required
def portal_system_settings():
    if not is_portal_admin():
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal'))
    db = get_db()
    if request.method == 'POST':
        for k in PORTAL_SYSTEM_KEYS:
            if k in ('smtp_ssl', 'openwa_enabled', 'google_oauth_enabled', 'recaptcha_enabled', 'chatbot_enabled'):
                v = '1' if request.form.get(k) else '0'
            else:
                v = request.form.get(k, '').strip()
            save_setting(db, k, v)
        db.commit()
        flash('Pengaturan sistem disimpan', 'success')
        return redirect(url_for('portal_system_settings'))
    cfg = get_settings(db)
    return render_template('portal_system_settings.html', cfg=cfg)

@app.route('/portal/system-settings/test-email', methods=['POST'])
@login_required
def portal_test_email():
    if not is_portal_admin():
        return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    db = get_db()
    cfg = get_settings(db)
    to_email = request.form.get('test_email', '').strip()
    if not to_email:
        return jsonify({'ok': False, 'msg': 'Masukkan alamat email tujuan test'})
    ok, err = send_email(cfg, to_email, 'Test Email — Hive',
                         '<h3>Test berhasil!</h3><p>Konfigurasi email sudah benar.</p>')
    return jsonify({'ok': ok, 'msg': 'Email berhasil dikirim' if ok else str(err)})

@app.route('/portal/system-settings/test-telegram', methods=['POST'])
@login_required
def portal_test_telegram():
    if not is_portal_admin():
        return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    db = get_db()
    cfg = get_settings(db)
    bot_token = cfg.get('telegram_bot_token', '').strip()
    chat_id   = request.form.get('test_chat_id', '').strip() or cfg.get('telegram_default_chat_id', '').strip()
    if not bot_token or not chat_id:
        return jsonify({'ok': False, 'msg': 'Bot token dan chat ID harus diisi'})
    ok, err = send_telegram(bot_token, chat_id,
                            '✅ <b>Test berhasil!</b>\n\nKonfigurasi Telegram sudah benar.')
    return jsonify({'ok': ok, 'msg': 'Pesan Telegram berhasil dikirim' if ok else str(err)})

@app.route('/portal/system-settings/test-whatsapp', methods=['POST'])
@login_required
def portal_test_whatsapp():
    if not is_portal_admin():
        return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    db  = get_db()
    cfg = get_settings(db)
    wa_url     = cfg.get('openwa_url', '').strip()
    wa_key     = cfg.get('openwa_api_key', '').strip()
    test_app   = request.form.get('app_slug', '').strip()
    wa_session = get_openwa_session(cfg, test_app) if test_app else get_openwa_session(cfg)
    phone      = request.form.get('test_wa_phone', '').strip()
    if not wa_url or not phone:
        return jsonify({'ok': False, 'msg': 'URL OpenWA dan nomor HP harus diisi'})
    app_label  = {'evaluasi': 'TalentCore', 'support': 'SupportCore',
                  'booking': 'BookingCore', 'aset': 'AssetCore'}.get(test_app, 'Hive')
    ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone,
                            f'✅ *Test berhasil!*\n\nSesi: `{wa_session}`\nAplikasi: {app_label}')
    chat_id = normalize_phone_wa(phone)
    return jsonify({'ok': ok, 'chat_id': chat_id,
                    'msg': f'Pesan terkirim ke {chat_id}' if ok else str(err)})

@app.route('/portal/system-settings/reload', methods=['POST'])
@login_required
def portal_reload_app():
    if not is_portal_admin():
        return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    try:
        # Cari PID master gunicorn (parent dari proses ini)
        ppid = os.getppid()
        os.kill(ppid, signal.SIGHUP)
        return jsonify({'ok': True, 'msg': f'Sinyal reload dikirim ke gunicorn master (PID {ppid}). Worker akan restart dalam beberapa detik.'})
    except Exception as ex:
        return jsonify({'ok': False, 'msg': str(ex)})

@app.route('/portal/system-settings/version-info', methods=['GET'])
@login_required
def portal_version_info():
    if not is_portal_admin():
        return jsonify({'ok': False})
    info = {'deployed': VERSION, 'release_date': RELEASE_DATE}
    try:
        git_hash = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                           cwd=os.path.dirname(__file__), stderr=subprocess.DEVNULL,
                                           timeout=3).decode().strip()
        git_msg  = subprocess.check_output(['git', 'log', '-1', '--pretty=%s'],
                                           cwd=os.path.dirname(__file__), stderr=subprocess.DEVNULL,
                                           timeout=3).decode().strip()
        git_date = subprocess.check_output(['git', 'log', '-1', '--pretty=%ci'],
                                           cwd=os.path.dirname(__file__), stderr=subprocess.DEVNULL,
                                           timeout=3).decode().strip()
        info.update({'git_hash': git_hash, 'git_msg': git_msg, 'git_date': git_date})
    except Exception:
        info.update({'git_hash': '-', 'git_msg': '-', 'git_date': '-'})
    return jsonify(info)

@app.route('/')
@login_required
def index():
    db = get_db()
    employees = db.execute('''
        SELECT emp.*,
               (SELECT COUNT(*) FROM evaluations WHERE employee_id=emp.id) AS eval_count,
               (SELECT MAX(periode) FROM evaluations WHERE employee_id=emp.id) AS latest_periode,
               (SELECT final_total FROM evaluations WHERE employee_id=emp.id ORDER BY id DESC LIMIT 1) AS latest_score,
               (SELECT status FROM evaluations WHERE employee_id=emp.id ORDER BY id DESC LIMIT 1) AS latest_status,
               (SELECT id FROM evaluations WHERE employee_id=emp.id ORDER BY id DESC LIMIT 1) AS latest_eval_id
        FROM employees emp WHERE emp.is_active=1
        ORDER BY emp.divisi, emp.name
    ''').fetchall()

    today = date.today()
    contracts_alert = db.execute('''
        SELECT *, julianday(contract_end) - julianday('now') AS days_left
        FROM employees WHERE employment_type='kontrak' AND contract_end != ''
        AND is_active=1 ORDER BY contract_end
    ''').fetchall()
    expiring_soon = [e for e in contracts_alert if e['days_left'] is not None and e['days_left'] <= 30]

    divisi_list  = get_divisi_list(db)
    divisi_stats = {}
    for d in divisi_list:
        row = db.execute('''
            SELECT COUNT(DISTINCT emp.id) AS total,
                   COUNT(DISTINCT CASE WHEN ev.status='final' THEN ev.employee_id END) AS done
            FROM employees emp LEFT JOIN evaluations ev ON ev.employee_id=emp.id AND ev.status='final'
            WHERE emp.divisi=? AND emp.is_active=1
        ''', (d,)).fetchone()
        divisi_stats[d] = row
    return render_template('index.html', employees=employees, divisi_stats=divisi_stats,
                           expiring_soon=expiring_soon)

# ─── Employee CRUD ──────────────────────────────────────────────────────────────

@app.route('/emp/add', methods=['GET', 'POST'])
@login_required
def emp_add():
    if request.method == 'POST':
        name  = request.form['name'].strip()
        divisi = request.form['divisi']
        if not name:
            flash('Nama tidak boleh kosong', 'danger')
            db = get_db()
            employees_all = db.execute('SELECT id,name,jabatan,divisi,level FROM employees WHERE is_active=1 ORDER BY name').fetchall()
            return render_template('employee_form.html', emp=None, employees_all=employees_all)
        db = get_db()
        def _int_or_none(v):
            return int(v) if v and str(v).isdigit() else None
        emp_type = request.form.get('employment_type','tetap')
        rate_md_raw = request.form.get('rate_mandays','').strip()
        rate_md = float(rate_md_raw) if rate_md_raw and emp_type == 'staff_worker' else None
        db.execute('''INSERT INTO employees(name,jabatan,divisi,level,employment_type,
                      contract_start,contract_end,rate_mandays,email,phone,telegram_id,notes,
                      supervisor_id,leader_id,manager_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            name,
            request.form.get('jabatan','').strip(),
            divisi,
            request.form.get('level','Staff'),
            emp_type,
            request.form.get('contract_start','') if emp_type in ('kontrak','staff_worker') else '',
            request.form.get('contract_end','')   if emp_type in ('kontrak','staff_worker') else '',
            rate_md,
            request.form.get('email','').strip(),
            request.form.get('phone','').strip(),
            normalize_telegram_id(request.form.get('telegram_id','')),
            request.form.get('notes','').strip(),
            _int_or_none(request.form.get('supervisor_id','')),
            _int_or_none(request.form.get('leader_id','')),
            _int_or_none(request.form.get('manager_id','')),
        ))
        db.commit()
        flash(f'Karyawan {name} berhasil ditambahkan', 'success')
        return redirect(url_for('index'))
    db = get_db()
    employees_all = db.execute('SELECT id,name,jabatan,divisi,level FROM employees WHERE is_active=1 ORDER BY name').fetchall()
    return render_template('employee_form.html', emp=None, employees_all=employees_all)

@app.route('/emp/<int:emp_id>/edit', methods=['GET', 'POST'])
@login_required
def emp_edit(emp_id):
    db = get_db()
    emp = db.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
    if not emp:
        flash('Karyawan tidak ditemukan', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        action = request.form.get('action', 'save')
        if action == 'promote':
            # Role promotion only for superadmin
            if session.get('user_role') != 'superadmin':
                flash('Hanya Superadmin yang bisa promote role', 'danger')
                return redirect(url_for('emp_edit', emp_id=emp_id))
            new_level = request.form.get('promote_level', emp['level'])
            new_jabatan = request.form.get('promote_jabatan', emp['jabatan'])
            db.execute('UPDATE employees SET level=?,jabatan=? WHERE id=?',
                       (new_level, new_jabatan, emp_id))
            db.commit()
            flash(f'{emp["name"]} dipromosikan ke {new_level} — {new_jabatan}', 'success')
            return redirect(url_for('emp_edit', emp_id=emp_id))
        # Normal save
        def _int_or_none(v):
            return int(v) if v and str(v).isdigit() else None
        emp_type = request.form.get('employment_type', 'tetap')
        rate_md_raw = request.form.get('rate_mandays','').strip()
        rate_md = float(rate_md_raw) if rate_md_raw and emp_type == 'staff_worker' else None
        db.execute('''UPDATE employees SET name=?,jabatan=?,divisi=?,level=?,
                      employment_type=?,contract_start=?,contract_end=?,rate_mandays=?,
                      email=?,phone=?,telegram_id=?,notes=?,
                      supervisor_id=?,leader_id=?,manager_id=? WHERE id=?''', (
            request.form['name'].strip(),
            request.form.get('jabatan','').strip(),
            request.form['divisi'],
            request.form.get('level','Staff'),
            emp_type,
            request.form.get('contract_start','') if emp_type in ('kontrak','staff_worker') else '',
            request.form.get('contract_end','')   if emp_type in ('kontrak','staff_worker') else '',
            rate_md,
            request.form.get('email','').strip(),
            request.form.get('phone','').strip(),
            normalize_telegram_id(request.form.get('telegram_id','')),
            request.form.get('notes','').strip(),
            _int_or_none(request.form.get('supervisor_id','')),
            _int_or_none(request.form.get('leader_id','')),
            _int_or_none(request.form.get('manager_id','')),
            emp_id,
        ))
        db.commit()
        flash('Data karyawan diperbarui', 'success')
        return redirect(url_for('emp_edit', emp_id=emp_id))
    # Reminder logs for this employee
    logs = db.execute('''SELECT * FROM reminder_logs WHERE employee_id=?
                         ORDER BY created_at DESC LIMIT 10''', (emp_id,)).fetchall()
    employees_all = db.execute('SELECT id,name,jabatan,divisi,level FROM employees WHERE is_active=1 AND id!=? ORDER BY name', (emp_id,)).fetchall()
    linked_user = db.execute('SELECT id,username,role,is_active FROM users WHERE id=?', (emp['user_id'],)).fetchone() if emp['user_id'] else None
    apps = db.execute('SELECT slug, name, icon FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()
    all_roles    = db.execute("SELECT name, description, app_slug FROM roles ORDER BY is_system DESC, name").fetchall()
    global_roles = [r for r in all_roles if r['app_slug'] == '']
    roles_by_app = {}
    for a in apps:
        specific = [r for r in all_roles if r['app_slug'] == a['slug']]
        roles_by_app[a['slug']] = global_roles + specific
    user_access = {}
    if emp['user_id']:
        user_access = {r['app_slug']: r['app_role'] for r in
                       db.execute('SELECT app_slug,app_role FROM user_app_access WHERE user_id=? AND is_active=1', (emp['user_id'],)).fetchall()}
    return render_template('employee_form.html', emp=emp, logs=logs,
                           employees_all=employees_all, linked_user=linked_user,
                           apps=apps, global_roles=global_roles,
                           roles_by_app=roles_by_app, user_access=user_access)

@app.route('/emp/<int:emp_id>/delete', methods=['POST'])
@superadmin_required
def emp_delete(emp_id):
    db = get_db()
    emp = db.execute('SELECT name FROM employees WHERE id=?', (emp_id,)).fetchone()
    if emp:
        db.execute('UPDATE employees SET is_active=0 WHERE id=?', (emp_id,))
        db.commit()
        flash(f'Karyawan {emp["name"]} dinonaktifkan', 'warning')
    return redirect(url_for('index'))

# ─── Import Excel: Karyawan & Gaji ────────────────────────────────────────────

@app.route('/emp/import/template')
@permission_required('manage_employees')
def emp_import_template():
    """Download template Excel untuk import karyawan."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = 'Data Karyawan'

    headers = [
        ('name',            'Nama Lengkap *',         30, 'Wajib diisi'),
        ('jabatan',         'Jabatan',                 25, 'Contoh: Staff IT, Manager HRD'),
        ('divisi',          'Divisi *',                20, 'Wajib. Harus sesuai divisi di sistem'),
        ('level',           'Level',                   15, 'Staff / Leader / Manager / Director'),
        ('employment_type', 'Tipe *',                  12, 'tetap / kontrak'),
        ('contract_start',  'Mulai Kontrak',           15, 'Format: YYYY-MM-DD, kosongkan jika tetap'),
        ('contract_end',    'Akhir Kontrak',           15, 'Format: YYYY-MM-DD, kosongkan jika tetap'),
        ('email',           'Email',                   28, 'Alamat email karyawan'),
        ('phone',           'No. HP/WA',               18, 'Contoh: 628123456789'),
        ('telegram_id',     'Telegram ID',             18, 'Contoh: @username atau chat_id'),
        ('notes',           'Catatan',                 30, 'Catatan tambahan (opsional)'),
    ]

    hdr_fill = PatternFill('solid', fgColor='1F4E79')
    req_fill = PatternFill('solid', fgColor='2E7D32')
    tip_fill = PatternFill('solid', fgColor='F5F5F5')
    hdr_font = Font(color='FFFFFF', bold=True, size=10)
    tip_font = Font(color='555555', italic=True, size=9)
    thin = Side(style='thin', color='CCCCCC')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Row 1: header; Row 2: tips; Row 3+: data
    for col_idx, (field, label, width, tip) in enumerate(headers, 1):
        fill = req_fill if '*' in label else hdr_fill
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font = hdr_font; cell.fill = fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border

        tip_cell = ws.cell(row=2, column=col_idx, value=tip)
        tip_cell.font = tip_font; tip_cell.fill = tip_fill
        tip_cell.alignment = Alignment(wrap_text=True, vertical='top')
        tip_cell.border = border

        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width

    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 36
    ws.freeze_panes = 'A3'

    # Data validation: tipe
    dv_type = DataValidation(type='list', formula1='"tetap,kontrak"', showDropDown=False)
    ws.add_data_validation(dv_type)
    dv_type.sqref = 'E3:E1000'

    # Contoh baris data
    sample = ['Budi Santoso', 'Staff IT', 'IT', 'Staff', 'tetap', '', '', 'budi@company.com', '6281234567890', '', '']
    for col_idx, val in enumerate(sample, 1):
        c = ws.cell(row=3, column=col_idx, value=val)
        c.border = border

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return Response(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename=template_import_karyawan.xlsx'})


@app.route('/emp/import', methods=['POST'])
@permission_required('manage_employees')
def emp_import():
    """Import karyawan dari file Excel."""
    from openpyxl import load_workbook
    f = request.files.get('file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('Upload file .xlsx yang valid', 'danger')
        return redirect(url_for('karyawan'))

    try:
        wb = load_workbook(f, read_only=True, data_only=True)
        ws = wb.active
        db = get_db()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Header row = 1, tip row = 2, data starts row 3
        FIELD_MAP = {
            'nama lengkap': 'name', 'jabatan': 'jabatan', 'divisi': 'divisi',
            'level': 'level', 'tipe': 'employment_type',
            'mulai kontrak': 'contract_start', 'akhir kontrak': 'contract_end',
            'email': 'email', 'no. hp/wa': 'phone', 'telegram id': 'telegram_id',
            'catatan': 'notes',
        }
        # Read header row to get column mapping
        hdr_row = [str(c.value or '').lower().strip().rstrip(' *') for c in ws[1]]
        col_map = {}
        for idx, hdr in enumerate(hdr_row):
            field = FIELD_MAP.get(hdr)
            if field:
                col_map[field] = idx

        inserted = updated = skipped = 0
        errors = []
        mode = request.form.get('mode', 'insert')  # insert | upsert

        for row_num, row in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
            if all(v is None or str(v).strip() == '' for v in row):
                continue
            def get(field):
                idx = col_map.get(field)
                v = row[idx] if idx is not None and idx < len(row) else None
                return str(v).strip() if v is not None else ''

            name   = get('name')
            divisi = get('divisi')
            if not name or not divisi:
                errors.append(f'Baris {row_num}: name/divisi wajib diisi')
                skipped += 1
                continue

            emp_type = get('employment_type') or 'tetap'
            if emp_type not in ('tetap', 'kontrak'):
                emp_type = 'tetap'

            fields = {
                'name': name, 'jabatan': get('jabatan'), 'divisi': divisi,
                'level': get('level') or 'Staff', 'employment_type': emp_type,
                'contract_start': get('contract_start'), 'contract_end': get('contract_end'),
                'email': get('email'), 'phone': get('phone'),
                'telegram_id': get('telegram_id'), 'notes': get('notes'),
                'is_active': 1,
            }

            existing = db.execute('SELECT id FROM employees WHERE name=? AND divisi=?',
                                  (name, divisi)).fetchone()
            if existing:
                if mode == 'upsert':
                    sets = ', '.join(f'{k}=?' for k in fields if k != 'name')
                    vals = [fields[k] for k in fields if k != 'name'] + [existing['id']]
                    db.execute(f'UPDATE employees SET {sets} WHERE id=?', vals)
                    updated += 1
                else:
                    skipped += 1
            else:
                cols = ', '.join(fields.keys())
                phs  = ', '.join('?' for _ in fields)
                db.execute(f'INSERT INTO employees ({cols}) VALUES ({phs})', list(fields.values()))
                inserted += 1

        db.commit()
        msg = f'Import selesai: {inserted} ditambah, {updated} diperbarui, {skipped} dilewati'
        if errors:
            msg += f'. {len(errors)} baris error (lihat log)'
        flash(msg, 'success' if not errors else 'warning')
    except Exception as e:
        flash(f'Gagal import: {e}', 'danger')

    return redirect(url_for('karyawan'))


@app.route('/salary/import/template')
@permission_required('manage_salary')
@mfa_challenge_required('mfa_salary_verified')
def salary_import_template():
    """Download template Excel untuk import data gaji."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = 'Data Gaji'

    db = get_db()
    year = date.today().year
    emps = db.execute(
        "SELECT id, name, jabatan, divisi, employment_type FROM employees WHERE is_active=1 ORDER BY employment_type DESC, divisi, name"
    ).fetchall()

    headers = [
        ('employee_id',   'ID Karyawan *', 12),
        ('name',          'Nama Karyawan', 30),
        ('jabatan',       'Jabatan',       22),
        ('divisi',        'Divisi',        15),
        ('year',          'Tahun *',       10),
        ('base_salary',   'SALARY (Gaji Pokok)', 20),
        ('al_001',        'AL_001 (Tj. Jabatan)', 20),
        ('al_002',        'AL_002 (Tj. Komunikasi)', 22),
        ('al_003',        'AL_003 (Tj. Performance)', 22),
        ('al_004',        'AL_004 (Tj. Kehadiran)', 20),
        ('increase_pct',  '% Kenaikan', 14),
        ('increase_date', 'Bulan Kenaikan', 16),
        ('notes',         'Catatan', 25),
    ]

    hdr_fill = PatternFill('solid', fgColor='1F4E79')
    req_fill = PatternFill('solid', fgColor='2E7D32')
    emp_fill = PatternFill('solid', fgColor='EFF7EF')
    num_fill = PatternFill('solid', fgColor='FAFEFF')
    hdr_font = Font(color='FFFFFF', bold=True, size=10)
    thin = Side(style='thin', color='CCCCCC')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    from openpyxl.styles import numbers as xl_numbers

    for col_idx, (field, label, width) in enumerate(headers, 1):
        fill = req_fill if '*' in label else hdr_fill
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font = hdr_font; cell.fill = fill
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border = border
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width

    ws.row_dimensions[1].height = 32
    ws.freeze_panes = 'F2'

    from openpyxl.utils import get_column_letter
    for row_idx, emp in enumerate(emps, start=2):
        row_fill = PatternFill('solid', fgColor='FFF8E1' if emp['employment_type'] == 'kontrak' else 'F0F9FF')
        vals = [emp['id'], emp['name'], emp['jabatan'] or '', emp['divisi'], year,
                0, 0, 0, 0, 0, 0, '', '']
        for col_idx, val in enumerate(vals, 1):
            c = ws.cell(row=row_idx, column=col_idx, value=val)
            c.border = border
            # Read-only info columns
            if col_idx <= 4:
                c.fill = emp_fill
                if col_idx == 1:
                    c.font = Font(bold=True, color='1F4E79')
            elif col_idx == 5:
                c.fill = emp_fill
            elif 6 <= col_idx <= 10:
                c.fill = num_fill
                c.number_format = '#,##0'
            elif col_idx == 11:
                c.fill = num_fill
                c.number_format = '0.00'

    ws.row_dimensions[1].height = 30

    # Second sheet: instructions
    ws2 = wb.create_sheet('Petunjuk')
    instructions = [
        ['PETUNJUK IMPORT DATA GAJI'],
        [''],
        ['1. Jangan ubah kolom ID Karyawan — digunakan sebagai kunci pencarian'],
        ['2. Kolom Tahun wajib diisi dengan angka 4 digit (mis. 2024)'],
        ['3. Kolom gaji diisi angka tanpa titik/koma (mis. 5000000)'],
        ['4. % Kenaikan: angka desimal mis. 7.5 untuk 7.5%'],
        ['5. Bulan Kenaikan: format bebas mis. Apr-2025'],
        ['6. Data yang sudah ada di tahun tersebut akan DITIMPA (update)'],
        ['7. Baris dengan ID kosong akan dilewati'],
    ]
    for r, row in enumerate(instructions, 1):
        c = ws2.cell(row=r, column=1, value=row[0])
        if r == 1:
            c.font = Font(bold=True, size=13, color='1F4E79')
        ws2.column_dimensions['A'].width = 65

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return Response(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': 'attachment; filename=template_import_gaji.xlsx'})


@app.route('/salary/import', methods=['POST'])
@permission_required('manage_salary')
@mfa_challenge_required('mfa_salary_verified')
def salary_import():
    """Import data gaji dari file Excel."""
    from openpyxl import load_workbook
    f = request.files.get('file')
    if not f or not f.filename.endswith('.xlsx'):
        flash('Upload file .xlsx yang valid', 'danger')
        return redirect(url_for('salary_table'))

    try:
        wb   = load_workbook(f, read_only=True, data_only=True)
        ws   = wb.active
        db   = get_db()
        now  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Header row 1 → field map
        FIELD_MAP = {
            'id karyawan': 'employee_id', 'tahun': 'year',
            'salary (gaji pokok)': 'base_salary', 'al_001 (tj. jabatan)': 'al_001',
            'al_002 (tj. komunikasi)': 'al_002', 'al_003 (tj. performance)': 'al_003',
            'al_004 (tj. kehadiran)': 'al_004',
            '% kenaikan': 'increase_pct', 'bulan kenaikan': 'increase_date',
            'catatan': 'notes',
        }
        hdr_row = [str(c.value or '').lower().strip().rstrip(' *') for c in ws[1]]
        col_map = {field: idx for idx, hdr in enumerate(hdr_row)
                   for key, field in FIELD_MAP.items() if hdr == key}

        upserted = skipped = 0
        errors   = []

        for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if all(v is None or str(v).strip() == '' for v in row):
                continue
            def get(field, default=''):
                idx = col_map.get(field)
                v = row[idx] if idx is not None and idx < len(row) else None
                return v if v is not None else default

            emp_id = get('employee_id')
            year   = get('year')
            try:
                emp_id = int(emp_id)
                year   = int(year)
            except (TypeError, ValueError):
                errors.append(f'Baris {row_num}: ID/tahun tidak valid')
                skipped += 1
                continue

            emp = db.execute('SELECT id FROM employees WHERE id=?', (emp_id,)).fetchone()
            if not emp:
                errors.append(f'Baris {row_num}: ID {emp_id} tidak ditemukan')
                skipped += 1
                continue

            def num(field):
                v = get(field, 0)
                try: return float(v) if v != '' else 0.0
                except (TypeError, ValueError): return 0.0

            db.execute('''
                INSERT INTO employee_salary
                    (employee_id, year, base_salary, al_001, al_002, al_003, al_004,
                     increase_pct, increase_date, notes, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(employee_id, year) DO UPDATE SET
                    base_salary=excluded.base_salary, al_001=excluded.al_001,
                    al_002=excluded.al_002, al_003=excluded.al_003,
                    al_004=excluded.al_004, increase_pct=excluded.increase_pct,
                    increase_date=excluded.increase_date, notes=excluded.notes,
                    updated_at=excluded.updated_at
            ''', (emp_id, year,
                  _fenc(num('base_salary')), _fenc(num('al_001')), _fenc(num('al_002')),
                  _fenc(num('al_003')), _fenc(num('al_004')),
                  num('increase_pct'), str(get('increase_date', '')), str(get('notes', '')), now))
            upserted += 1

        db.commit()
        msg = f'Import gaji selesai: {upserted} baris disimpan, {skipped} dilewati'
        if errors:
            msg += f'. Error: ' + '; '.join(errors[:5])
            if len(errors) > 5:
                msg += f' (+{len(errors)-5} lainnya)'
        flash(msg, 'success' if not errors else 'warning')
    except Exception as e:
        flash(f'Gagal import gaji: {e}', 'danger')

    year_param = request.form.get('year_redirect', date.today().year)
    return redirect(url_for('salary_table', year=year_param))

# ─── Contract Management ───────────────────────────────────────────────────────

@app.route('/karyawan')
@login_required
def karyawan():
    db = get_db()
    today = date.today()
    kontrak = db.execute('''
        SELECT *, julianday(contract_end) - julianday('now') AS days_left
        FROM employees
        WHERE employment_type IN ('kontrak','staff_worker') AND is_active = 1
        ORDER BY CASE WHEN contract_end='' OR contract_end IS NULL THEN 1 ELSE 0 END,
                 contract_end ASC
    ''').fetchall()
    tetap = db.execute('''
        SELECT * FROM employees
        WHERE employment_type = 'tetap' AND is_active = 1
        ORDER BY divisi, name
    ''').fetchall()
    emp_user_map = {r['id']: r for r in db.execute('''
        SELECT e.id, u.username, u.role, u.is_active AS u_active
        FROM employees e JOIN users u ON u.id = e.user_id
        WHERE e.is_active=1
    ''').fetchall()}
    apps = db.execute('SELECT slug, name, icon FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()
    all_roles    = db.execute("SELECT name, description, app_slug FROM roles ORDER BY is_system DESC, name").fetchall()
    global_roles = [r for r in all_roles if r['app_slug'] == '']
    roles_by_app = {}
    for a in apps:
        specific = [r for r in all_roles if r['app_slug'] == a['slug']]
        roles_by_app[a['slug']] = global_roles + specific
    return render_template('karyawan.html', kontrak=kontrak, tetap=tetap, today=today,
                           emp_user_map=emp_user_map, global_roles=global_roles,
                           apps=apps, roles_by_app=roles_by_app)

@app.route('/contracts')
@login_required
def contracts():
    return redirect(url_for('karyawan'))

@app.route('/contracts/remind/<int:emp_id>', methods=['POST'])
@login_required
def contract_remind_one(emp_id):
    db = get_db()
    emp = db.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
    if not emp or not emp['contract_end']:
        flash('Data karyawan tidak valid', 'danger')
        return redirect(url_for('contracts'))
    settings = get_settings(db)
    try:
        end_date  = date.fromisoformat(emp['contract_end'])
        days_left = (end_date - date.today()).days
    except Exception:
        flash('Format tanggal kontrak tidak valid', 'danger')
        return redirect(url_for('contracts'))

    who = session.get('username', 'manual')
    s, f = _send_contract_notification(db, emp, days_left, settings, who)
    db.commit()
    if s == 0 and f == 0:
        flash('Tidak ada channel notifikasi yang dikonfigurasi (email / telegram / whatsapp)', 'warning')
    else:
        flash(f'Reminder dikirim — {s} berhasil, {f} gagal (termasuk ke Manajerial/Leader/Manager)', 'success' if f == 0 else 'warning')
    return redirect(url_for('karyawan'))

@app.route('/contracts/remind-all', methods=['POST'])
@login_required
def contract_remind_all():
    sent, failed = run_contract_reminders(triggered_by=session.get('username','manual'))
    flash(f'Selesai — {sent} berhasil, {failed} gagal', 'success' if failed == 0 else 'warning')
    return redirect(url_for('karyawan'))

@app.route('/reminders')
@login_required
def reminder_log():
    db   = get_db()
    logs = db.execute('''
        SELECT rl.*, emp.name AS emp_name, emp.divisi
        FROM reminder_logs rl
        LEFT JOIN employees emp ON emp.id = rl.employee_id
        ORDER BY rl.created_at DESC LIMIT 200
    ''').fetchall()
    stats = db.execute('''
        SELECT channel, status, COUNT(*) AS cnt
        FROM reminder_logs GROUP BY channel, status
    ''').fetchall()
    return render_template('reminder_log.html', logs=logs, stats=stats)

# ─── SupportCore ───────────────────────────────────────────────────────────────

def sc_require(perm):
    """Check SupportCore permission, redirect to /support if denied."""
    db = get_db()
    if not has_permission(session.get('user_role', ''), perm, db):
        flash(f'Akses ditolak — permission "{perm}" diperlukan', 'danger')
        return False
    return True

@app.route('/support/')
@login_required
def sc_index():
    db = get_db()
    counts = {
        'customers':    db.execute('SELECT COUNT(*) FROM sc_customers WHERE is_active=1').fetchone()[0],
        'services':     db.execute('SELECT COUNT(*) FROM sc_services WHERE is_active=1').fetchone()[0],
        'support_types':db.execute('SELECT COUNT(*) FROM sc_support_types WHERE is_active=1').fetchone()[0],
        'sla_categories':db.execute('SELECT COUNT(*) FROM sc_sla_categories WHERE is_active=1').fetchone()[0],
    }
    return render_template('sc_index.html', counts=counts)

# ── Master Customer ────────────────────────────────────────────────────────────
@app.route('/support/customers')
@login_required
def sc_customers():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    from datetime import date as _date, timedelta
    db    = get_db()
    today = _date.today().isoformat()
    soon  = (_date.today() + timedelta(days=90)).isoformat()
    rows  = db.execute('SELECT * FROM sc_customers ORDER BY name').fetchall()
    # Contract status per customer: green / orange / red
    contract_status = {}
    for r in rows:
        ctr = db.execute('''SELECT end_date FROM sc_contracts
                            WHERE customer_id=? AND status='active'
                            ORDER BY end_date DESC LIMIT 1''', (r['id'],)).fetchone()
        if not ctr:
            contract_status[r['id']] = 'none'       # merah
        elif ctr['end_date'] <= soon:
            contract_status[r['id']] = 'expiring'   # orange
        else:
            contract_status[r['id']] = 'active'     # hijau
    return render_template('sc_customers.html', rows=rows, contract_status=contract_status)

@app.route('/support/api/customer-pics/<int:cid>')
@login_required
def sc_api_customer_pics(cid):
    db = get_db()
    return jsonify(_sc_customer_pic_info(db, cid))

@app.route('/support/customers/add', methods=['GET', 'POST'])
@login_required
def sc_customer_add():
    if not sc_require('sc_manage_customers'): return redirect(url_for('sc_customers'))
    db = get_db()
    helpdesk, implementor, coleader, sales = _sc_pic_employees(db)
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        if not code or not name:
            flash('Kode dan nama wajib diisi', 'danger')
        else:
            try:
                db.execute(
                    '''INSERT INTO sc_customers
                       (code,name,address,contact_person,phone,email,notes,customer_type,
                        pic_helpdesk_id,pic_helpdesk_backup_id,pic_implementor_id,pic_coleader_id,
                        pic_sales_id,telegram_group_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (code, name,
                     request.form.get('address','').strip(),
                     request.form.get('contact_person','').strip(),
                     request.form.get('phone','').strip(),
                     request.form.get('email','').strip(),
                     request.form.get('notes','').strip(),
                     request.form.get('customer_type','aktif'),
                     request.form.get('pic_helpdesk_id') or None,
                     request.form.get('pic_helpdesk_backup_id') or None,
                     request.form.get('pic_implementor_id') or None,
                     request.form.get('pic_coleader_id') or None,
                     request.form.get('pic_sales_id') or None,
                     request.form.get('telegram_group_id','').strip()))
                db.commit()
                flash(f'Customer "{name}" ditambahkan', 'success')
                return redirect(url_for('sc_customers'))
            except Exception:
                flash('Kode customer sudah ada', 'danger')
    return render_template('sc_customer_form.html', row=None,
                           helpdesk=helpdesk, implementor=implementor, coleader=coleader, sales=sales)

@app.route('/support/customers/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
def sc_customer_edit(cid):
    if not sc_require('sc_manage_customers'): return redirect(url_for('sc_customers'))
    db  = get_db()
    row = db.execute('SELECT * FROM sc_customers WHERE id=?', (cid,)).fetchone()
    if not row:
        flash('Customer tidak ditemukan', 'danger')
        return redirect(url_for('sc_customers'))
    helpdesk, implementor, coleader, sales = _sc_pic_employees(db)
    if request.method == 'POST':
        name      = request.form.get('name', '').strip()
        is_active = 1 if request.form.get('is_active') else 0
        db.execute('''UPDATE sc_customers
                      SET name=?,address=?,contact_person=?,phone=?,email=?,notes=?,is_active=?,
                          customer_type=?,
                          pic_helpdesk_id=?,pic_helpdesk_backup_id=?,pic_implementor_id=?,pic_coleader_id=?,
                          pic_sales_id=?,telegram_group_id=?
                      WHERE id=?''',
                   (name,
                    request.form.get('address','').strip(),
                    request.form.get('contact_person','').strip(),
                    request.form.get('phone','').strip(),
                    request.form.get('email','').strip(),
                    request.form.get('notes','').strip(),
                    is_active,
                    request.form.get('customer_type','aktif'),
                    request.form.get('pic_helpdesk_id') or None,
                    request.form.get('pic_helpdesk_backup_id') or None,
                    request.form.get('pic_implementor_id') or None,
                    request.form.get('pic_coleader_id') or None,
                    request.form.get('pic_sales_id') or None,
                    request.form.get('telegram_group_id','').strip(),
                    cid))
        db.commit()
        flash('Customer diperbarui', 'success')
        return redirect(url_for('sc_customers'))
    return render_template('sc_customer_form.html', row=row,
                           helpdesk=helpdesk, implementor=implementor, coleader=coleader, sales=sales)

@app.route('/support/customers/<int:cid>/delete', methods=['POST'])
@login_required
def sc_customer_delete(cid):
    if not sc_require('sc_manage_customers'): return redirect(url_for('sc_customers'))
    db = get_db()
    db.execute('UPDATE sc_customers SET is_active=0 WHERE id=?', (cid,))
    db.commit()
    flash('Customer dinonaktifkan', 'warning')
    return redirect(url_for('sc_customers'))

# ── Notification helper ────────────────────────────────────────────────────────
def _save_ticket_attachments(db, ticket_id, section):
    """Simpan file upload dari request.files (multiple) ke tabel sc_ticket_attachments."""
    files = request.files.getlist(f'attach_{section}')
    for f in files:
        if not f or not f.filename:
            continue
        url = _save_upload(f, f'tickets/{ticket_id}/{section}')
        if url:
            db.execute('''INSERT INTO sc_ticket_attachments(ticket_id,section,filename,original_name,uploaded_by,uploaded_by_name)
                          VALUES(?,?,?,?,?,?)''',
                       (ticket_id, section, url, f.filename,
                        session.get('user_id'), session.get('user_name','')))

def _sync_external_assignees(db, table, fk_col, fk_val, names):
    """Ganti seluruh external assignees dengan daftar nama baru."""
    db.execute(f'DELETE FROM {table} WHERE {fk_col}=?', (fk_val,))
    for name in names:
        name = name.strip()
        if name:
            db.execute(f'INSERT INTO {table}({fk_col},name,added_by) VALUES(?,?,?)',
                       (fk_val, name, session.get('user_id')))

def _sc_ticket_history(db, ticket_id, action, field_name='', old_value='', new_value='', notes=''):
    """Catat history perubahan tiket (tidak menimpa, selalu tambah baris baru)."""
    try:
        db.execute('''INSERT INTO sc_ticket_history
                      (ticket_id,changed_by,changed_by_name,action,field_name,old_value,new_value,notes)
                      VALUES(?,?,?,?,?,?,?,?)''',
                   (ticket_id, session.get('user_id'), session.get('user_name',''),
                    action, field_name, str(old_value or ''), str(new_value or ''), notes))
        db.commit()
    except Exception:
        pass

def _sc_sync_assignees(db, ticket_id, employee_ids):
    """Sync multiple assignee ke sc_ticket_assignees."""
    old_ids = {r['employee_id'] for r in
               db.execute('SELECT employee_id FROM sc_ticket_assignees WHERE ticket_id=?', (ticket_id,)).fetchall()}
    new_ids = set(int(i) for i in employee_ids if i)
    # tambah yang baru
    for eid in new_ids - old_ids:
        emp = db.execute('SELECT name, divisi FROM employees WHERE id=?', (eid,)).fetchone()
        db.execute('INSERT OR IGNORE INTO sc_ticket_assignees(ticket_id,employee_id,divisi) VALUES(?,?,?)',
                   (ticket_id, eid, emp['divisi'] if emp else ''))
        if emp:
            _sc_ticket_history(db, ticket_id, 'assignee_added', 'assignee', '', emp['name'],
                               f"Assignee ditambahkan: {emp['name']}")
    # hapus yang dicopot
    for eid in old_ids - new_ids:
        emp = db.execute('SELECT name FROM employees WHERE id=?', (eid,)).fetchone()
        db.execute('DELETE FROM sc_ticket_assignees WHERE ticket_id=? AND employee_id=?', (ticket_id, eid))
        if emp:
            _sc_ticket_history(db, ticket_id, 'assignee_removed', 'assignee', emp['name'], '',
                               f"Assignee dicopot: {emp['name']}")
    db.commit()

def _sc_notify_ticket(db, ticket_id, event='created'):
    """Send Telegram notification to customer group when ticket event occurs."""
    try:
        bot_token = db.execute("SELECT value FROM app_settings WHERE key='telegram_bot_token'").fetchone()
        if not bot_token or not bot_token['value']:
            return
        t = db.execute('''SELECT t.*, cu.name as customer_name, cu.telegram_group_id,
                          st.name as type_name, e.name as assignee_name
                          FROM sc_tickets t
                          JOIN sc_customers cu ON cu.id=t.customer_id
                          JOIN sc_support_types st ON st.id=t.support_type_id
                          LEFT JOIN employees e ON e.id=t.assignee_id
                          WHERE t.id=?''', (ticket_id,)).fetchone()
        if not t or not t['telegram_group_id']:
            return
        labels = {'created': '🎫 Tiket Baru', 'assigned': '👤 Tiket Ditugaskan', 'status': '🔄 Status Tiket Diperbarui'}
        header = labels.get(event, '📋 Update Tiket')
        msg = f"{header}\n\n*{t['ticket_no']}* — {t['subject']}\nCustomer: {t['customer_name']}\nTipe: {t['type_name']}\nStatus: {t['status']}"
        if t['assignee_name']:
            msg += f"\nAssignee: {t['assignee_name']}"
        send_telegram(bot_token['value'], t['telegram_group_id'], msg)
    except Exception:
        pass

# ── Auto-log tiket ke Project Performance evaluasi karyawan ───────────────────
def _sc_log_ticket_to_eval(db, ticket_id, event='assigned'):
    """Catat aktivitas tiket ke project_entries evaluasi karyawan (assignee)."""
    try:
        from datetime import date as _date
        t = db.execute('''SELECT t.*, cu.name as customer_name, st.name as type_name
                          FROM sc_tickets t
                          JOIN sc_customers cu ON cu.id=t.customer_id
                          JOIN sc_support_types st ON st.id=t.support_type_id
                          WHERE t.id=?''', (ticket_id,)).fetchone()
        if not t or not t['assignee_id']:
            return
        periode = str(_date.today().year)
        # Cari atau buat evaluasi periode ini untuk assignee
        ev = db.execute('SELECT id FROM evaluations WHERE employee_id=? AND periode=?',
                        (t['assignee_id'], periode)).fetchone()
        if not ev:
            cur = db.execute('INSERT INTO evaluations(employee_id, periode, status) VALUES(?,?,?)',
                             (t['assignee_id'], periode, 'draft'))
            eval_id = cur.lastrowid
        else:
            eval_id = ev['id']
        # Cek apakah tiket ini sudah ada di project_entries
        existing = db.execute(
            "SELECT id FROM project_entries WHERE eval_id=? AND project_name=?",
            (eval_id, t['ticket_no'])).fetchone()
        status_map = {
            'assigned':    'ON PROGRESS',
            'in_progress': 'ON PROGRESS',
            'resolved':    'DONE',
            'closed':      'DONE',
            'hold':        'HOLD',
            'feedback':    'ON PROGRESS',
            'rejected':    'CANCELLED',
        }
        entry_status = status_map.get(event, status_map.get(t['status'], 'ON PROGRESS'))
        detail = f"[{t['type_name']}] {t['subject']} — {t['customer_name']}"
        if existing:
            db.execute('UPDATE project_entries SET status=?, detail_task=? WHERE id=?',
                       (entry_status, detail, existing['id']))
        else:
            max_order = db.execute('SELECT COALESCE(MAX(sort_order),0) FROM project_entries WHERE eval_id=? AND entry_type=?',
                                   (eval_id, 'history')).fetchone()[0]
            db.execute('''INSERT INTO project_entries(eval_id,entry_type,project_name,detail_task,status,notes,sort_order)
                          VALUES(?,?,?,?,?,?,?)''',
                       (eval_id, 'history', t['ticket_no'], detail, entry_status,
                        f"Auto dari SupportCore #{t['ticket_no']}", max_order + 1))
        db.commit()
    except Exception:
        pass

# ── Master Apps / Modules ──────────────────────────────────────────────────────
@app.route('/support/apps')
@login_required
def sc_apps():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db   = get_db()
    apps = db.execute('SELECT * FROM sc_apps ORDER BY name').fetchall()
    mods = db.execute('''SELECT m.*, a.name as app_name FROM sc_modules m
                         JOIN sc_apps a ON a.id=m.app_id ORDER BY a.name, m.name''').fetchall()
    return render_template('sc_apps.html', apps=apps, mods=mods)

@app.route('/support/apps/add', methods=['GET','POST'])
@login_required
def sc_app_add():
    if not sc_require('sc_manage_apps'): return redirect(url_for('sc_apps'))
    db = get_db()
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        if not name:
            flash('Nama aplikasi wajib diisi', 'danger')
        else:
            try:
                db.execute('INSERT INTO sc_apps(name,description) VALUES(?,?)',
                           (name, request.form.get('description','').strip()))
                db.commit()
                flash(f'Aplikasi "{name}" ditambahkan', 'success')
                return redirect(url_for('sc_apps'))
            except Exception:
                flash('Nama aplikasi sudah ada', 'danger')
    return render_template('sc_apps.html', apps=db.execute('SELECT * FROM sc_apps ORDER BY name').fetchall(),
                           mods=db.execute('SELECT m.*, a.name as app_name FROM sc_modules m JOIN sc_apps a ON a.id=m.app_id ORDER BY a.name, m.name').fetchall(),
                           add_app=True)

@app.route('/support/apps/<int:aid>/delete', methods=['POST'])
@login_required
def sc_app_delete(aid):
    if not sc_require('sc_manage_apps'): return redirect(url_for('sc_apps'))
    db = get_db()
    db.execute('DELETE FROM sc_apps WHERE id=?', (aid,))
    db.commit()
    flash('Aplikasi dihapus', 'warning')
    return redirect(url_for('sc_apps'))

@app.route('/support/modules/add', methods=['POST'])
@login_required
def sc_module_add():
    if not sc_require('sc_manage_apps'): return redirect(url_for('sc_apps'))
    db = get_db()
    app_id = request.form.get('app_id')
    name   = request.form.get('name','').strip()
    if not app_id or not name:
        flash('Aplikasi dan nama modul wajib diisi', 'danger')
    else:
        try:
            db.execute('INSERT INTO sc_modules(app_id,name,description) VALUES(?,?,?)',
                       (app_id, name, request.form.get('description','').strip()))
            db.commit()
            flash(f'Modul "{name}" ditambahkan', 'success')
        except Exception:
            flash('Modul sudah ada di aplikasi tersebut', 'danger')
    return redirect(url_for('sc_apps'))

@app.route('/support/modules/<int:mid>/delete', methods=['POST'])
@login_required
def sc_module_delete(mid):
    if not sc_require('sc_manage_apps'): return redirect(url_for('sc_apps'))
    db = get_db()
    db.execute('DELETE FROM sc_modules WHERE id=?', (mid,))
    db.commit()
    flash('Modul dihapus', 'warning')
    return redirect(url_for('sc_apps'))

@app.route('/support/api/modules')
@login_required
def sc_api_modules():
    db = get_db()
    mods = db.execute('''SELECT m.id, m.name, a.id as app_id, a.name as app_name
                         FROM sc_modules m JOIN sc_apps a ON a.id=m.app_id
                         WHERE m.is_active=1 ORDER BY a.name, m.name''').fetchall()
    return jsonify([dict(r) for r in mods])

# ── Master Services ────────────────────────────────────────────────────────────
@app.route('/support/services')
@login_required
def sc_services():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db = get_db()
    rows = db.execute('SELECT * FROM sc_services ORDER BY name').fetchall()
    return render_template('sc_services.html', rows=rows)

@app.route('/support/services/add', methods=['GET', 'POST'])
@login_required
def sc_service_add():
    if not sc_require('sc_manage_services'): return redirect(url_for('sc_services'))
    db = get_db()
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        if not code or not name:
            flash('Kode dan nama wajib diisi', 'danger')
        else:
            try:
                db.execute('INSERT INTO sc_services(code,name,description) VALUES(?,?,?)',
                           (code, name, request.form.get('description','').strip()))
                db.commit()
                flash(f'Layanan "{name}" ditambahkan', 'success')
                return redirect(url_for('sc_services'))
            except Exception:
                flash('Kode layanan sudah ada', 'danger')
    return render_template('sc_service_form.html', row=None)

@app.route('/support/services/<int:sid>/edit', methods=['GET', 'POST'])
@login_required
def sc_service_edit(sid):
    if not sc_require('sc_manage_services'): return redirect(url_for('sc_services'))
    db  = get_db()
    row = db.execute('SELECT * FROM sc_services WHERE id=?', (sid,)).fetchone()
    if not row:
        flash('Layanan tidak ditemukan', 'danger')
        return redirect(url_for('sc_services'))
    if request.method == 'POST':
        is_active = 1 if request.form.get('is_active') else 0
        db.execute('UPDATE sc_services SET name=?,description=?,is_active=? WHERE id=?',
                   (request.form.get('name','').strip(),
                    request.form.get('description','').strip(),
                    is_active, sid))
        db.commit()
        flash('Layanan diperbarui', 'success')
        return redirect(url_for('sc_services'))
    return render_template('sc_service_form.html', row=row)

@app.route('/support/services/<int:sid>/delete', methods=['POST'])
@login_required
def sc_service_delete(sid):
    if not sc_require('sc_manage_services'): return redirect(url_for('sc_services'))
    db = get_db()
    db.execute('UPDATE sc_services SET is_active=0 WHERE id=?', (sid,))
    db.commit()
    flash('Layanan dinonaktifkan', 'warning')
    return redirect(url_for('sc_services'))

# ── Master Support Types ───────────────────────────────────────────────────────
@app.route('/support/support-types')
@login_required
def sc_support_types():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db = get_db()
    rows = db.execute('SELECT * FROM sc_support_types ORDER BY code').fetchall()
    return render_template('sc_support_types.html', rows=rows)

@app.route('/support/support-types/add', methods=['GET', 'POST'])
@login_required
def sc_support_type_add():
    if not sc_require('sc_manage_types'): return redirect(url_for('sc_support_types'))
    db = get_db()
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        if not code or not name:
            flash('Kode dan nama wajib diisi', 'danger')
        else:
            try:
                db.execute('INSERT INTO sc_support_types(code,name,description) VALUES(?,?,?)',
                           (code, name, request.form.get('description','').strip()))
                db.commit()
                flash(f'Tipe support "{name}" ditambahkan', 'success')
                return redirect(url_for('sc_support_types'))
            except Exception:
                flash('Kode tipe sudah ada', 'danger')
    return render_template('sc_support_type_form.html', row=None)

@app.route('/support/support-types/<int:tid>/edit', methods=['GET', 'POST'])
@login_required
def sc_support_type_edit(tid):
    if not sc_require('sc_manage_types'): return redirect(url_for('sc_support_types'))
    db  = get_db()
    row = db.execute('SELECT * FROM sc_support_types WHERE id=?', (tid,)).fetchone()
    if not row:
        flash('Tipe support tidak ditemukan', 'danger')
        return redirect(url_for('sc_support_types'))
    if request.method == 'POST':
        is_active = 1 if request.form.get('is_active') else 0
        db.execute('UPDATE sc_support_types SET name=?,description=?,is_active=? WHERE id=?',
                   (request.form.get('name','').strip(),
                    request.form.get('description','').strip(),
                    is_active, tid))
        db.commit()
        flash('Tipe support diperbarui', 'success')
        return redirect(url_for('sc_support_types'))
    return render_template('sc_support_type_form.html', row=row)

@app.route('/support/support-types/<int:tid>/delete', methods=['POST'])
@login_required
def sc_support_type_delete(tid):
    if not sc_require('sc_manage_types'): return redirect(url_for('sc_support_types'))
    db = get_db()
    db.execute('UPDATE sc_support_types SET is_active=0 WHERE id=?', (tid,))
    db.commit()
    flash('Tipe support dinonaktifkan', 'warning')
    return redirect(url_for('sc_support_types'))

# ── Master SLA Categories ──────────────────────────────────────────────────────
@app.route('/support/sla-categories')
@login_required
def sc_sla_categories():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db = get_db()
    rows = db.execute('SELECT * FROM sc_sla_categories ORDER BY priority, name').fetchall()
    return render_template('sc_sla_categories.html', rows=rows)

@app.route('/support/sla-categories/add', methods=['GET', 'POST'])
@login_required
def sc_sla_category_add():
    if not sc_require('sc_manage_sla'): return redirect(url_for('sc_sla_categories'))
    db = get_db()
    if request.method == 'POST':
        code = request.form.get('code', '').strip().upper()
        name = request.form.get('name', '').strip()
        if not code or not name:
            flash('Kode dan nama wajib diisi', 'danger')
        else:
            try:
                wta_raw = request.form.get('workaround_time_hours','').strip()
                db.execute('''INSERT INTO sc_sla_categories
                              (code,name,priority,response_time_hours,workaround_time_hours,
                               resolution_time_hours,maintenance_type,description)
                              VALUES(?,?,?,?,?,?,?,?)''',
                           (code, name,
                            request.form.get('priority','Medium'),
                            float(request.form.get('response_time_hours', 4) or 4),
                            float(wta_raw) if wta_raw else None,
                            float(request.form.get('resolution_time_hours', 24) or 24),
                            request.form.get('maintenance_type','corrective'),
                            request.form.get('description','').strip()))
                db.commit()
                flash(f'Kategori SLA "{name}" ditambahkan', 'success')
                return redirect(url_for('sc_sla_categories'))
            except Exception:
                flash('Kode kategori sudah ada', 'danger')
    return render_template('sc_sla_category_form.html', row=None)

@app.route('/support/sla-categories/<int:kid>/edit', methods=['GET', 'POST'])
@login_required
def sc_sla_category_edit(kid):
    if not sc_require('sc_manage_sla'): return redirect(url_for('sc_sla_categories'))
    db  = get_db()
    row = db.execute('SELECT * FROM sc_sla_categories WHERE id=?', (kid,)).fetchone()
    if not row:
        flash('Kategori SLA tidak ditemukan', 'danger')
        return redirect(url_for('sc_sla_categories'))
    if request.method == 'POST':
        is_active = 1 if request.form.get('is_active') else 0
        wta_raw = request.form.get('workaround_time_hours','').strip()
        db.execute('''UPDATE sc_sla_categories
                      SET name=?,priority=?,response_time_hours=?,workaround_time_hours=?,
                          resolution_time_hours=?,maintenance_type=?,description=?,is_active=?
                      WHERE id=?''',
                   (request.form.get('name','').strip(),
                    request.form.get('priority','Medium'),
                    float(request.form.get('response_time_hours', 4) or 4),
                    float(wta_raw) if wta_raw else None,
                    float(request.form.get('resolution_time_hours', 24) or 24),
                    request.form.get('maintenance_type','corrective'),
                    request.form.get('description','').strip(),
                    is_active, kid))
        db.commit()
        flash('Kategori SLA diperbarui', 'success')
        return redirect(url_for('sc_sla_categories'))
    return render_template('sc_sla_category_form.html', row=row)

@app.route('/support/sla-categories/<int:kid>/delete', methods=['POST'])
@login_required
def sc_sla_category_delete(kid):
    if not sc_require('sc_manage_sla'): return redirect(url_for('sc_sla_categories'))
    db = get_db()
    db.execute('UPDATE sc_sla_categories SET is_active=0 WHERE id=?', (kid,))
    db.commit()
    flash('Kategori SLA dinonaktifkan', 'warning')
    return redirect(url_for('sc_sla_categories'))

# ─── SupportCore Helpers ───────────────────────────────────────────────────────

def generate_ticket_no(db):
    from datetime import datetime as _dt
    year = datetime.now().year
    last = db.execute(
        "SELECT ticket_no FROM sc_tickets WHERE ticket_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'TKT-{year}-%',)
    ).fetchone()
    seq = (int(last['ticket_no'].rsplit('-', 1)[-1]) + 1) if last else 1
    return f'TKT-{year}-{seq:04d}'

def calc_sla(reported_at_str, done_at_str, limit_hours):
    """Returns ('met'|'violated'|'pending', hours_taken_or_None)"""
    if not reported_at_str:
        return 'pending', None
    from datetime import datetime as _dt
    fmt = '%Y-%m-%d %H:%M:%S'
    try:
        t0 = datetime.strptime(reported_at_str[:19], fmt)
        if done_at_str:
            t1 = datetime.strptime(done_at_str[:19], fmt)
            hours = (t1 - t0).total_seconds() / 3600
            return ('met' if hours <= limit_hours else 'violated'), round(hours, 1)
        return 'pending', None
    except Exception:
        return 'pending', None

# ─── SupportCore: Contracts ────────────────────────────────────────────────────

@app.route('/support/contracts')
@login_required
def sc_contracts():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db = get_db()
    from datetime import date as _date
    today = _date.today().isoformat()
    customer_id = request.args.get('customer_id', '')
    status_f    = request.args.get('status', '')
    q = '''SELECT c.*, cu.name as customer_name
           FROM sc_contracts c
           JOIN sc_customers cu ON cu.id = c.customer_id
           WHERE 1=1'''
    params = []
    if customer_id:
        q += ' AND c.customer_id=?'; params.append(customer_id)
    if status_f:
        q += ' AND c.status=?'; params.append(status_f)
    q += ' ORDER BY c.start_date DESC'
    rows = db.execute(q, params).fetchall()
    customers = db.execute('SELECT id, name FROM sc_customers WHERE is_active=1 ORDER BY name').fetchall()
    # Compute display status
    contracts = []
    for r in rows:
        d = dict(r)
        if d['status'] not in ('terminated', 'draft') and d['end_date'] < today:
            d['display_status'] = 'expired'
        else:
            d['display_status'] = d['status']
        contracts.append(d)
    return render_template('sc_contracts.html', contracts=contracts, customers=customers,
                           filter_customer=customer_id, filter_status=status_f, today=today)

def _sc_pic_employees(db):
    """Return (helpdesk, implementor, coleader, sales) employee lists for PIC dropdowns."""
    helpdesk   = db.execute("SELECT id,name,jabatan FROM employees WHERE is_active=1 AND LOWER(divisi) LIKE '%helpdesk%' ORDER BY name").fetchall()
    implementor= db.execute("SELECT id,name,jabatan FROM employees WHERE is_active=1 AND LOWER(divisi) LIKE '%implementor%' ORDER BY name").fetchall()
    coleader   = db.execute("SELECT id,name,jabatan FROM employees WHERE is_active=1 AND LOWER(level) LIKE '%co-leader%' ORDER BY name").fetchall()
    sales      = db.execute("SELECT id,name,jabatan FROM employees WHERE is_active=1 AND (LOWER(divisi) LIKE '%sales%' OR LOWER(jabatan) LIKE '%sales%') ORDER BY name").fetchall()
    return helpdesk, implementor, coleader, sales

def _sc_customer_pic_info(db, customer_id):
    """Return dict of PIC names for a customer (for AJAX & template display)."""
    c = db.execute('SELECT pic_helpdesk_id,pic_helpdesk_backup_id,pic_implementor_id,pic_coleader_id FROM sc_customers WHERE id=?', (customer_id,)).fetchone()
    if not c:
        return {}
    def emp_name(eid):
        if not eid: return None
        r = db.execute('SELECT name, jabatan FROM employees WHERE id=?', (eid,)).fetchone()
        return f"{r['name']} ({r['jabatan']})" if r else None
    return {
        'pic_helpdesk':        emp_name(c['pic_helpdesk_id']),
        'pic_helpdesk_backup': emp_name(c['pic_helpdesk_backup_id']),
        'pic_implementor':     emp_name(c['pic_implementor_id']),
        'pic_coleader':        emp_name(c['pic_coleader_id']),
    }

def _sc_contract_lookups(db):
    """Data lookup yang dibutuhkan form kontrak."""
    customers     = db.execute('SELECT id, name FROM sc_customers WHERE is_active=1 ORDER BY name').fetchall()
    services      = db.execute('SELECT id, name FROM sc_services WHERE is_active=1 ORDER BY name').fetchall()
    support_types = db.execute('SELECT id, code, name FROM sc_support_types WHERE is_active=1 ORDER BY code').fetchall()
    helpdesk_emps = db.execute(
        "SELECT id, name, jabatan FROM employees WHERE is_active=1 AND LOWER(divisi) LIKE '%helpdesk%' ORDER BY name"
    ).fetchall()
    return customers, services, support_types, helpdesk_emps

@app.route('/support/contracts/add', methods=['GET','POST'])
@login_required
def sc_contract_add():
    if not sc_require('sc_manage_contracts'): return redirect(url_for('sc_contracts'))
    db = get_db()
    customers, services, support_types, helpdesk_emps = _sc_contract_lookups(db)
    if request.method == 'POST':
        code      = request.form['code'].strip()
        cid       = request.form['customer_id']
        title     = request.form['title'].strip()
        desc      = request.form.get('description','').strip()
        sd        = request.form['start_date']
        ed        = request.form['end_date']
        val       = float(request.form.get('contract_value') or 0)
        status    = request.form.get('status','active')
        notes     = request.form.get('notes','').strip()
        pic_id    = request.form.get('pic_helpdesk_id') or None
        svc_ids   = request.form.getlist('service_ids')
        type_ids  = request.form.getlist('support_type_ids')
        try:
            db.execute('''INSERT INTO sc_contracts(code,customer_id,title,description,start_date,end_date,
                          contract_value,status,notes,pic_helpdesk_id,created_by)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       (code, cid, title, desc, sd, ed, val, status, notes, pic_id, session.get('user_id')))
            ctr_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            for sid in svc_ids:
                db.execute('INSERT OR IGNORE INTO sc_contract_services(contract_id,service_id) VALUES(?,?)', (ctr_id, sid))
            for tid in type_ids:
                db.execute('INSERT OR IGNORE INTO sc_contract_support_types(contract_id,support_type_id) VALUES(?,?)', (ctr_id, tid))
            db.commit()
            flash('Kontrak berhasil ditambahkan', 'success')
            return redirect(url_for('sc_contracts'))
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return render_template('sc_contract_form.html', row=None,
                           customers=customers, services=services,
                           support_types=support_types, helpdesk_emps=helpdesk_emps,
                           sel_services=[], sel_types=[], sel_pic=None)

@app.route('/support/contracts/<int:cid>/edit', methods=['GET','POST'])
@login_required
def sc_contract_edit(cid):
    if not sc_require('sc_manage_contracts'): return redirect(url_for('sc_contracts'))
    db  = get_db()
    row = db.execute('SELECT * FROM sc_contracts WHERE id=?', (cid,)).fetchone()
    if not row: abort(404)
    customers, services, support_types, helpdesk_emps = _sc_contract_lookups(db)
    sel_services = [r['service_id']      for r in db.execute('SELECT service_id FROM sc_contract_services WHERE contract_id=?',      (cid,)).fetchall()]
    sel_types    = [r['support_type_id'] for r in db.execute('SELECT support_type_id FROM sc_contract_support_types WHERE contract_id=?', (cid,)).fetchall()]
    if request.method == 'POST':
        code     = request.form['code'].strip()
        custid   = request.form['customer_id']
        title    = request.form['title'].strip()
        desc     = request.form.get('description','').strip()
        sd       = request.form['start_date']
        ed       = request.form['end_date']
        val      = float(request.form.get('contract_value') or 0)
        status   = request.form.get('status','active')
        notes    = request.form.get('notes','').strip()
        pic_id   = request.form.get('pic_helpdesk_id') or None
        svc_ids  = request.form.getlist('service_ids')
        type_ids = request.form.getlist('support_type_ids')
        try:
            db.execute('''UPDATE sc_contracts SET code=?,customer_id=?,title=?,description=?,start_date=?,
                          end_date=?,contract_value=?,status=?,notes=?,pic_helpdesk_id=? WHERE id=?''',
                       (code, custid, title, desc, sd, ed, val, status, notes, pic_id, cid))
            db.execute('DELETE FROM sc_contract_services WHERE contract_id=?', (cid,))
            db.execute('DELETE FROM sc_contract_support_types WHERE contract_id=?', (cid,))
            for sid in svc_ids:
                db.execute('INSERT OR IGNORE INTO sc_contract_services(contract_id,service_id) VALUES(?,?)', (cid, sid))
            for tid in type_ids:
                db.execute('INSERT OR IGNORE INTO sc_contract_support_types(contract_id,support_type_id) VALUES(?,?)', (cid, tid))
            db.commit()
            flash('Kontrak diperbarui', 'success')
            return redirect(url_for('sc_contracts'))
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return render_template('sc_contract_form.html', row=row,
                           customers=customers, services=services,
                           support_types=support_types, helpdesk_emps=helpdesk_emps,
                           sel_services=sel_services, sel_types=sel_types,
                           sel_pic=row['pic_helpdesk_id'])

@app.route('/support/contracts/<int:cid>')
@login_required
def sc_contract_detail(cid):
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db = get_db()
    from datetime import date as _date
    today = _date.today().isoformat()
    row = db.execute('''SELECT c.*, cu.name as customer_name
                        FROM sc_contracts c JOIN sc_customers cu ON cu.id=c.customer_id
                        WHERE c.id=?''', (cid,)).fetchone()
    if not row: abort(404)
    ctr = dict(row)
    if ctr['status'] not in ('terminated','draft') and ctr['end_date'] < today:
        ctr['display_status'] = 'expired'
    else:
        ctr['display_status'] = ctr['status']
    svcs = db.execute('''SELECT s.name FROM sc_contract_services cs
                         JOIN sc_services s ON s.id=cs.service_id
                         WHERE cs.contract_id=?''', (cid,)).fetchall()
    tickets = db.execute('''SELECT t.*, st.name as type_name, sc.name as sla_name,
                             sc.response_time_hours, sc.resolution_time_hours
                             FROM sc_tickets t
                             JOIN sc_support_types st ON st.id=t.support_type_id
                             LEFT JOIN sc_sla_categories sc ON sc.id=t.sla_category_id
                             WHERE t.contract_id=?
                             ORDER BY t.reported_at DESC''', (cid,)).fetchall()
    sla_stats = {'met': 0, 'violated': 0, 'pending': 0}
    ticket_list = []
    for t in tickets:
        d = dict(t)
        d['sla_status'], _ = calc_sla(d['reported_at'], d['responded_at'], d['response_time_hours'] or 999)
        sla_stats[d['sla_status']] += 1
        ticket_list.append(d)
    return render_template('sc_contract_detail.html', ctr=ctr, svcs=svcs, tickets=ticket_list, sla_stats=sla_stats)

@app.route('/support/contracts/<int:cid>/delete', methods=['POST'])
@login_required
def sc_contract_delete(cid):
    if not sc_require('sc_manage_contracts'): return redirect(url_for('sc_contracts'))
    db = get_db()
    db.execute("UPDATE sc_contracts SET status='terminated' WHERE id=?", (cid,))
    db.commit()
    flash('Kontrak diterminasi', 'warning')
    return redirect(url_for('sc_contracts'))

# ─── SupportCore: Tickets ──────────────────────────────────────────────────────

@app.route('/support/tickets')
@login_required
def sc_tickets():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db = get_db()
    status_f  = request.args.get('status','')
    cust_f    = request.args.get('customer_id','')
    type_f    = request.args.get('support_type_id','')
    date_from = request.args.get('date_from','')
    date_to   = request.args.get('date_to','')
    q = '''SELECT t.*, cu.name as customer_name, st.name as type_name,
                  sc.name as sla_name, sc.response_time_hours
           FROM sc_tickets t
           JOIN sc_customers cu ON cu.id=t.customer_id
           JOIN sc_support_types st ON st.id=t.support_type_id
           LEFT JOIN sc_sla_categories sc ON sc.id=t.sla_category_id
           WHERE 1=1'''
    params = []
    if status_f:
        q += ' AND t.status=?'; params.append(status_f)
    if cust_f:
        q += ' AND t.customer_id=?'; params.append(cust_f)
    if type_f:
        q += ' AND t.support_type_id=?'; params.append(type_f)
    if date_from:
        q += ' AND t.reported_at >= ?'; params.append(date_from)
    if date_to:
        q += ' AND t.reported_at <= ?'; params.append(date_to + ' 23:59:59')
    q += ' ORDER BY t.reported_at DESC'
    rows = db.execute(q, params).fetchall()
    tickets = []
    for r in rows:
        d = dict(r)
        d['sla_status'], d['sla_hours'] = calc_sla(d['reported_at'], d['responded_at'], d['response_time_hours'] or 999)
        tickets.append(d)
    customers    = db.execute('SELECT id, name FROM sc_customers WHERE is_active=1 ORDER BY name').fetchall()
    support_types = db.execute('SELECT id, name FROM sc_support_types WHERE is_active=1 ORDER BY name').fetchall()
    return render_template('sc_tickets.html', tickets=tickets, customers=customers,
                           support_types=support_types, filter_status=status_f,
                           filter_customer=cust_f, filter_type=type_f,
                           filter_date_from=date_from, filter_date_to=date_to)

def _sc_ticket_lookups(db):
    customers     = db.execute('SELECT id, name FROM sc_customers WHERE is_active=1 ORDER BY name').fetchall()
    support_types = db.execute('SELECT id, name FROM sc_support_types WHERE is_active=1 ORDER BY name').fetchall()
    sla_cats      = db.execute('SELECT id, name FROM sc_sla_categories WHERE is_active=1 ORDER BY name').fetchall()
    contracts     = db.execute('SELECT id, code, title, customer_id FROM sc_contracts ORDER BY start_date DESC').fetchall()
    modules       = db.execute('''SELECT m.id, m.name, a.id as app_id, a.name as app_name
                                  FROM sc_modules m JOIN sc_apps a ON a.id=m.app_id
                                  WHERE m.is_active=1 ORDER BY a.name, m.name''').fetchall()
    employees     = db.execute("SELECT id, name, jabatan, divisi FROM employees WHERE is_active=1 ORDER BY divisi, name").fetchall()
    return customers, support_types, sla_cats, contracts, modules, employees

@app.route('/support/tickets/add', methods=['GET','POST'])
@login_required
def sc_ticket_add():
    if not sc_require('sc_manage_tickets'): return redirect(url_for('sc_tickets'))
    db = get_db()
    customers, support_types, sla_cats, contracts, modules, employees = _sc_ticket_lookups(db)
    if request.method == 'POST':
        ticket_no     = generate_ticket_no(db)
        contract_id   = request.form.get('contract_id') or None
        customer_id   = request.form['customer_id']
        support_type_id = request.form['support_type_id']
        sla_cat_id    = request.form.get('sla_category_id') or None
        subject       = request.form['subject'].strip()
        description   = request.form.get('description','').strip()
        reported_by   = request.form.get('reported_by','').strip()
        reported_at   = request.form.get('reported_at','').strip() or None
        notes         = request.form.get('notes','').strip()
        module_id     = request.form.get('module_id') or None
        assignee_ids  = request.form.getlist('assignee_ids')
        status_note   = request.form.get('status_note','').strip()
        mandays       = request.form.get('mandays','').strip() or None
        pct_done      = request.form.get('pct_done',0)
        solution_type = request.form.get('solution_type','').strip()
        solution_note = request.form.get('solution_note','').strip()
        due_date      = request.form.get('due_date','').strip() or None
        work_start_date = request.form.get('work_start_date','').strip() or None
        media_lapor   = request.form.get('media_lapor','').strip()
        priority      = request.form.get('priority','Medium').strip()
        from datetime import datetime as _dt
        if not reported_at:
            reported_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            cur = db.execute('''INSERT INTO sc_tickets(ticket_no,contract_id,customer_id,support_type_id,
                          sla_category_id,subject,description,reported_by,reported_at,notes,created_by,
                          module_id,status_note,mandays,pct_done,solution_type,solution_note,
                          due_date,work_start_date,media_lapor,priority)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (ticket_no, contract_id, customer_id, support_type_id, sla_cat_id,
                        subject, description, reported_by, reported_at, notes, session.get('user_id'),
                        module_id, status_note, mandays, pct_done,
                        solution_type, solution_note, due_date, work_start_date, media_lapor, priority))
            new_id = cur.lastrowid
            db.commit()
            _sc_ticket_history(db, new_id, 'created', notes=f'Tiket dibuat oleh {session.get("user_name","")}')
            for sec in ('description', 'status_note', 'solution_note'):
                _save_ticket_attachments(db, new_id, sec)
            ext_names = [n for n in request.form.get('external_assignees','').split('\n') if n.strip()]
            _sync_external_assignees(db, 'sc_ticket_external_assignees', 'ticket_id', new_id, ext_names)
            db.commit()
            _sc_sync_assignees(db, new_id, assignee_ids)
            _sc_notify_ticket(db, new_id, 'created')
            if assignee_ids:
                _sc_notify_ticket(db, new_id, 'assigned')
                for aid in assignee_ids:
                    if aid: _sc_log_ticket_to_eval(db, new_id, 'assigned')
            flash(f'Tiket {ticket_no} berhasil dibuat', 'success')
            return redirect(url_for('sc_tickets'))
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    return render_template('sc_ticket_form.html', row=None, sel_assignees=[], ext_assignees=[],
                           customers=customers,
                           support_types=support_types, sla_cats=sla_cats, contracts=contracts,
                           modules=modules, employees=employees,
                           sc_ticket_statuses=SC_TICKET_STATUSES,
                           sc_solution_types=SC_SOLUTION_TYPES,
                           sc_media_lapor=SC_MEDIA_LAPOR,
                           sc_ticket_priorities=SC_TICKET_PRIORITIES)

@app.route('/support/tickets/<int:tid>/edit', methods=['GET','POST'])
@login_required
def sc_ticket_edit(tid):
    if not sc_require('sc_manage_tickets'): return redirect(url_for('sc_tickets'))
    db = get_db()
    row = db.execute('SELECT * FROM sc_tickets WHERE id=?', (tid,)).fetchone()
    if not row: abort(404)
    customers, support_types, sla_cats, contracts, modules, employees = _sc_ticket_lookups(db)
    sel_assignees = [r['employee_id'] for r in
                     db.execute('SELECT employee_id FROM sc_ticket_assignees WHERE ticket_id=?', (tid,)).fetchall()]
    if request.method == 'POST':
        contract_id     = request.form.get('contract_id') or None
        customer_id     = request.form['customer_id']
        support_type_id = request.form['support_type_id']
        sla_cat_id      = request.form.get('sla_category_id') or None
        subject         = request.form['subject'].strip()
        description     = request.form.get('description','').strip()
        reported_by     = request.form.get('reported_by','').strip()
        reported_at     = request.form.get('reported_at','').strip() or None
        notes           = request.form.get('notes','').strip()
        module_id       = request.form.get('module_id') or None
        assignee_ids    = request.form.getlist('assignee_ids')
        status_note     = request.form.get('status_note','').strip()
        mandays         = request.form.get('mandays','').strip() or None
        pct_done        = request.form.get('pct_done',0)
        solution_type   = request.form.get('solution_type','').strip()
        solution_note   = request.form.get('solution_note','').strip()
        due_date        = request.form.get('due_date','').strip() or None
        work_start_date = request.form.get('work_start_date','').strip() or None
        media_lapor     = request.form.get('media_lapor','').strip()
        priority        = request.form.get('priority','Medium').strip()
        # Track changed fields for history
        changed = []
        if row['status_note'] != status_note: changed.append(('status_note','Keterangan',row['status_note'],status_note))
        if str(row['pct_done'] or 0) != str(pct_done): changed.append(('pct_done','% Done',row['pct_done'],pct_done))
        if (row['solution_type'] or '') != solution_type: changed.append(('solution_type','Tipe Solusi',row['solution_type'],solution_type))
        if (row['priority'] or 'Medium') != priority: changed.append(('priority','Prioritas',row['priority'],priority))
        try:
            db.execute('''UPDATE sc_tickets SET contract_id=?,customer_id=?,support_type_id=?,
                          sla_category_id=?,subject=?,description=?,reported_by=?,reported_at=?,notes=?,
                          module_id=?,status_note=?,mandays=?,pct_done=?,
                          solution_type=?,solution_note=?,due_date=?,work_start_date=?,media_lapor=?,priority=?
                          WHERE id=?''',
                       (contract_id, customer_id, support_type_id, sla_cat_id,
                        subject, description, reported_by, reported_at, notes,
                        module_id, status_note, mandays, pct_done,
                        solution_type, solution_note, due_date, work_start_date, media_lapor, priority, tid))
            db.commit()
            for sec in ('description', 'status_note', 'solution_note'):
                _save_ticket_attachments(db, tid, sec)
            ext_names = [n for n in request.form.get('external_assignees','').split('\n') if n.strip()]
            _sync_external_assignees(db, 'sc_ticket_external_assignees', 'ticket_id', tid, ext_names)
            db.commit()
            for field, label, old, new in changed:
                _sc_ticket_history(db, tid, 'update', field, old, new, f'{label} diubah')
            _sc_sync_assignees(db, tid, assignee_ids)
            for aid in assignee_ids:
                if aid: _sc_log_ticket_to_eval(db, tid, 'assigned')
            flash('Tiket diperbarui', 'success')
            return redirect(url_for('sc_ticket_detail', tid=tid))
        except Exception as e:
            flash(f'Error: {e}', 'danger')
    sel_ext = db.execute(
        'SELECT name FROM sc_ticket_external_assignees WHERE ticket_id=? ORDER BY id', (tid,)).fetchall()
    return render_template('sc_ticket_form.html', row=row, sel_assignees=sel_assignees,
                           ext_assignees=[r['name'] for r in sel_ext],
                           customers=customers,
                           support_types=support_types, sla_cats=sla_cats, contracts=contracts,
                           modules=modules, employees=employees,
                           sc_ticket_statuses=SC_TICKET_STATUSES,
                           sc_solution_types=SC_SOLUTION_TYPES,
                           sc_media_lapor=SC_MEDIA_LAPOR,
                           sc_ticket_priorities=SC_TICKET_PRIORITIES)

@app.route('/support/tickets/<int:tid>')
@login_required
def sc_ticket_detail(tid):
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db = get_db()
    row = db.execute('''SELECT t.*, cu.name as customer_name, st.name as type_name,
                        sc.name as sla_name, sc.response_time_hours, sc.resolution_time_hours,
                        co.code as contract_code, co.title as contract_title
                        FROM sc_tickets t
                        JOIN sc_customers cu ON cu.id=t.customer_id
                        JOIN sc_support_types st ON st.id=t.support_type_id
                        LEFT JOIN sc_sla_categories sc ON sc.id=t.sla_category_id
                        LEFT JOIN sc_contracts co ON co.id=t.contract_id
                        WHERE t.id=?''', (tid,)).fetchone()
    if not row: abort(404)
    t = dict(row)
    t['resp_sla'], t['resp_hours'] = calc_sla(t['reported_at'], t['responded_at'], t['response_time_hours'] or 999)
    t['res_sla'],  t['res_hours']  = calc_sla(t['reported_at'], t['resolved_at'],  t['resolution_time_hours'] or 999)
    history  = db.execute('SELECT * FROM sc_ticket_history WHERE ticket_id=? ORDER BY id DESC', (tid,)).fetchall()
    assignees = db.execute('''SELECT e.name, e.jabatan, e.divisi, ta.role_note
                              FROM sc_ticket_assignees ta
                              JOIN employees e ON e.id=ta.employee_id
                              WHERE ta.ticket_id=? ORDER BY e.divisi, e.name''', (tid,)).fetchall()
    attachments = db.execute('SELECT * FROM sc_ticket_attachments WHERE ticket_id=? ORDER BY section, id',
                             (tid,)).fetchall()
    att_by_section = {}
    for a in attachments:
        att_by_section.setdefault(a['section'], []).append(a)
    ext_assignees = db.execute(
        'SELECT * FROM sc_ticket_external_assignees WHERE ticket_id=? ORDER BY id', (tid,)).fetchall()
    can_manage = has_permission(session.get('user_role',''), 'sc_manage_tickets', db)
    return render_template('sc_ticket_detail.html', t=t, history=history, assignees=assignees,
                           ext_assignees=ext_assignees,
                           att_by_section=att_by_section, can_manage=can_manage,
                           sc_ticket_statuses=SC_TICKET_STATUSES)

@app.route('/support/tickets/<int:tid>/attachments/<int:att_id>/delete', methods=['POST'])
@login_required
def sc_ticket_attachment_delete(tid, att_id):
    if not sc_require('sc_manage_tickets'): return redirect(url_for('sc_ticket_detail', tid=tid))
    db = get_db()
    att = db.execute('SELECT * FROM sc_ticket_attachments WHERE id=? AND ticket_id=?', (att_id, tid)).fetchone()
    if att:
        try:
            import os as _os
            fpath = _os.path.join(_os.path.dirname(__file__), att['filename'].lstrip('/'))
            if _os.path.exists(fpath):
                _os.remove(fpath)
        except Exception:
            pass
        db.execute('DELETE FROM sc_ticket_attachments WHERE id=?', (att_id,))
        db.commit()
    return redirect(url_for('sc_ticket_detail', tid=tid))

@app.route('/support/tickets/<int:tid>/status', methods=['POST'])
@login_required
def sc_ticket_status(tid):
    db  = get_db()
    row = db.execute('SELECT * FROM sc_tickets WHERE id=?', (tid,)).fetchone()
    if not row: abort(404)
    # Allow any assignee OR users with sc_manage_tickets permission
    user_id    = session.get('user_id')
    emp        = db.execute('SELECT id FROM employees WHERE user_id=?', (user_id,)).fetchone()
    is_assignee = emp and db.execute('SELECT 1 FROM sc_ticket_assignees WHERE ticket_id=? AND employee_id=?',
                                     (tid, emp['id'])).fetchone()
    if not is_assignee and not sc_require('sc_manage_tickets'):
        return redirect(url_for('sc_ticket_detail', tid=tid))
    _allowed_statuses = tuple(s for s, *_ in SC_TICKET_STATUSES)
    new_status  = request.form.get('new_status','')
    if new_status not in _allowed_statuses:
        flash('Status tidak valid.', 'danger')
        return redirect(url_for('sc_ticket_detail', tid=tid))
    status_note = request.form.get('status_note','').strip()
    mandays     = request.form.get('mandays','').strip() or None
    pct_done    = request.form.get('pct_done', row['pct_done'])
    solution_type = request.form.get('solution_type', row['solution_type'] or '')
    solution_note = request.form.get('solution_note', row['solution_note'] or '')
    due_date    = request.form.get('due_date','').strip() or row['due_date']
    work_start  = request.form.get('work_start_date','').strip() or row['work_start_date']
    from datetime import datetime as _dt
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    updates = {'status': new_status, 'status_note': status_note,
               'mandays': mandays, 'pct_done': pct_done,
               'solution_type': solution_type, 'solution_note': solution_note,
               'due_date': due_date, 'work_start_date': work_start}
    if new_status == 'in_progress' and not row['responded_at']:
        updates['responded_at'] = now
    if new_status in ('resolved','feedback') and not row['resolved_at']:
        updates['resolved_at'] = now
    if new_status == 'closed':
        updates['closed_at'] = now
    sets = ', '.join(f'{k}=?' for k in updates)
    db.execute(f'UPDATE sc_tickets SET {sets} WHERE id=?', list(updates.values()) + [tid])
    db.commit()
    _sc_ticket_history(db, tid, 'status_change', 'status', row['status'], new_status,
                       status_note or f'Status diubah ke {new_status}')
    _sc_notify_ticket(db, tid, 'status')
    _sc_log_ticket_to_eval(db, tid, new_status)
    flash(f'Status tiket diubah ke {new_status}', 'success')
    return redirect(url_for('sc_ticket_detail', tid=tid))

# ─── SupportCore: SLA Monitor ──────────────────────────────────────────────────

@app.route('/support/sla-monitor')
@login_required
def sc_sla_monitor():
    if not sc_require('sc_view_reports'): return redirect(url_for('sc_index'))
    db = get_db()
    from datetime import date as _date
    month = request.args.get('month', _date.today().strftime('%Y-%m'))
    date_from = month + '-01'
    # last day
    import calendar
    y, m = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(y, m)[1]
    date_to = f'{month}-{last_day:02d} 23:59:59'
    tickets = db.execute('''SELECT t.*, cu.name as customer_name, st.name as type_name,
                             sc.response_time_hours, sc.resolution_time_hours
                             FROM sc_tickets t
                             JOIN sc_customers cu ON cu.id=t.customer_id
                             JOIN sc_support_types st ON st.id=t.support_type_id
                             LEFT JOIN sc_sla_categories sc ON sc.id=t.sla_category_id
                             WHERE t.reported_at >= ? AND t.reported_at <= ?
                             ORDER BY t.reported_at DESC''', (date_from, date_to)).fetchall()
    total = len(tickets)
    open_cnt = sum(1 for t in tickets if t['status'] == 'open')
    inprog   = sum(1 for t in tickets if t['status'] == 'in_progress')
    sla_met  = 0; sla_vio = 0
    by_customer = {}
    by_type     = {}
    for t in tickets:
        st, _ = calc_sla(t['reported_at'], t['responded_at'], t['response_time_hours'] or 999)
        if st == 'met':   sla_met += 1
        elif st == 'violated': sla_vio += 1
        cn = t['customer_name']
        if cn not in by_customer:
            by_customer[cn] = {'total':0,'met':0,'violated':0,'pending':0}
        by_customer[cn]['total'] += 1
        by_customer[cn][st] += 1
        tn = t['type_name']
        if tn not in by_type:
            by_type[tn] = {'total':0,'met':0,'violated':0,'pending':0}
        by_type[tn]['total'] += 1
        by_type[tn][st] += 1
    # Monthly trend last 6 months
    import json
    trend_labels = []
    trend_met    = []
    trend_vio    = []
    for i in range(5, -1, -1):
        mm = m - i
        yy = y
        while mm <= 0: mm += 12; yy -= 1
        label = f'{yy}-{mm:02d}'
        ld = calendar.monthrange(yy, mm)[1]
        df = f'{label}-01'
        dt = f'{label}-{ld:02d} 23:59:59'
        ts = db.execute('''SELECT t.reported_at, t.responded_at, sc.response_time_hours
                           FROM sc_tickets t
                           LEFT JOIN sc_sla_categories sc ON sc.id=t.sla_category_id
                           WHERE t.reported_at >= ? AND t.reported_at <= ?''', (df, dt)).fetchall()
        met_c = sum(1 for x in ts if calc_sla(x['reported_at'], x['responded_at'], x['response_time_hours'] or 999)[0] == 'met')
        vio_c = sum(1 for x in ts if calc_sla(x['reported_at'], x['responded_at'], x['response_time_hours'] or 999)[0] == 'violated')
        trend_labels.append(label)
        trend_met.append(met_c)
        trend_vio.append(vio_c)
    sla_pct = round(sla_met / total * 100, 1) if total > 0 else 0
    return render_template('sc_sla_monitor.html',
                           total=total, open_cnt=open_cnt, inprog=inprog,
                           sla_met=sla_met, sla_vio=sla_vio, sla_pct=sla_pct,
                           by_customer=by_customer, by_type=by_type,
                           trend_labels=json.dumps(trend_labels),
                           trend_met=json.dumps(trend_met),
                           trend_vio=json.dumps(trend_vio),
                           month=month)

# ─── SupportCore: Presales & POC ──────────────────────────────────────────────

def _sc_presales_no(db):
    from datetime import date as _d
    prefix = f"PSL-{_d.today().year}-"
    last = db.execute("SELECT req_no FROM sc_presales_requests WHERE req_no LIKE ? ORDER BY id DESC LIMIT 1",
                      (prefix + '%',)).fetchone()
    seq = int(last['req_no'].split('-')[-1]) + 1 if last else 1
    return f"{prefix}{seq:04d}"

SC_PRESALES_STATUSES = [
    ('new',         'Baru',       'secondary'),
    ('in_progress', 'In Progress','primary'),
    ('done',        'Selesai',    'success'),
    ('cancelled',   'Dibatalkan', 'danger'),
]

@app.route('/support/presales')
@login_required
def sc_presales():
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db   = get_db()
    type_filter   = request.args.get('type', '')
    status_filter = request.args.get('status', '')
    q = '''SELECT r.*, cu.name as customer_name, cu.customer_type
           FROM sc_presales_requests r
           JOIN sc_customers cu ON cu.id=r.customer_id'''
    conds, params = [], []
    if type_filter:   conds.append('r.request_type=?'); params.append(type_filter)
    if status_filter: conds.append('r.status=?');       params.append(status_filter)
    if conds: q += ' WHERE ' + ' AND '.join(conds)
    q += ' ORDER BY r.id DESC'
    rows = db.execute(q, params).fetchall()
    # Ambil assignees per request
    assignees_map = {}
    for r in rows:
        assignees_map[r['id']] = db.execute(
            '''SELECT e.name, e.divisi FROM sc_presales_assignees pa
               JOIN employees e ON e.id=pa.employee_id WHERE pa.request_id=?''', (r['id'],)).fetchall()
    return render_template('sc_presales.html', rows=rows, assignees_map=assignees_map,
                           sc_presales_statuses=SC_PRESALES_STATUSES,
                           type_filter=type_filter, status_filter=status_filter)

@app.route('/support/presales/add', methods=['GET','POST'])
@login_required
def sc_presales_add():
    if not sc_require('sc_manage_presales'): return redirect(url_for('sc_presales'))
    db = get_db()
    calon_customers = db.execute(
        "SELECT id, name, code FROM sc_customers WHERE is_active=1 ORDER BY name").fetchall()
    employees = db.execute("SELECT id, name, jabatan, divisi FROM employees WHERE is_active=1 ORDER BY divisi, name").fetchall()
    if request.method == 'POST':
        customer_id  = request.form.get('customer_id')
        req_type     = request.form.get('request_type', 'presales')
        subject      = request.form.get('subject','').strip()
        description  = request.form.get('description','').strip()
        assignee_ids = request.form.getlist('assignee_ids')
        if not customer_id or not subject:
            flash('Customer dan subjek wajib diisi', 'danger')
        else:
            req_no = _sc_presales_no(db)
            try:
                cur = db.execute('''INSERT INTO sc_presales_requests
                                    (req_no,customer_id,request_type,subject,description,created_by)
                                    VALUES(?,?,?,?,?,?)''',
                                 (req_no, customer_id, req_type, subject, description, session.get('user_id')))
                req_id = cur.lastrowid
                db.commit()
                for eid in assignee_ids:
                    if eid:
                        emp = db.execute('SELECT divisi FROM employees WHERE id=?', (eid,)).fetchone()
                        db.execute('INSERT OR IGNORE INTO sc_presales_assignees(request_id,employee_id,divisi) VALUES(?,?,?)',
                                   (req_id, eid, emp['divisi'] if emp else ''))
                db.commit()
                db.execute('''INSERT INTO sc_presales_history(request_id,changed_by,changed_by_name,action,notes)
                              VALUES(?,?,?,?,?)''',
                           (req_id, session.get('user_id'), session.get('user_name',''),
                            'created', f'Request {req_type} dibuat: {subject}'))
                db.commit()
                flash(f'Request {req_no} berhasil dibuat', 'success')
                return redirect(url_for('sc_presales'))
            except Exception as e:
                flash(f'Error: {e}', 'danger')
    return render_template('sc_presales_form.html', row=None, sel_assignees=[],
                           calon_customers=calon_customers, employees=employees,
                           sc_presales_statuses=SC_PRESALES_STATUSES)

@app.route('/support/presales/<int:rid>/edit', methods=['GET','POST'])
@login_required
def sc_presales_edit(rid):
    if not sc_require('sc_manage_presales'): return redirect(url_for('sc_presales'))
    db  = get_db()
    row = db.execute('SELECT * FROM sc_presales_requests WHERE id=?', (rid,)).fetchone()
    if not row: abort(404)
    calon_customers = db.execute("SELECT id, name, code FROM sc_customers WHERE is_active=1 ORDER BY name").fetchall()
    employees = db.execute("SELECT id, name, jabatan, divisi FROM employees WHERE is_active=1 ORDER BY divisi, name").fetchall()
    sel_assignees = [r['employee_id'] for r in
                     db.execute('SELECT employee_id FROM sc_presales_assignees WHERE request_id=?', (rid,)).fetchall()]
    if request.method == 'POST':
        new_status   = request.form.get('status', row['status'])
        status_note  = request.form.get('status_note','').strip()
        subject      = request.form.get('subject','').strip()
        description  = request.form.get('description','').strip()
        assignee_ids = request.form.getlist('assignee_ids')
        old_status   = row['status']
        db.execute('UPDATE sc_presales_requests SET status=?,status_note=?,subject=?,description=? WHERE id=?',
                   (new_status, status_note, subject, description, rid))
        db.commit()
        # Sync assignees
        old_ids = set(sel_assignees)
        new_ids = set(int(i) for i in assignee_ids if i)
        for eid in new_ids - old_ids:
            emp = db.execute('SELECT divisi FROM employees WHERE id=?', (eid,)).fetchone()
            db.execute('INSERT OR IGNORE INTO sc_presales_assignees(request_id,employee_id,divisi) VALUES(?,?,?)',
                       (rid, eid, emp['divisi'] if emp else ''))
        for eid in old_ids - new_ids:
            db.execute('DELETE FROM sc_presales_assignees WHERE request_id=? AND employee_id=?', (rid, eid))
        db.commit()
        note_parts = []
        if old_status != new_status: note_parts.append(f'Status: {old_status} → {new_status}')
        if status_note: note_parts.append(status_note)
        db.execute('''INSERT INTO sc_presales_history(request_id,changed_by,changed_by_name,action,notes)
                      VALUES(?,?,?,?,?)''',
                   (rid, session.get('user_id'), session.get('user_name',''),
                    'status_change' if old_status != new_status else 'update',
                    '; '.join(note_parts) or 'Diperbarui'))
        db.commit()
        flash('Request diperbarui', 'success')
        return redirect(url_for('sc_presales_detail', rid=rid))
    return render_template('sc_presales_form.html', row=row, sel_assignees=sel_assignees,
                           calon_customers=calon_customers, employees=employees,
                           sc_presales_statuses=SC_PRESALES_STATUSES)

@app.route('/support/presales/<int:rid>')
@login_required
def sc_presales_detail(rid):
    if not sc_require('sc_view'): return redirect(url_for('sc_index'))
    db  = get_db()
    row = db.execute('''SELECT r.*, cu.name as customer_name, cu.customer_type
                        FROM sc_presales_requests r
                        JOIN sc_customers cu ON cu.id=r.customer_id
                        WHERE r.id=?''', (rid,)).fetchone()
    if not row: abort(404)
    history   = db.execute('SELECT * FROM sc_presales_history WHERE request_id=? ORDER BY id DESC', (rid,)).fetchall()
    assignees = db.execute('''SELECT e.name, e.jabatan, e.divisi, pa.role_note
                              FROM sc_presales_assignees pa
                              JOIN employees e ON e.id=pa.employee_id
                              WHERE pa.request_id=? ORDER BY e.divisi, e.name''', (rid,)).fetchall()
    return render_template('sc_presales_detail.html', row=row, history=history, assignees=assignees,
                           sc_presales_statuses=SC_PRESALES_STATUSES)

@app.route('/support/presales/<int:rid>/delete', methods=['POST'])
@login_required
def sc_presales_delete(rid):
    if not sc_require('sc_manage_presales'): return redirect(url_for('sc_presales'))
    db = get_db()
    db.execute('DELETE FROM sc_presales_requests WHERE id=?', (rid,))
    db.commit()
    flash('Request dihapus', 'warning')
    return redirect(url_for('sc_presales'))

# ─── SupportCore: Reports ──────────────────────────────────────────────────────

@app.route('/support/reports')
@login_required
def sc_reports():
    if not sc_require('sc_view_reports'): return redirect(url_for('sc_index'))
    db = get_db()
    from datetime import date as _date
    today       = _date.today()
    date_from   = request.args.get('date_from', today.replace(day=1).strftime('%Y-%m-%d'))
    date_to     = request.args.get('date_to',   today.strftime('%Y-%m-%d'))
    cust_f      = request.args.get('customer_id', '')
    creator_f   = request.args.get('created_by',  '')
    pic_f       = request.args.get('pic_id',      '')
    assignee_f  = request.args.get('assignee_id', '')

    q_base = '''FROM sc_tickets t
                JOIN sc_customers cu ON cu.id=t.customer_id
                JOIN sc_support_types st ON st.id=t.support_type_id
                LEFT JOIN sc_sla_categories sc ON sc.id=t.sla_category_id
                LEFT JOIN employees cr ON cr.id=t.created_by
                LEFT JOIN employees as_e ON as_e.id=t.assigned_to
                WHERE date(t.reported_at) BETWEEN ? AND ?'''
    params = [date_from, date_to]
    if cust_f:
        q_base += ' AND t.customer_id=?'; params.append(cust_f)
    if creator_f:
        q_base += ' AND t.created_by=?'; params.append(creator_f)
    if pic_f:
        # filter tiket yang customernya memiliki PIC = pic_f
        q_base += (' AND cu.id IN ('
                   'SELECT id FROM sc_customers WHERE '
                   'pic_helpdesk_id=? OR pic_helpdesk_backup_id=? OR '
                   'pic_implementor_id=? OR pic_coleader_id=? OR pic_sales_id=?)')
        params.extend([pic_f]*5)
    if assignee_f:
        q_base += (' AND (t.assigned_to=? OR t.id IN ('
                   'SELECT ticket_id FROM sc_ticket_assignees WHERE employee_id=?))')
        params.extend([assignee_f, assignee_f])

    tickets = db.execute(
        'SELECT t.*, cu.name as customer_name, st.name as type_name, '
        'sc.response_time_hours, sc.resolution_time_hours, '
        'cr.name as creator_name, as_e.name as assignee_name ' + q_base
        + ' ORDER BY t.reported_at', params).fetchall()

    # Monthly summary
    monthly = {}
    for t in tickets:
        mo = t['reported_at'][:7]
        if mo not in monthly:
            monthly[mo] = {'total':0,'open':0,'closed':0,'corrective':0,'preventive':0,'onsite':0}
        monthly[mo]['total'] += 1
        if t['status'] in ('open','in_progress'): monthly[mo]['open'] += 1
        else: monthly[mo]['closed'] += 1
        tn = (t['type_name'] or '').lower()
        if 'corrective' in tn: monthly[mo]['corrective'] += 1
        elif 'preventive' in tn: monthly[mo]['preventive'] += 1
        elif 'onsite' in tn: monthly[mo]['onsite'] += 1
    filter_year = date_from[:4]  # kept for template compat in card title

    # Avg response & resolution time
    resp_times, res_times = [], []
    for t in tickets:
        _, rh = calc_sla(t['reported_at'], t['responded_at'], 999)
        if rh is not None: resp_times.append(rh)
        _, reh = calc_sla(t['reported_at'], t['resolved_at'], 999)
        if reh is not None: res_times.append(reh)
    avg_resp = round(sum(resp_times)/len(resp_times), 1) if resp_times else None
    avg_res  = round(sum(res_times)/len(res_times), 1)  if res_times  else None

    # Top customers
    cust_count = {}
    for t in tickets:
        cn = t['customer_name']
        cust_count[cn] = cust_count.get(cn, 0) + 1
    top_customers = sorted(cust_count.items(), key=lambda x: -x[1])[:10]

    customers = db.execute('SELECT id, name FROM sc_customers WHERE is_active=1 ORDER BY name').fetchall()
    employees = db.execute('SELECT id, name, divisi FROM employees WHERE is_active=1 ORDER BY name').fetchall()

    return render_template('sc_reports.html', monthly=monthly, avg_resp=avg_resp,
                           avg_res=avg_res, top_customers=top_customers,
                           customers=customers, employees=employees,
                           filter_date_from=date_from, filter_date_to=date_to,
                           filter_year=filter_year,
                           filter_customer=cust_f, filter_creator=creator_f,
                           filter_pic=pic_f, filter_assignee=assignee_f,
                           total_tickets=len(tickets))

# ─── Settings ─────────────────────────────────────────────────────────────────

TALENTCORE_SETTINGS_KEYS = [
    'app_name', 'reminder_days', 'reminder_enabled',
    'notification_emails', 'notification_telegram_ids', 'openwa_extra_phones',
]

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    db = get_db()
    if not has_permission(session.get('user_role', ''), 'manage_settings', db):
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        for k in TALENTCORE_SETTINGS_KEYS:
            v = request.form.get(k, '').strip()
            if k == 'reminder_enabled':
                v = '1' if request.form.get('reminder_enabled') else '0'
            save_setting(db, k, v)
        db.commit()
        flash('Pengaturan TalentCore disimpan', 'success')
        return redirect(url_for('settings'))
    cfg = get_settings(db)
    return render_template('settings.html', cfg=cfg)

@app.route('/settings/test-email', methods=['POST'])
@superadmin_required
def test_email():
    db = get_db()
    settings_data = get_settings(db)
    to_email = request.form.get('test_email','').strip()
    if not to_email:
        return jsonify({'ok': False, 'msg': 'Masukkan alamat email tujuan test'})
    ok, err = send_email(settings_data, to_email,
                         'Test Email — TalentCore',
                         '<h3>Test berhasil!</h3><p>Konfigurasi email sudah benar.</p>')
    return jsonify({'ok': ok, 'msg': 'Email berhasil dikirim' if ok else str(err)})

@app.route('/settings/test-telegram', methods=['POST'])
@superadmin_required
def test_telegram():
    db = get_db()
    cfg = get_settings(db)
    bot_token = cfg.get('telegram_bot_token','').strip()
    chat_id   = request.form.get('test_chat_id','').strip() or cfg.get('telegram_default_chat_id','').strip()
    if not bot_token or not chat_id:
        return jsonify({'ok': False, 'msg': 'Bot token dan chat ID harus diisi'})
    ok, err = send_telegram(bot_token, chat_id,
                            '✅ <b>Test berhasil!</b>\n\nKonfigurasi Telegram sudah benar.')
    return jsonify({'ok': ok, 'msg': 'Pesan Telegram berhasil dikirim' if ok else str(err)})

@app.route('/settings/test-whatsapp', methods=['POST'])
@superadmin_required
def test_whatsapp():
    db  = get_db()
    cfg = get_settings(db)
    wa_url     = request.form.get('test_wa_url', '').strip() or cfg.get('openwa_url', '').strip()
    wa_key     = cfg.get('openwa_api_key', '').strip()
    wa_session = get_openwa_session(cfg, 'evaluasi')
    phone      = request.form.get('test_wa_phone', '').strip()
    if not wa_url or not phone:
        return jsonify({'ok': False, 'msg': 'URL OpenWA dan nomor HP harus diisi'})
    ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone,
                            '✅ *Test berhasil!*\n\nKonfigurasi OpenWA WhatsApp sudah terhubung dengan TalentCore.')
    chat_id = normalize_phone_wa(phone)
    return jsonify({'ok': ok, 'chat_id': chat_id,
                    'msg': f'Pesan terkirim ke {chat_id}' if ok else str(err)})

@app.route('/settings/run-reminders', methods=['POST'])
@login_required
def run_reminders_now():
    db = get_db()
    if not has_permission(session.get('user_role', ''), 'manage_settings', db):
        return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    sent, failed = run_contract_reminders(triggered_by=session.get('username','manual'))
    return jsonify({'sent': sent, 'failed': failed,
                    'msg': f'{sent} reminder dikirim, {failed} gagal'})

# ─── Evaluation Routes (unchanged logic, login_required added) ─────────────────

@app.route('/eval/new/<int:emp_id>', methods=['POST'])
@login_required
def eval_new(emp_id):
    db = get_db()
    periode = request.form.get('periode', str(date.today().year)).strip()
    cur = db.execute('INSERT INTO evaluations(employee_id, periode) VALUES(?,?)', (emp_id, periode))
    eval_id = cur.lastrowid
    for slot in range(1, 6):
        db.execute('INSERT INTO peer_reviews(eval_id, slot) VALUES(?,?)', (eval_id, slot))
    db.commit()

    emp = db.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
    if emp and (emp['email'] or emp['telegram_id']):
        results, any_ok, link = _send_self_assessment(
            db, eval_id, emp, periode,
            request.host_url.rstrip('/'),
            triggered_by=session.get('username', 'auto-new')
        )
        db.commit()
        if any_ok:
            flash(f'Evaluasi dibuat & link self-assessment dikirim — ' + ' | '.join(results), 'success')
        else:
            flash(f'Evaluasi dibuat. Gagal kirim notifikasi ({"; ".join(results)}). '
                  f'Kirim manual dari halaman Summary.', 'warning')
    else:
        flash('Evaluasi berhasil dibuat. Email/Telegram karyawan belum diisi — '
              'kirim link self-assessment manual dari halaman Summary.', 'info')

    return redirect(url_for('eval_project', eval_id=eval_id))

@app.route('/eval/<int:eval_id>/project', methods=['GET', 'POST'])
@login_required
def eval_project(eval_id):
    db = get_db()
    ev = get_eval_or_404(db, eval_id)
    if not ev:
        flash('Evaluasi tidak ditemukan', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        try:
            db.execute('DELETE FROM project_entries WHERE eval_id=?', (eval_id,))
            for t in ['history','top_task','improvement']:
                names    = request.form.getlist(f'{t}_project_name')
                tasks    = request.form.getlist(f'{t}_detail_task')
                statuses = request.form.getlist(f'{t}_status')
                notes_l  = request.form.getlist(f'{t}_notes')
                for i, name in enumerate(names):
                    if name.strip() or (i < len(tasks) and tasks[i].strip()):
                        db.execute('''INSERT INTO project_entries
                                      (eval_id,entry_type,project_name,detail_task,status,notes,sort_order)
                                      VALUES(?,?,?,?,?,?,?)''',
                                   (eval_id, t, name.strip(),
                                    tasks[i] if i < len(tasks) else '',
                                    statuses[i] if i < len(statuses) else 'DONE',
                                    notes_l[i] if i < len(notes_l) else '', i))
            pp_score = int(request.form.get('pp_score', 0))
            db.execute('UPDATE evaluations SET pp_score=? WHERE id=?', (pp_score, eval_id))
            db.commit()
        except Exception:
            db.execute('ROLLBACK')
            flash('Terjadi kesalahan saat menyimpan data project.', 'danger')
            return redirect(url_for('eval_project', eval_id=eval_id))
        recalc(db, eval_id)
        if request.form.get('action') == 'next':
            return redirect(url_for('eval_hardskill', eval_id=eval_id))
        flash('Project Performance disimpan', 'success')
        return redirect(url_for('eval_project', eval_id=eval_id))
    entries = {t: db.execute(
        'SELECT * FROM project_entries WHERE eval_id=? AND entry_type=? ORDER BY sort_order',
        (eval_id, t)).fetchall() for t in ['history','top_task','improvement']}
    return render_template('eval_project.html', ev=ev, entries=entries)

@app.route('/eval/<int:eval_id>/hardskill', methods=['GET', 'POST'])
@login_required
def eval_hardskill(eval_id):
    db = get_db()
    ev = get_eval_or_404(db, eval_id)
    if not ev: return redirect(url_for('index'))
    if request.method == 'POST':
        for key, val in request.form.items():
            if key.startswith('score_'):
                item_id = int(key.split('_')[1])
                score   = max(0, min(5, int(val or 0)))
                db.execute('''INSERT INTO skill_scores(eval_id,skill_item_id,score) VALUES(?,?,?)
                              ON CONFLICT(eval_id,skill_item_id) DO UPDATE SET score=excluded.score''',
                           (eval_id, item_id, score))
        db.commit(); recalc(db, eval_id)
        if request.form.get('action') == 'next':
            return redirect(url_for('eval_ability', eval_id=eval_id))
        flash('Hard Skill disimpan', 'success')
        return redirect(url_for('eval_hardskill', eval_id=eval_id))
    categories = db.execute('''SELECT sc.id, sc.name FROM skill_categories sc
                               WHERE sc.divisi=? ORDER BY sc.sort_order''', (ev['divisi'],)).fetchall()
    items_by_cat = {cat['id']: db.execute('''
        SELECT si.*, COALESCE(ss.score,0) AS current_score,
               ROUND((COALESCE(ss.score,0)/4.0)*si.bobot,2) AS final_value
        FROM skill_items si LEFT JOIN skill_scores ss ON ss.skill_item_id=si.id AND ss.eval_id=?
        WHERE si.category_id=? ORDER BY si.sort_order
    ''', (eval_id, cat['id'])).fetchall() for cat in categories}
    hs_total = db.execute('SELECT hs_total FROM evaluations WHERE id=?', (eval_id,)).fetchone()['hs_total']
    return render_template('eval_hardskill.html', ev=ev, categories=categories,
                           items_by_cat=items_by_cat, hs_total=hs_total)

@app.route('/eval/<int:eval_id>/ability', methods=['GET', 'POST'])
@login_required
def eval_ability(eval_id):
    db = get_db()
    ev = get_eval_or_404(db, eval_id)
    if not ev: return redirect(url_for('index'))
    if request.method == 'POST':
        for key, val in request.form.items():
            if key.startswith('level_'):
                item_id = int(key.split('_')[1])
                level   = val if val in ('A','B','C','D') else ''
                db.execute('''INSERT INTO ability_scores(eval_id,ability_item_id,level) VALUES(?,?,?)
                              ON CONFLICT(eval_id,ability_item_id) DO UPDATE SET level=excluded.level''',
                           (eval_id, item_id, level))
        db.commit(); recalc(db, eval_id)
        if request.form.get('action') == 'next':
            return redirect(url_for('eval_competency', eval_id=eval_id))
        flash('Ability disimpan', 'success')
        return redirect(url_for('eval_ability', eval_id=eval_id))
    items    = db.execute('''SELECT ai.*, COALESCE(abs.level,'') AS current_level
                             FROM ability_items ai
                             LEFT JOIN ability_scores abs ON abs.ability_item_id=ai.id AND abs.eval_id=?
                             WHERE ai.divisi=? ORDER BY ai.sort_order''',
                          (eval_id, ev['divisi'])).fetchall()
    ev_row   = db.execute('SELECT ability_score FROM evaluations WHERE id=?', (eval_id,)).fetchone()
    return render_template('eval_ability.html', ev=ev, items=items,
                           ability_score=ev_row['ability_score'])

@app.route('/eval/<int:eval_id>/competency', methods=['GET', 'POST'])
@login_required
def eval_competency(eval_id):
    db = get_db()
    ev = get_eval_or_404(db, eval_id)
    if not ev: return redirect(url_for('index'))
    if request.method == 'POST':
        for key, val in request.form.items():
            if key.startswith('rating_'):
                item_id = int(key.split('_')[1])
                rating  = max(0, min(5, int(val or 0)))
                db.execute('''INSERT INTO competency_scores(eval_id,competency_item_id,rating) VALUES(?,?,?)
                              ON CONFLICT(eval_id,competency_item_id) DO UPDATE SET rating=excluded.rating''',
                           (eval_id, item_id, rating))
        db.commit(); recalc(db, eval_id)
        if request.form.get('action') == 'next':
            return redirect(url_for('eval_summary', eval_id=eval_id))
        flash('Competency disimpan', 'success')
        return redirect(url_for('eval_competency', eval_id=eval_id))
    ev_row   = db.execute('SELECT hs_total, competency_total FROM evaluations WHERE id=?', (eval_id,)).fetchone()
    hs_total = ev_row['hs_total']
    items    = db.execute('''SELECT ci.*, COALESCE(cs.rating,0) AS current_rating
                             FROM competency_items ci
                             LEFT JOIN competency_scores cs ON cs.competency_item_id=ci.id AND cs.eval_id=?
                             WHERE ci.divisi=? ORDER BY ci.sort_order''',
                          (eval_id, ev['divisi'])).fetchall()
    items_with_val = [{'item': i, 'val': round(hs_total*i['bobot'],2) if i['is_hardskill']
                       else round(i['current_rating']*20.0*i['bobot'],2)} for i in items]
    return render_template('eval_competency.html', ev=ev, items_with_val=items_with_val,
                           hs_total=hs_total, competency_total=ev_row['competency_total'])

@app.route('/eval/<int:eval_id>/summary', methods=['GET', 'POST'])
@login_required
def eval_summary(eval_id):
    db = get_db()
    ev = get_eval_or_404(db, eval_id)
    if not ev: return redirect(url_for('index'))
    if request.method == 'POST':
        evaluator = request.form.get('evaluator','').strip()
        overall   = request.form.get('overall_assessment','').strip()
        plan      = request.form.get('development_plan','').strip()
        action    = request.form.get('action','save')
        status    = 'final' if action == 'finalize' else 'draft'
        db.execute('''UPDATE evaluations SET evaluator=?,overall_assessment=?,
                      development_plan=?,status=? WHERE id=?''',
                   (evaluator, overall, plan, status, eval_id))
        for slot in range(1, 6):
            db.execute('UPDATE peer_reviews SET reviewer_name=?,feedback=? WHERE eval_id=? AND slot=?',
                       (request.form.get(f'peer_name_{slot}','').strip(),
                        request.form.get(f'peer_feedback_{slot}','').strip(),
                        eval_id, slot))
        db.commit()
        if action == 'finalize':
            flash('Evaluasi berhasil difinalkan!', 'success')
            return redirect(url_for('index'))
        flash('Summary disimpan', 'success')
        return redirect(url_for('eval_summary', eval_id=eval_id))
    recalc(db, eval_id)
    ev    = get_eval_or_404(db, eval_id)
    peers = db.execute('SELECT * FROM peer_reviews WHERE eval_id=? ORDER BY slot', (eval_id,)).fetchall()
    entries = {t: db.execute(
        'SELECT * FROM project_entries WHERE eval_id=? AND entry_type=? ORDER BY sort_order',
        (eval_id, t)).fetchall() for t in ['history','top_task','improvement']}
    competency_items = db.execute('''SELECT ci.*, COALESCE(cs.rating,0) AS current_rating
                                     FROM competency_items ci
                                     LEFT JOIN competency_scores cs ON cs.competency_item_id=ci.id AND cs.eval_id=?
                                     WHERE ci.divisi=? ORDER BY ci.sort_order''',
                                  (eval_id, ev['divisi'])).fetchall()
    emp = db.execute('''SELECT e.*,
        e1.name AS sup_name, e2.name AS lead_name, e3.name AS mgr_name
        FROM employees e
        LEFT JOIN employees e1 ON e1.id = e.supervisor_id
        LEFT JOIN employees e2 ON e2.id = e.leader_id
        LEFT JOIN employees e3 ON e3.id = e.manager_id
        WHERE e.id=?''', (ev['employee_id'],)).fetchone()
    eval_token = db.execute('SELECT * FROM eval_tokens WHERE eval_id=?', (eval_id,)).fetchone()
    all_reviews = db.execute('''SELECT er.*, u.full_name, u.username
        FROM eval_reviews er JOIN users u ON u.id = er.reviewer_user_id
        WHERE er.eval_id=? ORDER BY er.submitted_at''', (eval_id,)).fetchall()
    base_url = request.host_url.rstrip('/')
    self_link = f"{base_url}/assess/{eval_token['token']}" if eval_token else None
    return render_template('eval_summary.html', ev=ev, peers=peers, entries=entries,
                           competency_items=competency_items, emp=emp,
                           eval_token=eval_token, self_link=self_link,
                           all_reviews=all_reviews)

@app.route('/eval/<int:eval_id>/delete', methods=['POST'])
@superadmin_required
def eval_delete(eval_id):
    db = get_db()
    db.execute('DELETE FROM evaluations WHERE id=?', (eval_id,))
    db.commit()
    flash('Evaluasi dihapus', 'warning')
    return redirect(url_for('index'))

# ─── Self-Assessment & Review Routes ──────────────────────────────────────────

def _send_self_assessment(db, eval_id, emp, periode, base_url, triggered_by='auto'):
    """Kirim link self-assessment via email dan/atau Telegram. Return (results, any_ok, link)."""
    token    = get_or_create_eval_token(db, eval_id)
    link     = f'{base_url}/assess/{token}'
    settings = get_settings(db)
    now_str  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    results  = []

    if emp['email']:
        subject   = f'TalentCore Self-Assessment {emp["name"]} — Periode {periode}'
        html_body = f"""
<h3 style="color:#1e2a3a">TalentCore Self-Assessment</h3>
<p>Yth. <strong>{emp['name']}</strong>,</p>
<p>Anda diminta mengisi <b>penilaian diri (self-assessment)</b> untuk TalentCore
   periode <strong>{periode}</strong>.</p>
<p>Silakan klik tombol di bawah untuk mengisi form:</p>
<p style="margin:20px 0">
  <a href="{link}" style="background:#4da8da;color:#fff;padding:12px 24px;border-radius:6px;
     text-decoration:none;font-weight:bold">&#128221; Isi Self-Assessment</a>
</p>
<p style="color:#666;font-size:.9em">Atau buka link: <a href="{link}">{link}</a></p>
<hr>
<p style="color:#999;font-size:.85em">
  Mohon segera mengisi sebelum batas waktu. Hubungi tim HR/evaluator jika ada pertanyaan.
</p>"""
        ok, err = send_email(settings, emp['email'], subject, html_body)
        log_reminder(db, emp['id'], 'email', subject, html_body, ok, err, triggered_by)
        if ok:
            db.execute('''INSERT INTO eval_tokens(eval_id, token, email_sent_to, sent_at)
                          VALUES(?,?,?,?)
                          ON CONFLICT(eval_id) DO UPDATE SET token=excluded.token,email_sent_to=excluded.email_sent_to,sent_at=excluded.sent_at''',
                       (eval_id, token, emp['email'], now_str))
            results.append(f'Email {emp["email"]} ✓')
        else:
            results.append(f'Email gagal: {err}')

    bot_token = settings.get('telegram_bot_token', '').strip()
    tg_id     = normalize_telegram_id(emp['telegram_id'] or '')
    if bot_token and tg_id:
        tg_msg = (
            f"📝 <b>TalentCore Self-Assessment</b>\n\n"
            f"Yth. <b>{emp['name']}</b>,\n\n"
            f"Anda diminta mengisi penilaian diri (self-assessment) untuk TalentCore "
            f"periode <b>{periode}</b>.\n\n"
            f"Silakan buka link berikut:\n<a href=\"{link}\">{link}</a>\n\n"
            f"<i>Mohon segera mengisi sebelum batas waktu.</i>"
        )
        ok, err = send_telegram(bot_token, tg_id, tg_msg)
        log_reminder(db, emp['id'], 'telegram', f'Self-Assessment {emp["name"]}', tg_msg, ok, err, triggered_by)
        if ok:
            results.append(f'Telegram {tg_id} ✓')
        else:
            results.append(f'Telegram {tg_id} gagal: {err}')
    elif emp['telegram_id'] and not bot_token:
        results.append('Telegram: bot token belum dikonfigurasi')

    # ── WhatsApp (OpenWA) ──
    wa_url     = settings.get('openwa_url', '').strip()
    wa_key     = settings.get('openwa_api_key', '').strip()
    wa_session = get_openwa_session(settings, 'evaluasi')
    wa_enabled = settings.get('openwa_enabled', '0') == '1'
    emp_phone  = emp['phone'] or ''
    if wa_enabled and wa_url and emp_phone:
        wa_chat_id = normalize_phone_wa(emp_phone)
        wa_msg = (
            f"📝 *TalentCore Self-Assessment*\n\n"
            f"Yth. *{emp['name']}*,\n\n"
            f"Anda diminta mengisi penilaian diri (self-assessment) untuk TalentCore "
            f"periode *{periode}*.\n\n"
            f"Silakan buka link berikut:\n{link}\n\n"
            f"_Mohon segera mengisi sebelum batas waktu._"
        )
        ok, err = send_whatsapp(wa_url, wa_key, wa_session, emp_phone, wa_msg)
        log_reminder(db, emp['id'], 'whatsapp', f'Self-Assessment {emp["name"]}', wa_msg, ok, err, triggered_by)
        if ok:
            results.append(f'WhatsApp {wa_chat_id} ✓')
        else:
            results.append(f'WhatsApp gagal: {err}')
    elif wa_enabled and wa_url and not emp_phone:
        results.append('WhatsApp: nomor HP karyawan belum diisi')

    any_ok = any('✓' in r for r in results)
    if any_ok:
        db.execute("UPDATE evaluations SET review_status='pending_self' WHERE id=? AND review_status='draft'",
                   (eval_id,))
    return results, any_ok, link


@app.route('/eval/<int:eval_id>/send-self-link', methods=['POST'])
@login_required
def eval_send_self_link(eval_id):
    db = get_db()
    ev = get_eval_or_404(db, eval_id)
    if not ev:
        flash('Evaluasi tidak ditemukan', 'danger')
        return redirect(url_for('index'))
    emp = db.execute('SELECT * FROM employees WHERE id=?', (ev['employee_id'],)).fetchone()
    if not emp or (not emp['email'] and not emp['telegram_id']):
        flash('Email dan Telegram karyawan belum diisi. Lengkapi data karyawan terlebih dahulu.', 'warning')
        return redirect(url_for('eval_summary', eval_id=eval_id))

    results, any_ok, link = _send_self_assessment(
        db, eval_id, emp, ev['periode'],
        request.host_url.rstrip('/'),
        triggered_by=session.get('username', 'manual')
    )
    db.commit()
    if any_ok:
        flash('Self-assessment dikirim — ' + ' | '.join(results), 'success')
    else:
        flash('Gagal mengirim — ' + ' | '.join(results) + f'. Link manual: {link}', 'warning')
    return redirect(url_for('eval_summary', eval_id=eval_id))


@app.route('/assess/<token>', methods=['GET', 'POST'])
def self_assess(token):
    """Public — no login required. Employee fills self-assessment via token link."""
    db = _get_raw_db()
    try:
        tok = db.execute("SELECT * FROM eval_tokens WHERE token=?", (token,)).fetchone()
        if not tok:
            return render_template('eval_self.html', error='Link tidak valid atau sudah kadaluarsa.')

        ev_row = db.execute('''SELECT e.*, emp.name AS emp_name, emp.jabatan, emp.divisi, emp.id AS emp_id
                               FROM evaluations e JOIN employees emp ON emp.id=e.employee_id
                               WHERE e.id=?''', (tok['eval_id'],)).fetchone()
        if not ev_row:
            return render_template('eval_self.html', error='Data evaluasi tidak ditemukan.')

        if tok['status'] == 'submitted':
            return render_template('eval_self.html', ev=ev_row, already_submitted=True)

        if request.method == 'POST':
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            db.execute('''UPDATE evaluations SET self_achievements=?, self_notes=?,
                          self_improvements=?,
                          review_status=CASE WHEN review_status='pending_self' THEN 'self_filled'
                                             ELSE review_status END
                          WHERE id=?''', (
                request.form.get('self_achievements','').strip(),
                request.form.get('self_notes','').strip(),
                request.form.get('self_improvements','').strip(),
                tok['eval_id']
            ))
            db.execute("UPDATE eval_tokens SET status='submitted', submitted_at=? WHERE id=?",
                       (now_str, tok['id']))
            # Create pending review rows for assigned supervisors (employee hierarchy → user)
            emp = db.execute('SELECT * FROM employees WHERE id=?', (ev_row['emp_id'],)).fetchone()
            for emp_ref_id, role in [
                (emp['supervisor_id'], 'Atasan Langsung'),
                (emp['leader_id'],     'Leader'),
                (emp['manager_id'],    'Manager'),
            ]:
                if emp_ref_id:
                    rev_emp = db.execute('SELECT user_id FROM employees WHERE id=?', (emp_ref_id,)).fetchone()
                    if rev_emp and rev_emp['user_id']:
                        db.execute('''INSERT OR IGNORE INTO eval_reviews(eval_id,reviewer_user_id,reviewer_role)
                                      VALUES(?,?,?)''', (tok['eval_id'], rev_emp['user_id'], role))
            db.commit()
            return render_template('eval_self.html', ev=ev_row, submitted=True)

        if not tok['accessed_at']:
            db.execute('UPDATE eval_tokens SET accessed_at=? WHERE id=?',
                       (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), tok['id']))
            db.commit()
        return render_template('eval_self.html', ev=ev_row, token=token)
    finally:
        db.close()


@app.route('/reviews')
@login_required
def reviews():
    db  = get_db()
    uid = session['user_id']
    role = session.get('user_role', '')
    if role == 'superadmin':
        evals = db.execute('''
            SELECT e.*, emp.name AS emp_name, emp.jabatan, emp.divisi,
                   emp.supervisor_id, emp.leader_id, emp.manager_id,
                   u.full_name AS reviewed_by_name
            FROM evaluations e
            JOIN employees emp ON emp.id = e.employee_id
            LEFT JOIN users u ON u.id = e.reviewed_by
            WHERE e.review_status != 'draft'
            ORDER BY CASE e.review_status WHEN 'self_filled' THEN 0 WHEN 'pending_self' THEN 1 ELSE 2 END,
                     e.id DESC
        ''').fetchall()
    else:
        my_emp_id = get_user_emp_id(db, uid) or -1
        evals = db.execute('''
            SELECT e.*, emp.name AS emp_name, emp.jabatan, emp.divisi,
                   emp.supervisor_id, emp.leader_id, emp.manager_id,
                   u.full_name AS reviewed_by_name
            FROM evaluations e
            JOIN employees emp ON emp.id = e.employee_id
            LEFT JOIN users u ON u.id = e.reviewed_by
            WHERE e.review_status != 'draft'
            AND (emp.supervisor_id=? OR emp.leader_id=? OR emp.manager_id=?)
            ORDER BY CASE e.review_status WHEN 'self_filled' THEN 0 WHEN 'pending_self' THEN 1 ELSE 2 END,
                     e.id DESC
        ''', (my_emp_id, my_emp_id, my_emp_id)).fetchall()
    my_emp_id = get_user_emp_id(db, uid) if role != 'superadmin' else None
    # mark own pending_review rows for each eval
    my_reviews = {r['eval_id']: r for r in db.execute(
        'SELECT * FROM eval_reviews WHERE reviewer_user_id=?', (uid,)).fetchall()}
    return render_template('reviews.html', evals=evals, my_reviews=my_reviews, my_emp_id=my_emp_id)


@app.route('/eval/<int:eval_id>/review', methods=['GET', 'POST'])
@login_required
def eval_review(eval_id):
    db  = get_db()
    uid = session['user_id']
    if not can_review_eval(db, uid, session.get('user_role',''), eval_id):
        flash('Anda tidak memiliki akses mereview evaluasi ini', 'danger')
        return redirect(url_for('index'))

    ev  = get_eval_or_404(db, eval_id)
    emp = db.execute('''SELECT e.*,
        e1.name AS sup_name, e2.name AS lead_name, e3.name AS mgr_name
        FROM employees e
        LEFT JOIN employees e1 ON e1.id=e.supervisor_id
        LEFT JOIN employees e2 ON e2.id=e.leader_id
        LEFT JOIN employees e3 ON e3.id=e.manager_id
        WHERE e.id=?''', (ev['employee_id'],)).fetchone()

    my_emp_id = get_user_emp_id(db, uid)
    def _reviewer_role():
        if my_emp_id and my_emp_id == emp['supervisor_id']: return 'Atasan Langsung'
        if my_emp_id and my_emp_id == emp['leader_id']:     return 'Leader'
        if my_emp_id and my_emp_id == emp['manager_id']:    return 'Manager'
        if session.get('user_role') == 'superadmin':        return 'Superadmin'
        return ''

    if request.method == 'POST':
        action = request.form.get('action', 'save_review')
        notes  = request.form.get('review_notes','').strip()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        reviewer_role = _reviewer_role()

        db.execute('''INSERT INTO eval_reviews
                      (eval_id, reviewer_user_id, reviewer_role, notes, status, submitted_at)
                      VALUES(?,?,?,?,'submitted',?)
                      ON CONFLICT(eval_id,reviewer_user_id) DO UPDATE SET reviewer_role=excluded.reviewer_role,notes=excluded.notes,status=excluded.status,submitted_at=excluded.submitted_at''',
                   (eval_id, uid, reviewer_role, notes, now_str))

        if action == 'approve':
            db.execute('''UPDATE evaluations SET review_status='approved',
                          reviewed_by=?, reviewed_at=?, review_notes=? WHERE id=?''',
                       (uid, now_str, notes, eval_id))
            db.commit()
            flash('Evaluasi disetujui', 'success')
            return redirect(url_for('reviews'))
        elif action == 'reject':
            db.execute('''UPDATE evaluations SET review_status='rejected',
                          reviewed_by=?, reviewed_at=?, review_notes=? WHERE id=?''',
                       (uid, now_str, notes, eval_id))
            db.commit()
            flash('Evaluasi dikembalikan untuk perbaikan', 'warning')
            return redirect(url_for('reviews'))
        else:
            db.commit()
            flash('Catatan review disimpan', 'success')
            return redirect(url_for('eval_review', eval_id=eval_id))

    my_review = db.execute('SELECT * FROM eval_reviews WHERE eval_id=? AND reviewer_user_id=?',
                            (eval_id, uid)).fetchone()
    all_reviews = db.execute('''SELECT er.*, u.full_name, u.username
        FROM eval_reviews er JOIN users u ON u.id=er.reviewer_user_id
        WHERE er.eval_id=? ORDER BY er.submitted_at''', (eval_id,)).fetchall()
    competency_items = db.execute('''SELECT ci.*, COALESCE(cs.rating,0) AS current_rating
        FROM competency_items ci
        LEFT JOIN competency_scores cs ON cs.competency_item_id=ci.id AND cs.eval_id=?
        WHERE ci.divisi=? ORDER BY ci.sort_order''', (eval_id, ev['divisi'])).fetchall()

    reviewer_role = _reviewer_role()

    return render_template('eval_review.html', ev=ev, emp=emp,
                           my_review=my_review, all_reviews=all_reviews,
                           competency_items=competency_items,
                           reviewer_role=reviewer_role)


@app.route('/api/eval/<int:eval_id>/score')
@login_required
def api_score(eval_id):
    db = get_db()
    recalc(db, eval_id)
    row = db.execute('SELECT pp_total,hs_total,ability_score,competency_total,final_total FROM evaluations WHERE id=?',
                     (eval_id,)).fetchone()
    return (dict(row) if row else {'error':'not found'})

# ─── Admin Routes ──────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin():
    db = get_db()
    data = {}
    divisi_list = get_divisi_list(db)
    for d in divisi_list:
        cats = db.execute('''SELECT sc.id, sc.name, COUNT(si.id) AS item_count
                             FROM skill_categories sc LEFT JOIN skill_items si ON si.category_id=sc.id
                             WHERE sc.divisi=? GROUP BY sc.id ORDER BY sc.sort_order''', (d,)).fetchall()
        comp = db.execute('SELECT COUNT(*) AS c FROM competency_items WHERE divisi=?', (d,)).fetchone()['c']
        abl  = db.execute('SELECT COUNT(*) AS c FROM ability_items WHERE divisi=?', (d,)).fetchone()['c']
        data[d] = {'cats': cats, 'comp_count': comp, 'ability_count': abl}
    return render_template('admin.html', data=data)

@app.route('/admin/divisi/<path:divisi>')
@login_required
def admin_divisi(divisi):
    db = get_db()
    cats = db.execute('SELECT * FROM skill_categories WHERE divisi=? ORDER BY sort_order', (divisi,)).fetchall()
    items_by_cat = {cat['id']: db.execute(
        'SELECT * FROM skill_items WHERE category_id=? ORDER BY sort_order', (cat['id'],)
    ).fetchall() for cat in cats}
    comp_items    = db.execute('SELECT * FROM competency_items WHERE divisi=? ORDER BY sort_order', (divisi,)).fetchall()
    ability_items = db.execute('SELECT * FROM ability_items WHERE divisi=? ORDER BY sort_order', (divisi,)).fetchall()
    return render_template('admin_divisi.html', divisi=divisi, cats=cats,
                           items_by_cat=items_by_cat, comp_items=comp_items,
                           ability_items=ability_items)

# ─── Division Management ──────────────────────────────────────────────────────

@app.route('/admin/divisions', methods=['GET', 'POST'])
@superadmin_required
def admin_divisions():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            name = request.form.get('name', '').strip()
            desc = request.form.get('description', '').strip()
            if name:
                try:
                    max_order = db.execute('SELECT COALESCE(MAX(sort_order),0) FROM divisions').fetchone()[0]
                    db.execute('INSERT INTO divisions(name, description, sort_order) VALUES(?,?,?)',
                               (name, desc, max_order + 1))
                    db.commit()
                    flash(f'Divisi "{name}" ditambahkan', 'success')
                except Exception:
                    flash('Nama divisi sudah ada', 'danger')
        elif action == 'edit':
            div_id = request.form.get('id')
            name   = request.form.get('name', '').strip()
            desc   = request.form.get('description', '').strip()
            sort_o = request.form.get('sort_order', '0')
            is_act = 1 if request.form.get('is_active') else 0
            if div_id and name:
                db.execute('UPDATE divisions SET name=?,description=?,sort_order=?,is_active=? WHERE id=?',
                           (name, desc, int(sort_o), is_act, int(div_id)))
                db.commit()
                flash('Divisi diperbarui', 'success')
        elif action == 'delete':
            div_id = request.form.get('id')
            if div_id:
                emp_count = db.execute('SELECT COUNT(*) FROM employees WHERE divisi=(SELECT name FROM divisions WHERE id=?) AND is_active=1',
                                       (int(div_id),)).fetchone()[0]
                if emp_count > 0:
                    flash(f'Tidak bisa hapus divisi — masih ada {emp_count} karyawan aktif', 'danger')
                else:
                    db.execute('DELETE FROM divisions WHERE id=?', (int(div_id),))
                    db.commit()
                    flash('Divisi dihapus', 'warning')
        return redirect(url_for('admin_divisions'))
    divisions = db.execute('SELECT * FROM divisions ORDER BY sort_order, name').fetchall()
    emp_counts = {r['divisi']: r['c'] for r in db.execute(
        'SELECT divisi, COUNT(*) AS c FROM employees WHERE is_active=1 GROUP BY divisi').fetchall()}
    return render_template('admin_divisions.html', divisions=divisions, emp_counts=emp_counts)


# ─── Promote Employee to App User ─────────────────────────────────────────────

@app.route('/emp/<int:emp_id>/promote-role', methods=['POST'])
@superadmin_required
def emp_promote_role(emp_id):
    db  = get_db()
    emp = db.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
    if not emp:
        flash('Karyawan tidak ditemukan', 'danger')
        return redirect(url_for('karyawan'))

    # role portal: hanya set jika access_portal dicentang, default viewer
    role = request.form.get('role', 'viewer') if request.form.get('access_portal') else 'viewer'
    apps = db.execute('SELECT slug, name FROM superapp_apps WHERE is_active=1').fetchall()

    def _save_app_access(uid):
        db.execute('DELETE FROM user_app_access WHERE user_id=?', (uid,))
        for a in apps:
            if request.form.get(f'access_{a["slug"]}'):
                app_role = request.form.get(f'role_{a["slug"]}', 'admin')
                db.execute('INSERT INTO user_app_access(user_id,app_slug,app_role,is_active) VALUES(?,?,?,1)',
                           (uid, a['slug'], app_role))

    if emp['user_id']:
        db.execute('UPDATE users SET role=? WHERE id=?', (role, emp['user_id']))
        _save_app_access(emp['user_id'])
        db.commit()
        flash(f'Role & akses {emp["name"]} diperbarui', 'success')
    else:
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username:
            flash('Username diperlukan', 'danger')
            return redirect(url_for('karyawan'))
        existing = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if existing:
            db.execute('UPDATE employees SET user_id=? WHERE id=?', (existing['id'], emp_id))
            db.execute('UPDATE users SET role=?, full_name=?, is_active=1 WHERE id=?',
                       (role, emp['name'], existing['id']))
            _save_app_access(existing['id'])
            db.commit()
            flash(f'{emp["name"]} dihubungkan ke akun "{username}"', 'success')
        else:
            if not password:
                flash('Password diperlukan untuk akun baru', 'danger')
                return redirect(url_for('karyawan'))
            new_uid = db.execute(
                'INSERT INTO users(username,password_hash,full_name,role) VALUES(?,?,?,?)',
                (username, generate_password_hash(password), emp['name'], role)
            ).lastrowid
            db.execute('UPDATE employees SET user_id=? WHERE id=?', (new_uid, emp_id))
            _save_app_access(new_uid)
            db.commit()
            flash(f'Akun "{username}" dibuat untuk {emp["name"]}', 'success')
    return redirect(url_for('karyawan'))


# ─── Role Management ──────────────────────────────────────────────────────────

@app.route('/admin/roles', methods=['GET', 'POST'])
@permission_required('manage_roles')
def admin_roles():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            name = request.form.get('name', '').strip().lower().replace(' ', '_')
            desc = request.form.get('description', '').strip()
            if name:
                try:
                    db.execute('INSERT INTO roles(name,description,is_system,app_slug) VALUES(?,?,0,?)', (name, desc, 'evaluasi'))
                    db.commit()
                    flash(f'Role "{name}" ditambahkan', 'success')
                except Exception:
                    flash('Nama role sudah ada', 'danger')
        elif action == 'delete':
            rname = request.form.get('role_name', '')
            is_sys = db.execute('SELECT is_system FROM roles WHERE name=?', (rname,)).fetchone()
            if is_sys and is_sys['is_system']:
                flash('Role sistem tidak bisa dihapus', 'danger')
            else:
                db.execute('DELETE FROM roles WHERE name=?', (rname,))
                db.execute('DELETE FROM role_permissions WHERE role_name=?', (rname,))
                db.commit()
                flash(f'Role "{rname}" dihapus', 'warning')
        return redirect(url_for('admin_roles'))
    roles = db.execute("SELECT * FROM roles WHERE app_slug='evaluasi' OR app_slug='' ORDER BY is_system DESC, name").fetchall()
    perms_by_role = {}
    for r in roles:
        perms_by_role[r['name']] = get_role_permissions(db, r['name'])
    return render_template('admin_roles.html', roles=roles, perms_by_role=perms_by_role,
                           all_permissions=ALL_PERMISSIONS)

@app.route('/admin/roles/<role_name>/permissions', methods=['POST'])
@permission_required('manage_roles')
def admin_role_perms(role_name):
    db = get_db()
    selected = set(request.form.getlist('permissions'))
    db.execute('DELETE FROM role_permissions WHERE role_name=?', (role_name,))
    for perm in selected:
        if perm in ALL_PERMISSIONS:
            db.execute('INSERT OR IGNORE INTO role_permissions(role_name,permission) VALUES(?,?)',
                       (role_name, perm))
    db.commit()
    flash(f'Permission role "{role_name}" diperbarui', 'success')
    return redirect(url_for('admin_roles'))

# ─── Template Editor ──────────────────────────────────────────────────────────

@app.route('/admin/template/<path:divisi>', methods=['POST'])
@permission_required('manage_template')
def admin_template_edit(divisi):
    db     = get_db()
    action = request.form.get('action')

    if action == 'add_category':
        name  = request.form.get('name', '').strip()
        if name:
            max_o = db.execute('SELECT COALESCE(MAX(sort_order),0) FROM skill_categories WHERE divisi=?', (divisi,)).fetchone()[0]
            db.execute('INSERT INTO skill_categories(divisi,name,sort_order) VALUES(?,?,?)', (divisi, name, max_o+1))
            db.commit()
            flash(f'Kategori "{name}" ditambahkan', 'success')

    elif action == 'edit_category':
        cid  = request.form.get('id')
        name = request.form.get('name', '').strip()
        if cid and name:
            db.execute('UPDATE skill_categories SET name=? WHERE id=? AND divisi=?', (name, cid, divisi))
            db.commit()
            flash('Kategori diperbarui', 'success')

    elif action == 'delete_category':
        cid = request.form.get('id')
        if cid:
            db.execute('DELETE FROM skill_categories WHERE id=? AND divisi=?', (cid, divisi))
            db.commit()
            flash('Kategori dihapus', 'warning')

    elif action == 'add_skill':
        cat_id = request.form.get('category_id')
        name   = request.form.get('name', '').strip()
        desc   = request.form.get('description', '').strip()
        bobot  = request.form.get('bobot', '1')
        if cat_id and name:
            max_o = db.execute('SELECT COALESCE(MAX(sort_order),0) FROM skill_items WHERE category_id=?', (cat_id,)).fetchone()[0]
            db.execute('INSERT INTO skill_items(category_id,name,description,bobot,sort_order) VALUES(?,?,?,?,?)',
                       (cat_id, name, desc, float(bobot), max_o+1))
            db.commit()
            flash(f'Skill "{name}" ditambahkan', 'success')

    elif action == 'edit_skill':
        sid   = request.form.get('id')
        name  = request.form.get('name', '').strip()
        desc  = request.form.get('description', '').strip()
        bobot = request.form.get('bobot', '1')
        if sid and name:
            db.execute('UPDATE skill_items SET name=?,description=?,bobot=? WHERE id=?',
                       (name, desc, float(bobot), sid))
            db.commit()
            flash('Skill diperbarui', 'success')

    elif action == 'delete_skill':
        sid = request.form.get('id')
        if sid:
            db.execute('DELETE FROM skill_items WHERE id=?', (sid,))
            db.commit()
            flash('Skill dihapus', 'warning')

    elif action == 'add_competency':
        measurement = request.form.get('point_measurement', '').strip()
        bobot       = request.form.get('bobot', '0')
        is_hs       = 1 if request.form.get('is_hardskill') else 0
        if measurement:
            max_o = db.execute('SELECT COALESCE(MAX(sort_order),0) FROM competency_items WHERE divisi=?', (divisi,)).fetchone()[0]
            db.execute('INSERT INTO competency_items(divisi,point_measurement,bobot,sort_order,is_hardskill) VALUES(?,?,?,?,?)',
                       (divisi, measurement, float(bobot), max_o+1, is_hs))
            db.commit()
            flash('Kompetensi ditambahkan', 'success')

    elif action == 'edit_competency':
        cid  = request.form.get('id')
        meas = request.form.get('point_measurement', '').strip()
        bob  = request.form.get('bobot', '0')
        is_hs = 1 if request.form.get('is_hardskill') else 0
        if cid and meas:
            db.execute('UPDATE competency_items SET point_measurement=?,bobot=?,is_hardskill=? WHERE id=?',
                       (meas, float(bob), is_hs, cid))
            db.commit()
            flash('Kompetensi diperbarui', 'success')

    elif action == 'delete_competency':
        cid = request.form.get('id')
        if cid:
            db.execute('DELETE FROM competency_items WHERE id=?', (cid,))
            db.commit()
            flash('Kompetensi dihapus', 'warning')

    elif action == 'add_ability':
        name = request.form.get('name','').strip()
        if name:
            max_o = db.execute('SELECT COALESCE(MAX(sort_order),0) FROM ability_items WHERE divisi=?', (divisi,)).fetchone()[0]
            db.execute('INSERT INTO ability_items(divisi,name,desc_a,desc_b,desc_c,desc_d,sort_order) VALUES(?,?,?,?,?,?,?)',
                       (divisi, name, request.form.get('desc_a',''), request.form.get('desc_b',''),
                        request.form.get('desc_c',''), request.form.get('desc_d',''), max_o+1))
            db.commit()
            flash('Ability ditambahkan', 'success')

    elif action == 'edit_ability':
        aid = request.form.get('id')
        if aid:
            db.execute('UPDATE ability_items SET name=?,desc_a=?,desc_b=?,desc_c=?,desc_d=? WHERE id=?',
                       (request.form.get('name',''), request.form.get('desc_a',''), request.form.get('desc_b',''),
                        request.form.get('desc_c',''), request.form.get('desc_d',''), aid))
            db.commit()
            flash('Ability diperbarui', 'success')

    elif action == 'delete_ability':
        aid = request.form.get('id')
        if aid:
            db.execute('DELETE FROM ability_items WHERE id=?', (aid,))
            db.commit()
            flash('Ability dihapus', 'warning')

    return redirect(url_for('admin_divisi', divisi=divisi) + '#' + request.form.get('tab','tab-skill'))

# ─── Salary (MFA protected) ───────────────────────────────────────────────────

# Kolom yang dienkripsi di tabel employee_salary
SALARY_ENC_FIELDS = frozenset(['base_salary', 'al_001', 'al_002', 'al_003', 'al_004'])

def _get_fernet():
    if not _CRYPTO_OK:
        return None
    key = os.environ.get('FIELD_ENCRYPT_KEY', '')
    if not key:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        return None

def _fenc(val):
    """Enkripsi nilai numerik gaji. Return string Fernet token, atau float jika key tidak ada."""
    if val is None:
        return None
    f = _get_fernet()
    if f is None:
        return float(val) if not isinstance(val, str) else val
    return f.encrypt(str(float(val)).encode()).decode()

def _fdec(val):
    """Dekripsi nilai gaji. Handle: None, float legacy, string token terenkripsi."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)  # data lama belum terenkripsi
    f = _get_fernet()
    if f is None:
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0
    try:
        return float(f.decrypt(val.encode() if isinstance(val, str) else val).decode())
    except Exception:
        try:
            return float(val)  # fallback: coba parse langsung (data lama)
        except (ValueError, TypeError):
            return 0.0

def _dec_sal_row(row):
    """Konversi sqlite3.Row salary ke dict dengan nilai terdekripsi."""
    d = dict(row)
    for field in SALARY_ENC_FIELDS:
        if field in d:
            d[field] = _fdec(d[field])
        prev = f'p_{field}'
        if prev in d:
            d[prev] = _fdec(d[prev])
    return d

SALARY_COLS = [
    ('base_salary', 'SALARY',    'Gaji Pokok'),
    ('al_001',      'AL_001',    'Tunjangan Jabatan'),
    ('al_002',      'AL_002',    'Tunjangan Komunikasi'),
    ('al_003',      'AL_003',    'Tunjangan Performance'),
    ('al_004',      'AL_004',    'Tunjangan Kehadiran'),
]

def _salary_total(row):
    if not row:
        return 0
    return sum(_fdec(row[col]) for col, *_ in SALARY_COLS)

@app.route('/salary')
@permission_required('view_salary')
@mfa_challenge_required('mfa_salary_verified')
def salary_table():
    db       = get_db()
    year     = request.args.get('year', date.today().year, type=int)
    prev_year = year - 1
    emps = db.execute('''
        SELECT e.*, s.id AS sal_id,
               s.base_salary, s.al_001, s.al_002, s.al_003, s.al_004,
               s.increase_pct, s.increase_date, s.notes,
               p.base_salary AS p_base_salary, p.al_001 AS p_al_001,
               p.al_002 AS p_al_002, p.al_003 AS p_al_003, p.al_004 AS p_al_004
        FROM employees e
        LEFT JOIN employee_salary s ON s.employee_id = e.id AND s.year = ?
        LEFT JOIN employee_salary p ON p.employee_id = e.id AND p.year = ?
        WHERE e.is_active = 1
        ORDER BY e.employment_type DESC, e.divisi, e.name
    ''', (year, prev_year)).fetchall()
    emps = [_dec_sal_row(r) for r in emps]
    years = [r['year'] for r in db.execute(
        'SELECT DISTINCT year FROM employee_salary ORDER BY year DESC').fetchall()]
    if year not in years:
        years = sorted(set(years + [year]), reverse=True)
    return render_template('salary.html', emps=emps, year=year, prev_year=prev_year, years=years,
                           salary_cols=SALARY_COLS, salary_total=_salary_total,
                           can_edit='manage_salary' in get_role_permissions(db, session.get('user_role','')))

@app.route('/salary/save', methods=['POST'])
@permission_required('manage_salary')
@mfa_challenge_required('mfa_salary_verified')
def salary_save():
    db      = get_db()
    emp_id  = request.form.get('employee_id', type=int)
    year    = request.form.get('year', type=int)
    field   = request.form.get('field', '').strip()
    value   = request.form.get('value', '').strip()
    valid_fields = {c for c, *_ in SALARY_COLS} | {'increase_pct', 'increase_date', 'notes'}
    if not emp_id or not year or field not in valid_fields:
        return jsonify({'ok': False, 'msg': 'Parameter tidak valid'})
    try:
        if field in ('notes', 'increase_date'):
            val = value
        elif field in SALARY_ENC_FIELDS:
            val = _fenc(float(value))
        else:
            val = float(value)
    except ValueError:
        val = _fenc(0.0) if field in SALARY_ENC_FIELDS else 0.0
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(f'''
        INSERT INTO employee_salary(employee_id, year, {field}, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(employee_id, year) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at
    ''', (emp_id, year, val, now))
    db.commit()
    # Return updated row totals (decrypt before computing)
    row = db.execute('SELECT * FROM employee_salary WHERE employee_id=? AND year=?',
                     (emp_id, year)).fetchone()
    row = _dec_sal_row(row) if row else row
    total = _salary_total(row)
    next_pct  = (row['increase_pct'] or 0) if row else 0
    next_total = round(total * (1 + next_pct / 100))
    return jsonify({'ok': True, 'total': total, 'next_total': next_total})

@app.route('/salary/add-year', methods=['POST'])
@permission_required('manage_salary')
@mfa_challenge_required('mfa_salary_verified')
def salary_add_year():
    """Buat data gaji untuk tahun baru (salin dari tahun sumber jika ada)."""
    db       = get_db()
    year     = request.form.get('year', type=int)
    src_year = request.form.get('src_year', type=int)
    now      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if not year or year < 2000 or year > 2100:
        return jsonify({'ok': False, 'msg': 'Tahun tidak valid'})

    # Copy from source year if provided and data exists
    copied = 0
    if src_year:
        rows = db.execute('SELECT * FROM employee_salary WHERE year=?', (src_year,)).fetchall()
        pct_apply = request.form.get('apply_increase', '0') == '1'
        for r in rows:
            pct    = (r['increase_pct'] or 0) if pct_apply else 0
            factor = 1 + pct / 100
            try:
                db.execute('''
                    INSERT OR IGNORE INTO employee_salary
                    (employee_id, year, base_salary, al_001, al_002, al_003, al_004,
                     increase_pct, notes, updated_at)
                    VALUES(?,?,?,?,?,?,?,0,'',?)
                ''', (r['employee_id'], year,
                      _fenc(round(_fdec(r['base_salary']) * factor)),
                      _fenc(round(_fdec(r['al_001']) * factor)),
                      _fenc(round(_fdec(r['al_002']) * factor)),
                      _fenc(round(_fdec(r['al_003']) * factor)),
                      _fenc(round(_fdec(r['al_004']) * factor)),
                      now))
                copied += 1
            except Exception:
                pass
    else:
        # Buat baris kosong untuk semua karyawan aktif
        emps = db.execute("SELECT id FROM employees WHERE is_active=1").fetchall()
        for e in emps:
            try:
                db.execute('''
                    INSERT OR IGNORE INTO employee_salary(employee_id, year, updated_at)
                    VALUES(?,?,?)
                ''', (e['id'], year, now))
                copied += 1
            except Exception:
                pass
    db.commit()

    years_available = [r['year'] for r in db.execute(
        'SELECT DISTINCT year FROM employee_salary ORDER BY year DESC').fetchall()]
    msg = (f'{copied} data gaji disalin dari tahun {src_year} ke {year}'
           if src_year else f'Tahun {year} dibuat dengan {copied} baris kosong')
    return jsonify({'ok': True, 'copied': copied, 'msg': msg, 'years': years_available})

@app.route('/emp/<int:emp_id>/salary', methods=['GET'])
@permission_required('view_salary')
@mfa_challenge_required('mfa_salary_verified')
def emp_salary_view(emp_id):
    db  = get_db()
    emp = db.execute('SELECT id, name FROM employees WHERE id=?', (emp_id,)).fetchone()
    if not emp:
        return jsonify({'ok': False, 'msg': 'Karyawan tidak ditemukan'})
    return jsonify({'ok': True, 'name': emp['name']})

@app.route('/emp/<int:emp_id>/salary', methods=['POST'])
@permission_required('manage_salary')
@mfa_challenge_required('mfa_salary_verified')
def emp_salary_update(emp_id):
    return jsonify({'ok': True, 'msg': 'Gunakan /salary untuk edit komponen gaji'})

# ─── Error handlers (audit trail) ─────────────────────────────────────────────

@app.errorhandler(404)
def err_404(e):
    try:
        db = get_db()
        db.execute('''INSERT INTO audit_errors(app_slug,user_id,username,url,method,error_code,error_type,error_msg,ip)
                      VALUES(?,?,?,?,?,?,?,?,?)''',
                   (session.get('active_app','portal'), session.get('user_id'), session.get('user_name',''),
                    request.path, request.method, 404, 'NotFound', str(e),
                    _real_ip()))
        db.commit()
    except Exception:
        pass
    return render_template('error.html', code=404, msg='Halaman tidak ditemukan'), 404

@app.errorhandler(500)
def err_500(e):
    import traceback as _tb
    tb = _tb.format_exc()
    # Gunakan koneksi BARU — koneksi lama (g.db) mungkin dalam state aborted transaction
    # (misal setelah psycopg2 error), sehingga tidak bisa digunakan untuk INSERT.
    try:
        if DB_TYPE == 'postgresql':
            _econn = _pg_connect()
            _econn.autocommit = False
            _edb = _DBWrapper(_econn, is_pg=True)
        else:
            import sqlite3 as _sq3
            _raw = _sq3.connect(DB_PATH)
            _raw.row_factory = _sq3.Row
            _edb = _DBWrapper(_raw, is_pg=False)
        _edb.execute('''INSERT INTO audit_errors(app_slug,user_id,username,url,method,error_code,error_type,error_msg,traceback,ip)
                        VALUES(?,?,?,?,?,?,?,?,?,?)''',
                     (session.get('active_app','portal'), session.get('user_id'), session.get('user_name',''),
                      request.path, request.method, 500, type(e).__name__, str(e), tb[:3000],
                      _real_ip()))
        _edb.commit()
        _edb.close()
    except Exception:
        pass
    return render_template('error.html', code=500, msg='Terjadi kesalahan server'), 500

# ─── Portal: Update Center ─────────────────────────────────────────────────────

@app.route('/portal/update')
@login_required
def portal_update():
    role = session.get('user_role', '')
    db   = get_db()
    settings = get_settings(db)
    notify_roles  = [r.strip() for r in settings.get('update_notify_roles',  'superadmin,admin').split(',')]
    trigger_roles = [r.strip() for r in settings.get('update_trigger_roles', 'superadmin').split(',')]
    if role not in notify_roles:
        abort(403)

    # Semua tag tersedia dari GitHub (cache di app_settings sebagai JSON)
    import json
    all_tags_raw = settings.get('update_all_tags', '[]')
    try:
        _raw = json.loads(all_tags_raw)
        if _raw and isinstance(_raw[0], str):
            _raw = [{'tag': t, 'notes': ''} for t in _raw]
        def _vp(v):
            try: return [int(x) for x in v.strip().lstrip('v').split('.')]
            except: return [0]
        _cur = _vp(VERSION)
        for item in _raw:
            _v = _vp(item.get('tag', ''))
            if _v == _cur:
                item['status'] = 'installed'
            elif _v > _cur:
                item['status'] = 'newer'   # lebih baru dari terpasang
            else:
                item['status'] = 'old'
        all_tags = _raw[:5]
        # Hitung update_available langsung dari all_tags (bukan dari DB)
        # agar selalu sinkron dengan daftar versi yang ditampilkan
        _newer = [i for i in all_tags if i.get('status') == 'newer']
        _latest_newer = max(_newer, key=lambda i: _vp(i['tag']), default=None)
        update_available_live = bool(_newer)
        latest_version_live   = _latest_newer['tag'].lstrip('v') if _latest_newer else settings.get('update_latest_version', '')
    except Exception:
        all_tags             = []
        update_available_live = settings.get('update_available', '0') == '1'
        latest_version_live   = settings.get('update_latest_version', '')

    # Status deploy sedang berjalan
    deploy_running = os.path.exists(UPDATE_TRIGGER_FILE)
    deploy_done    = False
    if os.path.exists(UPDATE_LOG_FILE):
        with open(UPDATE_LOG_FILE, 'r', errors='replace') as f:
            log_tail = f.read()
        deploy_done = UPDATE_DONE_MARKER in log_tail or UPDATE_FAIL_MARKER in log_tail
    else:
        log_tail = ''

    return render_template('update_center.html',
        settings       = settings,
        current_version= VERSION,
        latest_version = latest_version_live,
        latest_tag     = settings.get('update_latest_tag', ''),
        update_available = update_available_live,
        release_notes  = settings.get('update_release_notes', ''),
        check_last     = settings.get('update_check_last', ''),
        all_tags       = all_tags,
        can_trigger    = role in trigger_roles,
        deploy_running = deploy_running,
        deploy_done    = deploy_done,
        log_tail       = log_tail[-8000:] if log_tail else '',
    )


@app.route('/portal/update/check', methods=['POST'])
@login_required
def portal_update_check():
    role = session.get('user_role', '')
    settings = get_settings(get_db())
    notify_roles = [r.strip() for r in settings.get('update_notify_roles', 'superadmin,admin').split(',')]
    if role not in notify_roles:
        abort(403)
    check_for_updates()
    # Baca hasil cek untuk flash message yang informatif
    _db2 = get_db()
    _s2  = get_settings(_db2)
    _lv  = _s2.get('update_latest_version', '')
    if _s2.get('update_available', '0') == '1':
        flash(f'Update tersedia: v{_lv}', 'info')
    elif _lv:
        flash(f'Hive sudah versi terbaru (v{_lv}).', 'success')
    else:
        flash('Pengecekan selesai.', 'secondary')
    return redirect(url_for('portal_update'))


@app.route('/portal/update/trigger', methods=['POST'])
@login_required
def portal_update_trigger():
    role = session.get('user_role', '')
    db   = get_db()
    settings = get_settings(db)
    trigger_roles = [r.strip() for r in settings.get('update_trigger_roles', 'superadmin').split(',')]
    if role not in trigger_roles:
        abort(403)

    target_version = request.form.get('version', '').strip()
    if not target_version:
        flash('Versi tidak valid.', 'danger')
        return redirect(url_for('portal_update'))

    # Tulis trigger file — systemd path unit akan mendeteksi dan menjalankan deploy
    try:
        # Bersihkan log lama
        if os.path.exists(UPDATE_LOG_FILE):
            os.remove(UPDATE_LOG_FILE)
        with open(UPDATE_TRIGGER_FILE, 'w') as f:
            f.write(f'{target_version}\n')
        flash(f'Update ke v{target_version} dimulai! Pantau progress di bawah.', 'success')
    except Exception as e:
        flash(f'Gagal memulai update: {e}', 'danger')

    return redirect(url_for('portal_update'))


@app.route('/portal/update/log-stream')
@login_required
def portal_update_log_stream():
    """SSE stream untuk memantau log deploy secara realtime."""
    role = session.get('user_role', '')
    settings = get_settings(get_db())
    notify_roles = [r.strip() for r in settings.get('update_notify_roles', 'superadmin,admin').split(',')]
    if role not in notify_roles:
        abort(403)

    def generate():
        import time
        pos = 0
        timeout = 600  # max 10 menit
        start   = time.time()
        while time.time() - start < timeout:
            if os.path.exists(UPDATE_LOG_FILE):
                with open(UPDATE_LOG_FILE, 'r', errors='replace') as f:
                    f.seek(pos)
                    chunk = f.read(4096)
                    if chunk:
                        pos = f.tell()
                        for line in chunk.splitlines():
                            yield f'data: {line}\n\n'
                        if UPDATE_DONE_MARKER in chunk:
                            yield f'data: {UPDATE_DONE_MARKER}\n\n'
                            return
                        if UPDATE_FAIL_MARKER in chunk:
                            yield f'data: {UPDATE_FAIL_MARKER}\n\n'
                            return
            time.sleep(1)
        yield f'data: TIMEOUT\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/portal/update/cancel', methods=['POST'])
@login_required
def portal_update_cancel():
    role = session.get('user_role', '')
    settings = get_settings(get_db())
    trigger_roles = [r.strip() for r in settings.get('update_trigger_roles', 'superadmin').split(',')]
    if role not in trigger_roles:
        abort(403)
    if os.path.exists(UPDATE_TRIGGER_FILE):
        os.remove(UPDATE_TRIGGER_FILE)
        flash('Trigger dibatalkan (jika deploy sudah berjalan, tidak bisa dihentikan di tengah jalan).', 'warning')
    return redirect(url_for('portal_update'))


# ─── Kinerja Task: Individual & Tim ──────────────────────────────────────────

@app.route('/kinerja/individu/<int:emp_id>')
@login_required
def kinerja_individu(emp_id):
    db   = get_db()
    emp  = db.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
    if not emp:
        abort(404)

    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    benchmark = float(request.args.get('benchmark', 100))

    perf = calc_task_perf(db, emp_id, date_from, date_to, benchmark)

    # Riwayat evaluasi terakhir
    evals = db.execute('''
        SELECT id, periode, final_total, task_score, status, task_date_from, task_date_to
        FROM evaluations WHERE employee_id=? ORDER BY id DESC LIMIT 6
    ''', (emp_id,)).fetchall()

    # Skor rata-rata tim satu divisi (untuk perbandingan)
    divisi_avg = None
    if emp['divisi']:
        row = db.execute('''
            SELECT AVG(e.final_total) AS avg_score
            FROM evaluations e JOIN employees em ON em.id=e.employee_id
            WHERE em.divisi=? AND e.status='final'
        ''', (emp['divisi'],)).fetchone()
        divisi_avg = round(row['avg_score'], 1) if row and row['avg_score'] else None

    return render_template('kinerja_individu.html',
        emp=emp, perf=perf, evals=evals,
        divisi_avg=divisi_avg,
        date_from=date_from, date_to=date_to, benchmark=benchmark,
    )


@app.route('/kinerja/tim')
@login_required
def kinerja_tim():
    db      = get_db()
    divisi  = request.args.get('divisi', '')
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    benchmark = float(request.args.get('benchmark', 100))

    # Daftar semua divisi
    divisi_list = [r['divisi'] for r in db.execute(
        "SELECT DISTINCT divisi FROM employees WHERE is_active=1 AND divisi!='' ORDER BY divisi"
    ).fetchall()]

    # Karyawan di divisi terpilih (atau semua jika tidak filter)
    if divisi:
        emps = db.execute(
            "SELECT * FROM employees WHERE is_active=1 AND divisi=? ORDER BY name", (divisi,)
        ).fetchall()
    else:
        emps = db.execute(
            "SELECT * FROM employees WHERE is_active=1 ORDER BY divisi, name"
        ).fetchall()

    members = []
    for emp in emps:
        perf = calc_task_perf(db, emp['id'], date_from, date_to, benchmark)
        last_eval = db.execute('''
            SELECT final_total, task_score, status, periode FROM evaluations
            WHERE employee_id=? ORDER BY id DESC LIMIT 1
        ''', (emp['id'],)).fetchone()
        members.append({
            'emp':        emp,
            'perf':       perf,
            'last_eval':  last_eval,
        })

    # Urutkan: task_score tertinggi dulu
    members.sort(key=lambda x: x['perf']['task_score'], reverse=True)
    # Tambahkan rank
    for i, m in enumerate(members, 1):
        m['rank'] = i

    # Statistik tim
    scores = [m['perf']['task_score'] for m in members if m['perf']['task_score'] > 0]
    tim_avg  = round(sum(scores) / len(scores), 1) if scores else 0
    tim_max  = max(scores) if scores else 0
    tim_min  = min(scores) if scores else 0

    return render_template('kinerja_tim.html',
        members=members, divisi=divisi, divisi_list=divisi_list,
        date_from=date_from, date_to=date_to, benchmark=benchmark,
        tim_avg=tim_avg, tim_max=tim_max, tim_min=tim_min,
    )


@app.route('/kinerja/analitik')
@login_required
def kinerja_analitik():
    db        = get_db()
    divisi    = request.args.get('divisi', '')
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    benchmark = float(request.args.get('benchmark', 100))
    level     = request.args.get('level', '')

    divisi_list = [r['divisi'] for r in db.execute(
        "SELECT DISTINCT divisi FROM employees WHERE is_active=1 AND divisi!='' ORDER BY divisi"
    ).fetchall()]

    # Filter karyawan
    q = "SELECT * FROM employees WHERE is_active=1"
    params = []
    if divisi:
        q += " AND divisi=?"; params.append(divisi)
    if level:
        q += " AND level=?"; params.append(level)
    q += " ORDER BY divisi, name"
    emps = db.execute(q, params).fetchall()

    members = []
    for emp in emps:
        analytics = calc_task_analytics(db, emp['id'], date_from, date_to)
        perf      = calc_task_perf(db, emp['id'], date_from, date_to, benchmark)
        members.append({
            'emp':       emp,
            'a':         analytics,
            'perf':      perf,
        })

    # Urutkan: total task selesai tertinggi
    members.sort(key=lambda x: x['a']['total_done'], reverse=True)
    for i, m in enumerate(members, 1):
        m['rank'] = i

    # Aggregate per divisi
    div_stats = {}
    for m in members:
        d = m['emp']['divisi'] or '—'
        if d not in div_stats:
            div_stats[d] = {'count': 0, 'total_done': 0, 'ontime': 0,
                            'delay': 0, 'overtime': 0, 'score_sum': 0}
        s = div_stats[d]
        s['count']      += 1
        s['total_done'] += m['a']['total_done']
        s['ontime']     += m['a']['done_ontime']
        s['delay']      += m['a']['done_delay']
        s['overtime']   += m['a']['open_overtime']
        s['score_sum']  += m['perf']['task_score']
    for d in div_stats:
        n = div_stats[d]['count']
        div_stats[d]['avg_score'] = round(div_stats[d]['score_sum'] / n, 1) if n else 0

    return render_template('kinerja_analitik.html',
        members=members, divisi=divisi, divisi_list=divisi_list,
        date_from=date_from, date_to=date_to, benchmark=benchmark,
        level=level, div_stats=div_stats,
    )


@app.route('/api/kinerja/task-score/<int:emp_id>')
@login_required
def api_task_score(emp_id):
    import json as _json
    db = get_db()
    date_from = request.args.get('from', '')
    date_to   = request.args.get('to', '')
    benchmark = float(request.args.get('benchmark', 100))
    perf = calc_task_perf(db, emp_id, date_from, date_to, benchmark)
    return app.response_class(
        response=_json.dumps(perf, ensure_ascii=False),
        mimetype='application/json'
    )


# ─── AI Chatbot ────────────────────────────────────────────────────────────────

CHATBOT_TOOLS = [
    {
        "name": "cari_tiket_support",
        "description": "Cari tiket support berdasarkan kata kunci subjek, nomor tiket, customer, atau status. Gunakan untuk pertanyaan tentang masalah/issue yang pernah dilaporkan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Kata kunci pencarian (subjek, nomor tiket, atau nama customer)"},
                "status":  {"type": "string", "description": "Filter status: open, in_progress, resolved, closed (opsional)"},
                "limit":   {"type": "integer", "description": "Jumlah hasil maksimum (default 5)"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "detail_tiket",
        "description": "Ambil detail lengkap satu tiket support termasuk deskripsi, riwayat, dan solusi.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_no": {"type": "string", "description": "Nomor tiket (misal TKT-001)"}
            },
            "required": ["ticket_no"]
        }
    },
    {
        "name": "cari_project",
        "description": "Cari project dan task berdasarkan nama atau status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Nama project atau kata kunci"},
                "status":  {"type": "string", "description": "Filter status project (opsional): active, completed, on_hold"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "cari_karyawan",
        "description": "Cari data karyawan berdasarkan nama, jabatan, atau divisi. TIDAK menampilkan data gaji.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Nama, jabatan, atau divisi karyawan"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "cari_aset",
        "description": "Cari data aset, lisensi software, atau infrastruktur IT.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Nama aset, vendor, atau kategori"},
                "category": {"type": "string", "description": "Kategori aset (opsional)"}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "statistik_aplikasi",
        "description": "Ambil ringkasan statistik aplikasi: jumlah tiket, project aktif, karyawan, aset, dll.",
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {"type": "string", "description": "Modul: support, project, talent, asset, atau all"}
            },
            "required": ["module"]
        }
    },
]

def _chatbot_exec_tool(db, name, inp):
    """Eksekusi tool chatbot dan return hasil sebagai string."""
    try:
        if name == 'cari_tiket_support':
            kw = f"%{inp.get('keyword','')}%"
            st = inp.get('status','')
            lm = min(int(inp.get('limit', 5)), 20)
            where = "AND t.status=?" if st else ""
            params = [kw, kw, kw] + ([st] if st else []) + [lm]
            rows = db.execute(f'''
                SELECT t.ticket_no, t.subject, t.status, t.priority,
                       cu.name as customer, t.reported_at, t.resolved_at
                FROM sc_tickets t
                JOIN sc_customers cu ON cu.id=t.customer_id
                WHERE (t.ticket_no LIKE ? OR t.subject LIKE ? OR cu.name LIKE ?) {where}
                ORDER BY t.id DESC LIMIT ?
            ''', params).fetchall()
            if not rows: return "Tidak ditemukan tiket yang cocok."
            out = []
            for r in rows:
                out.append(f"[{r['ticket_no']}] {r['subject']} | Status: {r['status']} | Prioritas: {r['priority'] or 'Medium'} | Customer: {r['customer']} | Lapor: {r['reported_at']}")
            return "\n".join(out)

        elif name == 'detail_tiket':
            tno = inp.get('ticket_no','').strip()
            row = db.execute('''
                SELECT t.*, cu.name as customer, st.name as type_name
                FROM sc_tickets t
                JOIN sc_customers cu ON cu.id=t.customer_id
                JOIN sc_support_types st ON st.id=t.support_type_id
                WHERE t.ticket_no=?
            ''', (tno,)).fetchone()
            if not row: return f"Tiket {tno} tidak ditemukan."
            hist = db.execute('''
                SELECT action, notes, new_value, created_at FROM sc_ticket_history
                WHERE ticket_id=? ORDER BY id DESC LIMIT 5
            ''', (row['id'],)).fetchall()
            out = [
                f"Tiket: {row['ticket_no']}",
                f"Subjek: {row['subject']}",
                f"Customer: {row['customer']}",
                f"Tipe: {row['type_name']}",
                f"Status: {row['status']} | Prioritas: {row['priority'] or 'Medium'}",
                f"Deskripsi: {row['description'] or '—'}",
                f"Solusi: {row['solution_note'] or '—'}",
            ]
            if hist:
                out.append("Riwayat terakhir:")
                for h in hist:
                    out.append(f"  [{h['created_at']}] {h['action']} — {h['notes'] or h['new_value'] or ''}")
            return "\n".join(out)

        elif name == 'cari_project':
            kw = f"%{inp.get('keyword','')}%"
            st = inp.get('status','')
            where = "AND p.status=?" if st else ""
            params = [kw, kw] + ([st] if st else [])
            rows = db.execute(f'''
                SELECT p.name, p.status, p.start_date, p.end_date, p.description,
                       COUNT(DISTINCT t.id) as task_count
                FROM pc_projects p
                LEFT JOIN pc_tasks t ON t.project_id=p.id
                WHERE (p.name LIKE ? OR p.description LIKE ?) {where}
                GROUP BY p.id ORDER BY p.id DESC LIMIT 10
            ''', params).fetchall()
            if not rows: return "Tidak ditemukan project yang cocok."
            out = []
            for r in rows:
                out.append(f"[{r['status']}] {r['name']} | Tasks: {r['task_count']} | {r['start_date'] or '?'} s/d {r['end_date'] or '?'}")
            return "\n".join(out)

        elif name == 'cari_karyawan':
            kw = f"%{inp.get('keyword','')}%"
            rows = db.execute('''
                SELECT name, jabatan, divisi, email, is_active FROM employees
                WHERE (name LIKE ? OR jabatan LIKE ? OR divisi LIKE ?) AND is_active=1
                ORDER BY divisi, name LIMIT 15
            ''', (kw, kw, kw)).fetchall()
            if not rows: return "Tidak ditemukan karyawan yang cocok."
            out = []
            for r in rows:
                out.append(f"{r['name']} | {r['jabatan'] or '—'} | {r['divisi'] or '—'} | {r['email'] or '—'}")
            return "\n".join(out)

        elif name == 'cari_aset':
            kw = f"%{inp.get('keyword','')}%"
            cat = f"%{inp.get('category','')}%" if inp.get('category') else '%'
            rows = db.execute('''
                SELECT label, category, vendor, status, location, notes
                FROM ac_assets
                WHERE (label LIKE ? OR vendor LIKE ? OR notes LIKE ?) AND category LIKE ?
                ORDER BY id DESC LIMIT 15
            ''', (kw, kw, kw, cat)).fetchall()
            if not rows: return "Tidak ditemukan aset yang cocok."
            out = []
            for r in rows:
                out.append(f"{r['label']} | {r['category'] or '—'} | {r['vendor'] or '—'} | Status: {r['status']} | Lokasi: {r['location'] or '—'}")
            return "\n".join(out)

        elif name == 'statistik_aplikasi':
            mod = inp.get('module','all')
            out = []
            if mod in ('support','all'):
                t = db.execute("SELECT COUNT(*) as n FROM sc_tickets").fetchone()['n']
                o = db.execute("SELECT COUNT(*) as n FROM sc_tickets WHERE status NOT IN ('resolved','closed')").fetchone()['n']
                out.append(f"Support: {t} total tiket, {o} open")
            if mod in ('project','all'):
                p = db.execute("SELECT COUNT(*) as n FROM pc_projects WHERE status='active'").fetchone()['n']
                tk = db.execute("SELECT COUNT(*) as n FROM pc_tasks WHERE status NOT IN ('done','cancelled')").fetchone()['n']
                out.append(f"Project: {p} project aktif, {tk} task open")
            if mod in ('talent','all'):
                e = db.execute("SELECT COUNT(*) as n FROM employees WHERE is_active=1").fetchone()['n']
                out.append(f"Talent: {e} karyawan aktif")
            if mod in ('asset','all'):
                a = db.execute("SELECT COUNT(*) as n FROM ac_assets WHERE status='active'").fetchone()['n']
                out.append(f"Aset: {a} aset aktif")
            return "\n".join(out) if out else "Tidak ada data statistik."

    except Exception as ex:
        return f"Error mengambil data: {ex}"
    return "Tool tidak dikenal."

CHATBOT_SYSTEM = """Kamu adalah asisten AI untuk aplikasi Hive — sistem manajemen IT internal perusahaan.
Hive terdiri dari modul: TalentCore (karyawan & evaluasi), SupportCore (tiket support), ProjectCore (project & task), AssetCore (aset IT), BookingCore (ruangan & kendaraan).

PANDUAN:
- Jawab pertanyaan tentang data di aplikasi dengan memanggil tools yang tersedia
- Untuk troubleshooting teknis, gunakan kombinasi data tiket + pengetahuan umum IT
- Data RAHASIA yang TIDAK BOLEH ditampilkan: gaji, komponen kompensasi, data evaluasi pribadi yang sensitif
- Jika pertanyaan di luar lingkup aplikasi, gunakan pengetahuan umum kamu
- Jawab dalam Bahasa Indonesia kecuali ditanya dalam bahasa lain
- Singkat dan langsung ke inti, gunakan format poin jika ada beberapa item
"""

@app.route('/chatbot')
@login_required
def chatbot():
    db = get_db()
    settings = get_settings(db)
    if settings.get('chatbot_enabled','0') != '1':
        flash('Fitur chatbot belum diaktifkan. Hubungi administrator.', 'warning')
        return redirect(url_for('portal'))
    allowed = [r.strip() for r in settings.get('chatbot_roles','superadmin,admin,user').split(',')]
    if session.get('user_role','') not in allowed:
        flash('Akses ditolak.', 'danger')
        return redirect(url_for('portal'))
    return render_template('chatbot.html')

AI_PROVIDER_MODELS = {
    'anthropic':    ['claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-opus-4-8'],
    'openai':       ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],
    'openai_compat': [],  # user isi manual
}

AI_PROVIDER_DEFAULTS = {
    'anthropic':    'claude-sonnet-4-6',
    'openai':       'gpt-4o',
    'openai_compat': 'gpt-4o',
}

# Tools dalam format OpenAI (function calling)
def _tools_openai():
    result = []
    for t in CHATBOT_TOOLS:
        result.append({
            'type': 'function',
            'function': {
                'name': t['name'],
                'description': t['description'],
                'parameters': t['input_schema'],
            }
        })
    return result

def _chatbot_call_anthropic(api_key, model, messages, system, tools):
    import anthropic as _ant
    client = _ant.Anthropic(api_key=api_key)
    for _ in range(5):
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == 'end_turn':
            return next((b.text for b in resp.content if hasattr(b,'text')), '')
        if resp.stop_reason == 'tool_use':
            messages.append({'role': 'assistant', 'content': resp.content})
            tool_results = []
            for blk in resp.content:
                if blk.type == 'tool_use':
                    from flask import g as _g
                    db = get_db()
                    result = _chatbot_exec_tool(db, blk.name, blk.input)
                    tool_results.append({'type': 'tool_result', 'tool_use_id': blk.id, 'content': result})
            messages.append({'role': 'user', 'content': tool_results})
            continue
        break
    return 'Tidak ada respons dari AI.'

def _chatbot_call_openai(api_key, model, messages, system, tools_oa, base_url=None):
    import openai as _oai
    kwargs = {'api_key': api_key}
    if base_url:
        kwargs['base_url'] = base_url
    client = _oai.OpenAI(**kwargs)
    oai_msgs = [{'role': 'system', 'content': system}] + messages
    for _ in range(5):
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2048,
            tools=tools_oa,
            tool_choice='auto',
            messages=oai_msgs,
        )
        choice = resp.choices[0]
        if choice.finish_reason == 'stop':
            return choice.message.content or ''
        if choice.finish_reason == 'tool_calls':
            msg = choice.message
            oai_msgs.append(msg)
            for tc in msg.tool_calls:
                import json as _json
                inp = _json.loads(tc.function.arguments)
                db = get_db()
                result = _chatbot_exec_tool(db, tc.function.name, inp)
                oai_msgs.append({'role': 'tool', 'tool_call_id': tc.id, 'content': result})
            continue
        break
    return 'Tidak ada respons dari AI.'

@app.route('/api/chatbot/send', methods=['POST'])
@login_required
def chatbot_send():
    db = get_db()
    settings = get_settings(db)
    if settings.get('chatbot_enabled','0') != '1':
        return jsonify({'error': 'Chatbot tidak aktif'}), 403

    provider = settings.get('ai_provider','anthropic').strip()
    api_key  = settings.get('ai_api_key','').strip()
    # backward compat: jika ai_api_key kosong, cek anthropic_api_key lama
    if not api_key:
        api_key = settings.get('anthropic_api_key','').strip()
    if not api_key:
        return jsonify({'error': f'API key belum dikonfigurasi. Isi di System Settings → AI Assistant.'}), 503

    model    = settings.get('ai_model','').strip() or AI_PROVIDER_DEFAULTS.get(provider, 'gpt-4o')
    base_url = settings.get('ai_base_url','').strip() or None

    data = request.get_json()
    messages = data.get('messages', [])
    if not messages:
        return jsonify({'error': 'Pesan kosong'}), 400
    messages = messages[-20:]

    try:
        if provider == 'anthropic':
            reply = _chatbot_call_anthropic(api_key, model, messages, CHATBOT_SYSTEM, CHATBOT_TOOLS)
        else:
            reply = _chatbot_call_openai(api_key, model, messages, CHATBOT_SYSTEM, _tools_openai(), base_url)
        return jsonify({'reply': reply})
    except Exception as ex:
        return jsonify({'error': str(ex)}), 500

# ─── Portal: Audit Trail ───────────────────────────────────────────────────────

@app.route('/portal/audit')
@login_required
def portal_audit():
    if not is_portal_admin():
        flash('Akses ditolak', 'danger')
        return redirect(url_for('portal'))
    db = get_db()
    app_filter   = request.args.get('app', '')
    tab          = request.args.get('tab', 'activity')
    user_filter  = request.args.get('user', '')
    date_from    = request.args.get('from', '')
    date_to      = request.args.get('to', '')
    page         = max(1, int(request.args.get('page', 1)))
    per_page     = 50
    offset       = (page - 1) * per_page

    apps_list = db.execute('SELECT slug, name FROM superapp_apps WHERE is_active=1 ORDER BY sort_order').fetchall()

    def build_where(base_conds, params):
        where = ' AND '.join(base_conds) if base_conds else '1=1'
        return f'WHERE {where}', params

    if tab == 'activity':
        conds, params = [], []
        if app_filter: conds.append('app_slug=?'); params.append(app_filter)
        if user_filter: conds.append('username LIKE ?'); params.append(f'%{user_filter}%')
        if date_from: conds.append('created_at >= ?'); params.append(date_from)
        if date_to: conds.append('created_at <= ?'); params.append(date_to + ' 23:59:59')
        where, params = build_where(conds, params)
        total = db.execute(f'SELECT COUNT(*) FROM audit_activity {where}', params).fetchone()[0]
        rows  = db.execute(f'SELECT * FROM audit_activity {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                           params + [per_page, offset]).fetchall()

    elif tab == 'errors':
        conds, params = [], []
        if app_filter: conds.append('app_slug=?'); params.append(app_filter)
        if date_from: conds.append('created_at >= ?'); params.append(date_from)
        if date_to: conds.append('created_at <= ?'); params.append(date_to + ' 23:59:59')
        where, params = build_where(conds, params)
        total = db.execute(f'SELECT COUNT(*) FROM audit_errors {where}', params).fetchone()[0]
        rows  = db.execute(f'SELECT * FROM audit_errors {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                           params + [per_page, offset]).fetchall()

    else:  # notifications
        conds, params = [], []
        if app_filter: conds.append('app_slug=?'); params.append(app_filter)
        if date_from: conds.append('created_at >= ?'); params.append(date_from)
        if date_to: conds.append('created_at <= ?'); params.append(date_to + ' 23:59:59')
        where, params = build_where(conds, params)
        total = db.execute(f'SELECT COUNT(*) FROM audit_notifications {where}', params).fetchone()[0]
        rows  = db.execute(f'SELECT * FROM audit_notifications {where} ORDER BY id DESC LIMIT ? OFFSET ?',
                           params + [per_page, offset]).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template('portal_audit.html',
                           rows=rows, tab=tab, apps_list=apps_list,
                           app_filter=app_filter, user_filter=user_filter,
                           date_from=date_from, date_to=date_to,
                           page=page, total_pages=total_pages, total=total)

@app.route('/portal/audit/clear', methods=['POST'])
@login_required
def portal_audit_clear():
    if not is_portal_admin():
        flash('Akses ditolak', 'danger')
        return redirect(url_for('portal'))
    db  = get_db()
    tab = request.form.get('tab', 'activity')
    tbl = {'activity': 'audit_activity', 'errors': 'audit_errors', 'notifications': 'audit_notifications'}.get(tab)
    if tbl:
        app_filter = request.form.get('app', '')
        if app_filter:
            db.execute(f'DELETE FROM {tbl} WHERE app_slug=?', (app_filter,))
        else:
            db.execute(f'DELETE FROM {tbl}')
        db.commit()
        flash('Log berhasil dihapus', 'success')
    return redirect(url_for('portal_audit', tab=tab))

# ─── BookingCore ───────────────────────────────────────────────────────────────

def _bk_require_access():
    if not session.get('user_id'):
        return redirect(url_for('login'))
    acc = get_db().execute(
        'SELECT app_role FROM user_app_access WHERE user_id=? AND app_slug=? AND is_active=1',
        (session['user_id'], 'booking')
    ).fetchone()
    if session.get('user_role') == 'superadmin' or acc:
        return None
    flash('Anda tidak memiliki akses ke BookingCore.', 'danger')
    return redirect(url_for('portal'))

def _bk_generate_recurring(base, resource_id, title, purpose, booked_by, attendees,
                             attendee_count, notes, rec_type, rec_days, rec_until,
                             destination, start_dt, end_dt):
    from datetime import datetime, timedelta
    db = get_db()
    dt_start = datetime.strptime(start_dt, '%Y-%m-%d %H:%M')
    dt_end   = datetime.strptime(end_dt,   '%Y-%m-%d %H:%M')
    until    = datetime.strptime(rec_until, '%Y-%m-%d')
    delta    = dt_end - dt_start
    days_map = {'sen':0,'sel':1,'rab':2,'kam':3,'jum':4,'sab':5,'min':6}
    chosen   = [days_map[d] for d in (rec_days.split(',') if rec_days else []) if d in days_map]

    cur = dt_start + timedelta(days=1)
    count = 0
    while cur.date() <= until.date() and count < 365:
        if rec_type == 'weekly' and chosen:
            if cur.weekday() not in chosen:
                cur += timedelta(days=1)
                continue
        elif rec_type == 'daily':
            pass
        elif rec_type == 'weekly' and not chosen:
            if cur.weekday() != dt_start.weekday():
                cur += timedelta(days=1)
                continue
        s = cur.strftime('%Y-%m-%d %H:%M')
        e = (cur + delta).strftime('%Y-%m-%d %H:%M')
        db.execute('''INSERT INTO bk_bookings(resource_id,title,purpose,booked_by,attendees,
            attendee_count,start_dt,end_dt,status,notes,is_recurring,recurring_type,
            recurring_days,recurring_until,parent_id,destination)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (resource_id,title,purpose,booked_by,attendees,attendee_count,
             s,e,'confirmed',notes,1,rec_type,rec_days,rec_until,base,destination))
        cur += timedelta(days=1)
        count += 1
    db.commit()


@app.route('/booking/')
@app.route('/booking')
def booking_index():
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    resources = db.execute('SELECT * FROM bk_resources WHERE is_active=1 ORDER BY sort_order').fetchall()
    resource_id = request.args.get('resource', type=int)
    view = request.args.get('view', 'list')
    date_str = request.args.get('date', '')

    from datetime import datetime, timedelta
    today = datetime.now().date()
    if date_str:
        try: ref_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except: ref_date = today
    else:
        ref_date = today

    week_start = ref_date - timedelta(days=ref_date.weekday())
    week_dates = [week_start + timedelta(days=i) for i in range(7)]

    q = 'SELECT b.*,r.name res_name,r.type res_type,r.color res_color,r.icon res_icon,u.full_name booker_name FROM bk_bookings b JOIN bk_resources r ON r.id=b.resource_id JOIN users u ON u.id=b.booked_by WHERE b.status!=? '
    params = ['cancelled']
    if resource_id:
        q += ' AND b.resource_id=?'
        params.append(resource_id)
    if view == 'week':
        q += ' AND date(b.start_dt)>=? AND date(b.start_dt)<=?'
        params += [week_start.isoformat(), (week_start + timedelta(days=6)).isoformat()]
    else:
        q += ' AND date(b.start_dt)>=?'
        params.append(today.isoformat())
    q += ' ORDER BY b.start_dt'
    bookings = db.execute(q, params).fetchall()

    return render_template('booking_index.html',
        resources=resources, bookings=bookings,
        selected_resource=resource_id, view=view,
        ref_date=ref_date, today=today,
        week_dates=week_dates,
        prev_week=(week_start - timedelta(days=7)).isoformat(),
        next_week=(week_start + timedelta(days=7)).isoformat())


@app.route('/booking/new', methods=['GET','POST'])
def booking_new():
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    resources = db.execute('SELECT * FROM bk_resources WHERE is_active=1 ORDER BY sort_order').fetchall()

    if request.method == 'POST':
        resource_id    = int(request.form['resource_id'])
        title          = request.form['title'].strip()
        purpose        = request.form.get('purpose','').strip()
        attendees      = request.form.get('attendees','').strip()
        attendee_count = int(request.form.get('attendee_count') or 0)
        notes          = request.form.get('notes','').strip()
        destination    = request.form.get('destination','').strip()
        start_dt       = request.form['start_dt']
        end_dt         = request.form['end_dt']
        is_recurring   = 1 if request.form.get('is_recurring') else 0
        rec_type       = request.form.get('recurring_type','')
        rec_days       = ','.join(request.form.getlist('recurring_days'))
        rec_until      = request.form.get('recurring_until','')

        if not title or not start_dt or not end_dt:
            flash('Judul, waktu mulai dan selesai wajib diisi.', 'danger')
        elif start_dt >= end_dt:
            flash('Waktu selesai harus setelah waktu mulai.', 'danger')
        else:
            # check conflict
            conflict = db.execute('''SELECT id FROM bk_bookings
                WHERE resource_id=? AND status!='cancelled'
                AND start_dt < ? AND end_dt > ?''',
                (resource_id, end_dt, start_dt)).fetchone()
            if conflict:
                flash('Waktu yang dipilih bertabrakan dengan booking lain.', 'danger')
            else:
                db.execute('''INSERT INTO bk_bookings(resource_id,title,purpose,booked_by,attendees,
                    attendee_count,start_dt,end_dt,status,notes,is_recurring,recurring_type,
                    recurring_days,recurring_until,destination)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (resource_id,title,purpose,session['user_id'],attendees,attendee_count,
                     start_dt,end_dt,'confirmed',notes,is_recurring,rec_type,rec_days,rec_until,destination))
                db.commit()
                parent_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

                if is_recurring and rec_until:
                    _bk_generate_recurring(parent_id, resource_id, title, purpose,
                        session['user_id'], attendees, attendee_count, notes,
                        rec_type, rec_days, rec_until, destination, start_dt, end_dt)

                # Simpan booking items
                for item_id_str in request.form.getlist('item_ids'):
                    try:
                        item_id = int(item_id_str)
                        qty = int(request.form.get(f'item_qty_{item_id}') or 1)
                        item_notes = request.form.get(f'item_notes_{item_id}', '')
                        db.execute('INSERT INTO bk_booking_items(booking_id,item_id,qty,notes) VALUES(?,?,?,?)',
                                   (parent_id, item_id, qty, item_notes))
                    except (ValueError, TypeError):
                        pass
                db.commit()
                flash('Booking berhasil dibuat!', 'success')
                return redirect(url_for('booking_detail', bid=parent_id))

    pre_resource = request.args.get('resource', type=int)
    bk_items = db.execute('SELECT * FROM bk_items WHERE is_active=1 ORDER BY sort_order').fetchall()
    # load images per resource untuk preview
    res_images = {}
    for r in resources:
        imgs = db.execute('SELECT image FROM bk_resource_images WHERE resource_id=? ORDER BY sort_order LIMIT 5', (r['id'],)).fetchall()
        all_imgs = []
        if r['image']:
            all_imgs.append(r['image'])
        all_imgs += [i['image'] for i in imgs]
        res_images[r['id']] = all_imgs
    return render_template('booking_form.html', resources=resources, pre_resource=pre_resource,
                           bk_items=bk_items, res_images=res_images)


@app.route('/booking/resource/<int:rid>')
def booking_resource_detail(rid):
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    resource = db.execute('SELECT * FROM bk_resources WHERE id=? AND is_active=1', (rid,)).fetchone()
    if not resource:
        flash('Resource tidak ditemukan.', 'danger')
        return redirect(url_for('booking_index'))
    from datetime import datetime as _dt
    today = _dt.now().date().isoformat()
    upcoming = db.execute('''
        SELECT b.*,u.full_name booker_name FROM bk_bookings b
        JOIN users u ON u.id=b.booked_by
        WHERE b.resource_id=? AND b.status!='cancelled' AND date(b.start_dt)>=?
        ORDER BY b.start_dt LIMIT 30''', (rid, today)).fetchall()
    past = db.execute('''
        SELECT b.*,u.full_name booker_name FROM bk_bookings b
        JOIN users u ON u.id=b.booked_by
        WHERE b.resource_id=? AND b.status!='cancelled' AND date(b.start_dt)<?
        ORDER BY b.start_dt DESC LIMIT 10''', (rid, today)).fetchall()
    images = db.execute('SELECT * FROM bk_resource_images WHERE resource_id=? ORDER BY sort_order', (rid,)).fetchall()
    return render_template('booking_resource.html', resource=resource,
                           images=images, upcoming=upcoming, past=past, today=today)


@app.route('/booking/resource/<int:rid>/edit', methods=['GET', 'POST'])
@login_required
def booking_resource_edit(rid):
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    resource = db.execute('SELECT * FROM bk_resources WHERE id=?', (rid,)).fetchone()
    if not resource:
        flash('Resource tidak ditemukan.', 'danger')
        return redirect(url_for('booking_index'))
    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        rtype      = request.form.get('type', 'room')
        subtype    = request.form.get('subtype', '').strip()
        capacity   = request.form.get('capacity', 0, type=int)
        location   = request.form.get('location', '').strip()
        description= request.form.get('description', '').strip()
        facilities = request.form.get('facilities', '').strip()
        notes      = request.form.get('notes', '').strip()
        color      = request.form.get('color', '#d97706').strip()
        icon       = request.form.get('icon', 'door-open').strip()
        sort_order = request.form.get('sort_order', 0, type=int)
        is_active  = 1 if request.form.get('is_active') else 0
        image      = resource['image'] or ''
        # Handle image upload
        f = request.files.get('image')
        if f and f.filename:
            saved = _save_upload(f, 'resources')
            if saved:
                image = saved
            else:
                flash('Format gambar tidak didukung. Gunakan JPG, PNG, atau WEBP.', 'warning')
        # Handle remove image
        if request.form.get('remove_image') == '1':
            image = ''
        if not name:
            flash('Nama resource wajib diisi.', 'danger')
        else:
            db.execute('''UPDATE bk_resources SET name=?,type=?,subtype=?,capacity=?,
                location=?,description=?,facilities=?,notes=?,color=?,icon=?,
                sort_order=?,is_active=?,image=? WHERE id=?''',
                (name, rtype, subtype, capacity, location, description,
                 facilities, notes, color, icon, sort_order, is_active, image, rid))
            db.commit()
            flash('Resource berhasil diperbarui.', 'success')
            return redirect(url_for('booking_resource_detail', rid=rid))
    images = db.execute('SELECT * FROM bk_resource_images WHERE resource_id=? ORDER BY sort_order', (rid,)).fetchall()
    return render_template('booking_resource_edit.html', resource=resource, images=images)


@app.route('/booking/resource/add', methods=['GET', 'POST'])
@login_required
def booking_resource_add():
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        rtype      = request.form.get('type', 'room')
        subtype    = request.form.get('subtype', '').strip()
        capacity   = request.form.get('capacity', 0, type=int)
        location   = request.form.get('location', '').strip()
        description= request.form.get('description', '').strip()
        facilities = request.form.get('facilities', '').strip()
        notes      = request.form.get('notes', '').strip()
        color      = request.form.get('color', '#d97706').strip()
        icon       = request.form.get('icon', 'door-open').strip()
        sort_order = request.form.get('sort_order', 0, type=int)
        image      = ''
        f = request.files.get('image')
        if f and f.filename:
            saved = _save_upload(f, 'resources')
            if saved:
                image = saved
        if not name:
            flash('Nama resource wajib diisi.', 'danger')
        else:
            db.execute('''INSERT INTO bk_resources(name,type,subtype,capacity,location,
                description,facilities,notes,color,icon,sort_order,image)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (name, rtype, subtype, capacity, location, description,
                 facilities, notes, color, icon, sort_order, image))
            db.commit()
            flash(f'Resource "{name}" berhasil ditambahkan.', 'success')
            return redirect(url_for('booking_index'))
    return render_template('booking_resource_edit.html', resource=None)


@app.route('/booking/resource/<int:rid>/images', methods=['POST'])
@login_required
def booking_resource_images(rid):
    """Upload atau hapus gambar tambahan resource."""
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    action = request.form.get('action', 'upload')
    if action == 'delete':
        img_id = request.form.get('img_id', type=int)
        if img_id:
            db.execute('DELETE FROM bk_resource_images WHERE id=? AND resource_id=?', (img_id, rid))
            db.commit()
    else:
        files = request.files.getlist('images')
        sort_base = db.execute('SELECT COALESCE(MAX(sort_order),0) FROM bk_resource_images WHERE resource_id=?', (rid,)).fetchone()[0]
        for i, f in enumerate(files):
            if f and f.filename:
                saved = _save_upload(f, 'resources')
                if saved:
                    caption = request.form.get(f'caption_{i}', '')
                    db.execute('INSERT INTO bk_resource_images(resource_id,image,caption,sort_order) VALUES(?,?,?,?)',
                               (rid, saved, caption, sort_base + i + 1))
        db.commit()
    return redirect(url_for('booking_resource_edit', rid=rid))


@app.route('/booking/<int:bid>')
def booking_detail(bid):
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    b = db.execute('''SELECT b.*,r.name res_name,r.type res_type,r.color res_color,r.icon res_icon,
        r.capacity res_cap,r.location res_loc,u.full_name booker_name,u.username booker_username
        FROM bk_bookings b JOIN bk_resources r ON r.id=b.resource_id
        JOIN users u ON u.id=b.booked_by WHERE b.id=?''', (bid,)).fetchone()
    if not b:
        flash('Booking tidak ditemukan.', 'danger')
        return redirect(url_for('booking_index'))
    children = []
    if b['is_recurring'] and not b['parent_id']:
        children = db.execute('''SELECT * FROM bk_bookings WHERE parent_id=? AND status!='cancelled'
            ORDER BY start_dt LIMIT 50''', (bid,)).fetchall()
    booking_items = db.execute('''SELECT bi.*,i.name item_name,i.icon item_icon,i.category item_cat
        FROM bk_booking_items bi JOIN bk_items i ON i.id=bi.item_id
        WHERE bi.booking_id=? ORDER BY i.sort_order''', (bid,)).fetchall()
    return render_template('booking_detail.html', b=b, children=children, booking_items=booking_items)


@app.route('/booking/<int:bid>/cancel', methods=['POST'])
def booking_cancel(bid):
    redir = _bk_require_access()
    if redir: return redir
    db = get_db()
    b = db.execute('SELECT * FROM bk_bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        flash('Booking tidak ditemukan.', 'danger')
        return redirect(url_for('booking_index'))
    is_owner = b['booked_by'] == session['user_id']
    is_admin = session.get('user_role') in ('superadmin', 'admin')
    if not is_owner and not is_admin:
        flash('Hanya pemesan atau admin yang dapat membatalkan booking ini.', 'danger')
        return redirect(url_for('booking_detail', bid=bid))
    cancel_children = request.form.get('cancel_children') == '1'
    from datetime import datetime
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute('UPDATE bk_bookings SET status=?,cancelled_at=?,cancelled_by=? WHERE id=?',
               ('cancelled', now, session['user_id'], bid))
    if cancel_children and b['is_recurring'] and not b['parent_id']:
        db.execute('UPDATE bk_bookings SET status=?,cancelled_at=?,cancelled_by=? WHERE parent_id=? AND status!=?',
                   ('cancelled', now, session['user_id'], bid, 'cancelled'))
    db.commit()
    flash('Booking berhasil dibatalkan.', 'success')
    return redirect(url_for('booking_index'))


@app.route('/booking/api/slots')
def booking_api_slots():
    redir = _bk_require_access()
    if redir: return ('', 403)
    resource_id = request.args.get('resource', type=int)
    date_str    = request.args.get('date', '')
    if not resource_id or not date_str:
        return app.response_class('[]', mimetype='application/json')
    db = get_db()
    bookings = db.execute('''SELECT id,title,start_dt,end_dt,status FROM bk_bookings
        WHERE resource_id=? AND date(start_dt)=? AND status!='cancelled'
        ORDER BY start_dt''', (resource_id, date_str)).fetchall()
    import json
    data = [dict(id=r['id'],title=r['title'],start=r['start_dt'],end=r['end_dt']) for r in bookings]
    return app.response_class(json.dumps(data), mimetype='application/json')


# ─── AssetCore ─────────────────────────────────────────────────────────────────

def ac_require(perm):
    db = get_db()
    if not has_permission(session.get('user_role', ''), perm, db):
        flash(f'Akses ditolak — permission "{perm}" diperlukan', 'danger')
        return False
    return True

def _record_asset_history(db, asset_id, employee_id, manual_name, started_at, reason='', notes=''):
    """Simpan satu baris history penggunaan asset. Dipanggil sebelum employee diganti."""
    if not employee_id and not (manual_name or '').strip():
        return  # tidak ada user sebelumnya, tidak perlu catat
    today = date.today().isoformat()
    db.execute(
        '''INSERT INTO ac_asset_history
           (asset_id, employee_id, manual_employee_name, started_at, ended_at, reason, notes)
           VALUES (?,?,?,?,?,?,?)''',
        (asset_id, employee_id, manual_name or '', started_at or '', today, reason, notes)
    )

@app.route('/aset/')
@login_required
def ac_index():
    db = get_db()
    if not ac_require('ac_view'): return redirect(url_for('portal'))
    from datetime import date as _date, timedelta
    today = _date.today().isoformat()
    limit30 = (_date.today() + timedelta(days=30)).isoformat()
    stats = {
        'assets':    db.execute('SELECT COUNT(*) FROM ac_assets').fetchone()[0],
        'infra':     db.execute('SELECT COUNT(*) FROM ac_infrastructure').fetchone()[0],
        'licenses':  db.execute('SELECT COUNT(*) FROM ac_licenses WHERE is_active=1').fetchone()[0],
        'subs':      db.execute('SELECT COUNT(*) FROM ac_subscriptions WHERE is_active=1').fetchone()[0],
        'requests':  db.execute("SELECT COUNT(*) FROM ac_software_requests WHERE status='Pending'").fetchone()[0],
    }
    expiring = db.execute(
        "SELECT * FROM ac_subscriptions WHERE is_active=1 AND end_date!='' AND end_date BETWEEN ? AND ? ORDER BY end_date",
        (today, limit30)).fetchall()
    recent_requests = db.execute(
        "SELECT r.*, e.name as emp_name FROM ac_software_requests r LEFT JOIN employees e ON r.employee_id=e.id ORDER BY r.requested_at DESC LIMIT 5"
    ).fetchall()
    recent_history = db.execute(
        """SELECT h.*, e.name as emp_name,
                  a.brand, a.device_type, a.asset_tag, a.id as asset_id
           FROM ac_asset_history h
           LEFT JOIN employees e ON h.employee_id=e.id
           LEFT JOIN ac_assets a ON h.asset_id=a.id
           ORDER BY h.id DESC LIMIT 10"""
    ).fetchall()
    limit30 = (_date.today() + timedelta(days=30)).isoformat()
    maintenance_alert = db.execute(
        """SELECT * FROM ac_maintenance WHERE is_active=1
           AND next_maintenance!='' AND next_maintenance<=?
           ORDER BY next_maintenance ASC LIMIT 10""", (limit30,)
    ).fetchall()
    return render_template('ac_index.html', stats=stats, expiring=expiring,
                           recent_requests=recent_requests, recent_history=recent_history,
                           maintenance_alert=maintenance_alert, today=today)

# ── Assets ────────────────────────────────────────────────────────────────────
@app.route('/aset/assets')
@login_required
def ac_assets():
    if not ac_require('ac_view'): return redirect(url_for('ac_index'))
    db = get_db()
    q             = request.args.get('q', '')
    divisi        = request.args.get('divisi', '')
    status_filter = request.args.get('status', '')       # 'linked' | 'unlinked'
    asset_status  = request.args.get('asset_status', '') # 'aktif' | 'end'
    sort          = request.args.get('sort', 'updated')  # 'updated' | 'name' | 'divisi'
    sql = """SELECT a.*, e.name as emp_name, e.divisi,
             CASE WHEN a.employee_id IS NOT NULL THEN 'linked'
                  WHEN a.manual_employee_name!='' THEN 'unlinked'
                  ELSE 'no_user' END as link_status
             FROM ac_assets a LEFT JOIN employees e ON a.employee_id=e.id WHERE 1=1"""
    params = []
    if q:
        sql += ' AND (e.name LIKE ? OR a.manual_employee_name LIKE ? OR a.device_type LIKE ? OR a.asset_tag LIKE ?)'
        params += [f'%{q}%'] * 4
    if divisi:
        sql += ' AND e.divisi=?'; params.append(divisi)
    if status_filter == 'unlinked':
        sql += ' AND a.employee_id IS NULL AND a.manual_employee_name!=""'
    elif status_filter == 'linked':
        sql += ' AND a.employee_id IS NOT NULL'
    if asset_status == 'end':
        sql += " AND a.status='End'"
    elif asset_status == 'aktif':
        sql += " AND (a.status IS NULL OR a.status='Aktif')"
    ORDER_MAP = {
        'updated': 'a.updated_at DESC, a.id DESC',
        'name':    'COALESCE(e.name, a.manual_employee_name) ASC',
        'divisi':  'COALESCE(e.divisi,"") ASC, COALESCE(e.name, a.manual_employee_name) ASC',
    }
    sql += ' ORDER BY a.status ASC, ' + ORDER_MAP.get(sort, ORDER_MAP['updated'])
    assets = db.execute(sql, params).fetchall()
    divisis       = [r[0] for r in db.execute("SELECT DISTINCT divisi FROM employees WHERE divisi!='' ORDER BY divisi").fetchall()]
    unlinked_count = db.execute("SELECT COUNT(*) FROM ac_assets WHERE (status IS NULL OR status='Aktif') AND employee_id IS NULL AND manual_employee_name!=''").fetchone()[0]
    end_count      = db.execute("SELECT COUNT(*) FROM ac_assets WHERE status='End'").fetchone()[0]
    return render_template('ac_assets.html', assets=assets, q=q, divisi=divisi,
                           divisis=divisis, status_filter=status_filter,
                           asset_status=asset_status, sort=sort,
                           unlinked_count=unlinked_count, end_count=end_count)

@app.route('/aset/assets/new', methods=['GET','POST'])
@login_required
def ac_asset_new():
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_assets'))
    db = get_db()
    employees = db.execute('SELECT id,name,divisi FROM employees WHERE is_active=1 ORDER BY divisi,name').fetchall()
    if request.method == 'POST':
        mode = request.form.get('emp_mode', 'linked')
        emp_id = request.form.get('employee_id') or None if mode == 'linked' else None
        manual_name = request.form.get('manual_employee_name', '').strip() if mode == 'manual' else ''
        today = date.today().isoformat()
        started = today if (emp_id or manual_name) else ''
        cur = db.execute(
            'INSERT INTO ac_assets(employee_id,manual_employee_name,device_type,brand,os,os_license_type,processor,ram,disk,office_version,asset_tag,serial_number,purchase_date,condition,notes,started_using) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (emp_id, manual_name, request.form.get('device_type','Laptop'),
             request.form.get('brand',''), request.form.get('os',''), request.form.get('os_license_type',''),
             request.form.get('processor',''), request.form.get('ram',''), request.form.get('disk',''),
             request.form.get('office_version',''), request.form.get('asset_tag',''),
             request.form.get('serial_number',''), request.form.get('purchase_date',''),
             request.form.get('condition','Baik'), request.form.get('notes',''), started))
        aid = cur.lastrowid
        for s in [x.strip() for x in request.form.get('softwares','').split('\n') if x.strip()]:
            db.execute('INSERT INTO ac_asset_software(asset_id,software_name) VALUES(?,?)', (aid, s))
        db.commit()
        label = request.form.get('employee_id','') if mode == 'linked' else manual_name
        audit_log('create', 'ac_assets', aid, f"Asset baru: {label}", 'aset')
        flash('Asset berhasil ditambahkan.', 'success')
        return redirect(url_for('ac_asset_detail', aid=aid))
    return render_template('ac_asset_form.html', asset=None, employees=employees, sw_text='')

@app.route('/aset/assets/<int:aid>')
@login_required
def ac_asset_detail(aid):
    if not ac_require('ac_view'): return redirect(url_for('ac_assets'))
    db = get_db()
    asset = db.execute('''SELECT a.*, e.name as emp_name, e.divisi, e.jabatan, e.phone, e.email,
                          CASE WHEN a.employee_id IS NOT NULL THEN 'linked'
                               WHEN a.manual_employee_name!='' THEN 'unlinked'
                               ELSE 'no_user' END as link_status
                          FROM ac_assets a LEFT JOIN employees e ON a.employee_id=e.id WHERE a.id=?''', (aid,)).fetchone()
    if not asset: flash('Asset tidak ditemukan.', 'danger'); return redirect(url_for('ac_assets'))
    softwares = db.execute('SELECT * FROM ac_asset_software WHERE asset_id=? ORDER BY software_name', (aid,)).fetchall()
    employees = db.execute('SELECT id,name,divisi FROM employees WHERE is_active=1 ORDER BY divisi,name').fetchall()
    history = db.execute(
        '''SELECT h.*, e.name as emp_name, e.divisi
           FROM ac_asset_history h LEFT JOIN employees e ON h.employee_id=e.id
           WHERE h.asset_id=? ORDER BY h.ended_at DESC, h.id DESC''', (aid,)).fetchall()
    return render_template('ac_asset_detail.html', asset=asset, softwares=softwares,
                           employees=employees, history=history)

@app.route('/aset/assets/<int:aid>/link', methods=['POST'])
@login_required
def ac_asset_link(aid):
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_asset_detail', aid=aid))
    db = get_db()
    cur_asset = db.execute('SELECT * FROM ac_assets WHERE id=?', (aid,)).fetchone()
    if not cur_asset: return redirect(url_for('ac_assets'))
    action = request.form.get('action', 'link')
    reason = request.form.get('reason', '').strip() or ('Putuskan koneksi' if action == 'unlink' else 'Hubungkan ke karyawan')
    today  = date.today().isoformat()

    if action == 'unlink':
        old_user = _asset_user_display(db, cur_asset)
        _record_asset_history(db, aid, cur_asset['employee_id'], cur_asset['manual_employee_name'],
                              cur_asset['started_using'], reason)
        db.execute('UPDATE ac_assets SET employee_id=NULL, started_using=?, updated_at=datetime("now","localtime") WHERE id=?',
                   ('', aid))
        db.commit()
        _notify_asset_change(db, cur_asset, 'released', old_user, '', reason)
        flash('Link karyawan dilepas. Riwayat penggunaan tersimpan.', 'success')
    else:
        emp_id = request.form.get('employee_id') or None
        if not emp_id:
            flash('Pilih karyawan terlebih dahulu.', 'warning')
            return redirect(url_for('ac_asset_detail', aid=aid))
        old_user = _asset_user_display(db, cur_asset)
        _record_asset_history(db, aid, cur_asset['employee_id'], cur_asset['manual_employee_name'],
                              cur_asset['started_using'], reason)
        db.execute('UPDATE ac_assets SET employee_id=?, manual_employee_name=?, started_using=?, updated_at=datetime("now","localtime") WHERE id=?',
                   (emp_id, '', today, aid))
        db.commit()
        emp = db.execute('SELECT name FROM employees WHERE id=?', (emp_id,)).fetchone()
        new_user = emp['name'] if emp else ''
        _notify_asset_change(db, cur_asset, 'assigned', old_user, new_user, reason)
        flash(f'Asset dihubungkan ke {new_user or "karyawan"}. Riwayat tersimpan.', 'success')
    return redirect(url_for('ac_asset_detail', aid=aid))

@app.route('/aset/assets/<int:aid>/edit', methods=['GET','POST'])
@login_required
def ac_asset_edit(aid):
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_assets'))
    db = get_db()
    asset = db.execute('SELECT * FROM ac_assets WHERE id=?', (aid,)).fetchone()
    if not asset: flash('Asset tidak ditemukan.', 'danger'); return redirect(url_for('ac_assets'))
    employees = db.execute('SELECT id,name,divisi FROM employees WHERE is_active=1 ORDER BY divisi,name').fetchall()
    if request.method == 'POST':
        mode = request.form.get('emp_mode', 'linked')
        emp_id      = request.form.get('employee_id') or None if mode == 'linked' else None
        manual_name = request.form.get('manual_employee_name', '').strip() if mode == 'manual' else ''
        new_status  = request.form.get('status', 'Aktif')
        today = date.today().isoformat()

        # Alasan yang membuat asset kembali available (stok)
        FREEING_REASONS = {'Resign', 'Pindah ke Laptop Lain', 'Upgrade Laptop'}
        change_reason = request.form.get('change_reason', '').strip()
        has_current_user = bool(asset['employee_id'] or (asset['manual_employee_name'] or '').strip())

        notif_event = notif_old = notif_new = notif_reason = None

        if change_reason in FREEING_REASONS and has_current_user:
            # Paksa asset jadi available — simpan history lalu hapus user
            notif_old = _asset_user_display(db, asset)
            _record_asset_history(db, aid, asset['employee_id'], asset['manual_employee_name'],
                                  asset['started_using'], change_reason)
            emp_id      = None
            manual_name = ''
            new_started = ''
            notif_event = 'released'; notif_reason = change_reason
        else:
            # Deteksi perubahan user biasa → simpan history jika berbeda
            emp_changed = (str(emp_id or '') != str(asset['employee_id'] or '') or
                           manual_name != (asset['manual_employee_name'] or ''))
            if emp_changed:
                reason = change_reason or 'Edit asset'
                notif_old = _asset_user_display(db, asset)
                _record_asset_history(db, aid, asset['employee_id'], asset['manual_employee_name'],
                                      asset['started_using'], reason)
                new_started = today if (emp_id or manual_name) else ''
                notif_event  = 'assigned' if (emp_id or manual_name) else 'released'
                notif_reason = reason
            else:
                new_started = asset['started_using'] or ''

        db.execute(
            'UPDATE ac_assets SET employee_id=?,manual_employee_name=?,started_using=?,status=?,'
            'device_type=?,brand=?,os=?,os_license_type=?,processor=?,ram=?,disk=?,'
            'office_version=?,asset_tag=?,serial_number=?,purchase_date=?,condition=?,notes=?,'
            'updated_at=datetime("now","localtime") WHERE id=?',
            (emp_id, manual_name, new_started, new_status,
             request.form.get('device_type','Laptop'),
             request.form.get('brand',''), request.form.get('os',''), request.form.get('os_license_type',''),
             request.form.get('processor',''), request.form.get('ram',''), request.form.get('disk',''),
             request.form.get('office_version',''), request.form.get('asset_tag',''),
             request.form.get('serial_number',''), request.form.get('purchase_date',''),
             request.form.get('condition','Baik'), request.form.get('notes',''), aid))
        db.execute('DELETE FROM ac_asset_software WHERE asset_id=?', (aid,))
        for s in [x.strip() for x in request.form.get('softwares','').split('\n') if x.strip()]:
            db.execute('INSERT INTO ac_asset_software(asset_id,software_name) VALUES(?,?)', (aid, s))
        db.commit()

        if notif_event:
            if notif_event == 'assigned':
                if emp_id:
                    emp_row = db.execute('SELECT name FROM employees WHERE id=?', (emp_id,)).fetchone()
                    notif_new = emp_row['name'] if emp_row else manual_name
                else:
                    notif_new = manual_name or ''
            _notify_asset_change(db, asset, notif_event, notif_old or '', notif_new or '', notif_reason or '')

        flash('Asset diperbarui.', 'success')
        return redirect(url_for('ac_asset_detail', aid=aid))
    sw_text = '\n'.join(r['software_name'] for r in db.execute('SELECT software_name FROM ac_asset_software WHERE asset_id=? ORDER BY software_name', (aid,)).fetchall())
    return render_template('ac_asset_form.html', asset=asset, employees=employees, sw_text=sw_text)

@app.route('/aset/assets/<int:aid>/end', methods=['POST'])
@login_required
def ac_asset_end(aid):
    """Tandai asset sebagai End (rusak total / hilang). Simpan history user terakhir."""
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_asset_detail', aid=aid))
    db = get_db()
    asset = db.execute('SELECT * FROM ac_assets WHERE id=?', (aid,)).fetchone()
    if not asset: return redirect(url_for('ac_assets'))
    reason = request.form.get('end_reason', 'End').strip() or 'End'
    old_user = _asset_user_display(db, asset)
    _record_asset_history(db, aid, asset['employee_id'], asset['manual_employee_name'],
                          asset['started_using'], reason)
    db.execute(
        'UPDATE ac_assets SET status=?,employee_id=NULL,manual_employee_name=?,started_using=?,'
        'updated_at=datetime("now","localtime") WHERE id=?',
        ('End', asset['manual_employee_name'] or '', '', aid))
    db.commit()
    _notify_asset_change(db, asset, 'end', old_user, '', reason)
    audit_log('end', 'ac_assets', aid, f'Asset ditandai End: {reason}', 'aset')
    flash(f'Asset ditandai sebagai End ({reason}). Riwayat pengguna terakhir tersimpan.', 'warning')
    return redirect(url_for('ac_asset_detail', aid=aid))

@app.route('/aset/assets/<int:aid>/reactivate', methods=['POST'])
@login_required
def ac_asset_reactivate(aid):
    """Reaktifkan asset yang sebelumnya End."""
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_asset_detail', aid=aid))
    db = get_db()
    db.execute('UPDATE ac_assets SET status=?,updated_at=datetime("now","localtime") WHERE id=?',
               ('Aktif', aid))
    db.commit()
    flash('Asset diaktifkan kembali.', 'success')
    return redirect(url_for('ac_asset_detail', aid=aid))

@app.route('/aset/assets/<int:aid>/delete', methods=['POST'])
@login_required
def ac_asset_delete(aid):
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_assets'))
    get_db().execute('DELETE FROM ac_assets WHERE id=?', (aid,)); get_db().commit()
    flash('Asset dihapus.', 'success'); return redirect(url_for('ac_assets'))

# ── Maintenance ───────────────────────────────────────────────────────────────

MAINTENANCE_FREQ_DAYS = {
    'Harian': 1, 'Mingguan': 7, 'Bulanan': 30,
    '3 Bulan': 90, '6 Bulan': 180, 'Tahunan': 365,
}

def _maintenance_next(last_date_str, frequency):
    """Hitung next_maintenance dari last_maintenance + frequency."""
    try:
        from datetime import date as _d, timedelta
        last = _d.fromisoformat(last_date_str)
        delta = timedelta(days=MAINTENANCE_FREQ_DAYS.get(frequency, 30))
        return (last + delta).isoformat()
    except Exception:
        return ''

def _maintenance_status(item, today_str):
    nxt = (item['next_maintenance'] or '').strip()
    if not nxt:
        return 'no_schedule'
    if nxt < today_str:
        return 'overdue'
    from datetime import date as _d, timedelta
    soon = (_d.today() + timedelta(days=30)).isoformat()
    return 'upcoming' if nxt <= soon else 'ok'

@app.route('/aset/maintenance')
@login_required
def ac_maintenance():
    if not ac_require('ac_view'): return redirect(url_for('ac_index'))
    db = get_db()
    from datetime import date as _d
    today = _d.today().isoformat()
    q = request.args.get('q', '')
    cat = request.args.get('cat', '')
    sort = request.args.get('sort', 'schedule')
    sql = "SELECT * FROM ac_maintenance WHERE is_active=1"
    params = []
    if q:
        sql += " AND (title LIKE ? OR location LIKE ? OR vendor LIKE ? OR category LIKE ?)"
        params += [f'%{q}%'] * 4
    if cat:
        sql += " AND category=?"
        params.append(cat)
    if sort == 'updated':
        sql += " ORDER BY COALESCE(NULLIF(updated_at,''),'1970') DESC, id DESC"
    else:
        sql += " ORDER BY CASE WHEN next_maintenance='' THEN '9999' ELSE next_maintenance END ASC"
    items = db.execute(sql, params).fetchall()
    items_with_status = [(r, _maintenance_status(r, today)) for r in items]
    categories = [r[0] for r in db.execute("SELECT DISTINCT category FROM ac_maintenance WHERE category!='' ORDER BY category").fetchall()]
    overdue_count = sum(1 for _, s in items_with_status if s == 'overdue')
    return render_template('ac_maintenance.html', items=items_with_status, q=q, cat=cat, sort=sort,
                           categories=categories, today=today, overdue_count=overdue_count)

@app.route('/aset/maintenance/new', methods=['GET', 'POST'])
@app.route('/aset/maintenance/<int:mid>/edit', methods=['GET', 'POST'])
@login_required
def ac_maintenance_form(mid=None):
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_maintenance'))
    db = get_db()
    item = db.execute('SELECT * FROM ac_maintenance WHERE id=?', (mid,)).fetchone() if mid else None
    if request.method == 'POST':
        title       = request.form.get('title', '').strip()
        category    = request.form.get('category', 'Lainnya').strip()
        location    = request.form.get('location', '').strip()
        description = request.form.get('description', '').strip()
        frequency   = request.form.get('frequency', 'Bulanan').strip()
        last_maint  = request.form.get('last_maintenance', '').strip()
        next_maint  = request.form.get('next_maintenance', '').strip()
        vendor      = request.form.get('vendor', '').strip()
        pic         = request.form.get('pic', '').strip()
        cost_raw    = request.form.get('cost_estimate', '').strip()
        cost_est    = float(cost_raw) if cost_raw else None
        notes       = request.form.get('notes', '').strip()
        if not title:
            flash('Judul wajib diisi.', 'warning')
            return render_template('ac_maintenance_form.html', item=item)
        if not next_maint and last_maint:
            next_maint = _maintenance_next(last_maint, frequency)
        if mid:
            db.execute(
                'UPDATE ac_maintenance SET title=?,category=?,location=?,description=?,frequency=?,'
                'last_maintenance=?,next_maintenance=?,vendor=?,pic=?,cost_estimate=?,notes=?,'
                'updated_at=datetime("now","localtime") WHERE id=?',
                (title, category, location, description, frequency, last_maint, next_maint,
                 vendor, pic, cost_est, notes, mid))
            db.commit()
            flash('Jadwal maintenance diperbarui.', 'success')
        else:
            db.execute(
                'INSERT INTO ac_maintenance(title,category,location,description,frequency,'
                'last_maintenance,next_maintenance,vendor,pic,cost_estimate,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (title, category, location, description, frequency, last_maint, next_maint,
                 vendor, pic, cost_est, notes))
            db.commit()
            flash('Jadwal maintenance ditambahkan.', 'success')
        return redirect(url_for('ac_maintenance'))
    return render_template('ac_maintenance_form.html', item=item)

@app.route('/aset/maintenance/<int:mid>')
@login_required
def ac_maintenance_detail(mid):
    if not ac_require('ac_view'): return redirect(url_for('ac_index'))
    db = get_db()
    from datetime import date as _d
    item = db.execute('SELECT * FROM ac_maintenance WHERE id=?', (mid,)).fetchone()
    if not item: flash('Data tidak ditemukan.', 'danger'); return redirect(url_for('ac_maintenance'))
    logs = db.execute('SELECT * FROM ac_maintenance_log WHERE maintenance_id=? ORDER BY done_at DESC, id DESC', (mid,)).fetchall()
    today = _d.today().isoformat()
    status = _maintenance_status(item, today)
    return render_template('ac_maintenance_detail.html', item=item, logs=logs, status=status, today=today)

@app.route('/aset/maintenance/<int:mid>/done', methods=['POST'])
@login_required
def ac_maintenance_done(mid):
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_maintenance_detail', mid=mid))
    db = get_db()
    item = db.execute('SELECT * FROM ac_maintenance WHERE id=?', (mid,)).fetchone()
    if not item: return redirect(url_for('ac_maintenance'))
    done_at  = request.form.get('done_at', '').strip() or date.today().isoformat()
    done_by  = request.form.get('done_by', '').strip()
    result   = request.form.get('result', 'OK').strip()
    cost_raw = request.form.get('cost', '').strip()
    cost     = float(cost_raw) if cost_raw else None
    notes    = request.form.get('notes', '').strip()
    db.execute(
        'INSERT INTO ac_maintenance_log(maintenance_id,done_at,done_by,cost,result,notes) VALUES(?,?,?,?,?,?)',
        (mid, done_at, done_by, cost, result, notes))
    next_maint = _maintenance_next(done_at, item['frequency'])
    db.execute(
        'UPDATE ac_maintenance SET last_maintenance=?,next_maintenance=?,updated_at=datetime("now","localtime") WHERE id=?',
        (done_at, next_maint, mid))
    db.commit()
    flash(f'Maintenance dicatat. Jadwal berikutnya: {next_maint or "—"}.', 'success')
    return redirect(url_for('ac_maintenance_detail', mid=mid))

@app.route('/aset/maintenance/<int:mid>/delete', methods=['POST'])
@login_required
def ac_maintenance_delete(mid):
    if not ac_require('ac_manage_assets'): return redirect(url_for('ac_maintenance'))
    db = get_db()
    db.execute('DELETE FROM ac_maintenance WHERE id=?', (mid,))
    db.commit()
    flash('Jadwal maintenance dihapus.', 'success')
    return redirect(url_for('ac_maintenance'))

# ── Infrastruktur ─────────────────────────────────────────────────────────────
@app.route('/aset/infra')
@login_required
def ac_infra():
    if not ac_require('ac_view'): return redirect(url_for('ac_index'))
    db = get_db()
    q = request.args.get('q',''); dtype = request.args.get('dtype','')
    sort = request.args.get('sort', 'updated')
    sql = 'SELECT * FROM ac_infrastructure WHERE 1=1'; params = []
    if q:
        sql += ' AND (device_type LIKE ? OR model LIKE ? OR nickname LIKE ? OR serial_number LIKE ?)'; params += [f'%{q}%']*4
    if dtype:
        sql += ' AND device_type=?'; params.append(dtype)
    order = ('COALESCE(NULLIF(updated_at,""),"1970") DESC, id DESC' if sort == 'updated'
             else 'device_type ASC, nickname ASC')
    items = db.execute(sql + ' ORDER BY ' + order, params).fetchall()
    dtypes = [r[0] for r in db.execute("SELECT DISTINCT device_type FROM ac_infrastructure ORDER BY device_type").fetchall()]
    return render_template('ac_infra.html', items=items, q=q, dtype=dtype, dtypes=dtypes, sort=sort)

@app.route('/aset/infra/new', methods=['GET','POST'])
@login_required
def ac_infra_new():
    if not ac_require('ac_manage_infra'): return redirect(url_for('ac_infra'))
    db = get_db()
    if request.method == 'POST':
        db.execute('INSERT INTO ac_infrastructure(device_type,brand,model,description,serial_number,nickname,ups_group,location,status,condition_notes) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (request.form.get('device_type',''), request.form.get('brand',''), request.form.get('model',''),
                    request.form.get('description',''), request.form.get('serial_number',''), request.form.get('nickname',''),
                    request.form.get('ups_group',''), request.form.get('location',''),
                    request.form.get('status','Aktif'), request.form.get('condition_notes','')))
        db.commit(); flash('Perangkat ditambahkan.', 'success')
        return redirect(url_for('ac_infra'))
    return render_template('ac_infra_form.html', item=None)

@app.route('/aset/infra/<int:iid>/edit', methods=['GET','POST'])
@login_required
def ac_infra_edit(iid):
    if not ac_require('ac_manage_infra'): return redirect(url_for('ac_infra'))
    db = get_db()
    item = db.execute('SELECT * FROM ac_infrastructure WHERE id=?', (iid,)).fetchone()
    if not item: flash('Tidak ditemukan.', 'danger'); return redirect(url_for('ac_infra'))
    if request.method == 'POST':
        db.execute('UPDATE ac_infrastructure SET device_type=?,brand=?,model=?,description=?,serial_number=?,nickname=?,ups_group=?,location=?,status=?,condition_notes=?,updated_at=datetime("now","localtime") WHERE id=?',
                   (request.form.get('device_type',''), request.form.get('brand',''), request.form.get('model',''),
                    request.form.get('description',''), request.form.get('serial_number',''), request.form.get('nickname',''),
                    request.form.get('ups_group',''), request.form.get('location',''),
                    request.form.get('status','Aktif'), request.form.get('condition_notes',''), iid))
        db.commit(); flash('Perangkat diperbarui.', 'success')
        return redirect(url_for('ac_infra'))
    return render_template('ac_infra_form.html', item=item)

@app.route('/aset/infra/<int:iid>/delete', methods=['POST'])
@login_required
def ac_infra_delete(iid):
    if not ac_require('ac_manage_infra'): return redirect(url_for('ac_infra'))
    get_db().execute('DELETE FROM ac_infrastructure WHERE id=?', (iid,)); get_db().commit()
    flash('Perangkat dihapus.', 'success'); return redirect(url_for('ac_infra'))

# ── Lisensi ───────────────────────────────────────────────────────────────────
@app.route('/aset/licenses')
@login_required
def ac_licenses():
    if not ac_require('ac_view'): return redirect(url_for('ac_index'))
    db = get_db()
    q = request.args.get('q','')
    sort = request.args.get('sort', 'updated')
    sql = "SELECT l.*, COUNT(la.id) as assigned FROM ac_licenses l LEFT JOIN ac_license_assignments la ON la.license_id=l.id"
    params = []
    if q:
        sql += ' WHERE l.software_name LIKE ?'; params.append(f'%{q}%')
    order = ('COALESCE(NULLIF(l.updated_at,""),"1970") DESC, l.id DESC' if sort == 'updated'
             else 'l.software_name ASC')
    lics = db.execute(sql + ' GROUP BY l.id ORDER BY ' + order, params).fetchall()
    return render_template('ac_licenses.html', licenses=lics, q=q, sort=sort)

@app.route('/aset/licenses/new', methods=['GET','POST'])
@app.route('/aset/licenses/<int:lid>/edit', methods=['GET','POST'])
@login_required
def ac_license_form(lid=None):
    if not ac_require('ac_manage_licenses'): return redirect(url_for('ac_licenses'))
    db = get_db()
    lic = db.execute('SELECT * FROM ac_licenses WHERE id=?', (lid,)).fetchone() if lid else None
    employees = db.execute('SELECT id,name,divisi FROM employees WHERE is_active=1 ORDER BY name').fetchall()
    assignments = [r['employee_id'] for r in db.execute('SELECT employee_id FROM ac_license_assignments WHERE license_id=?', (lid,)).fetchall()] if lid else []
    if request.method == 'POST':
        if lid:
            db.execute('UPDATE ac_licenses SET software_name=?,license_key=?,license_type=?,version=?,year=?,max_seats=?,notes=?,is_active=?,updated_at=datetime("now","localtime") WHERE id=?',
                       (request.form['software_name'].strip(), request.form.get('license_key','').strip(),
                        request.form.get('license_type','Perpetual'), request.form.get('version','').strip(),
                        request.form.get('year') or None, request.form.get('max_seats',1),
                        request.form.get('notes','').strip(), 1 if request.form.get('is_active') else 0, lid))
            db.execute('DELETE FROM ac_license_assignments WHERE license_id=?', (lid,))
        else:
            cur = db.execute('INSERT INTO ac_licenses(software_name,license_key,license_type,version,year,max_seats,notes) VALUES(?,?,?,?,?,?,?)',
                       (request.form['software_name'].strip(), request.form.get('license_key','').strip(),
                        request.form.get('license_type','Perpetual'), request.form.get('version','').strip(),
                        request.form.get('year') or None, request.form.get('max_seats',1), request.form.get('notes','').strip()))
            lid = cur.lastrowid
        for emp_id in request.form.getlist('assigned_employees'):
            if emp_id: db.execute('INSERT INTO ac_license_assignments(license_id,employee_id) VALUES(?,?)', (lid, emp_id))
        db.commit(); flash('Lisensi disimpan.', 'success')
        return redirect(url_for('ac_licenses'))
    return render_template('ac_license_form.html', lic=lic, employees=employees, assignments=assignments)

@app.route('/aset/licenses/<int:lid>/delete', methods=['POST'])
@login_required
def ac_license_delete(lid):
    if not ac_require('ac_manage_licenses'): return redirect(url_for('ac_licenses'))
    get_db().execute('DELETE FROM ac_licenses WHERE id=?', (lid,)); get_db().commit()
    flash('Lisensi dihapus.', 'success'); return redirect(url_for('ac_licenses'))

# ── Subscription ──────────────────────────────────────────────────────────────
@app.route('/aset/subscriptions')
@login_required
def ac_subscriptions():
    if not ac_require('ac_view'): return redirect(url_for('ac_index'))
    from datetime import date as _date, timedelta
    today = _date.today()
    settings = get_settings(get_db())
    reminder_days_raw = settings.get('ac_sub_reminder_days', '30,14,7,1')
    reminder_days = [int(d.strip()) for d in reminder_days_raw.split(',') if d.strip().isdigit()]
    sort = request.args.get('sort', 'updated')
    db = get_db()
    order = ("COALESCE(NULLIF(updated_at,''),'1970') DESC, id DESC" if sort == 'updated'
             else 'is_active DESC, end_date ASC')
    subs = db.execute(f'SELECT * FROM ac_subscriptions ORDER BY {order}').fetchall()
    return render_template('ac_subscriptions.html',
                           subscriptions=subs, sort=sort,
                           today=today.isoformat(),
                           today_plus_7=(today + timedelta(days=7)).isoformat(),
                           today_plus_30=(today + timedelta(days=30)).isoformat(),
                           reminder_days=sorted(reminder_days, reverse=True))

@app.route('/aset/subscriptions/new', methods=['GET','POST'])
@app.route('/aset/subscriptions/<int:sid>/edit', methods=['GET','POST'])
@login_required
def ac_subscription_form(sid=None):
    if not ac_require('ac_manage_subs'): return redirect(url_for('ac_subscriptions'))
    db = get_db()
    sub = db.execute('SELECT * FROM ac_subscriptions WHERE id=?', (sid,)).fetchone() if sid else None
    if request.method == 'POST':
        vals = (request.form['provider'].strip(), request.form.get('category','SaaS'),
                request.form.get('billing_cycle','Monthly'), request.form.get('start_date',''),
                request.form.get('end_date',''), request.form.get('username','').strip(),
                request.form.get('password','').strip(), request.form.get('access_url','').strip(),
                request.form.get('notes','').strip())
        if sid:
            db.execute('UPDATE ac_subscriptions SET provider=?,category=?,billing_cycle=?,start_date=?,end_date=?,username=?,password=?,access_url=?,notes=?,is_active=?,updated_at=datetime("now","localtime") WHERE id=?',
                       vals + (1 if request.form.get('is_active') else 0, sid))
        else:
            db.execute('INSERT INTO ac_subscriptions(provider,category,billing_cycle,start_date,end_date,username,password,access_url,notes) VALUES(?,?,?,?,?,?,?,?,?)', vals)
        db.commit(); flash('Subscription disimpan.', 'success')
        return redirect(url_for('ac_subscriptions'))
    return render_template('ac_subscription_form.html', sub=sub)

@app.route('/aset/subscriptions/<int:sid>/delete', methods=['POST'])
@login_required
def ac_subscription_delete(sid):
    if not ac_require('ac_manage_subs'): return redirect(url_for('ac_subscriptions'))
    get_db().execute('DELETE FROM ac_subscriptions WHERE id=?', (sid,)); get_db().commit()
    flash('Subscription dihapus.', 'success'); return redirect(url_for('ac_subscriptions'))

@app.route('/aset/subscriptions/remind', methods=['POST'])
@login_required
def ac_subscription_remind():
    """Trigger manual reminder untuk semua subscription yang mendekati expired."""
    if not ac_require('ac_manage_subs'): return redirect(url_for('ac_subscriptions'))
    sent, failed = run_subscription_reminders(triggered_by='manual')
    if sent == 0 and failed == 0:
        flash('Tidak ada subscription yang perlu diingatkan (belum masuk periode reminder).', 'info')
    else:
        flash(f'Reminder terkirim: {sent} sukses, {failed} gagal.', 'success' if failed == 0 else 'warning')
    return redirect(url_for('ac_subscriptions'))

@app.route('/aset/subscriptions/<int:sid>/remind', methods=['POST'])
@login_required
def ac_subscription_remind_one(sid):
    """Trigger reminder manual untuk satu subscription tertentu."""
    if not ac_require('ac_manage_subs'): return redirect(url_for('ac_subscriptions'))
    db = get_db()
    sub = db.execute('SELECT * FROM ac_subscriptions WHERE id=?', (sid,)).fetchone()
    if not sub:
        flash('Subscription tidak ditemukan.', 'danger')
        return redirect(url_for('ac_subscriptions'))
    settings = get_settings(db)
    try:
        days_left = (date.fromisoformat(sub['end_date']) - date.today()).days if sub['end_date'] else 999
    except Exception:
        days_left = 999
    sent = failed = 0
    subj   = f"[AssetCore] Reminder {sub['provider']} — {'berakhir dalam '+str(days_left)+' hari' if days_left > 0 else 'BERAKHIR HARI INI'}"
    html   = compose_sub_email(sub, days_left)
    tg_msg = compose_sub_telegram(sub, days_left)
    wa_msg = compose_sub_wa(sub, days_left)
    bot_token  = settings.get('telegram_bot_token', '').strip()
    wa_url     = settings.get('openwa_url', '').strip()
    wa_key     = settings.get('openwa_api_key', '').strip()
    wa_session = get_openwa_session(settings, 'aset')
    wa_enabled = settings.get('openwa_enabled', '0') == '1'
    ac_emails  = get_ac_notification_emails(settings)
    ac_tg_ids  = get_ac_notification_telegram_ids(settings)
    ac_phones  = get_ac_notification_wa_phones(settings)
    if settings.get('smtp_host', '').strip() and ac_emails:
        for to_email in ac_emails:
            ok, err = send_email(settings, to_email, subj, html)
            audit_notif('email', to_email, subj, html, ok, err, 'manual', app_slug='aset')
            if ok: sent += 1
            else:  failed += 1
    if bot_token and ac_tg_ids:
        for chat_id in ac_tg_ids:
            ok, err = send_telegram(bot_token, chat_id, tg_msg, _log_subject=subj, _app_slug='aset')
            if ok: sent += 1
            else:  failed += 1
    if wa_enabled and wa_url and ac_phones:
        for phone in ac_phones:
            ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone, wa_msg)
            audit_notif('whatsapp', phone, subj, wa_msg, ok, err, 'manual', app_slug='aset')
            if ok: sent += 1
            else:  failed += 1
    db.execute("UPDATE ac_subscriptions SET last_reminder_sent=? WHERE id=?",
               (date.today().isoformat(), sid))
    db.commit()
    if sent == 0 and failed == 0:
        flash(f'Tidak ada penerima notifikasi yang dikonfigurasi. Isi dulu di Pengaturan → Notifikasi.', 'warning')
    else:
        flash(f'Reminder "{sub["provider"]}": {sent} terkirim, {failed} gagal.', 'success' if failed == 0 else 'warning')
    return redirect(url_for('ac_subscriptions'))

# ── AssetCore Settings ────────────────────────────────────────────────────────
AC_SETTINGS_KEYS = [
    'ac_sub_reminder_enabled', 'ac_sub_reminder_days',
    'ac_notification_emails', 'ac_notification_telegram_ids', 'ac_notification_wa_phones',
]

@app.route('/aset/settings', methods=['GET', 'POST'])
@login_required
def ac_settings():
    if not ac_require('ac_manage_subs'): return redirect(url_for('ac_index'))
    db = get_db()
    if request.method == 'POST':
        for k in AC_SETTINGS_KEYS:
            if k == 'ac_sub_reminder_enabled':
                v = '1' if request.form.get(k) else '0'
            else:
                v = request.form.get(k, '').strip()
            save_setting(db, k, v)
        db.commit()
        flash('Pengaturan notifikasi AssetCore disimpan.', 'success')
        return redirect(url_for('ac_settings'))
    cfg = get_settings(db)
    return render_template('ac_settings.html', cfg=cfg)

@app.route('/aset/settings/test-email', methods=['POST'])
@login_required
def ac_settings_test_email():
    if not ac_require('ac_manage_subs'): return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    db = get_db()
    settings = get_settings(db)
    to_email = request.form.get('test_email', '').strip()
    if not to_email:
        return jsonify({'ok': False, 'msg': 'Masukkan alamat email tujuan'})
    if not settings.get('smtp_host', '').strip():
        return jsonify({'ok': False, 'msg': 'SMTP belum dikonfigurasi di Pengaturan Sistem Portal'})
    ok, err = send_email(settings, to_email, '[AssetCore] Test Email Notifikasi',
                         '<h3>✅ Test berhasil!</h3><p>Konfigurasi notifikasi email AssetCore sudah benar.</p>')
    return jsonify({'ok': ok, 'msg': 'Email berhasil dikirim' if ok else str(err)})

@app.route('/aset/settings/test-telegram', methods=['POST'])
@login_required
def ac_settings_test_telegram():
    if not ac_require('ac_manage_subs'): return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    db = get_db()
    settings = get_settings(db)
    bot_token = settings.get('telegram_bot_token', '').strip()
    chat_id   = request.form.get('test_chat_id', '').strip()
    if not bot_token:
        return jsonify({'ok': False, 'msg': 'Bot Token belum dikonfigurasi di Pengaturan Sistem Portal'})
    if not chat_id:
        return jsonify({'ok': False, 'msg': 'Masukkan Chat ID tujuan test'})
    ok, err = send_telegram(bot_token, normalize_telegram_id(chat_id),
                            '✅ <b>Test berhasil!</b>\n\nNotifikasi Telegram AssetCore sudah terhubung.',
                            _app_slug='aset')
    return jsonify({'ok': ok, 'msg': 'Pesan Telegram berhasil dikirim' if ok else str(err)})

@app.route('/aset/settings/test-whatsapp', methods=['POST'])
@login_required
def ac_settings_test_whatsapp():
    if not ac_require('ac_manage_subs'): return jsonify({'ok': False, 'msg': 'Akses ditolak'})
    db = get_db()
    settings = get_settings(db)
    wa_url     = settings.get('openwa_url', '').strip()
    wa_key     = settings.get('openwa_api_key', '').strip()
    wa_session = get_openwa_session(settings, 'aset')
    phone      = request.form.get('test_wa_phone', '').strip()
    if not settings.get('openwa_enabled', '0') == '1' or not wa_url:
        return jsonify({'ok': False, 'msg': 'WhatsApp (OpenWA) belum diaktifkan di Pengaturan Sistem Portal'})
    if not phone:
        return jsonify({'ok': False, 'msg': 'Masukkan nomor HP tujuan test'})
    ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone,
                            '✅ *Test berhasil!*\n\nNotifikasi WhatsApp AssetCore sudah terhubung.')
    return jsonify({'ok': ok, 'msg': f'Pesan terkirim ke {normalize_phone_wa(phone)}' if ok else str(err)})

# ── Software Requests ─────────────────────────────────────────────────────────
@app.route('/aset/requests')
@login_required
def ac_requests():
    if not ac_require('ac_view'): return redirect(url_for('ac_index'))
    db = get_db()
    status_filter = request.args.get('status','')
    sort = request.args.get('sort', 'updated')
    sql = "SELECT r.*, e.name as emp_name, e.divisi FROM ac_software_requests r LEFT JOIN employees e ON r.employee_id=e.id WHERE 1=1"
    params = []
    if status_filter:
        sql += ' AND r.status=?'; params.append(status_filter)
    order = ("COALESCE(NULLIF(r.updated_at,''),r.requested_at) DESC, r.id DESC" if sort == 'updated'
             else 'r.requested_at DESC')
    reqs = db.execute(sql + ' ORDER BY ' + order, params).fetchall()
    return render_template('ac_requests.html', requests=reqs, status_filter=status_filter, sort=sort)

@app.route('/aset/requests/new', methods=['GET','POST'])
@login_required
def ac_request_new():
    if not ac_require('ac_manage_requests'): return redirect(url_for('ac_requests'))
    db = get_db()
    employees = db.execute('SELECT id,name,divisi FROM employees WHERE is_active=1 ORDER BY name').fetchall()
    if request.method == 'POST':
        db.execute('INSERT INTO ac_software_requests(employee_id,software_name,version,reason) VALUES(?,?,?,?)',
                   (request.form.get('employee_id') or None, request.form['software_name'].strip(),
                    request.form.get('version','').strip(), request.form.get('reason','').strip()))
        db.commit(); flash('Request ditambahkan.', 'success')
        return redirect(url_for('ac_requests'))
    return render_template('ac_request_form.html', employees=employees)

@app.route('/aset/requests/<int:rid>/status', methods=['POST'])
@login_required
def ac_request_status(rid):
    if not ac_require('ac_manage_requests'): return redirect(url_for('ac_requests'))
    from datetime import datetime as _dt
    db = get_db()
    new_status = request.form.get('status')
    resolved_at = _dt.now().strftime('%Y-%m-%d %H:%M:%S') if new_status in ('Approved','Rejected','Installed') else ''
    db.execute('UPDATE ac_software_requests SET status=?,notes=?,resolved_at=?,resolved_by=?,updated_at=datetime("now","localtime") WHERE id=?',
               (new_status, request.form.get('notes','').strip(), resolved_at, session.get('user_name',''), rid))
    db.commit(); flash(f'Status diubah ke {new_status}.', 'success')
    return redirect(url_for('ac_requests'))

@app.route('/aset/requests/<int:rid>/delete', methods=['POST'])
@login_required
def ac_request_delete(rid):
    if not ac_require('ac_manage_requests'): return redirect(url_for('ac_requests'))
    get_db().execute('DELETE FROM ac_software_requests WHERE id=?', (rid,)); get_db().commit()
    flash('Request dihapus.', 'success'); return redirect(url_for('ac_requests'))

# ─── ProjectCore ───────────────────────────────────────────────────────────────

def _pc_next_issue_no(db, project_id):
    last = db.execute(
        "SELECT issue_no FROM pc_issues WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
    ).fetchone()
    proj = db.execute("SELECT code FROM pc_projects WHERE id=?", (project_id,)).fetchone()
    prefix = proj['code'].upper() if proj else 'PC'
    if not last:
        return f"{prefix}-001"
    try:
        n = int(last['issue_no'].rsplit('-', 1)[-1]) + 1
    except Exception:
        n = 1
    return f"{prefix}-{n:03d}"

def _pc_log_issue(db, issue_id, action, old_val, new_val, notes=''):
    uid = session.get('user_id')
    db.execute(
        "INSERT INTO pc_issue_history(issue_id,action,old_value,new_value,changed_by,notes) VALUES(?,?,?,?,?,?)",
        (issue_id, action, old_val or '', new_val or '', uid, notes)
    )

def _pc_members(db, project_id):
    rows = db.execute(
        '''SELECT pm.id, pm.role, pm.name_ext,
                  e.id as emp_id, e.name, e.jabatan, e.divisi
           FROM pc_members pm
           LEFT JOIN employees e ON e.id=pm.employee_id
           WHERE pm.project_id=? ORDER BY pm.role, e.name, pm.name_ext''', (project_id,)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if not d['name'] and d['name_ext']:
            d['name'] = d['name_ext']
            d['jabatan'] = 'Eksternal'
        result.append(d)
    return result

@app.route('/project/')
@app.route('/project')
@login_required
def pc_index():
    db = get_db()
    projects = db.execute(
        '''SELECT p.*, e.name as pic_name,
                  (SELECT COUNT(*) FROM pc_issues WHERE project_id=p.id) as total_issues,
                  (SELECT COUNT(*) FROM pc_issues WHERE project_id=p.id AND status_programmer='Done') as done_issues,
                  (SELECT COUNT(*) FROM pc_tasks WHERE project_id=p.id) as total_tasks,
                  (SELECT COUNT(*) FROM pc_tasks WHERE project_id=p.id AND status='done') as done_tasks
           FROM pc_projects p
           LEFT JOIN employees e ON e.id=p.pic_id
           WHERE p.status != 'archived' AND p.deleted_at IS NULL
           ORDER BY p.created_at DESC'''
    ).fetchall()
    counts = {
        'projects': len(projects),
        'open_issues': db.execute(
            "SELECT COUNT(*) FROM pc_issues WHERE status_programmer NOT IN ('Done','Hold')"
        ).fetchone()[0],
        'pending_tasks': db.execute(
            "SELECT COUNT(*) FROM pc_tasks WHERE status NOT IN ('done')"
        ).fetchone()[0],
        'milestones': db.execute(
            "SELECT COUNT(*) FROM pc_milestones WHERE status='in_progress'"
        ).fetchone()[0],
    }
    return render_template('pc_index.html', projects=projects, counts=counts)

@app.route('/project/projects')
@login_required
def pc_projects():
    db       = get_db()
    show_del = request.args.get('show') == 'deleted'
    if show_del:
        rows = db.execute(
            '''SELECT p.*, e.name as pic_name, c.name as customer_name,
                      (SELECT COUNT(*) FROM pc_issues WHERE project_id=p.id) as total_issues,
                      (SELECT COUNT(*) FROM pc_issues WHERE project_id=p.id AND status_programmer NOT IN ('Done','Hold')) as open_issues
               FROM pc_projects p
               LEFT JOIN employees e ON e.id=p.pic_id
               LEFT JOIN sc_customers c ON c.id=p.customer_id
               WHERE p.deleted_at IS NOT NULL
               ORDER BY p.deleted_at DESC'''
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT p.*, e.name as pic_name, c.name as customer_name,
                      (SELECT COUNT(*) FROM pc_issues WHERE project_id=p.id) as total_issues,
                      (SELECT COUNT(*) FROM pc_issues WHERE project_id=p.id AND status_programmer NOT IN ('Done','Hold')) as open_issues
               FROM pc_projects p
               LEFT JOIN employees e ON e.id=p.pic_id
               LEFT JOIN sc_customers c ON c.id=p.customer_id
               WHERE p.deleted_at IS NULL
               ORDER BY p.status, p.created_at DESC'''
        ).fetchall()
    deleted_count = db.execute(
        "SELECT COUNT(*) FROM pc_projects WHERE deleted_at IS NOT NULL"
    ).fetchone()[0]
    return render_template('pc_projects.html', rows=rows, show_deleted=show_del, deleted_count=deleted_count)

def _pc_save_team_members(db, pid, programmers, prog_exts, testers, test_exts):
    """Replace programmer dan QC/tester members untuk project pid.
    programmers/testers: list of employee_id (int string) or '' if external
    prog_exts/test_exts: list of external name strings
    """
    db.execute("DELETE FROM pc_members WHERE project_id=? AND role IN ('programmer','qc_tester')", (pid,))
    for eid, ext in zip(programmers, prog_exts):
        eid = eid.strip(); ext = ext.strip()
        if eid:
            try:
                db.execute("INSERT INTO pc_members(project_id,employee_id,name_ext,role) VALUES(?,?,?,'programmer')",
                           (pid, int(eid), ''))
            except Exception:
                pass
        elif ext:
            db.execute("INSERT INTO pc_members(project_id,employee_id,name_ext,role) VALUES(?,NULL,?,'programmer')",
                       (pid, ext))
    for eid, ext in zip(testers, test_exts):
        eid = eid.strip(); ext = ext.strip()
        if eid:
            try:
                db.execute("INSERT INTO pc_members(project_id,employee_id,name_ext,role) VALUES(?,?,?,'qc_tester')",
                           (pid, int(eid), ''))
            except Exception:
                pass
        elif ext:
            db.execute("INSERT INTO pc_members(project_id,employee_id,name_ext,role) VALUES(?,NULL,?,'qc_tester')",
                       (pid, ext))

@app.route('/project/projects/add', methods=['GET','POST'])
@login_required
def pc_project_add():
    db        = get_db()
    emps      = db.execute("SELECT id, name, jabatan FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    customers = db.execute("SELECT id, code, name FROM sc_customers WHERE is_active=1 ORDER BY name").fetchall()
    if request.method == 'POST':
        code = request.form.get('code','').strip().upper()
        name = request.form.get('name','').strip()
        if not code or not name:
            flash('Kode dan nama wajib diisi', 'danger')
            return render_template('pc_project_form.html', emps=emps, customers=customers, colors=PC_PROJECT_COLORS, r={}, members_prog=[], members_test=[])
        if db.execute("SELECT id FROM pc_projects WHERE code=?", (code,)).fetchone():
            flash(f'Kode proyek {code} sudah dipakai', 'danger')
            return render_template('pc_project_form.html', emps=emps, customers=customers, colors=PC_PROJECT_COLORS, r={}, members_prog=[], members_test=[])
        cur = db.execute(
            '''INSERT INTO pc_projects(code,name,customer_id,description,status,start_date,end_date,
               pic_id,pic_ext,implementor_id,implementor_ext,co_leader_id,co_leader_ext,color,created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (code, name,
             request.form.get('customer_id') or None,
             request.form.get('description','').strip(),
             request.form.get('status','active'),
             request.form.get('start_date') or None,
             request.form.get('end_date') or None,
             request.form.get('pic_id') or None,
             request.form.get('pic_ext','').strip(),
             request.form.get('implementor_id') or None,
             request.form.get('implementor_ext','').strip(),
             request.form.get('co_leader_id') or None,
             request.form.get('co_leader_ext','').strip(),
             request.form.get('color','#0ea5e9'),
             session.get('user_id'))
        )
        pid = cur.lastrowid
        _pc_save_team_members(db, pid,
                              request.form.getlist('programmers'),
                              request.form.getlist('prog_exts'),
                              request.form.getlist('testers'),
                              request.form.getlist('test_exts'))
        db.commit()
        flash(f'Proyek {name} berhasil dibuat', 'success')
        return redirect(url_for('pc_projects'))
    return render_template('pc_project_form.html', emps=emps, customers=customers,
                           colors=PC_PROJECT_COLORS, r={}, members_prog=[], members_test=[])

@app.route('/project/projects/<int:pid>/edit', methods=['GET','POST'])
@login_required
def pc_project_edit(pid):
    db        = get_db()
    proj      = db.execute("SELECT * FROM pc_projects WHERE id=?", (pid,)).fetchone()
    if not proj: abort(404)
    emps      = db.execute("SELECT id, name, jabatan FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    customers = db.execute("SELECT id, code, name FROM sc_customers WHERE is_active=1 ORDER BY name").fetchall()
    if request.method == 'POST':
        db.execute(
            '''UPDATE pc_projects SET name=?,customer_id=?,description=?,status=?,
               start_date=?,end_date=?,pic_id=?,pic_ext=?,
               implementor_id=?,implementor_ext=?,co_leader_id=?,co_leader_ext=?,color=? WHERE id=?''',
            (request.form.get('name','').strip(),
             request.form.get('customer_id') or None,
             request.form.get('description','').strip(),
             request.form.get('status','active'),
             request.form.get('start_date') or None,
             request.form.get('end_date') or None,
             request.form.get('pic_id') or None,
             request.form.get('pic_ext','').strip(),
             request.form.get('implementor_id') or None,
             request.form.get('implementor_ext','').strip(),
             request.form.get('co_leader_id') or None,
             request.form.get('co_leader_ext','').strip(),
             request.form.get('color','#0ea5e9'),
             pid)
        )
        _pc_save_team_members(db, pid,
                              request.form.getlist('programmers'),
                              request.form.getlist('prog_exts'),
                              request.form.getlist('testers'),
                              request.form.getlist('test_exts'))
        db.commit()
        flash('Proyek diperbarui', 'success')
        return redirect(url_for('pc_project_detail', pid=pid))
    members_prog = db.execute(
        "SELECT employee_id, name_ext FROM pc_members WHERE project_id=? AND role='programmer'", (pid,)).fetchall()
    members_test = db.execute(
        "SELECT employee_id, name_ext FROM pc_members WHERE project_id=? AND role='qc_tester'", (pid,)).fetchall()
    return render_template('pc_project_form.html', emps=emps, customers=customers,
                           colors=PC_PROJECT_COLORS, r=dict(proj),
                           members_prog=members_prog, members_test=members_test)

@app.route('/project/projects/<int:pid>')
@login_required
def pc_project_detail(pid):
    db   = get_db()
    proj_row = db.execute(
        '''SELECT p.*, c.name as customer_name,
                  COALESCE(pm.name, p.pic_ext)        as pic_name,
                  COALESCE(im.name, p.implementor_ext) as implementor_name,
                  COALESCE(cl.name, p.co_leader_ext)   as co_leader_name
           FROM pc_projects p
           LEFT JOIN sc_customers c ON c.id=p.customer_id
           LEFT JOIN employees pm ON pm.id=p.pic_id
           LEFT JOIN employees im ON im.id=p.implementor_id
           LEFT JOIN employees cl ON cl.id=p.co_leader_id
           WHERE p.id=?''', (pid,)
    ).fetchone()
    proj = dict(proj_row) if proj_row else None
    if not proj: abort(404)
    issues = db.execute(
        '''SELECT i.*, ep.name as pic_prog_name, et.name as pic_test_name
           FROM pc_issues i
           LEFT JOIN employees ep ON ep.id=i.pic_programmer_id
           LEFT JOIN employees et ON et.id=i.pic_tester_id
           WHERE i.project_id=? ORDER BY i.id DESC''', (pid,)
    ).fetchall()
    _raw_tasks = db.execute(
        '''SELECT t.*, GROUP_CONCAT(e.name, ', ') as assignees
           FROM pc_tasks t
           LEFT JOIN pc_task_assignees ta ON ta.task_id=t.id
           LEFT JOIN employees e ON e.id=ta.employee_id
           WHERE t.project_id=? GROUP BY t.id ORDER BY t.sort_order, t.id''', (pid,)
    ).fetchall()
    _ext_map = {}
    for row in db.execute(
        '''SELECT ea.task_id, GROUP_CONCAT(ea.name, ', ') as ext_names
           FROM pc_task_external_assignees ea
           JOIN pc_tasks t ON t.id=ea.task_id
           WHERE t.project_id=? GROUP BY ea.task_id''', (pid,)
    ).fetchall():
        _ext_map[row['task_id']] = row['ext_names']
    tasks = []
    for t in _raw_tasks:
        t = dict(t)
        parts = [p for p in [t.get('assignees'), _ext_map.get(t['id'])] if p]
        t['assignees'] = ', '.join(parts) if parts else ''
        tasks.append(t)
    milestones = db.execute(
        "SELECT * FROM pc_milestones WHERE project_id=? ORDER BY due_date, sort_order", (pid,)
    ).fetchall()
    proposed = db.execute(
        "SELECT * FROM pc_proposed_changes WHERE project_id=? ORDER BY id", (pid,)
    ).fetchall()
    phases = db.execute(
        '''SELECT ph.*, COALESCE(e.name, ph.pic_ext) as pic_name,
                  a.name as app_name, m.name as module_name
           FROM pc_phases ph
           LEFT JOIN employees e   ON e.id=ph.pic_id
           LEFT JOIN sc_apps a     ON a.id=ph.app_id
           LEFT JOIN sc_modules m  ON m.id=ph.module_id
           WHERE ph.project_id=? ORDER BY ph.sort_order, ph.id''', (pid,)
    ).fetchall()
    sc_apps_list = db.execute(
        "SELECT id, name FROM sc_apps WHERE is_active=1 ORDER BY name"
    ).fetchall()
    sc_modules_list = db.execute(
        "SELECT id, app_id, name FROM sc_modules WHERE is_active=1 ORDER BY app_id, name"
    ).fetchall()
    members  = _pc_members(db, pid)
    emps     = db.execute("SELECT id, name, jabatan FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    stats = {
        'total_issues': len(issues),
        'open_issues':  sum(1 for i in issues if i['status_programmer'] not in ('Done','Hold')),
        'done_issues':  sum(1 for i in issues if i['status_programmer'] == 'Done'),
        'total_tasks':  len(tasks),
        'done_tasks':   sum(1 for t in tasks if t['status'] == 'done'),
        'total_phases': len(phases),
        'done_phases':  sum(1 for p in phases if p['status'] == 'done'),
    }
    kanban = {s: [t for t in tasks if t['status'] == s] for s, _, _ in PC_TASK_STATUSES}
    return render_template('pc_project_detail.html',
        proj=proj, issues=issues, tasks=tasks, milestones=milestones,
        proposed=proposed, phases=phases, members=members, emps=emps,
        stats=stats, kanban=kanban,
        task_statuses=PC_TASK_STATUSES, milestone_statuses=PC_MILESTONE_STATUSES,
        proposed_statuses=PC_PROPOSED_STATUSES,
        phase_types=PC_PHASE_TYPES, phase_statuses=PC_PHASE_STATUSES,
        sc_apps_list=sc_apps_list, sc_modules_list=sc_modules_list,
        priorities=PC_PRIORITIES, difficulties=PC_DIFFICULTIES)

@app.route('/project/projects/<int:pid>/delete', methods=['POST'])
@login_required
def pc_project_delete(pid):
    db = get_db()
    p  = db.execute("SELECT id FROM pc_projects WHERE id=? AND deleted_at IS NULL", (pid,)).fetchone()
    if not p: abort(404)
    db.execute("UPDATE pc_projects SET deleted_at=datetime('now','localtime') WHERE id=?", (pid,))
    db.commit()
    flash('Proyek dipindahkan ke tempat sampah. Bisa dipulihkan dari daftar proyek dihapus.', 'warning')
    return redirect(url_for('pc_projects'))

@app.route('/project/projects/<int:pid>/restore', methods=['POST'])
@login_required
def pc_project_restore(pid):
    db = get_db()
    p  = db.execute("SELECT id FROM pc_projects WHERE id=? AND deleted_at IS NOT NULL", (pid,)).fetchone()
    if not p: abort(404)
    db.execute("UPDATE pc_projects SET deleted_at=NULL WHERE id=?", (pid,))
    db.commit()
    flash('Proyek berhasil dipulihkan.', 'success')
    return redirect(url_for('pc_project_detail', pid=pid))

@app.route('/project/projects/<int:pid>/members', methods=['POST'])
@login_required
def pc_member_add(pid):
    db  = get_db()
    eid = request.form.get('employee_id')
    role= request.form.get('role','developer')
    if eid:
        try:
            db.execute("INSERT OR IGNORE INTO pc_members(project_id,employee_id,role) VALUES(?,?,?)", (pid, eid, role))
            db.commit()
        except Exception:
            pass
    return redirect(url_for('pc_project_detail', pid=pid) + '#members')

@app.route('/project/projects/<int:pid>/members/<int:eid>/delete', methods=['POST'])
@login_required
def pc_member_delete(pid, eid):
    db = get_db()
    db.execute("DELETE FROM pc_members WHERE project_id=? AND employee_id=?", (pid, eid))
    db.commit()
    return redirect(url_for('pc_project_detail', pid=pid) + '#members')

# ── Issues ─────────────────────────────────────────────────────────────────────

@app.route('/project/projects/<int:pid>/issues/add', methods=['GET','POST'])
@login_required
def pc_issue_add(pid):
    db   = get_db()
    proj = db.execute("SELECT * FROM pc_projects WHERE id=?", (pid,)).fetchone()
    if not proj: abort(404)
    emps = db.execute("SELECT id, name, jabatan FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    if request.method == 'POST':
        issue_no = _pc_next_issue_no(db, pid)
        db.execute(
            '''INSERT INTO pc_issues
               (project_id,issue_no,title,description,role,menu,stage,solution_type,
                priority,severity,difficulty,issued_type,issued_date,issued_by,
                pic_programmer_id,pic_tester_id,md_days,plan_hours,
                status_programmer,notes,redmine)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (pid, issue_no,
             request.form.get('title','').strip(),
             request.form.get('description','').strip(),
             request.form.get('role','').strip(),
             request.form.get('menu','').strip(),
             request.form.get('stage','').strip(),
             request.form.get('solution_type',''),
             request.form.get('priority','Medium'),
             request.form.get('severity','Medium'),
             request.form.get('difficulty','Normal'),
             request.form.get('issued_type','Bugs'),
             request.form.get('issued_date') or None,
             request.form.get('issued_by','').strip(),
             request.form.get('pic_programmer_id') or None,
             request.form.get('pic_tester_id') or None,
             request.form.get('md_days') or None,
             request.form.get('plan_hours') or None,
             'New',
             request.form.get('notes','').strip(),
             request.form.get('redmine','').strip())
        )
        iid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        _pc_log_issue(db, iid, 'created', '', 'New', f'Issue {issue_no} dibuat')
        db.commit()
        flash(f'Issue {issue_no} berhasil ditambahkan', 'success')
        return redirect(url_for('pc_project_detail', pid=pid) + '#issues')
    return render_template('pc_issue_form.html', proj=proj, emps=emps,
        priorities=PC_PRIORITIES, severities=PC_SEVERITIES,
        difficulties=PC_DIFFICULTIES, issued_types=PC_ISSUED_TYPES,
        solution_types=PC_SOLUTION_TYPES, r={})

@app.route('/project/issues/<int:iid>')
@login_required
def pc_issue_detail(iid):
    db    = get_db()
    issue = db.execute(
        '''SELECT i.*, p.name as project_name, p.id as project_id, p.code as project_code,
                  ep.name as pic_prog_name, et.name as pic_test_name
           FROM pc_issues i
           JOIN pc_projects p ON p.id=i.project_id
           LEFT JOIN employees ep ON ep.id=i.pic_programmer_id
           LEFT JOIN employees et ON et.id=i.pic_tester_id
           WHERE i.id=?''', (iid,)
    ).fetchone()
    if not issue: abort(404)
    history = db.execute(
        '''SELECT h.*, u.username as changed_by_name
           FROM pc_issue_history h LEFT JOIN users u ON u.id=h.changed_by
           WHERE h.issue_id=? ORDER BY h.id DESC''', (iid,)
    ).fetchall()
    emps = db.execute("SELECT id, name, jabatan FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    return render_template('pc_issue_detail.html', issue=issue, history=history, emps=emps,
        prg_statuses=PC_ISSUE_STATUSES_PRG, test_statuses=PC_ISSUE_STATUSES_TEST,
        priorities=PC_PRIORITIES, severities=PC_SEVERITIES,
        difficulties=PC_DIFFICULTIES, solution_types=PC_SOLUTION_TYPES)

@app.route('/project/issues/<int:iid>/edit', methods=['GET','POST'])
@login_required
def pc_issue_edit(iid):
    db    = get_db()
    issue = db.execute("SELECT * FROM pc_issues WHERE id=?", (iid,)).fetchone()
    if not issue: abort(404)
    proj  = db.execute("SELECT * FROM pc_projects WHERE id=?", (issue['project_id'],)).fetchone()
    emps  = db.execute("SELECT id, name, jabatan FROM employees WHERE is_active=1 ORDER BY name").fetchall()
    if request.method == 'POST':
        old_status = issue['status_programmer']
        new_status = request.form.get('status_programmer', old_status)
        db.execute(
            '''UPDATE pc_issues SET title=?,description=?,role=?,menu=?,stage=?,solution_type=?,
               priority=?,severity=?,difficulty=?,issued_type=?,issued_date=?,issued_by=?,
               pic_programmer_id=?,pic_tester_id=?,md_days=?,plan_hours=?,
               status_programmer=?,status_testing=?,testing_date=?,resolved_date=?,
               notes=?,redmine=? WHERE id=?''',
            (request.form.get('title','').strip(),
             request.form.get('description','').strip(),
             request.form.get('role','').strip(),
             request.form.get('menu','').strip(),
             request.form.get('stage','').strip(),
             request.form.get('solution_type',''),
             request.form.get('priority','Medium'),
             request.form.get('severity','Medium'),
             request.form.get('difficulty','Normal'),
             request.form.get('issued_type','Bugs'),
             request.form.get('issued_date') or None,
             request.form.get('issued_by','').strip(),
             request.form.get('pic_programmer_id') or None,
             request.form.get('pic_tester_id') or None,
             request.form.get('md_days') or None,
             request.form.get('plan_hours') or None,
             new_status,
             request.form.get('status_testing',''),
             request.form.get('testing_date') or None,
             request.form.get('resolved_date') or None,
             request.form.get('notes','').strip(),
             request.form.get('redmine','').strip(),
             iid)
        )
        if old_status != new_status:
            _pc_log_issue(db, iid, 'status_change', old_status, new_status)
        else:
            _pc_log_issue(db, iid, 'update', '', '', 'Data issue diperbarui')
        db.commit()
        flash('Issue diperbarui', 'success')
        return redirect(url_for('pc_issue_detail', iid=iid))
    return render_template('pc_issue_form.html', proj=proj, emps=emps,
        priorities=PC_PRIORITIES, severities=PC_SEVERITIES,
        difficulties=PC_DIFFICULTIES, issued_types=PC_ISSUED_TYPES,
        solution_types=PC_SOLUTION_TYPES, r=issue, is_edit=True)

@app.route('/project/issues/<int:iid>/status', methods=['POST'])
@login_required
def pc_issue_status(iid):
    db    = get_db()
    issue = db.execute("SELECT * FROM pc_issues WHERE id=?", (iid,)).fetchone()
    if not issue: abort(404)
    new_prg  = request.form.get('status_programmer', issue['status_programmer'])
    new_test = request.form.get('status_testing', issue['status_testing'] or '')
    notes    = request.form.get('notes','').strip()
    if new_prg != issue['status_programmer']:
        _pc_log_issue(db, iid, 'status_change', issue['status_programmer'], new_prg, notes)
    resolved_date = issue['resolved_date']
    if new_prg == 'Done' and not resolved_date:
        resolved_date = datetime.now().strftime('%Y-%m-%d')
    db.execute(
        "UPDATE pc_issues SET status_programmer=?,status_testing=?,resolved_date=? WHERE id=?",
        (new_prg, new_test, resolved_date, iid)
    )
    db.commit()
    flash('Status diperbarui', 'success')
    return redirect(url_for('pc_issue_detail', iid=iid))

@app.route('/project/issues/<int:iid>/delete', methods=['POST'])
@login_required
def pc_issue_delete(iid):
    db    = get_db()
    issue = db.execute("SELECT * FROM pc_issues WHERE id=?", (iid,)).fetchone()
    if not issue: abort(404)
    pid   = issue['project_id']
    db.execute("DELETE FROM pc_issues WHERE id=?", (iid,))
    db.commit()
    flash('Issue dihapus', 'warning')
    return redirect(url_for('pc_project_detail', pid=pid) + '#issues')

# ── Tasks / Kanban ─────────────────────────────────────────────────────────────

@app.route('/project/projects/<int:pid>/tasks/add', methods=['POST'])
@login_required
def pc_task_add(pid):
    db = get_db()
    title = request.form.get('title','').strip()
    if title:
        status = request.form.get('status','backlog')
        max_order = db.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM pc_tasks WHERE project_id=? AND status=?", (pid, status)
        ).fetchone()[0]
        db.execute(
            '''INSERT INTO pc_tasks(project_id,milestone_id,title,description,status,priority,due_date,sort_order)
               VALUES(?,?,?,?,?,?,?,?)''',
            (pid, request.form.get('milestone_id') or None,
             title, request.form.get('description','').strip(),
             status, request.form.get('priority','Medium'),
             request.form.get('due_date') or None, max_order + 1)
        )
        tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        for eid in request.form.getlist('assignee_ids'):
            db.execute("INSERT OR IGNORE INTO pc_task_assignees(task_id,employee_id) VALUES(?,?)", (tid, eid))
        ext_names = [n for n in request.form.get('external_assignees','').split('\n') if n.strip()]
        _sync_external_assignees(db, 'pc_task_external_assignees', 'task_id', tid, ext_names)
        db.commit()
        flash('Task ditambahkan', 'success')
    return redirect(url_for('pc_project_detail', pid=pid) + '#kanban')

@app.route('/project/tasks/<int:tid>/move', methods=['POST'])
@login_required
def pc_task_move(tid):
    db  = get_db()
    new_status = request.json.get('status') if request.is_json else request.form.get('status')
    if new_status:
        db.execute("UPDATE pc_tasks SET status=? WHERE id=?", (new_status, tid))
        db.commit()
    return jsonify({'ok': True})

@app.route('/project/tasks/<int:tid>/delete', methods=['POST'])
@login_required
def pc_task_delete(tid):
    db   = get_db()
    task = db.execute("SELECT project_id FROM pc_tasks WHERE id=?", (tid,)).fetchone()
    if not task: abort(404)
    pid  = task['project_id']
    db.execute("DELETE FROM pc_tasks WHERE id=?", (tid,))
    db.commit()
    flash('Task dihapus', 'warning')
    return redirect(url_for('pc_project_detail', pid=pid) + '#kanban')

# ── Milestones ─────────────────────────────────────────────────────────────────

@app.route('/project/projects/<int:pid>/milestones/add', methods=['POST'])
@login_required
def pc_milestone_add(pid):
    db    = get_db()
    title = request.form.get('title','').strip()
    due   = request.form.get('due_date','').strip()
    if title and due:
        db.execute(
            "INSERT INTO pc_milestones(project_id,title,description,due_date,status) VALUES(?,?,?,?,?)",
            (pid, title, request.form.get('description','').strip(), due, 'upcoming')
        )
        db.commit()
        flash('Milestone ditambahkan', 'success')
    return redirect(url_for('pc_project_detail', pid=pid) + '#milestones')

@app.route('/project/milestones/<int:mid>/status', methods=['POST'])
@login_required
def pc_milestone_status(mid):
    db = get_db()
    ms = db.execute("SELECT project_id FROM pc_milestones WHERE id=?", (mid,)).fetchone()
    if not ms: abort(404)
    db.execute("UPDATE pc_milestones SET status=? WHERE id=?", (request.form.get('status','upcoming'), mid))
    db.commit()
    flash('Status milestone diperbarui', 'success')
    return redirect(url_for('pc_project_detail', pid=ms['project_id']) + '#milestones')

@app.route('/project/milestones/<int:mid>/delete', methods=['POST'])
@login_required
def pc_milestone_delete(mid):
    db = get_db()
    ms = db.execute("SELECT project_id FROM pc_milestones WHERE id=?", (mid,)).fetchone()
    if not ms: abort(404)
    pid = ms['project_id']
    db.execute("DELETE FROM pc_milestones WHERE id=?", (mid,))
    db.commit()
    flash('Milestone dihapus', 'warning')
    return redirect(url_for('pc_project_detail', pid=pid) + '#milestones')

# ── Phases / Timeline ──────────────────────────────────────────────────────────

@app.route('/project/projects/<int:pid>/phases/add', methods=['POST'])
@login_required
def pc_phase_add(pid):
    db   = get_db()
    name = request.form.get('name','').strip()
    ptype= request.form.get('phase_type','custom')
    if not name:
        ptype_labels = {k: v for k, v, _ in PC_PHASE_TYPES}
        name = ptype_labels.get(ptype, 'Fase Baru')
    start = request.form.get('start_date','').strip() or None
    end   = request.form.get('end_date','').strip() or None
    pic_id    = request.form.get('pic_id','').strip() or None
    pic_ext   = request.form.get('pic_ext','').strip()
    app_id    = request.form.get('app_id','').strip() or None
    module_id = request.form.get('module_id','').strip() or None
    notes = request.form.get('notes','').strip()
    max_order = db.execute(
        "SELECT COALESCE(MAX(sort_order),0) FROM pc_phases WHERE project_id=?", (pid,)
    ).fetchone()[0]
    db.execute(
        '''INSERT INTO pc_phases(project_id,phase_type,name,start_date,end_date,
           status,sort_order,pic_id,pic_ext,app_id,module_id,notes)
           VALUES(?,?,?,?,?,'planned',?,?,?,?,?,?)''',
        (pid, ptype, name, start, end, max_order + 1, pic_id, pic_ext, app_id, module_id, notes)
    )
    db.commit()
    flash('Fase ditambahkan', 'success')
    return redirect(url_for('pc_project_detail', pid=pid) + '#timeline')

@app.route('/project/phases/<int:phid>/status', methods=['POST'])
@login_required
def pc_phase_status(phid):
    db = get_db()
    ph = db.execute("SELECT project_id FROM pc_phases WHERE id=?", (phid,)).fetchone()
    if not ph: abort(404)
    new_status = request.form.get('status', 'planned')
    sign_off   = request.form.get('sign_off_date','').strip() or None
    db.execute(
        "UPDATE pc_phases SET status=?, sign_off_date=COALESCE(?,sign_off_date) WHERE id=?",
        (new_status, sign_off, phid)
    )
    db.commit()
    flash('Status fase diperbarui', 'success')
    return redirect(url_for('pc_project_detail', pid=ph['project_id']) + '#timeline')

@app.route('/project/phases/<int:phid>/edit', methods=['POST'])
@login_required
def pc_phase_edit(phid):
    db = get_db()
    ph = db.execute("SELECT project_id FROM pc_phases WHERE id=?", (phid,)).fetchone()
    if not ph: abort(404)
    name      = request.form.get('name','').strip()
    start     = request.form.get('start_date','').strip() or None
    end       = request.form.get('end_date','').strip() or None
    pic_id    = request.form.get('pic_id','').strip() or None
    pic_ext   = request.form.get('pic_ext','').strip()
    app_id    = request.form.get('app_id','').strip() or None
    module_id = request.form.get('module_id','').strip() or None
    sign_off  = request.form.get('sign_off_date','').strip() or None
    notes     = request.form.get('notes','').strip()
    if name:
        db.execute(
            '''UPDATE pc_phases SET name=?,start_date=?,end_date=?,
               pic_id=?,pic_ext=?,app_id=?,module_id=?,sign_off_date=?,notes=? WHERE id=?''',
            (name, start, end, pic_id, pic_ext, app_id, module_id, sign_off, notes, phid)
        )
        db.commit()
        flash('Fase diperbarui', 'success')
    return redirect(url_for('pc_project_detail', pid=ph['project_id']) + '#timeline')

@app.route('/project/phases/<int:phid>/delete', methods=['POST'])
@login_required
def pc_phase_delete(phid):
    db = get_db()
    ph = db.execute("SELECT project_id FROM pc_phases WHERE id=?", (phid,)).fetchone()
    if not ph: abort(404)
    pid = ph['project_id']
    db.execute("DELETE FROM pc_phases WHERE id=?", (phid,))
    db.commit()
    flash('Fase dihapus', 'warning')
    return redirect(url_for('pc_project_detail', pid=pid) + '#timeline')

# ── Proposed Changes ───────────────────────────────────────────────────────────

@app.route('/project/projects/<int:pid>/proposed/add', methods=['POST'])
@login_required
def pc_proposed_add(pid):
    db = get_db()
    title = request.form.get('title','').strip()
    if title:
        db.execute(
            '''INSERT INTO pc_proposed_changes
               (project_id,title,description,module,pekerjaan,impact,difficulty,status,tester,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (pid, title,
             request.form.get('description','').strip(),
             request.form.get('module','').strip(),
             request.form.get('pekerjaan','').strip(),
             request.form.get('impact','Medium'),
             request.form.get('difficulty','Normal'),
             'proposed',
             request.form.get('tester','').strip(),
             request.form.get('notes','').strip())
        )
        db.commit()
        flash('Proposed change ditambahkan', 'success')
    return redirect(url_for('pc_project_detail', pid=pid) + '#proposed')

@app.route('/project/proposed/<int:cid>/status', methods=['POST'])
@login_required
def pc_proposed_status(cid):
    db = get_db()
    pc = db.execute("SELECT project_id FROM pc_proposed_changes WHERE id=?", (cid,)).fetchone()
    if not pc: abort(404)
    db.execute("UPDATE pc_proposed_changes SET status=? WHERE id=?",
               (request.form.get('status','proposed'), cid))
    db.commit()
    return redirect(url_for('pc_project_detail', pid=pc['project_id']) + '#proposed')

@app.route('/project/proposed/<int:cid>/delete', methods=['POST'])
@login_required
def pc_proposed_delete(cid):
    db = get_db()
    pc = db.execute("SELECT project_id FROM pc_proposed_changes WHERE id=?", (cid,)).fetchone()
    if not pc: abort(404)
    pid = pc['project_id']
    db.execute("DELETE FROM pc_proposed_changes WHERE id=?", (cid,))
    db.commit()
    flash('Proposed change dihapus', 'warning')
    return redirect(url_for('pc_project_detail', pid=pid) + '#proposed')

# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_scheduler()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    print("=" * 55)
    print(" Aplikasi TalentCore")
    print(f" Buka browser: http://127.0.0.1:{port}")
    print("=" * 55)
    app.run(debug=debug, host='0.0.0.0', port=port)
