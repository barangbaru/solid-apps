from flask import (Flask, render_template, request, redirect, url_for,
                   flash, g, session, jsonify, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3, os, smtplib, json, secrets, requests as req_lib, io, base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from seed_data import ALL_DIVISIONS, ABILITY_ITEMS
import pyotp, qrcode

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'evalkey-2024-superadmin-secure!')

# Agar request.host_url benar di balik nginx (ProxyFix)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

_default_db = os.path.join(os.path.dirname(__file__), 'evaluasi.db')
DB_PATH = os.environ.get('DATABASE_PATH', _default_db)
DIVISI_LIST = list(ALL_DIVISIONS.keys())

def get_divisi_list(db):
    try:
        rows = db.execute('SELECT name FROM divisions WHERE is_active=1 ORDER BY sort_order, name').fetchall()
        return [r['name'] for r in rows] if rows else DIVISI_LIST
    except Exception:
        return DIVISI_LIST

# ─── DB Helpers ────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
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
"""

MIGRATIONS = [
    ('employees', 'level',           "TEXT DEFAULT 'Staff'"),
    ('employees', 'employment_type', "TEXT DEFAULT 'tetap'"),
    ('employees', 'contract_start',  "TEXT DEFAULT ''"),
    ('employees', 'contract_end',    "TEXT DEFAULT ''"),
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
    'app_name': 'Evaluasi Kinerja Tim IT',
    'notification_emails': '',
    'notification_telegram_ids': '',
    'openwa_url': '',
    'openwa_api_key': '',
    'openwa_session_id': 'default',
    'openwa_enabled': '0',
    'openwa_extra_phones': '',
    'app_url': '',  # URL publik aplikasi mis. https://evaluasi.perusahaan.com (kosong = auto-detect)
}

LEVEL_CHOICES = ['Staff', 'Senior Staff', 'Co-Leader', 'Leader', 'Manager', 'Senior Manager', 'Director']

ALL_PERMISSIONS = {
    'manage_users':       'Kelola pengguna (tambah/edit/hapus)',
    'manage_roles':       'Kelola role dan permission',
    'manage_settings':    'Pengaturan notifikasi sistem',
    'manage_template':    'Edit template evaluasi (skill/kompetensi/ability)',
    'manage_employees':   'Tambah/edit/hapus data karyawan',
    'manage_divisions':   'Kelola divisi',
    'manage_evaluations': 'Buat/hapus evaluasi karyawan',
    'view_evaluations':   'Lihat hasil evaluasi',
    'send_reminders':     'Kirim reminder kontrak',
    'view_salary':        'Lihat data gaji karyawan',
    'manage_salary':      'Edit data gaji karyawan',
}

SYSTEM_ROLE_DEFAULTS = {
    'superadmin': list(ALL_PERMISSIONS.keys()),
    'admin': ['manage_employees','manage_divisions','manage_evaluations',
              'view_evaluations','send_reminders','manage_template'],
    'viewer': ['view_evaluations'],
}

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # Migrations
    for table, col, col_def in MIGRATIONS:
        existing = [r[1] for r in db.execute(f'PRAGMA table_info({table})').fetchall()]
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
    # Seed system roles
    for rname, rdesc, rsys in [('superadmin','Super Administrator',1),('admin','Administrator',1),('viewer','Viewer Read-Only',1)]:
        db.execute('INSERT OR IGNORE INTO roles(name,description,is_system) VALUES(?,?,?)', (rname, rdesc, rsys))
    for rname, perms in SYSTEM_ROLE_DEFAULTS.items():
        for perm in perms:
            db.execute('INSERT OR IGNORE INTO role_permissions(role_name,permission) VALUES(?,?)', (rname, perm))
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

def get_totp_uri(secret, username, issuer='Evaluasi Kinerja'):
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

@app.context_processor
def inject_globals():
    pending    = 0
    divisi_list = DIVISI_LIST
    user_perms  = set()
    if 'user_id' in session:
        try:
            db = get_db()
            pending     = get_pending_review_count(db, session['user_id'], session.get('user_role', ''))
            divisi_list = get_divisi_list(db)
            user_perms  = get_role_permissions(db, session.get('user_role', ''))
            if session.get('user_role') == 'superadmin':
                user_perms = set(ALL_PERMISSIONS.keys())
        except Exception:
            pass
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
    }

# ─── Force MFA setup for all logged-in users ───────────────────────────────────

@app.before_request
def enforce_mfa_setup():
    if 'user_id' not in session:
        return
    exempt = {'login', 'login_mfa', 'logout', 'mfa_setup', 'mfa_challenge', 'static'}
    if request.endpoint in exempt or (request.endpoint or '').startswith('static'):
        return
    try:
        db   = get_db()
        user = db.execute('SELECT mfa_enabled FROM users WHERE id=?',
                          (session['user_id'],)).fetchone()
        if user and not user['mfa_enabled']:
            flash('Aktifkan Google Authenticator MFA terlebih dahulu untuk melanjutkan.', 'warning')
            return redirect(url_for('mfa_setup'))
    except Exception:
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

def send_telegram(bot_token, chat_id, message):
    try:
        r = req_lib.post(
            f'https://api.telegram.org/bot{bot_token}/sendMessage',
            json={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'},
            timeout=10)
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)

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
    wa_session   = settings.get('openwa_session_id', 'default').strip()
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

    # ── Chain: Co-Leader / Leader / Manager ──
    col_role_map = [('supervisor_id', 'Co-Leader'), ('leader_id', 'Leader'), ('manager_id', 'Manager')]
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
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        settings = get_settings(db)
        if settings.get('reminder_enabled', '1') != '1' and triggered_by == 'auto':
            return 0, 0
        reminder_days = [int(d.strip()) for d in settings.get('reminder_days', '30,14,7,1').split(',')
                         if d.strip().isdigit()]
        sent = failed = 0
        today = date.today()
        emps = db.execute('''SELECT * FROM employees
                             WHERE employment_type='kontrak' AND contract_end != ''
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

# ─── APScheduler ───────────────────────────────────────────────────────────────

def start_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(lambda: run_contract_reminders('auto'),
                          'cron', hour=8, minute=0,
                          id='contract_reminder', replace_existing=True)
        scheduler.start()
        import atexit
        atexit.register(scheduler.shutdown)
        print(" Scheduler aktif: cek kontrak setiap hari jam 08:00")
    except Exception as e:
        print(f" Scheduler gagal: {e}")

# ─── Auth Routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username=? AND is_active=1',
                          (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            if user['mfa_enabled'] and user['totp_secret']:
                session['pending_mfa_user_id']   = user['id']
                session['pending_mfa_next']       = request.args.get('next') or url_for('index')
                return redirect(url_for('login_mfa'))
            session['user_id']   = user['id']
            session['username']  = user['username']
            session['user_name'] = user['full_name'] or user['username']
            session['user_role'] = user['role']
            db.execute('UPDATE users SET last_login=? WHERE id=?',
                       (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
            db.commit()
            flash(f'Selamat datang, {session["user_name"]}!', 'success')
            return redirect(request.args.get('next') or url_for('index'))
        flash('Username atau password salah', 'danger')
    return render_template('login.html')

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
            next_url = session.pop('pending_mfa_next', url_for('index'))
            session['user_id']   = user['id']
            session['username']  = user['username']
            session['user_name'] = user['full_name'] or user['username']
            session['user_role'] = user['role']
            db.execute('UPDATE users SET last_login=? WHERE id=?',
                       (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), user['id']))
            db.commit()
            flash(f'Selamat datang, {session["user_name"]}!', 'success')
            return redirect(next_url)
        flash('Kode MFA salah atau kadaluarsa. Coba lagi.', 'danger')
    return render_template('mfa_login.html')

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
                flash('Google Authenticator MFA berhasil diaktifkan!', 'success')
                return redirect(url_for('profile'))
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
    return render_template('mfa_setup.html', user=user, step='intro', mfa_on=mfa_on)

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
    subject  = 'Reset Password — Aplikasi Evaluasi Kinerja'
    body_html = f'''
<p>Halo <b>{display_name}</b>,</p>
<p>Anda (atau seseorang) meminta reset password untuk akun <b>{user['username']}</b>.</p>
<p><a href="{reset_link}" style="background:#1a7a3a;color:#fff;padding:10px 20px;border-radius:6px;
   text-decoration:none;font-weight:bold">Klik di sini untuk reset password</a></p>
<p>Link ini hanya berlaku <b>1 jam</b> dan <b>langsung kadaluarsa setelah dibuka satu kali</b>.</p>
<p>Jika bukan Anda yang meminta, abaikan email ini.</p>
<hr><p style="color:#888;font-size:12px">Aplikasi Evaluasi Kinerja Tim IT</p>
'''

    # Email
    if contacts.get('email'):
        try:
            ok, _ = send_email(
                settings.get('smtp_host',''), int(settings.get('smtp_port', 587) or 587),
                settings.get('smtp_user',''), settings.get('smtp_password',''),
                settings.get('smtp_from', settings.get('smtp_user','')),
                contacts['email'], subject, body_html
            )
            if ok:
                sent.append('email')
        except Exception:
            pass

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
    wa_session = settings.get('openwa_session_id','').strip()  # fix: key benar
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
        db.execute('''INSERT INTO employees(name,jabatan,divisi,level,employment_type,
                      contract_start,contract_end,email,phone,telegram_id,notes,
                      supervisor_id,leader_id,manager_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            name,
            request.form.get('jabatan','').strip(),
            divisi,
            request.form.get('level','Staff'),
            request.form.get('employment_type','tetap'),
            request.form.get('contract_start',''),
            request.form.get('contract_end',''),
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
        db.execute('''UPDATE employees SET name=?,jabatan=?,divisi=?,level=?,
                      employment_type=?,contract_start=?,contract_end=?,
                      email=?,phone=?,telegram_id=?,notes=?,
                      supervisor_id=?,leader_id=?,manager_id=? WHERE id=?''', (
            request.form['name'].strip(),
            request.form.get('jabatan','').strip(),
            request.form['divisi'],
            request.form.get('level','Staff'),
            emp_type,
            request.form.get('contract_start','') if emp_type == 'kontrak' else '',
            request.form.get('contract_end','')   if emp_type == 'kontrak' else '',
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
    return render_template('employee_form.html', emp=emp, logs=logs,
                           employees_all=employees_all, linked_user=linked_user)

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
            ''', (emp_id, year, num('base_salary'), num('al_001'), num('al_002'),
                  num('al_003'), num('al_004'), num('increase_pct'),
                  str(get('increase_date', '')), str(get('notes', '')), now))
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
        WHERE employment_type = 'kontrak' AND is_active = 1
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
    return render_template('karyawan.html', kontrak=kontrak, tetap=tetap, today=today,
                           emp_user_map=emp_user_map)

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
        flash(f'Reminder dikirim — {s} berhasil, {f} gagal (termasuk ke Co-Leader/Leader/Manager)', 'success' if f == 0 else 'warning')
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

# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@superadmin_required
def settings():
    db = get_db()
    if request.method == 'POST':
        keys = ['smtp_host','smtp_port','smtp_user','smtp_password','smtp_from','smtp_ssl',
                'telegram_bot_token','telegram_default_chat_id',
                'reminder_days','reminder_enabled','app_name',
                'notification_emails','notification_telegram_ids',
                'openwa_url','openwa_api_key','openwa_session_id','openwa_enabled',
                'openwa_extra_phones']
        for k in keys:
            v = request.form.get(k, '').strip()
            if k == 'smtp_ssl':
                v = '1' if request.form.get('smtp_ssl') else '0'
            if k == 'reminder_enabled':
                v = '1' if request.form.get('reminder_enabled') else '0'
            if k == 'openwa_enabled':
                v = '1' if request.form.get('openwa_enabled') else '0'
            save_setting(db, k, v)
        db.commit()
        flash('Pengaturan disimpan', 'success')
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
                         'Test Email — Evaluasi Kinerja',
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
    wa_session = cfg.get('openwa_session_id', 'default').strip()
    phone      = request.form.get('test_wa_phone', '').strip()
    if not wa_url or not phone:
        return jsonify({'ok': False, 'msg': 'URL OpenWA dan nomor HP harus diisi'})
    ok, err = send_whatsapp(wa_url, wa_key, wa_session, phone,
                            '✅ *Test berhasil!*\n\nKonfigurasi OpenWA WhatsApp sudah terhubung dengan Evaluasi Kinerja.')
    chat_id = normalize_phone_wa(phone)
    return jsonify({'ok': ok, 'chat_id': chat_id,
                    'msg': f'Pesan terkirim ke {chat_id}' if ok else str(err)})

@app.route('/settings/run-reminders', methods=['POST'])
@superadmin_required
def run_reminders_now():
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
        subject   = f'[Self-Assessment] Evaluasi Kinerja {emp["name"]} — Periode {periode}'
        html_body = f"""
