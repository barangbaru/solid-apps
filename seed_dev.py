#!/usr/bin/env python3
"""
Seed dummy data dev PostgreSQL — Hive.
Skenario berbasis performance profile per karyawan:
  - STAR   (5): multi-project, task selesai semua, gaji naik 10-12%
  - SOLID  (4): 2-3 project, task mostly done, naik 7-9%
  - AVERAGE(3): 1-2 project, campuran task, naik 4-6%
  - BELOW  (2): 1 project, banyak task tidak selesai, naik 2-3%
  - LAZY   (1): kontrak tidak diperpanjang, task menumpuk, naik 0-1%

Usage:
    FIELD_ENCRYPT_KEY=<key> python seed_dev.py
"""
import os, sys, random, datetime, warnings
warnings.filterwarnings('ignore')

os.environ['DB_TYPE'] = 'postgresql'
os.environ['PG_HOST'] = '10.150.10.41'
os.environ['PG_PORT'] = '5432'
os.environ['PG_NAME'] = 'hive_db'
os.environ['PG_USER'] = 'hive'
os.environ['PG_PASS'] = '6RSxtzGk0ROSqb5glnzD'
os.environ.setdefault('SECRET_KEY', 'dev-seed-secret-key-2026')

# Key dev yang fixed — harus sama persis dengan FIELD_ENCRYPT_KEY di .env server
DEV_FIELD_ENCRYPT_KEY = 'VGcKCP3l81uH7QlueXEZCu79wwlSRbMf7dlZv7LXs2w='
if not os.environ.get('FIELD_ENCRYPT_KEY'):
    os.environ['FIELD_ENCRYPT_KEY'] = DEV_FIELD_ENCRYPT_KEY
    print(f'[INFO] Menggunakan DEV key: {DEV_FIELD_ENCRYPT_KEY}')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app, init_db, get_db, _fenc
from werkzeug.security import generate_password_hash

# ── Helpers ──────────────────────────────────────────────────────────────────

rng = random.Random(42)

def rp(low, high, step=500_000):
    return rng.randint(int(low / step), int(high / step)) * step

def rp_exact(low, high):
    """Random integer (tanpa pembulatan step), untuk variasi kecil."""
    return rng.randint(int(low), int(high))

def date_add_months(d, months):
    m = d.month + months
    y = d.year + (m - 1) // 12
    m = ((m - 1) % 12) + 1
    last_day = (datetime.date(y, m % 12 + 1, 1) - datetime.timedelta(days=1)).day \
               if m != 12 else 31
    return datetime.date(y, m, min(d.day, last_day))

def past(days):
    return (datetime.date.today() - datetime.timedelta(days=rng.randint(1, days))).isoformat()

