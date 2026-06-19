# Blueprint super-us — Arsitektur Keseluruhan Aplikasi

## 1. Gambaran Umum

```
┌─────────────────────────────────────────────────────────────────────┐
│                        super-us Platform                            │
│                                                                     │
│  ┌──────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │TalentCore│  │ SupportCore │  │ BookingCore │  │  AssetCore  │  │
│  │    /     │  │  /support/  │  │  /booking/  │  │   /aset/    │  │
│  └──────────┘  └─────────────┘  └─────────────┘  └─────────────┘  │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                      Portal  /portal/                        │   │
│  │         Auth · MFA · User · Role · Permission · Audit        │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │              Single Flask app.py (~6500 baris)               │   │
│  │              Single SQLite database (evaluasi.db)            │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

**Prinsip arsitektur:**
- Satu file `app.py` — semua modul dalam satu proses Flask
- Satu database SQLite — tabel dipisah dengan prefix per modul
- Satu session/auth — login sekali, akses semua app yang diizinkan
- Sidebar adaptif — berubah otomatis sesuai modul yang aktif

---

## 2. Stack Teknologi

```
┌─────────────────────────────────────────────────────────────────┐
│  Client (Browser)                                               │
│  Bootstrap 5.3 · Bootstrap Icons · Nunito font · Vanilla JS    │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼────────────────────────────────────┐
│  Nginx (reverse proxy)                                          │
│  /static/ → langsung serve · / → proxy ke unix socket          │
└────────────────────────────┬────────────────────────────────────┘
                             │ unix socket
┌────────────────────────────▼────────────────────────────────────┐
│  Gunicorn (WSGI server)                                         │
│  workers=2, threads=4, worker_class=gthread                     │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  Flask app.py                                                   │
│  ├── Auth & MFA (pyotp, itsdangerous)                           │
│  ├── APScheduler (background jobs)                              │
│  ├── SQLite via sqlite3 (built-in)                              │
│  └── Jinja2 templates                                           │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  /var/lib/evaluasi/evaluasi.db  (SQLite)                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Struktur Database

### 3.1 Portal & Auth

```sql
users                      -- akun login
  id, username, name, email, password_hash
  role, is_active, mfa_secret, mfa_enabled
  employee_id → employees(id)

superapp_apps              -- registry aplikasi di portal
  id, slug, name, description, icon, color, bg_color
  url, is_active, is_coming_soon, sort_order, required_permission

user_app_access            -- akses user ke tiap app
  id, user_id → users(id), app_slug, role_name, granted_at

portal_roles               -- role yang bisa diassign per app
  id, app_slug, role_name, permissions (JSON)

audit_log                  -- log aktivitas sensitif
  id, user_id, action, entity, entity_id, detail, app_slug, created_at

password_reset_tokens      -- token reset password (1 jam TTL)
  id, user_id, token, expires_at, used
```

### 3.2 TalentCore (tanpa prefix)

```sql
employees                  -- data karyawan (shared, digunakan semua modul)
  id, name, divisi, jabatan, level, tipe_kepegawaian
  phone, email, telegram_id, is_active
  join_date, contract_end_date

evaluations                -- sesi evaluasi
  id, employee_id, periode, evaluator_id, status

eval_hardskill             -- skor hard skill
eval_competency            -- skor kompetensi
eval_ability               -- skor 18 item kemampuan
eval_projects              -- penilaian project

skill_templates            -- template skill per divisi
skill_items                -- item skill dalam template

salary_data                -- tabel gaji per karyawan per tahun
user_contracts             -- kontrak kerja (Tetap/Kontrak)
reminder_logs              -- log notifikasi terkirim

app_settings               -- konfigurasi SMTP, Telegram, WhatsApp, OpenWA
divisions                  -- daftar divisi
```

### 3.3 SupportCore (prefix `sc_`)

```sql
sc_customers               -- perusahaan customer pemegang kontrak
sc_customer_pics           -- PIC per customer (many-to-many → employees)
sc_apps                    -- aplikasi yang di-support per customer
sc_app_modules             -- modul dari tiap aplikasi

sc_services                -- jenis layanan teknis (instalasi, dll.)
sc_support_types           -- tipe support (Corrective/Preventive/Onsite)
sc_sla_categories          -- kategori SLA (Critical/High/Medium/Low)
  + response_time, resolve_time, workaround_time, maintenance_type

sc_contracts               -- kontrak support tahunan
sc_contract_services       -- layanan dalam kontrak (junction)
sc_contract_pics           -- PIC internal dalam kontrak

sc_tickets                 -- tiket support
sc_ticket_assignees        -- assignee per tiket (multi)
sc_ticket_logs             -- history perubahan tiket

sc_presales                -- request presales & POC
```

