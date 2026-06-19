# super-us — Platform Aplikasi Tim IT

Platform superapp berbasis **Flask + SQLite** yang mengintegrasikan beberapa aplikasi bisnis dalam satu portal terpusat, dengan manajemen user, role, MFA, dan hak akses per aplikasi.

## Aplikasi yang Tersedia

| App | URL | Deskripsi | Status |
|-----|-----|-----------|--------|
| **TalentCore** | `/` | Penilaian & manajemen kinerja karyawan | ✅ Aktif |
| **SupportCore** | `/support/` | Monitoring kontrak & tiket support tahunan | ✅ Aktif |
| **BookingCore** | `/booking/` | Reservasi ruangan & kendaraan operasional | ✅ Aktif |
| **AssetCore** | `/aset/` | Pencatatan & tracking aset IT perusahaan | ✅ Aktif |

---

## Arsitektur

```
super-us/
├── app.py                        # Satu file Flask — semua route, auth, scheduler (~6500 baris)
├── wsgi.py                       # Entry point Gunicorn (production)
├── seed_data.py                  # Seed template skill per divisi (TalentCore)
├── seed_assetcore.sql            # Seed data aset dari Excel dokumentasi IT
├── gen_seed.py                   # Script generate seed_assetcore.sql dari Excel
├── requirements.txt
│
├── templates/
│   ├── base.html                 # Layout utama (sidebar + topbar adaptif per app)
│   ├── login.html / mfa_*.html   # Auth & MFA flow
│   │
│   ├── # ── Portal ──────────────────────────────────────────
│   ├── portal.html               # App launcher (aurora background)
│   ├── portal_users.html         # Kelola user + akses app
│   ├── portal_user_form.html     # Form user dengan employee picker (autocomplete)
│   ├── portal_roles.html         # Role & permission per app
│   ├── portal_settings.html      # Toggle akses user × app
│   ├── portal_system_settings.html  # Konfigurasi notifikasi (SMTP/Telegram/WhatsApp)
│   ├── portal_audit.html         # Audit log aktivitas
│   │
│   ├── # ── TalentCore (/') ──────────────────────────────────
│   ├── index.html / karyawan.html / employee_form.html
│   ├── eval_hardskill.html / eval_competency.html / eval_ability.html
│   ├── eval_project.html / eval_summary.html / eval_self.html / eval_review.html
│   ├── reviews.html / contracts.html / reminder_log.html
│   ├── salary.html / profile.html / settings.html
│   ├── admin.html / admin_divisi.html / admin_divisions.html / admin_roles.html
│   │
│   ├── # ── SupportCore (/support/) ──────────────────────────
│   ├── sc_index.html             # Dashboard SupportCore
│   ├── sc_customers.html / sc_customer_form.html
│   ├── sc_apps.html              # Master Apps & Modul customer
│   ├── sc_services.html / sc_service_form.html
│   ├── sc_support_types.html / sc_support_type_form.html
│   ├── sc_sla_categories.html / sc_sla_category_form.html
│   ├── sc_contracts.html / sc_contract_form.html / sc_contract_detail.html
│   ├── sc_tickets.html / sc_ticket_form.html / sc_ticket_detail.html
│   ├── sc_presales.html / sc_presales_form.html / sc_presales_detail.html
│   ├── sc_sla_monitor.html / sc_reports.html
│   │
│   ├── # ── BookingCore (/booking/) ──────────────────────────
│   ├── booking_index.html / booking_form.html / booking_detail.html
│   │
│   └── # ── AssetCore (/aset/) ───────────────────────────────
│       ├── ac_index.html         # Dashboard AssetCore
│       ├── ac_assets.html / ac_asset_form.html / ac_asset_detail.html
│       ├── ac_infra.html / ac_infra_form.html
│       ├── ac_licenses.html / ac_license_form.html
│       ├── ac_subscriptions.html / ac_subscription_form.html
│       └── ac_requests.html / ac_request_form.html
│
├── evaluasi.service              # systemd service
├── gunicorn.conf.py              # Gunicorn config (unix socket)
└── deploy-ubuntu.sh              # Deploy & update script (idempotent)
```

---

## Fitur Portal

### App Launcher
- Aurora animated background (macOS Sequoia style)
- Kartu aplikasi glass-morphism — klik → masuk app, sidebar otomatis berubah
- Badge "Segera" untuk app yang belum aktif

### Kelola User (`/portal/users`)
- **Employee Picker**: cari karyawan via autocomplete → auto-fill username, nama, email, WA, Telegram
- Reset password via Email / WhatsApp / Telegram
- Reset MFA, nonaktifkan/hapus user
- Deteksi & merge duplikat user (Google SSO ↔ manual)