<h3 style="color:#1e2a3a">Self-Assessment Evaluasi Kinerja</h3>
<p>Yth. <strong>{emp['name']}</strong>,</p>
<p>Anda diminta mengisi <b>penilaian diri (self-assessment)</b> untuk evaluasi kinerja
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
            db.execute('''INSERT OR REPLACE INTO eval_tokens(eval_id, token, email_sent_to, sent_at)
                          VALUES(?,?,?,?)''', (eval_id, token, emp['email'], now_str))
            results.append(f'Email {emp["email"]} ✓')
        else:
            results.append(f'Email gagal: {err}')

    bot_token = settings.get('telegram_bot_token', '').strip()
    tg_id     = normalize_telegram_id(emp['telegram_id'] or '')
    if bot_token and tg_id:
        tg_msg = (
            f"📝 <b>Self-Assessment Evaluasi Kinerja</b>\n\n"
            f"Yth. <b>{emp['name']}</b>,\n\n"
            f"Anda diminta mengisi penilaian diri (self-assessment) untuk evaluasi kinerja "
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
    wa_session = settings.get('openwa_session_id', 'default').strip()
    wa_enabled = settings.get('openwa_enabled', '0') == '1'
    emp_phone  = emp['phone'] or ''
    if wa_enabled and wa_url and emp_phone:
        wa_chat_id = normalize_phone_wa(emp_phone)
        wa_msg = (
            f"📝 *Self-Assessment Evaluasi Kinerja*\n\n"
            f"Yth. *{emp['name']}*,\n\n"
            f"Anda diminta mengisi penilaian diri (self-assessment) untuk evaluasi kinerja "
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
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
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

        db.execute('''INSERT OR REPLACE INTO eval_reviews
                      (eval_id, reviewer_user_id, reviewer_role, notes, status, submitted_at)
                      VALUES(?,?,?,?,'submitted',?)''',
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

    role = request.form.get('role', 'admin')
    if role not in ('admin', 'superadmin'):
        flash('Role tidak valid', 'danger')
        return redirect(url_for('karyawan'))

    if emp['user_id']:
        # Update existing linked user's role
        db.execute('UPDATE users SET role=? WHERE id=?', (role, emp['user_id']))
        db.commit()
        flash(f'Role {emp["name"]} diupdate ke {role.upper()}', 'success')
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
            db.commit()
            flash(f'{emp["name"]} dihubungkan ke akun "{username}" sebagai {role.upper()}', 'success')
        else:
            if not password:
                flash('Password diperlukan untuk akun baru', 'danger')
                return redirect(url_for('karyawan'))
            new_uid = db.execute(
                'INSERT INTO users(username,password_hash,full_name,role) VALUES(?,?,?,?)',
                (username, generate_password_hash(password), emp['name'], role)
            ).lastrowid
            db.execute('UPDATE employees SET user_id=? WHERE id=?', (new_uid, emp_id))
            db.commit()
            flash(f'Akun "{username}" dibuat untuk {emp["name"]} sebagai {role.upper()}', 'success')
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
                    db.execute('INSERT INTO roles(name,description,is_system) VALUES(?,?,0)', (name, desc))
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
    roles = db.execute('SELECT * FROM roles ORDER BY is_system DESC, name').fetchall()
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
    return sum(row[col] or 0 for col, *_ in SALARY_COLS)

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
        val = value if field in ('notes', 'increase_date') else float(value)
    except ValueError:
        val = 0.0
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(f'''
        INSERT INTO employee_salary(employee_id, year, {field}, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(employee_id, year) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at
    ''', (emp_id, year, val, now))
    db.commit()
    # Return updated row totals
    row = db.execute('SELECT * FROM employee_salary WHERE employee_id=? AND year=?',
                     (emp_id, year)).fetchone()
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
                      round((r['base_salary'] or 0) * factor),
                      round((r['al_001'] or 0) * factor),
                      round((r['al_002'] or 0) * factor),
                      round((r['al_003'] or 0) * factor),
                      round((r['al_004'] or 0) * factor),
                      now))
                copied += 1
            except Exception:
                pass
    else:
        # Buat baris kosong untuk semua karyawan aktif
        emps = db.execute("SELECT id FROM employees WHERE status='aktif'").fetchall()
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

# ─── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_scheduler()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    print("=" * 55)
    print(" Aplikasi Evaluasi Kinerja Tim IT")
    print(f" Buka browser: http://127.0.0.1:{port}")
    print("=" * 55)
    app.run(debug=debug, host='0.0.0.0', port=port)