### 3.4 BookingCore (prefix `booking_`)

```sql
booking_resources          -- sumber daya yang bisa dibooking
  id, name, type (Ruangan/Kendaraan), capacity, description

bookings                   -- data reservasi
  id, resource_id, user_id, title, description
  start_datetime, end_datetime, status
  created_at, cancelled_at, cancel_reason
```

### 3.5 AssetCore (prefix `ac_`)

```sql
ac_assets                  -- laptop & PC karyawan
  id, employee_id → employees(id)
  device_type, brand, os, os_license_type
  processor, ram, disk, office_version
  asset_tag, serial_number, purchase_date
  condition, notes

ac_asset_software          -- software terpasang per asset
  id, asset_id → ac_assets(id), software_name

ac_infrastructure          -- perangkat jaringan & server
  id, device_type, brand, model, description
  serial_number, nickname, ups_group, location
  status, condition_notes

ac_licenses                -- lisensi software
  id, software_name, license_key, license_type
  version, year, max_seats, is_active, notes

ac_license_assignments     -- assign lisensi ke karyawan
  id, license_id → ac_licenses(id)
  employee_id → employees(id), seat_number

ac_subscriptions           -- SaaS & ISP subscription
  id, provider, category, billing_cycle
  start_date, end_date, username, password
  access_url, notes, is_active

ac_software_requests       -- request software dari karyawan
  id, employee_id, software_name, version, reason
  status (Pending/Approved/Installed/Rejected)
  requested_at, resolved_at, resolved_by, notes
```

---

## 4. Alur Autentikasi & Otorisasi

```
User buka URL
    │
    ▼
@login_required decorator
    │
    ├── Belum login → redirect /login
    │       │
    │       ▼
    │   POST /login (username + password)
    │       │
    │       ├── Password salah → flash error
    │       └── Benar → cek MFA
    │               │
    │               ├── MFA belum setup → redirect /mfa/setup
    │               │       └── scan QR → verify TOTP → aktifkan
    │               │
    │               └── MFA sudah ada → redirect /mfa
    │                       └── input 6-digit TOTP
    │                               │
    │                               └── Benar → set session → redirect /portal
    │
    └── Sudah login → lanjut
            │
            ▼
    cek has_permission(role, permission, db)
            │
            ├── superadmin → bypass semua
            ├── permission ada di role → izin
            └── tidak ada → flash "Akses ditolak"
```

### Permission Helper Pattern (per modul)

```python
# Contoh pattern di setiap modul:
def ac_require(perm):
    db = get_db()
    if not has_permission(session.get('user_role',''), perm, db):
        flash(f'Akses ditolak — permission "{perm}" diperlukan', 'danger')
        return False
    return True

@app.route('/aset/assets/new', methods=['GET','POST'])
@login_required
def ac_asset_new():
    if not ac_require('ac_manage_assets'):
        return redirect(url_for('ac_assets'))
    # ... logika route
```

---

## 5. Peta Route Lengkap

### Portal
| Method | URL | Fungsi |
|--------|-----|--------|
| GET | `/portal` | App launcher |
| GET | `/portal/open/<slug>` | Masuk ke app (set active_app) |
| GET/POST | `/portal/settings` | Toggle akses user × app |
| GET/POST | `/portal/users` | Kelola user |
| GET/POST | `/portal/users/add` | Tambah user |
| GET/POST | `/portal/users/<id>/edit` | Edit user |
| POST | `/portal/users/<id>/send-reset` | Kirim reset password |
| POST | `/portal/users/<id>/mfa-reset` | Reset MFA |
| GET/POST | `/portal/roles` | Kelola role & permission |
| GET/POST | `/portal/system-settings` | Konfigurasi notifikasi |
| GET | `/portal/audit` | Audit log |
| GET | `/portal/api/employees-search` | API autocomplete karyawan |