def past_fixed(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

def future(days):
    return (datetime.date.today() + datetime.timedelta(days=rng.randint(1, days))).isoformat()

def rand_date(start, end):
    delta = (end - start).days
    return (start + datetime.timedelta(days=rng.randint(0, max(0, delta)))).isoformat()

def in_year(year, month_start=1, month_end=12):
    start = datetime.date(year, month_start, 1)
    end   = datetime.date(year, month_end, 28)
    return rand_date(start, end)

LOREM = [
    'Implementasi fitur baru untuk meningkatkan performa sistem.',
    'Review dan optimasi query database yang lambat.',
    'Perbaikan bug pada modul laporan bulanan.',
    'Pembuatan dokumentasi teknis endpoint API.',
    'Setup environment staging dan testing pipeline.',
    'Koordinasi dengan tim customer mengenai kebutuhan integrasi.',
    'Analisa kebutuhan dan pembuatan spesifikasi teknis.',
    'Testing regresi setelah release versi terbaru.',
    'Refactoring modul autentikasi untuk keamanan lebih baik.',
    'Migrasi data dari sistem lama ke sistem baru.',
]
lorem = lambda: rng.choice(LOREM)

# ── Performance Profiles ──────────────────────────────────────────────────────
# Level 5=STAR, 4=SOLID, 3=AVERAGE, 2=BELOW, 1=LAZY
PERF = {
    # Management
    'Budi Santoso':     4,  # solid manager, PIC banyak proyek
    'Rina Wulandari':   5,  # star PM, drive semua proyek
    # Programmer
    'Dewi Rahayu':      5,  # star — senior, multi-project, semua task selesai
    'Andi Wijaya':      4,  # solid — junior tapi rajin, task on-track
    'Irwan Fauzi':      4,  # solid — senior, konsisten
    'Nadia Rahma':      3,  # average — kontrak, beberapa task terlambat
    'Reza Pratama':     1,  # LAZY — kontrak expired, task menumpuk tidak selesai
    # Implementor/BPS
    'Fajar Nugroho':    5,  # star — senior impl, multi-project
    'Maya Sari':        4,  # solid — implementor andalan
    'Yuli Astuti':      3,  # average — kontrak, perlu bimbingan
    'Dimas Kurniawan':  2,  # below — kontrak, task sering terlambat
    # Helpdesk
    'Hendra Kusuma':    5,  # star — senior HD, resolve tiket paling banyak
    'Dian Permata':     3,  # average — kontrak HD
    'Bagas Setiawan':   2,  # below — kontrak HD, tiket sering terlambat
    # Tester
    'Siti Nurhaliza':   5,  # star — QA lead, semua UAT
    'Putri Handayani':  3,  # average — junior tester
}

# Salary increase % berdasarkan performance (untuk kenaikan 2024→2025 dan 2025→2026)
INCREASE_MAP = {5: (9, 12), 4: (6, 9), 3: (3, 5), 2: (1, 3), 1: (0, 1)}

# Task status distribution per performance level
# (done%, in_progress%, review%, todo%, backlog%)
TASK_DIST = {
    5: [0.65, 0.20, 0.10, 0.05, 0.00],
    4: [0.50, 0.25, 0.10, 0.10, 0.05],
    3: [0.30, 0.25, 0.05, 0.25, 0.15],
    2: [0.15, 0.20, 0.05, 0.30, 0.30],
    1: [0.05, 0.10, 0.00, 0.25, 0.60],
}

def pick_task_status(perf_level):
    statuses = ['done', 'in_progress', 'review', 'todo', 'backlog']
    weights  = TASK_DIST[perf_level]
    return rng.choices(statuses, weights=weights, k=1)[0]

def eval_score(perf_level, bonus=0):
    """Score 1-5 sesuai performance level, dengan sedikit noise."""
    base = {5: 4, 4: 3, 3: 3, 2: 2, 1: 1}[perf_level]
    noise = rng.randint(-1, 1)
    return max(1, min(5, base + bonus + noise))

# ─────────────────────────────────────────────────────────────────────────────

with app.app_context():

    # ── [1] INIT ─────────────────────────────────────────────────────────────
    print('[1/10] Init DB & tabel...')
    init_db()
    db = get_db()

    # ── [2] BERSIHKAN ────────────────────────────────────────────────────────
    print('[2/10] Bersihkan data lama...')
    for tbl in [
        'pc_phases','pc_proposed_changes','pc_task_assignees','pc_tasks',
        'pc_milestones','pc_issue_history','pc_issues','pc_members','pc_projects',
        'ac_maintenance_log','ac_maintenance','ac_software_requests',
        'ac_license_assignments','ac_subscriptions','ac_licenses',
        'ac_asset_history','ac_asset_software','ac_assets','ac_infrastructure',
        'bk_booking_items','bk_bookings','bk_items','bk_resource_images','bk_resources',
        'sc_ticket_attachments','sc_ticket_history','sc_ticket_external_assignees',
        'sc_ticket_assignees','sc_tickets','sc_presales_history',
        'sc_presales_assignees','sc_presales_requests',
        'sc_contract_support_types','sc_contract_services','sc_contracts',
        'sc_sla_categories','sc_support_types','sc_services',
        'sc_modules','sc_apps','sc_customers',
        'peer_reviews','competency_scores','ability_scores','skill_scores',
        'project_entries','evaluations',
        'employee_salary','eval_reviews','eval_tokens',
        'user_app_access','role_permissions','roles',
        'employees','users',
        'skill_items','skill_categories','ability_items','competency_items',
        'divisions',
    ]:
        try:
            db.execute(f'DELETE FROM {tbl}')
        except Exception:
            pass
    db.commit()

    # ── [3] EMPLOYEES & USERS ────────────────────────────────────────────────
    print('[3/10] Users & Karyawan...')

    DIVISI_LIST = ['Programmer', 'Implementor/BPS', 'Helpdesk Support', 'Management', 'Tester']
    for div in DIVISI_LIST:
        db.execute("INSERT INTO divisions(name) VALUES(?) ON CONFLICT DO NOTHING", (div,))
    db.commit()

    today = datetime.date.today()

    def kontrak_expired():
        start = datetime.date(today.year - 1, rng.randint(1, 6), 1)
        end   = date_add_months(start, 6) - datetime.timedelta(days=1)
        return start.isoformat(), end.isoformat()

    def kontrak_aktif(sisa_bulan):
        end   = date_add_months(today, sisa_bulan)
        start = date_add_months(end, -12)
        return start.isoformat(), end.isoformat()

    _exp = kontrak_expired()
    _a2  = kontrak_aktif(2)
    _a3  = kontrak_aktif(3)
    _a5  = kontrak_aktif(5)
    _a6  = kontrak_aktif(6)
    _a12 = kontrak_aktif(12)

    # (name, jabatan, divisi, level, employment_type, email, phone, kdates)
    emps_raw = [
        ('Budi Santoso',    'IT Manager',             'Management',      'Manager','tetap',   'budi@example.com',   '081111000001', None),
        ('Rina Wulandari',  'Project Manager',        'Management',      'Manager','tetap',   'rina@example.com',   '081111000002', None),
        ('Dewi Rahayu',     'Senior Programmer',      'Programmer',      'Senior', 'tetap',   'dewi@example.com',   '081111000003', None),
        ('Andi Wijaya',     'Junior Programmer',      'Programmer',      'Junior', 'tetap',   'andi@example.com',   '081111000004', None),
        ('Irwan Fauzi',     'Senior Programmer',      'Programmer',      'Senior', 'tetap',   'irwan@example.com',  '081111000005', None),
        ('Nadia Rahma',     'Programmer',             'Programmer',      'Staff',  'kontrak', 'nadia@example.com',  '081111000006', _a6),
        ('Reza Pratama',    'Programmer',             'Programmer',      'Staff',  'kontrak', 'reza@example.com',   '081111000007', _exp),
        ('Fajar Nugroho',   'Senior Implementor',     'Implementor/BPS', 'Senior', 'tetap',   'fajar@example.com',  '081111000008', None),
        ('Maya Sari',       'Implementor',            'Implementor/BPS', 'Staff',  'tetap',   'maya@example.com',   '081111000009', None),
        ('Yuli Astuti',     'Business Process Spec.', 'Implementor/BPS', 'Staff',  'kontrak', 'yuli@example.com',   '081111000010', _a12),
        ('Dimas Kurniawan', 'Implementor',            'Implementor/BPS', 'Junior', 'kontrak', 'dimas@example.com',  '081111000011', _a3),
        ('Hendra Kusuma',   'Senior Helpdesk',        'Helpdesk Support','Senior', 'tetap',   'hendra@example.com', '081111000012', None),
        ('Dian Permata',    'Helpdesk Support',       'Helpdesk Support','Staff',  'kontrak', 'dian@example.com',   '081111000013', _a5),
        ('Bagas Setiawan',  'Helpdesk Support',       'Helpdesk Support','Staff',  'kontrak', 'bagas@example.com',  '081111000014', _a2),
        ('Siti Nurhaliza',  'QA Lead',                'Tester',          'Senior', 'tetap',   'siti@example.com',   '081111000015', None),
        ('Putri Handayani', 'Junior Tester',          'Tester',          'Junior', 'kontrak', 'putri@example.com',  '081111000016', _a12),
    ]

    emp_ids = []
    for name, jabatan, divisi, level, etype, email, phone, kdates in emps_raw:
        cs, ce = kdates if kdates else (None, None)
        db.execute(
            '''INSERT INTO employees(name,jabatan,divisi,level,employment_type,
               email,phone,contract_start,contract_end,is_active)
               VALUES(?,?,?,?,?,?,?,?,?,1)''',
            (name, jabatan, divisi, level, etype, email, phone, cs, ce)
        )
        emp_ids.append(db.execute("SELECT id FROM employees WHERE email=?", (email,)).fetchone()['id'])
    db.commit()

    EN = {emps_raw[i][0]: emp_ids[i] for i in range(len(emp_ids))}  # name → id

    # Supervisor
    for i in range(1, len(emp_ids)):
        db.execute("UPDATE employees SET supervisor_id=? WHERE id=?", (emp_ids[0], emp_ids[i]))
    db.commit()

    # Kelompok divisi
    prog_ids  = [EN[n] for n in ['Dewi Rahayu','Andi Wijaya','Irwan Fauzi','Nadia Rahma','Reza Pratama']]
    impl_ids  = [EN[n] for n in ['Fajar Nugroho','Maya Sari','Yuli Astuti','Dimas Kurniawan']]
    hd_ids    = [EN[n] for n in ['Hendra Kusuma','Dian Permata','Bagas Setiawan']]
    test_ids  = [EN[n] for n in ['Siti Nurhaliza','Putri Handayani']]
    mgmt_ids  = [EN[n] for n in ['Budi Santoso','Rina Wulandari']]

    # ── [3b] USERS ───────────────────────────────────────────────────────────
    users_def = [
        ('superadmin', 'superadmin', 'Superadmin',        None),
        ('admin',      'admin',      'Administrator',      None),
        ('budi',       'staff',      'Budi Santoso',       EN['Budi Santoso']),
        ('rina',       'staff',      'Rina Wulandari',     EN['Rina Wulandari']),
        ('dewi',       'staff',      'Dewi Rahayu',        EN['Dewi Rahayu']),
        ('fajar',      'staff',      'Fajar Nugroho',      EN['Fajar Nugroho']),
        ('hendra',     'staff',      'Hendra Kusuma',      EN['Hendra Kusuma']),
        ('siti',       'staff',      'Siti Nurhaliza',     EN['Siti Nurhaliza']),
    ]
    UID = {}
    for uname, role, fullname, eid in users_def:
        db.execute(
            "INSERT INTO users(username,password_hash,full_name,role,is_active) VALUES(?,?,?,?,1)",
            (uname, generate_password_hash('Admin@123'), fullname, role)
        )
        uid = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()['id']
        UID[uname] = uid
        if eid:
            db.execute("UPDATE employees SET user_id=? WHERE id=?", (uid, eid))
    db.commit()

    # ── [4] SALARY 2024 → 2025 → 2026 ───────────────────────────────────────
    print('[4/10] Salary (encrypted, multi-year dengan kenaikan berbasis kinerja)...')

    SAL_RANGE = {
        'Manager': (14_000_000, 22_000_000),
        'Senior':  ( 9_000_000, 14_000_000),
        'Staff':   ( 4_500_000,  7_000_000),
        'Junior':  ( 3_500_000,  5_500_000),
    }

    # Komponen tunjangan (persen dari gaji pokok)
    TJ_RANGE = {
        'al_001': (0.10, 0.20),  # Tj. Jabatan
        'al_002': (0.05, 0.10),  # Tj. Komunikasi
        'al_003': (0.08, 0.15),  # Tj. Performance
        'al_004': (0.05, 0.10),  # Tj. Kehadiran
    }

    for name, jabatan, divisi, level, etype, email, *_ in emps_raw:
        eid  = EN[name]
        perf = PERF[name]
        lo, hi = SAL_RANGE.get(level, (4_000_000, 7_000_000))

        # Gaji pokok 2024 (baseline)
        base_2024 = rp(lo, hi)
        tjs_2024 = {k: rp_exact(base_2024 * r[0], base_2024 * r[1])
                    for k, r in TJ_RANGE.items()}

        # Kenaikan 2024→2025 berdasarkan evaluasi Q4-2024
        inc_pct_2025 = round(rng.uniform(*INCREASE_MAP[perf]), 1)
        base_2025    = int(base_2024 * (1 + inc_pct_2025 / 100) / 500_000) * 500_000
        tjs_2025     = {k: rp_exact(base_2025 * r[0], base_2025 * r[1])
                        for k, r in TJ_RANGE.items()}

        # Kenaikan 2025→2026 — star/solid naik lagi, lazy/below stagnan
        inc_pct_2026 = round(rng.uniform(*INCREASE_MAP[perf]), 1)
        base_2026    = int(base_2025 * (1 + inc_pct_2026 / 100) / 500_000) * 500_000
        tjs_2026     = {k: rp_exact(base_2026 * r[0], base_2026 * r[1])
                        for k, r in TJ_RANGE.items()}

        for year, base, tjs, inc_pct, inc_date in [
            (2024, base_2024, tjs_2024, 0.0,         '2024-01-01'),
            (2025, base_2025, tjs_2025, inc_pct_2025, '2025-01-01'),
            (2026, base_2026, tjs_2026, inc_pct_2026, '2026-01-01'),
        ]:
            db.execute(
                '''INSERT INTO employee_salary
                   (employee_id,year,base_salary,al_001,al_002,al_003,al_004,
                    increase_pct,increase_date,notes)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(employee_id,year) DO NOTHING''',
                (eid, year,
                 _fenc(base),
                 _fenc(tjs['al_001']), _fenc(tjs['al_002']),
                 _fenc(tjs['al_003']), _fenc(tjs['al_004']),
                 inc_pct, inc_date,
                 f'Kenaikan {inc_pct}% dari evaluasi Q4-{year-1}' if inc_pct > 0
                 else 'Tidak ada kenaikan — evaluasi di bawah standar')
            )
    db.commit()
    print(f'   Salary 2024/2025/2026 untuk {len(emps_raw)} karyawan tersimpan.')

    # ── [5] SUPPORTCORE ──────────────────────────────────────────────────────
    print('[5/10] SupportCore...')

    apps_sc = [('HRIS','Sistem HR & Payroll'),('ERP','Enterprise Resource Planning'),
               ('CRM','Customer Relationship Management'),('Portal','Portal Internal'),
               ('Mobile','Aplikasi Mobile Field Force')]
    APP_ID = {}
    for aname, adesc in apps_sc:
        db.execute("INSERT INTO sc_apps(name,description,is_active) VALUES(?,?,1)", (aname, adesc))
        APP_ID[aname] = db.execute("SELECT id FROM sc_apps WHERE name=?", (aname,)).fetchone()['id']

    modules_sc = {
        'HRIS':   ['Karyawan','Payroll','Cuti','Absensi'],
        'ERP':    ['Pembelian','Penjualan','Inventori','Akuntansi'],
        'CRM':    ['Prospek','Kontrak','After Sales'],
        'Portal': ['Dashboard','Notifikasi','Pengaturan'],
        'Mobile': ['Check-in','Task','Laporan'],
    }
    MOD_ID = {}
    for aname, mods in modules_sc.items():
        for mname in mods:
            db.execute("INSERT INTO sc_modules(app_id,name,is_active) VALUES(?,?,1)", (APP_ID[aname], mname))
            MOD_ID[f'{aname}.{mname}'] = db.execute(
                "SELECT id FROM sc_modules WHERE app_id=? AND name=?", (APP_ID[aname], mname)
            ).fetchone()['id']

    customers = [
        ('PT Maju Bersama',    'MB', 'Jakarta',  'Andi Kurniawan', '02155001234', 'aktif'),
        ('CV Teknologi Jaya',  'TJ', 'Bandung',  'Sri Mulyani',    '02255001234', 'aktif'),
        ('PT Nusantara Global','NG', 'Surabaya', 'Heri Susanto',   '03155001234', 'aktif'),
        ('PT Delta Karya',     'DK', 'Medan',    'Rini Agustina',  '06155001234', 'prospek'),
    ]
    CUST_ID = {}
    for cname, code, addr, cp, phone, ctype in customers:
        db.execute(
            '''INSERT INTO sc_customers(name,code,address,contact_person,phone,
               is_active,customer_type,pic_implementor_id) VALUES(?,?,?,?,?,1,?,?)''',
            (cname, code, addr, cp, phone, ctype, impl_ids[0])
        )
        CUST_ID[code] = db.execute("SELECT id FROM sc_customers WHERE code=?", (code,)).fetchone()['id']

    SVC_ID = []
    for code, name in [('IMPL','Implementasi'),('MAINT','Maintenance'),('TRAIN','Training'),
                       ('CONS','Konsultasi'),('CDEV','Custom Development')]:
        db.execute("INSERT INTO sc_services(code,name) VALUES(?,?)", (code, name))
        SVC_ID.append(db.execute("SELECT id FROM sc_services WHERE code=?", (code,)).fetchone()['id'])

    STYPE_ID = []
    for code, name in [('BUG-CRIT','Bug Critical'),('BUG-MAJ','Bug Major'),
                       ('BUG-MIN','Bug Minor'),('ENH','Enhancement'),('QST','Question')]:
        db.execute("INSERT INTO sc_support_types(code,name) VALUES(?,?)", (code, name))
        STYPE_ID.append(db.execute("SELECT id FROM sc_support_types WHERE code=?", (code,)).fetchone()['id'])

    SLA_ID = []
    for code, name, prio, res, sol in [('GOLD','Gold','High',2,4),
                                        ('SILVER','Silver','Medium',4,8),
                                        ('BRONZE','Bronze','Low',8,24)]:
        db.execute(
            "INSERT INTO sc_sla_categories(code,name,priority,response_time_hours,resolution_time_hours) VALUES(?,?,?,?,?)",
            (code, name, prio, res, sol)
        )
        SLA_ID.append(db.execute("SELECT id FROM sc_sla_categories WHERE code=?", (code,)).fetchone()['id'])

    CONT_ID = {}
    for i, ckey in enumerate(['MB','TJ','NG']):
        code = f'KTK-2024-{i+1:03d}'
        db.execute(
            '''INSERT INTO sc_contracts(code,customer_id,title,start_date,end_date,
               contract_value,status,notes) VALUES(?,?,?,?,?,?,?,?)''',
            (code, CUST_ID[ckey], f'Kontrak Support & Maintenance — {customers[i][0]}',
             '2024-01-01', '2026-12-31',
             rp(50_000_000, 200_000_000), 'active', 'Kontrak tahunan.')
        )
        cid = db.execute("SELECT id FROM sc_contracts WHERE code=?", (code,)).fetchone()['id']
        CONT_ID[ckey] = cid
        for svc in rng.sample(SVC_ID, 3):
            db.execute(
                "INSERT INTO sc_contract_services(contract_id,service_id) VALUES(?,?) ON CONFLICT DO NOTHING",
                (cid, svc)
            )

    # ── Tiket: distribusi per PIC berdasarkan performance ────────────────────
    # Helpdesk star (Hendra) → banyak resolved/closed
    # Helpdesk below (Bagas) → banyak yang masih open/in_progress lama
    # Implementor star (Fajar) → semua selesai tepat waktu

    def ticket_status_for(assignee_name):
        p = PERF[assignee_name]
        if p >= 5:   return rng.choices(['resolved','closed','in_progress'],   [0.4,0.4,0.2],   k=1)[0]
        elif p == 4: return rng.choices(['resolved','closed','in_progress'],   [0.3,0.3,0.4],   k=1)[0]
        elif p == 3: return rng.choices(['open','in_progress','resolved'],      [0.3,0.4,0.3],   k=1)[0]
        else:        return rng.choices(['open','in_progress'],                 [0.6,0.4],        k=1)[0]

    tickets = [
        # (subject, type_i, assignee_name, cust_key, sla_i, year)
        # Hendra (STAR) — tiket banyak, mostly resolved/closed
        ('Login gagal setelah update HRIS',         0, 'Hendra Kusuma', 'MB', 0, 2024),
        ('Payroll Desember tidak terhitung',        0, 'Hendra Kusuma', 'MB', 0, 2024),
        ('Error cetak slip gaji PDF',               1, 'Hendra Kusuma', 'TJ', 1, 2024),
        ('Dashboard ERP lambat loading',            1, 'Hendra Kusuma', 'NG', 1, 2024),
        ('Error input pembelian barang',            0, 'Hendra Kusuma', 'MB', 0, 2024),
        ('Inventori minus setelah SO',              0, 'Hendra Kusuma', 'TJ', 0, 2025),
        ('Akuntansi jurnal ganda',                  0, 'Hendra Kusuma', 'NG', 1, 2025),
        ('User tidak bisa reset password',          2, 'Hendra Kusuma', 'MB', 2, 2025),
        ('Mobile app crash saat check-in',          0, 'Hendra Kusuma', 'TJ', 0, 2025),
        ('Laporan penjualan salah total',           0, 'Hendra Kusuma', 'NG', 0, 2025),
        # Dian (AVERAGE) — campuran
        ('Absensi tidak sinkron mobile',            2, 'Dian Permata',  'MB', 1, 2024),
        ('Laporan cuti tidak akurat',               2, 'Dian Permata',  'TJ', 2, 2024),
        ('Notifikasi email tidak terkirim',         2, 'Dian Permata',  'NG', 1, 2025),
        ('Bug filter tanggal pada laporan',         1, 'Dian Permata',  'MB', 2, 2025),
        ('Performance lambat modul inventori',      1, 'Dian Permata',  'TJ', 1, 2025),
        # Bagas (BELOW) — banyak open, sering terlambat
        ('Error permission user role baru',         2, 'Bagas Setiawan','MB', 1, 2024),
        ('Bug kalkulasi lembur karyawan',           0, 'Bagas Setiawan','TJ', 0, 2024),
        ('Mobile tidak bisa upload foto laporan',   2, 'Bagas Setiawan','NG', 1, 2025),
        ('Error perhitungan PPh 21',                0, 'Bagas Setiawan','MB', 0, 2025),
        ('Error validasi data karyawan baru',       2, 'Bagas Setiawan','TJ', 2, 2025),
        # Fajar (STAR implementor) — enhancement/training selesai
        ('Request tambah field kustom di CRM',      3, 'Fajar Nugroho', 'MB', 1, 2024),
        ('Request fitur approval multi-level',      3, 'Fajar Nugroho', 'TJ', 1, 2024),
        ('Pelatihan user modul Payroll',            4, 'Fajar Nugroho', 'NG', 2, 2024),
        ('Request kustomisasi template dokumen',    3, 'Fajar Nugroho', 'MB', 1, 2025),
        ('Konsultasi migrasi data ERP lama',        3, 'Fajar Nugroho', 'TJ', 2, 2025),
        # Maya (SOLID implementor)
        ('Implementasi modul After Sales CRM',      3, 'Maya Sari',     'MB', 2, 2024),
        ('Request eksport Excel format custom',     3, 'Maya Sari',     'NG', 2, 2024),
        ('Sync data gagal saat jam sibuk',          1, 'Maya Sari',     'TJ', 1, 2025),
        ('Request integrasi fingerprint ke HRIS',   3, 'Maya Sari',     'MB', 1, 2025),
        # Yuli (AVERAGE implementor)
        ('Request training modul ERP baru',         4, 'Yuli Astuti',   'NG', 2, 2024),
        ('Pelatihan penggunaan fitur dashboard',    4, 'Yuli Astuti',   'MB', 2, 2025),
        # Dimas (BELOW implementor) — enhancement belum selesai
        ('Setup integrasi API ke sistem klien',     3, 'Dimas Kurniawan','TJ', 1, 2024),
        ('Implementasi fitur bulk upload',          3, 'Dimas Kurniawan','NG', 1, 2025),
        # Dewi (STAR programmer) — bug technical dari developer
        ('Performa slow query laporan konsolidasi', 1, 'Dewi Rahayu',   'MB', 1, 2024),
        ('Request pengembangan API integrasi',      3, 'Dewi Rahayu',   'TJ', 1, 2025),
        # Hendra lagi — tiket extra
        ('Error generate nomor transaksi otomatis', 0, 'Hendra Kusuma', 'NG', 0, 2024),
        ('Bug di modul approval cuti',              2, 'Hendra Kusuma', 'MB', 1, 2024),
        ('Konsultasi best practice HRIS',           4, 'Fajar Nugroho', 'NG', 2, 2024),
        ('Error cetak laporan akuntansi',           1, 'Hendra Kusuma', 'TJ', 1, 2025),
        ('Karakter khusus rusak di PDF',            2, 'Dian Permata',  'MB', 2, 2025),
    ]

    for i, (subj, ttype_i, aname, ckey, sla_i, year) in enumerate(tickets):
        assignee = EN[aname]
        status   = ticket_status_for(aname)
        tno      = f'TKT-{year}-{i+1:04d}'
        crt      = in_year(year, 1, 10)
        res_at   = past(30) if status in ('resolved','closed') else None
        cls_at   = past(10) if status == 'closed' else None
        cont_id  = CONT_ID.get(ckey)
        db.execute(
            '''INSERT INTO sc_tickets(ticket_no,customer_id,contract_id,support_type_id,
               sla_category_id,subject,description,status,reported_by,reported_at,
               assigned_to,resolved_at,closed_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (tno, CUST_ID[ckey], cont_id, STYPE_ID[ttype_i], SLA_ID[sla_i],
             subj, lorem(), status,
             rng.choice(['user@customer.com','helpdesk@customer.com']),
             crt, assignee, res_at, cls_at, crt)
        )
        tid = db.execute("SELECT id FROM sc_tickets WHERE ticket_no=?", (tno,)).fetchone()['id']
        db.execute(
            "INSERT INTO sc_ticket_assignees(ticket_id,employee_id,divisi,role_note) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
            (tid, assignee, next(r[2] for r in emps_raw if EN.get(r[0]) == assignee), 'PIC')
        )
        db.execute(
            '''INSERT INTO sc_ticket_history(ticket_id,changed_by,action,notes,created_at)
               VALUES(?,?,?,?,?)''',
            (tid, UID['superadmin'], 'created', 'Tiket dibuat', crt)
        )
        if status in ('in_progress','resolved','closed'):
            db.execute(
                '''INSERT INTO sc_ticket_history(ticket_id,changed_by,action,notes,created_at)
                   VALUES(?,?,?,?,?)''',
                (tid, assignee, 'in_progress', 'Sedang ditangani', past(60))
            )
        if status in ('resolved','closed') and res_at:
            db.execute(
                '''INSERT INTO sc_ticket_history(ticket_id,changed_by,action,notes,created_at)
                   VALUES(?,?,?,?,?)''',
                (tid, assignee, 'resolved', 'Diselesaikan.', res_at)
            )
    db.commit()

    # POC / Presales
    poc_list = [
        ('POC-2024-001', 'DK', 'Demo HRIS untuk PT Delta Karya',      'poc',      'completed',
         [EN['Fajar Nugroho'], EN['Maya Sari'], EN['Dewi Rahayu']]),
        ('POC-2024-002', 'DK', 'Presales Presentasi Modul ERP',        'presales', 'completed',
         [EN['Fajar Nugroho'], EN['Rina Wulandari']]),
        ('POC-2025-001', 'DK', 'POC Integrasi Mobile App',             'poc',      'in_progress',
         [EN['Dewi Rahayu'], EN['Irwan Fauzi'], EN['Yuli Astuti']]),
        ('POC-2025-002', 'NG', 'Presales Pengembangan CRM Custom',     'presales', 'in_progress',
         [EN['Maya Sari'], EN['Budi Santoso']]),
        ('POC-2025-003', 'DK', 'Demo Modul Payroll & Absensi',         'poc',      'new',
         [EN['Fajar Nugroho'], EN['Dimas Kurniawan']]),
    ]
    for req_no, ckey, subj, rtype, status, assignees in poc_list:
        db.execute(
            '''INSERT INTO sc_presales_requests(req_no,customer_id,request_type,
               subject,description,status,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)''',
            (req_no, CUST_ID[ckey], rtype, subj, lorem(), status, UID['superadmin'], past(120))
        )
        rid = db.execute("SELECT id FROM sc_presales_requests WHERE req_no=?", (req_no,)).fetchone()['id']
        for eid in assignees:
            db.execute(
                "INSERT INTO sc_presales_assignees(request_id,employee_id,divisi) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (rid, eid, next(r[2] for r in emps_raw if EN.get(r[0]) == eid))
            )
        db.execute(
            '''INSERT INTO sc_presales_history(request_id,changed_by,changed_by_name,action,notes,created_at)
               VALUES(?,?,?,?,?,?)''',
            (rid, UID['superadmin'], 'Superadmin', 'created', f'Request {rtype} dibuat', past(90))
        )
        if status in ('in_progress','completed'):
            db.execute(
                '''INSERT INTO sc_presales_history(request_id,changed_by,changed_by_name,action,notes,created_at)
                   VALUES(?,?,?,?,?,?)''',
                (rid, UID['budi'], 'Budi Santoso', 'updated', 'Tim mulai persiapan', past(60))
            )
        if status == 'completed':
            db.execute(
                '''INSERT INTO sc_presales_history(request_id,changed_by,changed_by_name,action,notes,created_at)
                   VALUES(?,?,?,?,?,?)''',
                (rid, UID['budi'], 'Budi Santoso', 'completed', 'Selesai, feedback positif dari klien', past(30))
            )
    db.commit()

    # ── [6] ASSETCORE ────────────────────────────────────────────────────────
    print('[6/10] AssetCore...')
    for i, (sn, emp) in enumerate(
        [(f'SN-DELL-{i:04d}', emp_ids[i % len(emp_ids)]) for i in range(10)] +
        [('SN-HP-001', None), ('SN-EPS-001', None)]
    ):
        dtype = 'Printer' if 'EPS' in sn else 'Laptop'
        brand = 'Dell' if 'DELL' in sn else ('HP' if 'HP' in sn else 'Epson')
        db.execute(
            '''INSERT INTO ac_assets(device_type,brand,os,processor,ram,disk,
               serial_number,purchase_date,condition,employee_id) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (dtype, brand,
             'Windows 11 Pro' if dtype == 'Laptop' else '',
             'Intel Core i5' if dtype == 'Laptop' else '',
             '8 GB' if dtype == 'Laptop' else '',
             '256 GB SSD' if dtype == 'Laptop' else '',
             sn, '2022-01-15', 'Baik', emp)
        )
    for dtype, brand, model, sn, loc in [
        ('Switch','Cisco','SG350-24','SN-CISCO-001','Server Room'),
        ('Router','MikroTik','RB4011','SN-MT-001','Server Room'),
    ]:
        db.execute(
            "INSERT INTO ac_infrastructure(device_type,brand,model,serial_number,location,status) VALUES(?,?,?,?,?,?)",
            (dtype, brand, model, sn, loc, 'Aktif')
        )
    for sname, key, ltype, year, seats in [
        ('Microsoft Office 365','OFC365-KEY-001','Subscription',2025,15),
        ('Antivirus Kaspersky', 'KASP-KEY-001',  'Subscription',2025,20),
    ]:
        db.execute(
            "INSERT INTO ac_licenses(software_name,license_key,license_type,year,max_seats,is_active) VALUES(?,?,?,?,?,1)",
            (sname, key, ltype, year, seats)
        )
    for provider, cat, billing, start, end in [
        ('Google Workspace','SaaS','Monthly','2025-01-01','2025-12-31'),
        ('AWS EC2','IaaS','Monthly','2025-01-01','2025-12-31'),
        ('GitHub Enterprise','SaaS','Yearly','2025-01-01','2025-12-31'),
    ]:
        db.execute(
            "INSERT INTO ac_subscriptions(provider,category,billing_cycle,start_date,end_date,is_active) VALUES(?,?,?,?,?,1)",
            (provider, cat, billing, start, end)
        )
    db.commit()

    # ── [7] BOOKINGCORE ──────────────────────────────────────────────────────
    print('[7/10] BookingCore...')
    for rname, rtype, loc, cap in [
        ('Ruang Rapat A','room','Lantai 2',10),
        ('Ruang Rapat B','room','Lantai 3',20),
        ('Kendaraan Operasional','vehicle','Parkir B1',6),
        ('Proyektor Portable','equipment','Gudang IT',1),
    ]:
        db.execute("INSERT INTO bk_resources(name,type,location,capacity,is_active) VALUES(?,?,?,?,1)",
                   (rname, rtype, loc, cap))
    res_ids = [r['id'] for r in db.execute("SELECT id FROM bk_resources").fetchall()]
    for _ in range(20):
        rid = rng.choice(res_ids); uid = rng.choice(list(UID.values()))
        bd  = rand_date(datetime.date(2025,1,1), datetime.date(2026,12,31))
        hs  = rng.randint(8,15); he = hs + rng.randint(1,3)
        db.execute(
            "INSERT INTO bk_bookings(resource_id,title,booked_by,start_dt,end_dt,status,attendee_count) VALUES(?,?,?,?,?,?,?)",
            (rid, rng.choice(['Rapat Tim','Review Sprint','Meeting Client','Training']),
             uid, f'{bd} {hs:02d}:00', f'{bd} {he:02d}:00',
             rng.choice(['confirmed','pending','cancelled']), rng.randint(2,15))
        )
    db.commit()

    # ── [8] PROJECTCORE ──────────────────────────────────────────────────────
    print('[8/10] ProjectCore...')

    projects = [
        # PRJ-2025-001: HRIS aktif — SIT selesai, UAT jalan
        ('PRJ-2025-001','Implementasi HRIS PT Maju Bersama',
         CUST_ID['MB'], EN['Rina Wulandari'], EN['Fajar Nugroho'], EN['Dewi Rahayu'],
         '2025-01-15','2025-10-31','#0ea5e9','active'),
        # PRJ-2025-002: CRM internal — baru mulai
        ('PRJ-2025-002','Pengembangan CRM Internal',
         None, EN['Budi Santoso'], EN['Dewi Rahayu'], EN['Irwan Fauzi'],
         '2025-04-01','2025-12-31','#10b981','active'),
        # PRJ-2024-003: Migrasi ERP — completed
        ('PRJ-2024-003','Migrasi ERP CV Teknologi Jaya',
         CUST_ID['TJ'], EN['Rina Wulandari'], EN['Fajar Nugroho'], EN['Maya Sari'],
         '2024-03-01','2025-01-31','#f59e0b','completed'),
        # PRJ-2025-004: Mobile Field Force — planning
        ('PRJ-2025-004','Pengembangan Mobile Field Force',
         CUST_ID['NG'], EN['Budi Santoso'], EN['Irwan Fauzi'], EN['Yuli Astuti'],
         '2025-06-01','2026-03-31','#8b5cf6','active'),
    ]
    PROJ_ID = []
    for code, pname, cid, pic, impl, co, sd, ed, color, status in projects:
        db.execute(
            '''INSERT INTO pc_projects(code,name,customer_id,pic_id,implementor_id,
               co_leader_id,start_date,end_date,color,status,created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (code, pname, cid, pic, impl, co, sd, ed, color, status, UID['superadmin'])
        )
        PROJ_ID.append(db.execute("SELECT id FROM pc_projects WHERE code=?", (code,)).fetchone()['id'])

    # Members: STAR terlibat banyak proyek, LAZY hanya 1
    # Dewi (STAR): 4 proyek; Fajar (STAR): 3 proyek; Reza (LAZY): hanya PRJ-001
    proj_members_map = {
        PROJ_ID[0]: [
            # HRIS — semua divisi
            (EN['Dewi Rahayu'],     'programmer'),   # STAR
            (EN['Andi Wijaya'],     'programmer'),   # SOLID
            (EN['Irwan Fauzi'],     'programmer'),   # SOLID
            (EN['Nadia Rahma'],     'programmer'),   # AVERAGE
            (EN['Reza Pratama'],    'programmer'),   # LAZY — ikut 1 proyek saja
            (EN['Siti Nurhaliza'],  'qc_tester'),    # STAR
            (EN['Putri Handayani'],'qc_tester'),    # AVERAGE
            (EN['Maya Sari'],       'implementor'),  # SOLID
            (EN['Yuli Astuti'],     'implementor'),  # AVERAGE
        ],
        PROJ_ID[1]: [
            # CRM Internal — tanpa Reza (lazy)
            (EN['Dewi Rahayu'],     'programmer'),   # STAR — multi-project
            (EN['Irwan Fauzi'],     'programmer'),   # SOLID
            (EN['Nadia Rahma'],     'programmer'),   # AVERAGE
            (EN['Siti Nurhaliza'],  'qc_tester'),    # STAR
            (EN['Fajar Nugroho'],   'implementor'),  # STAR
        ],
        PROJ_ID[2]: [
            # ERP Migrasi (completed) — core team
            (EN['Dewi Rahayu'],     'programmer'),   # STAR
            (EN['Andi Wijaya'],     'programmer'),   # SOLID
            (EN['Siti Nurhaliza'],  'qc_tester'),    # STAR
            (EN['Putri Handayani'],'qc_tester'),    # AVERAGE
            (EN['Fajar Nugroho'],   'implementor'),  # STAR
            (EN['Dimas Kurniawan'], 'implementor'),  # BELOW
        ],
        PROJ_ID[3]: [
            # Mobile — tanpa Reza, lebih banyak junior
            (EN['Irwan Fauzi'],     'programmer'),   # SOLID
            (EN['Andi Wijaya'],     'programmer'),   # SOLID
            (EN['Nadia Rahma'],     'programmer'),   # AVERAGE
            (EN['Putri Handayani'],'qc_tester'),    # AVERAGE
            (EN['Yuli Astuti'],     'implementor'),  # AVERAGE
            (EN['Dimas Kurniawan'], 'implementor'),  # BELOW
        ],
    }
    for pid, members in proj_members_map.items():
        for eid, role in members:
            db.execute(
                "INSERT INTO pc_members(project_id,employee_id,role) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (pid, eid, role)
            )

    # Phases
    phase_defs = [('sit','SIT'),('uat','UAT'),('bast','BAST'),
                  ('promote','Promote to Production'),('golive','Go Live')]
    proj_phase_st = [
        ['done','in_progress','planned','planned','planned'],
        ['in_progress','planned','planned','planned','planned'],
        ['done','done','done','done','done'],
        ['planned','planned','planned','planned','planned'],
    ]
    for pi, (pid, proj) in enumerate(zip(PROJ_ID, projects)):
        sd  = datetime.date.fromisoformat(proj[6])
        ed  = datetime.date.fromisoformat(proj[7])
        seg = max(1, (ed - sd).days // len(phase_defs))
        for j, (ptype, pname) in enumerate(phase_defs):
            ps   = (sd + datetime.timedelta(days=seg*j)).isoformat()
            pe   = (sd + datetime.timedelta(days=seg*(j+1)-1)).isoformat()
            st   = proj_phase_st[pi][j]
            sign = past(30) if st == 'done' else None
            db.execute(
                '''INSERT INTO pc_phases(project_id,phase_type,name,start_date,end_date,
                   status,sort_order,pic_id,sign_off_date) VALUES(?,?,?,?,?,?,?,?,?)''',
                (pid, ptype, pname, ps, pe, st, j+1,
                 [EN['Siti Nurhaliza'], EN['Fajar Nugroho'], EN['Rina Wulandari'],
                  EN['Budi Santoso'], EN['Rina Wulandari']][j], sign)
            )

    # ── Tasks: volume & status sesuai performance profile ────────────────────
    # Template task per kategori (judul, divisi yang bertanggung jawab, priority)
    TASK_TMPL = {
        'Programmer': [
            ('Analisa & desain database schema {}',      'High'),
            ('Develop API endpoint modul {}',            'High'),
            ('Develop UI/frontend modul {}',             'Medium'),
            ('Implementasi business logic {}',           'High'),
            ('Code review & refactoring modul {}',       'Medium'),
            ('Fix bug: kalkulasi {} tidak tepat',        'High'),
            ('Fix bug: error saat load data {}',         'Medium'),
            ('Dokumentasi teknis API {}',                'Low'),
            ('Setup unit test modul {}',                 'Medium'),
            ('Optimasi performa query {}',               'Medium'),
            ('Integrasi modul {} dengan sistem lain',    'High'),
            ('Deploy & konfigurasi {} di staging',       'Medium'),
        ],
        'Tester': [
            ('Buat test case skenario {} happy path',    'High'),
            ('Buat test case skenario {} edge case',     'Medium'),
            ('Eksekusi UAT modul {}',                    'High'),
            ('Regression test setelah fix bug {}',       'High'),
            ('Bug reporting & verifikasi fix {}',        'Medium'),
            ('Performance test modul {}',                'Medium'),
        ],
        'Implementor/BPS': [
            ('Analisa kebutuhan bisnis modul {}',        'High'),
            ('Mapping proses bisnis ke sistem {}',       'High'),
            ('Konfigurasi master data {}',               'Medium'),
            ('Migrasi data dari sistem lama {}',         'High'),
            ('Training user modul {}',                   'Medium'),
            ('Dokumentasi SOP penggunaan {}',            'Low'),
            ('Pendampingan UAT user {}',                 'Medium'),
        ],
        'Management': [
            ('Kickoff meeting proyek {}',                'High'),
            ('Review progress mingguan {}',              'Medium'),
            ('Koordinasi dengan client {}',              'High'),
            ('Prepare dokumen BAST {}',                  'High'),
            ('Risk assessment proyek {}',                'Medium'),
        ],
    }

    modules_label = ['Payroll','Karyawan','Cuti','Absensi','Laporan','Dashboard',
                     'Inventori','Penjualan','API','Mobile','Autentikasi','Notifikasi']

    def make_tasks_for_employee(pid, eid, emp_name, divisi, n_tasks):
        """Buat n_tasks task untuk employee di proyek pid, status sesuai perf."""
        perf    = PERF.get(emp_name, 3)
        div_key = divisi if divisi in TASK_TMPL else 'Programmer'
        tmpls   = TASK_TMPL[div_key]
        for ti in range(n_tasks):
            tmpl, prio = rng.choice(tmpls)
            mod    = rng.choice(modules_label)
            title  = f'{tmpl.format(mod)} [{emp_name[:4]}-{ti+1}]'
            status = pick_task_status(perf)
            due    = past(20) if status == 'done' else (
                     past(5)  if status == 'review' else future(30))
            db.execute(
                '''INSERT INTO pc_tasks(project_id,title,description,status,priority,due_date,sort_order)
                   VALUES(?,?,?,?,?,?,?)''',
                (pid, title, lorem(), status, prio, due, ti)
            )
            task_id = db.execute(
                "SELECT id FROM pc_tasks WHERE project_id=? AND title=?", (pid, title)
            ).fetchone()['id']
            db.execute(
                "INSERT INTO pc_task_assignees(task_id,employee_id) VALUES(?,?) ON CONFLICT DO NOTHING",
                (task_id, eid)
            )
        return n_tasks

    # Jumlah task per employee per proyek berdasarkan performance
    TASK_VOLUME = {5: 8, 4: 6, 3: 4, 2: 3, 1: 2}

    total_tasks = 0
    for pi, (pid, proj) in enumerate(zip(PROJ_ID, projects)):
        members = proj_members_map[pid]
        for eid, role in members:
            name   = next(r[0] for r in emps_raw if EN.get(r[0]) == eid)
            divisi = next(r[2] for r in emps_raw if EN.get(r[0]) == eid)
            perf   = PERF.get(name, 3)
            n      = TASK_VOLUME[perf]
            # Star & solid di proyek aktif dapat lebih banyak task
            if perf >= 4 and proj[9] == 'active':
                n += 2
            total_tasks += make_tasks_for_employee(pid, eid, name, divisi, n)

    # Issues per proyek
    issue_pool = [
        ('Kalkulasi PPh 21 salah untuk karyawan golongan 3',      'Critical', 'Done'),
        ('Slip gaji tidak tampil nama rekening bank',              'High',     'Done'),
        ('Error saat import data lebih dari 1000 baris',           'High',     'In Progress'),
        ('UI mobile tidak responsif di perangkat iOS lama',        'Medium',   'In Progress'),
        ('Timeout saat generate laporan konsolidasi besar',        'High',     'Open'),
        ('Approval level 3 tidak terpicu otomatis',                'High',     'In Progress'),
        ('Data saldo cuti tidak update real-time',                 'Medium',   'Open'),
        ('Karakter khusus (æ, ø) rusak di export PDF',            'Low',      'Done'),
        ('Notifikasi email terkirim duplikat',                     'Medium',   'Done'),
        ('Dashboard tidak load di browser Firefox versi lama',     'Low',      'Open'),
        ('Error pagination setelah halaman ke-10',                 'Medium',   'In Progress'),
        ('Validasi NIK tidak menolak format yang salah',           'High',     'Done'),
        ('Session timeout terlalu cepat saat idle',                'Medium',   'Open'),
        ('Laporan PDF terpotong di halaman terakhir',              'Low',      'Done'),
    ]
    for pi, pid in enumerate(PROJ_ID):
        n_issues = [7, 5, 9, 4][pi]
        sampled  = rng.sample(issue_pool, min(n_issues, len(issue_pool)))
        proj_prog = [e for e, r in proj_members_map[pid] if r == 'programmer']
        if not proj_prog: proj_prog = prog_ids[:2]
        for ii, (title, prio, status) in enumerate(sampled):
            ino = f'ISS-P{pi+1}-{ii+1:03d}'
            pic = rng.choice(proj_prog)
            db.execute(
                '''INSERT INTO pc_issues(project_id,issue_no,title,description,priority,
                   status_programmer,pic_programmer_id,created_at) VALUES(?,?,?,?,?,?,?,?)''',
                (pid, ino, title, lorem(), prio, status, pic, past(90))
            )

    # Milestones
    ms_defs = [('Kickoff Meeting','completed'),('Sign-off Analisa','completed'),
               ('UAT Selesai','in_progress'),('Go Live','upcoming'),('Pelatihan User','upcoming')]
    for pi, pid in enumerate(PROJ_ID):
        sd  = datetime.date.fromisoformat(projects[pi][6])
        ed  = datetime.date.fromisoformat(projects[pi][7])
        seg = max(1, (ed - sd).days // len(ms_defs))
        for j, (title, st) in enumerate(ms_defs):
            due = (sd + datetime.timedelta(days=seg*(j+1))).isoformat()
            ast = 'completed' if projects[pi][8] == 'completed' else st
            db.execute(
                "INSERT INTO pc_milestones(project_id,title,description,due_date,status) VALUES(?,?,?,?,?)",
                (pid, title, lorem(), due, ast)
            )
    db.commit()

    # ── [9] TALENTCORE — Evaluasi lengkap ────────────────────────────────────
    print('[9/10] TalentCore evaluasi...')

    skill_cats_per_div = {
        'Programmer':      [('Technical Skills',['Backend Dev','Frontend Dev','Database','DevOps','Code Review']),
                            ('Soft Skills',['Komunikasi','Problem Solving','Inisiatif'])],
        'Implementor/BPS': [('Technical Skills',['Analisa Bisnis','Konfigurasi Sistem','Dokumentasi','Pelatihan User']),
                            ('Soft Skills',['Komunikasi','Presentasi','Manajemen Waktu'])],
        'Helpdesk Support':[('Technical Skills',['Troubleshooting','Tiket Management','SLA Awareness','Tools Support']),
                            ('Soft Skills',['Komunikasi','Empati','Respon Cepat'])],
        'Tester':          [('Technical Skills',['Test Case Design','Automation Test','Bug Reporting','Regression Test']),
                            ('Soft Skills',['Ketelitian','Komunikasi','Analisa'])],
        'Management':      [('Leadership',['Delegasi','Coaching','Decision Making','Strategic Thinking']),
                            ('Soft Skills',['Komunikasi','Negosiasi','Problem Solving'])],
    }

    competency_defs = [
        ('Kualitas Hasil Kerja',        1.5, 1),
        ('Ketepatan Waktu Penyelesaian', 1.5, 1),
        ('Inisiatif & Inovasi',          1.0, 0),
        ('Kerjasama Tim',                1.0, 0),
        ('Komunikasi',                   1.0, 0),
    ]
    COMP_ID = {}
    for divisi in DIVISI_LIST:
        for point, bobot, is_hs in competency_defs:
            db.execute(
                "INSERT INTO competency_items(divisi,point_measurement,bobot,is_hardskill) VALUES(?,?,?,?)",
                (divisi, point, bobot, is_hs)
            )
            COMP_ID[f'{divisi}.{point}'] = db.execute(
                "SELECT id FROM competency_items WHERE divisi=? AND point_measurement=?", (divisi, point)
            ).fetchone()['id']

    ability_defs = [
        ('Menguasai tools & teknologi sesuai peran',
         'Sangat menguasai, jadi referensi tim','Menguasai dengan baik',
         'Cukup menguasai dengan bimbingan','Perlu banyak bimbingan'),
        ('Menyelesaikan pekerjaan secara mandiri',
         'Selalu mandiri dan bantu tim lain','Mandiri dalam sebagian besar pekerjaan',
         'Mandiri dengan supervisi minimal','Masih butuh supervisi penuh'),
        ('Berkontribusi pada peningkatan proses',
         'Inisiator perbaikan proses utama','Aktif memberikan saran perbaikan',
         'Sesekali memberikan masukan','Belum aktif berkontribusi'),
    ]
    AITEM_ID = {}
    for divisi in DIVISI_LIST:
        for name, da, db_, dc, dd in ability_defs:
            db.execute(
                "INSERT INTO ability_items(divisi,name,desc_a,desc_b,desc_c,desc_d) VALUES(?,?,?,?,?,?)",
                (divisi, name, da, db_, dc, dd)
            )
            AITEM_ID[f'{divisi}.{name}'] = db.execute(
                "SELECT id FROM ability_items WHERE divisi=? AND name=?", (divisi, name)
            ).fetchone()['id']

    SITEM_ID = {}
    for divisi, cats in skill_cats_per_div.items():
        for cname, items in cats:
            db.execute("INSERT INTO skill_categories(divisi,name) VALUES(?,?)", (divisi, cname))
            cid = db.execute(
                "SELECT id FROM skill_categories WHERE divisi=? AND name=?", (divisi, cname)
            ).fetchone()['id']
            for iname in items:
                db.execute("INSERT INTO skill_items(category_id,name,bobot) VALUES(?,?,1.0)", (cid, iname))
                SITEM_ID[f'{divisi}.{cname}.{iname}'] = db.execute(
                    "SELECT id FROM skill_items WHERE category_id=? AND name=?", (cid, iname)
                ).fetchone()['id']

    # Project entries sesuai proyek nyata
    proj_entries_pool = [
        ('project',  'Implementasi HRIS PT Maju Bersama',   'Develop & testing modul Payroll',    'DONE'),
        ('project',  'Implementasi HRIS PT Maju Bersama',   'UAT pendampingan user',               'DONE'),
        ('project',  'Pengembangan CRM Internal',            'Backend API development',             'ON_PROGRESS'),
        ('project',  'Migrasi ERP CV Teknologi Jaya',        'Data migration & UAT',               'DONE'),
        ('project',  'Pengembangan Mobile Field Force',      'Wireframe & analisa kebutuhan',       'ON_PROGRESS'),
        ('activity', '',                                     'Pelatihan internal framework baru',   'DONE'),
        ('activity', '',                                     'Review & refactor legacy code',       'DONE'),
        ('activity', '',                                     'Pembuatan dokumentasi teknis',        'DONE'),
        ('activity', '',                                     'Knowledge sharing session tim',       'DONE'),
    ]

    # Hitung proyek yang diikuti per karyawan (untuk project_entries yang relevan)
    emp_project_count = {}
    for pid, members in proj_members_map.items():
        for eid, _ in members:
            emp_project_count[eid] = emp_project_count.get(eid, 0) + 1

    for eid_i, eid in enumerate(emp_ids):
        name   = emps_raw[eid_i][0]
        divisi = emps_raw[eid_i][2]
        level  = emps_raw[eid_i][3]
        perf   = PERF.get(name, 3)
        n_proj = emp_project_count.get(eid, 1)

        for year in [2024, 2025]:
            periode = f'Q4-{year}'
            # Score naik di 2025 untuk performer, stagnan untuk lazy
            bonus = 1 if (year == 2025 and perf >= 4) else 0

            db.execute(
                '''INSERT INTO evaluations(employee_id,periode,status,evaluator,created_at)
                   VALUES(?,?,?,?,?)''',
                (eid, periode, 'completed', 'Budi Santoso', f'{year}-11-15')
            )
            eval_id = db.execute(
                "SELECT id FROM evaluations WHERE employee_id=? AND periode=?", (eid, periode)
            ).fetchone()['id']

            # Skill scores — mencerminkan performance
            cats = skill_cats_per_div.get(divisi, skill_cats_per_div['Programmer'])
            for cname, items in cats:
                for iname in items:
                    key = f'{divisi}.{cname}.{iname}'
                    if key in SITEM_ID:
                        sc = eval_score(perf, bonus)
                        db.execute(
                            "INSERT INTO skill_scores(eval_id,skill_item_id,score) VALUES(?,?,?)",
                            (eval_id, SITEM_ID[key], sc)
                        )

            # Ability scores — A/B untuk top, C/D untuk bawah
            ability_choices = {5:['A','A','B'], 4:['A','B','B'],
                               3:['B','C','C'], 2:['C','C','D'], 1:['C','D','D']}
            for aname, *_ in ability_defs:
                key = f'{divisi}.{aname}'
                if key in AITEM_ID:
                    lvl = rng.choice(ability_choices.get(perf, ['C']))
                    # Bonus: 2025 naik 1 level untuk perf >= 4
                    if bonus and lvl == 'B': lvl = rng.choice(['A','B'])
                    db.execute(
                        "INSERT INTO ability_scores(eval_id,ability_item_id,level) VALUES(?,?,?)",
                        (eval_id, AITEM_ID[key], lvl)
                    )

            # Competency
            for point, *_ in competency_defs:
                key = f'{divisi}.{point}'
                if key in COMP_ID:
                    rating = eval_score(perf, bonus)
                    db.execute(
                        "INSERT INTO competency_scores(eval_id,competency_item_id,rating) VALUES(?,?,?)",
                        (eval_id, COMP_ID[key], rating)
                    )

            # Project entries: star dapat lebih banyak (ikut banyak proyek)
            n_entries = min(n_proj + 2, len(proj_entries_pool))
            if perf <= 1: n_entries = 1   # lazy hanya 1 entry
            for j, (etype, pname, detail, pst) in enumerate(
                rng.sample(proj_entries_pool, n_entries)
            ):
                db.execute(
                    '''INSERT INTO project_entries(eval_id,entry_type,project_name,detail_task,status,sort_order)
                       VALUES(?,?,?,?,?,?)''',
                    (eval_id, etype, pname, detail, pst, j)
                )

            # Peer reviews — star dapat pujian, lazy dapat kritik
            peer_pool = [e for e in emp_ids if e != eid]
            feedbacks_by_perf = {
                5: [f'{name} sangat proaktif, selalu deliver tepat waktu dan kualitas tinggi.',
                    f'Kontribusi {name} di proyek sangat signifikan, jadi tulang punggung tim.',
                    f'{name} aktif dalam knowledge sharing dan bantu tim lain.',
                    f'Bekerja sama dengan {name} sangat menyenangkan, solid dan responsif.'],
                4: [f'{name} konsisten deliver pekerjaan dengan baik.',
                    f'Kolaborasi dengan {name} lancar, komunikatif.',
                    f'{name} perlu sedikit peningkatan di inisiatif, tapi overall bagus.'],
                3: [f'{name} cukup baik, tapi kadang perlu diingatkan deadline.',
                    f'{name} butuh improvement di kecepatan penyelesaian task.',
                    f'Kerjasama dengan {name} oke, perlu lebih proaktif.'],
                2: [f'{name} sering terlambat selesaikan task, perlu improvement.',
                    f'Kualitas kerja {name} perlu ditingkatkan, banyak revisi.',
                    f'{name} kurang komunikatif saat ada hambatan.'],
                1: [f'{name} sering tidak menyelesaikan task tepat waktu.',
                    f'Banyak task {name} yang masih backlog tanpa progress.',
                    f'{name} perlu bimbingan intensif dan motivasi yang lebih tinggi.'],
            }
            for slot, peer_eid in enumerate(rng.sample(peer_pool, min(2, len(peer_pool))), 1):
                peer_name = emps_raw[emp_ids.index(peer_eid)][0]
                db.execute(
                    "INSERT INTO peer_reviews(eval_id,slot,reviewer_name,feedback) VALUES(?,?,?,?)",
                    (eval_id, slot, peer_name, rng.choice(feedbacks_by_perf.get(perf, feedbacks_by_perf[3])))
                )

            # Eval review oleh atasan
            reviewer = UID['rina'] if divisi == 'Management' else UID['budi']
            review_notes_by_perf = {
                5: [f'Kinerja {name} sangat memuaskan di {periode}. Direkomendasikan kenaikan gaji dan jenjang karir.',
                    f'{name} konsisten overperform target. Kandidat untuk lead di proyek berikutnya.'],
                4: [f'Kinerja {name} baik dan sesuai target {periode}. Direkomendasikan kenaikan sesuai grade.',
                    f'{name} solid performer, terus pertahankan konsistensi.'],
                3: [f'Kinerja {name} cukup, namun perlu peningkatan kecepatan dan inisiatif.',
                    f'{name} mencapai target dasar di {periode}, kenaikan standar.'],
                2: [f'Kinerja {name} di bawah harapan. Perlu PIP (Performance Improvement Plan).',
                    f'Ada beberapa task {name} yang terlambat dan perlu dikawal lebih ketat.'],
                1: [f'Kinerja {name} sangat mengecewakan. Kontrak tidak diperpanjang.',
                    f'Task {name} banyak yang tidak selesai. Tidak memenuhi standar minimum.'],
            }
            db.execute(
                '''INSERT INTO eval_reviews(eval_id,reviewer_user_id,reviewer_role,notes,status,submitted_at)
                   VALUES(?,?,?,?,?,?)''',
                (eval_id, reviewer, 'manager',
                 rng.choice(review_notes_by_perf.get(perf, review_notes_by_perf[3])),
                 'approved', f'{year}-12-01')
            )
    db.commit()

    # ── [10] RINGKASAN ───────────────────────────────────────────────────────
    task_count   = db.execute("SELECT COUNT(*) FROM pc_tasks").fetchone()[0]
    assign_count = db.execute("SELECT COUNT(*) FROM pc_task_assignees").fetchone()[0]

    print('\n[10/10] Selesai!')
    print('=' * 65)
    print(f'  Employees  : {len(emp_ids)} ({sum(1 for r in emps_raw if r[4]=="tetap")} tetap, '
          f'{sum(1 for r in emps_raw if r[4]=="kontrak")} kontrak — 1 expired)')
    print(f'  Users      : {len(users_def)} user (password: Admin@123)')
    print(f'  Salary     : 2024/2025/2026 — kenaikan 0-12% sesuai kinerja')
    print(f'  Tickets    : {len(tickets)} tiket per-PIC (Helpdesk & Implementor)')
    print(f'  POC        : {len(poc_list)} request presales/POC')
    print(f'  Projects   : {len(projects)} proyek, {task_count} tasks ({assign_count} assignee)')
    print(f'  Performance profile:')
    for name, *_ in emps_raw:
        p = PERF[name]
        label = {5:'** STAR',4:'OK SOLID',3:'~  AVERAGE',2:'v  BELOW',1:'x  LAZY'}[p]
        n_proj = emp_project_count.get(EN[name], 0)
        print(f'    {label:12s} {name} ({n_proj} proyek)')
    print(f'  Evaluasi   : {len(emp_ids)*2} eval (skill+ability+competency+peer+review)')
    print('=' * 65)
    print('  Login: superadmin / Admin@123')
    print('=' * 65)