### Role & Permission (`/portal/roles`)
- Tab per aplikasi — kelola role dan permission masing-masing app secara independen
- Toggle visual per permission
- Role sistem (superadmin/admin/viewer) tidak bisa dihapus

### Akses Aplikasi (`/portal/settings`)
- Grid user × app — toggle on/off per kombinasi
- Set role user di setiap app berbeda-beda
- Superadmin bypass semua — akses penuh ke semua app

### Keamanan
- **MFA wajib** (Google Authenticator) sebelum bisa akses portal maupun app
- Single-use token (1 jam TTL) untuk reset password
- `SECRET_KEY` wajib via environment variable
- Audit log semua aksi sensitif (`/portal/audit`)
- Open redirect protection, XSS protection

---

## Fitur TalentCore

### Penilaian Kinerja (5 divisi)
| Template | Divisi |
|----------|--------|
| Programmer | APPS, Backend, Frontend |
| Implementor/BPS | Implementasi |
| Helpdesk Support | Support, Helpdesk |
| Tester | QA/Testing |
| IT Support | Infrastruktur |

- **Formula final:** `PP × 0.3 + Kompetensi × 0.7`
- Hard Skill: `(Σ skor/4 × bobot) / Σbobot × 100`
- 18 item kemampuan (Ability) dengan deskripsi A/B/C/D
- Self-assessment via link token dikirim ke karyawan
- Penilaian project / target kerja terpisah

### Manajemen Karyawan
- Data per divisi dengan level jabatan & tipe (Tetap/Kontrak)
- Countdown masa kontrak: kritis (≤14 hari) / warning (≤30 hari)
- Import data dari Excel

### Tabel Gaji
- Komponen gaji per tahun, tren analitik, import Excel

### Reminder Otomatis (APScheduler)
- Cek harian pukul 08:00 WIB, laporan pukul 22:00 WIB
- Kirim via **Email (SMTP)**, **Telegram Bot**, **WhatsApp (WAHA/OpenWA)**

---

## Fitur SupportCore

### Master Data
| Entitas | Keterangan |
|---------|------------|
| Customer | Perusahaan pemegang kontrak support |
| Apps & Modul | Daftar aplikasi yang di-support per customer |
| Layanan/Jasa | Jenis layanan teknis (instalasi, migrasi, dll.) |
| Tipe Support | Corrective / Preventive / Onsite Support |
| Kategori SLA | Prioritas + target response & resolusi |

### Operasional
- **Kontrak Support Tahunan** — per customer, multi-PIC, multi-layanan
- **Tiket Support** — tracking penuh: status, PIC, SLA timer, multi-assignee
- **Presales & POC** — request presales, demo, proof of concept
- **Monitoring SLA** — dashboard kepatuhan SLA, trend bulanan, per customer
- **Laporan** — filter date range, per customer / tipe / assignee

---

## Fitur BookingCore

| Sumber Daya | Tipe |
|-------------|------|
| Big Meeting Room | Ruangan |
| Small Room A / B | Ruangan |
| Lounge Room | Ruangan |
| Mobil Operasional | Kendaraan |

- Kalender reservasi harian, cek ketersediaan slot real-time
- Approval / cancel booking
- Deteksi konflik jadwal otomatis

---

## Fitur AssetCore

### Inventaris Laptop/PC (`/aset/assets`)
- Data per karyawan: OS, lisensi OS, processor, RAM, disk, Office version
- Daftar software terpasang per unit
- Filter per divisi, cari per nama/asset tag
- Data diimport dari Excel dokumentasi IT

### Infrastruktur (`/aset/infra`)
| Tipe | Contoh |
|------|--------|
| Server | HP ProLiant, Dell PowerEdge |
| Router | Asus, Mikrotik Routerboard |
| Access Point | TP-Link (4 unit) |
| Switch | D-Link |
| Monitor / Peripheral | Philips, Logitech |
| Tools | LAN Cable Tester, Tang Crimping |

### Lisensi Software (`/aset/licenses`)
- License key per seat, tipe (Perpetual/Subscription/Volume/OEM)
- Assign ke karyawan, tracking kapasitas seat

### Subscription & ISP (`/aset/subscriptions`)
- SaaS: ChatGPT, Microsoft Teams, Lightroom, Office Timeline
- ISP: MyRepublic (IP Static + WiFi), Orbit
- Alert otomatis 30 hari sebelum berakhir (di dashboard)

### Request Software (`/aset/requests`)
- Karyawan request software baru
- Workflow status: Pending → Approved → Installed / Rejected

---

## Database Schema (ringkasan)