### TalentCore
| Method | URL | Fungsi |
|--------|-----|--------|
| GET | `/` | Dashboard karyawan |
| GET | `/karyawan` | Daftar karyawan |
| GET/POST | `/karyawan/<id>/eval/<periode>/hardskill` | Penilaian hard skill |
| GET/POST | `/karyawan/<id>/eval/<periode>/competency` | Penilaian kompetensi |
| GET/POST | `/karyawan/<id>/eval/<periode>/ability` | Penilaian kemampuan |
| GET/POST | `/karyawan/<id>/eval/<periode>/project` | Penilaian project |
| GET | `/karyawan/<id>/eval/<periode>/summary` | Ringkasan evaluasi |
| GET | `/eval/self/<token>` | Self-assessment (tokenized, no login) |
| GET | `/reviews` | Daftar semua evaluasi |
| GET | `/salary` | Tabel gaji |
| GET/POST | `/settings` | Konfigurasi notifikasi |
| GET | `/reminders` | Log reminder |
| GET/POST | `/admin` | Template evaluasi |
| GET/POST | `/admin/divisions` | Manajemen divisi |

### SupportCore
| Method | URL | Fungsi |
|--------|-----|--------|
| GET | `/support/` | Dashboard |
| GET/POST | `/support/customers` | Master customer |
| GET/POST | `/support/apps` | Master apps & modul |
| GET/POST | `/support/services` | Master layanan |
| GET/POST | `/support/support-types` | Master tipe support |
| GET/POST | `/support/sla-categories` | Master SLA |
| GET/POST | `/support/contracts` | Kontrak support |
| GET | `/support/contracts/<id>` | Detail kontrak |
| GET/POST | `/support/tickets` | Tiket support |
| GET | `/support/tickets/<id>` | Detail tiket |
| POST | `/support/tickets/<id>/status` | Update status tiket |
| GET/POST | `/support/presales` | Presales & POC |
| GET | `/support/sla-monitor` | Dashboard SLA |
| GET | `/support/reports` | Laporan |

### BookingCore
| Method | URL | Fungsi |
|--------|-----|--------|
| GET | `/booking/` | Kalender booking |
| GET/POST | `/booking/new` | Buat booking baru |
| GET | `/booking/<id>` | Detail booking |
| POST | `/booking/<id>/cancel` | Cancel booking |
| GET | `/booking/api/slots` | API cek slot tersedia |

### AssetCore
| Method | URL | Fungsi |
|--------|-----|--------|
| GET | `/aset/` | Dashboard |
| GET | `/aset/assets` | Daftar laptop/PC |
| GET/POST | `/aset/assets/new` | Tambah asset |
| GET | `/aset/assets/<id>` | Detail asset |
| GET/POST | `/aset/assets/<id>/edit` | Edit asset |
| POST | `/aset/assets/<id>/delete` | Hapus asset |
| GET | `/aset/infra` | Daftar infrastruktur |
| GET/POST | `/aset/infra/new` | Tambah perangkat |
| GET/POST | `/aset/infra/<id>/edit` | Edit perangkat |
| POST | `/aset/infra/<id>/delete` | Hapus perangkat |
| GET | `/aset/licenses` | Daftar lisensi |
| GET/POST | `/aset/licenses/new` | Tambah lisensi |
| GET/POST | `/aset/licenses/<id>/edit` | Edit lisensi |
| POST | `/aset/licenses/<id>/delete` | Hapus lisensi |
| GET | `/aset/subscriptions` | Daftar subscription |
| GET/POST | `/aset/subscriptions/new` | Tambah subscription |
| GET/POST | `/aset/subscriptions/<id>/edit` | Edit subscription |
| POST | `/aset/subscriptions/<id>/delete` | Hapus subscription |
| GET | `/aset/requests` | Daftar request software |
| GET/POST | `/aset/requests/new` | Tambah request |
| POST | `/aset/requests/<id>/status` | Update status request |
| POST | `/aset/requests/<id>/delete` | Hapus request |

---

## 6. Sistem Notifikasi

```
APScheduler (background thread dalam proses Flask)
    │
    ├── Job: send_daily_reminders()   — setiap hari 08:00 WIB
    │       └── Cek kontrak karyawan ≤ 30 hari
    │               │
    │               ├── Email (SMTP) ──────────── via smtplib
    │               ├── Telegram Bot ──────────── via requests → t.me/bot API
    │               └── WhatsApp (WAHA/OpenWA) ── via requests → local API
    │
    └── Job: send_daily_report()      — setiap hari 22:00 WIB
            └── Kirim ringkasan harian ke admin
```

### OpenWA Endpoint (rmyndharis/OpenWA)
```
POST /api/sessions/{sessionId}/messages/send-text
Header: X-API-Key: {api_key}
Body:   {"chatId": "628xxx@c.us", "text": "pesan"}
```

---

## 7. Deployment Topology (Production)

```
Internet
    │
    ▼
┌───────────────────────────────┐
│  Nginx (port 80/443)          │
│  /static/ ──► file system     │
│  /       ──► unix socket      │
└──────────────┬────────────────┘
               │ /run/evaluasi/evaluasi.sock
               ▼
┌───────────────────────────────┐
│  systemd: evaluasi.service    │
│  User=www-data                │
│  ProtectSystem=full           │
│  ReadWritePaths=              │
│    /var/lib/evaluasi/         │
│    /var/log/evaluasi/         │
│    /run/evaluasi/             │
│                               │
│  Gunicorn workers (gthread)   │
│    └── Flask app.py           │
│         └── APScheduler       │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│  /var/lib/evaluasi/           │
│  └── evaluasi.db  (SQLite)    │
└───────────────────────────────┘

/var/www/evaluasi/   (read-only app files)
├── app.py
├── wsgi.py
├── venv/
├── templates/
└── .env
```

---

## 8. Pola Pengembangan Modul Baru

Setiap modul baru mengikuti konvensi ini:

```python
# 1. Permission helper
def fc_require(perm):
    db = get_db()
    if not has_permission(session.get('user_role',''), perm, db):
        flash(f'Akses ditolak — permission "{perm}" diperlukan', 'danger')
        return False
    return True

# 2. Routes dengan prefix URL
@app.route('/finance/')
@login_required
def fc_index():
    if not fc_require('fc_view'): return redirect(url_for('portal'))
    # ... render dashboard

# 3. Tabel database dengan prefix
"""
CREATE TABLE IF NOT EXISTS fc_budgets ( ... );
CREATE TABLE IF NOT EXISTS fc_expenses ( ... );
"""

# 4. Permission entry di ALL_PERMISSIONS
ALL_PERMISSIONS['finance'] = {
    'fc_view':          'Lihat laporan keuangan',
    'fc_manage_budget': 'Kelola anggaran',
}

# 5. Portal entry di _apps (init_db)
('finance', 'FinanceCore', 'Kelola anggaran & laporan keuangan',
 'cash-stack', '#198754', '#e8f5e9', '/finance/', 1, 0, 4, 'fc_view')
```

**Checklist modul baru:**
- [ ] Tables di SCHEMA dengan prefix `xx_`
- [ ] `ALL_PERMISSIONS['slug']` diisi
- [ ] `_apps` entry di `init_db()` dengan `is_coming_soon=0`
- [ ] `xx_require()` helper
- [ ] Routes `/slug/*`
- [ ] `elif path.startswith('/slug')` di `auto_set_active_app()`
- [ ] `{% elif current_app_slug == 'slug' %}` sidebar di `base.html`
- [ ] Templates `xx_*.html`

---

## 9. Keterkaitan Antar Modul

```
employees  ◄──────────────────────────────────────────────────┐
(shared)   │                                                   │
    │      │                                                   │
    ├──────► TalentCore: evaluasi, gaji, kontrak kerja         │
    │                                                          │
    ├──────► SupportCore: sc_customer_pics, sc_contract_pics   │
    │         (PIC internal dari daftar karyawan)              │
    │                                                          │
    ├──────► AssetCore: ac_assets.employee_id                  │
    │         (laptop/PC diassign ke karyawan)                 │
    │         ac_license_assignments.employee_id               │
    │         ac_software_requests.employee_id                 │
    │                                                          │
    └──────► BookingCore: bookings.user_id → users → employees │
                                                               │
users ──────────────────────────────────────────────────────────┘
(users.employee_id → employees.id)
```

Data karyawan di `employees` adalah **sumber kebenaran tunggal** — user login (`users`) bisa di-link ke karyawan, sehingga AssetCore bisa menampilkan laptop milik siapa, SupportCore bisa pilih PIC dari daftar karyawan, dsb.

---

## 10. Roadmap Modul Berikutnya (Kandidat)

| Slug | Nama | Deskripsi | Warna |
|------|------|-----------|-------|
| `finance` | FinanceCore | Pengelolaan anggaran IT, PO, invoice | `#198754` |
| `helpdesk` | HelpdeskCore | Tiket internal IT support untuk karyawan | `#dc3545` |
| `project` | ProjectCore | Tracking project internal tim IT | `#fd7e14` |
| `docs` | DocsCore | Knowledge base & dokumentasi internal | `#20c997` |

Setiap modul baru cukup ikuti pola di bagian 8 — tidak perlu app terpisah, cukup tambah ke `app.py`, `base.html`, dan folder `templates/`.