| Prefix | Modul | Tabel Utama |
|--------|-------|-------------|
| *(tanpa prefix)* | TalentCore | `employees`, `evaluations`, `salary_data`, `user_contracts` |
| `sc_` | SupportCore | `sc_customers`, `sc_contracts`, `sc_tickets`, `sc_presales`, `sc_sla_categories` |
| `booking_` | BookingCore | `booking_resources`, `bookings` |
| `ac_` | AssetCore | `ac_assets`, `ac_infrastructure`, `ac_licenses`, `ac_subscriptions`, `ac_software_requests` |
| *(portal)* | Portal/Auth | `users`, `user_app_access`, `superapp_apps`, `portal_roles`, `audit_log` |

---

## Role & Hak Akses

### Level Portal (superadmin only)
| Aksi | Endpoint |
|------|----------|
| Kelola User | `/portal/users` |
| Role & Permission | `/portal/roles` |
| Toggle Akses App | `/portal/settings` |
| Konfigurasi Notifikasi | `/portal/system-settings` |
| Audit Log | `/portal/audit` |

### Permission per Modul
| Modul | Permission Tersedia |
|-------|---------------------|
| TalentCore | `view_eval`, `manage_eval`, `view_salary`, `manage_salary`, `manage_employees`, `manage_divisions`, `manage_settings`, `manage_roles`, `send_reminders` |
| SupportCore | `sc_view`, `sc_manage_tickets`, `sc_manage_contracts`, `sc_manage_master`, `sc_manage_presales`, `sc_view_reports` |
| BookingCore | `booking_view`, `booking_create`, `booking_manage` |
| AssetCore | `ac_view`, `ac_manage_assets`, `ac_manage_infra`, `ac_manage_licenses`, `ac_manage_subs`, `ac_manage_requests` |

---

## Cara Menjalankan

### Development

```bash
git clone https://github.com/barangbaru/solid-apps.git
cd solid-apps

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

export SECRET_KEY="dev-secret-ganti-di-production"
python3 app.py
```

Buka: **http://127.0.0.1:5000**
Login: `superadmin` / `Admin@123` — **segera ganti password!**

---

### Production — Ubuntu + systemd

```bash
# Install/update (idempotent)
sudo bash deploy-ubuntu.sh
```

Script otomatis:
- Clone dari GitHub, rsync ke `/var/www/evaluasi/`
- Setup virtualenv & install dependencies
- Buat `/var/lib/evaluasi/` untuk database (di luar app dir)
- Auto-migrasi DATABASE_PATH lama jika perlu
- Install/restart systemd service `evaluasi`
- Setup Nginx (skip jika sudah dikonfigurasi)

#### Import data aset dari Excel (sekali saja)
```bash
sqlite3 /var/lib/evaluasi/evaluasi.db < /var/www/evaluasi/seed_assetcore.sql
```

#### Environment Variables (`.env`)
```bash
SECRET_KEY=random-string-min-32-karakter
DATABASE_PATH=/var/lib/evaluasi/evaluasi.db
TZ=Asia/Jakarta
```

Generate `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

#### systemd + Nginx
- Service: `evaluasi.service` (ProtectSystem=full, ReadWritePaths=/var/lib/evaluasi)
- Socket: `unix:/run/evaluasi/evaluasi.sock`
- DB: `/var/lib/evaluasi/evaluasi.db` (dipisah dari app dir)

---

### Production — Docker Compose

```bash
cp .env.example .env   # edit SECRET_KEY
docker compose up -d
```

---

## Menambah Aplikasi Baru

1. **Daftarkan di `_apps`** dalam `init_db()`:
```python
('finance', 'FinanceCore', 'Kelola anggaran & laporan keuangan',
 'cash-stack', '#198754', '#e8f5e9', '/finance/', 1, 0, 4, 'fc_view'),
```

2. **Tambah permissions** di `ALL_PERMISSIONS`:
```python
'finance': {
    'fc_view':          'Lihat laporan keuangan',
    'fc_manage_budget': 'Kelola anggaran',
}
```

3. **Tambah `elif current_app_slug == 'finance'`** di `base.html` untuk sidebar.

4. **Tambah `elif path.startswith('/finance')`** di `auto_set_active_app()`.

5. Buat tabel schema, routes, dan templates dengan prefix `fc_`.

6. Deploy → schema otomatis dibuat saat restart.

---

## Teknologi

| Komponen | Detail |
|----------|--------|
| Python | 3.11 |
| Flask | 3.1.x |
| SQLite | bawaan Python (single file DB) |
| Gunicorn | 21.2.x + unix socket |
| APScheduler | 3.11.x — cron jobs reminder |
| Bootstrap | 5.3.3 + Bootstrap Icons |
| Font | Nunito (Google Fonts) |
| Nginx | reverse proxy + static files |
| OpenWA / WAHA | WhatsApp notification |
| Telegram Bot | notification alternatif |

---

*Developed for PT MMI IT Team — barangbaru*
