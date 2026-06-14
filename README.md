# super-us — Platform Aplikasi Tim IT

Platform superapp berbasis Flask + SQLite yang mengintegrasikan beberapa aplikasi bisnis dalam satu portal terpusat dengan manajemen user, role, dan akses per aplikasi.

**Aplikasi yang tersedia:**
| App | Deskripsi | Status |
|-----|-----------|--------|
| **TalentCore** | Penilaian & manajemen kinerja karyawan | ✅ Aktif |
| **AssetCore** | Pencatatan & tracking aset perusahaan | 🔜 Segera |

---

## Arsitektur

```
super-us/
├── app.py                        # Core Flask app — routes, auth, scheduler
├── wsgi.py                       # Entry point Gunicorn (production)
├── seed_data.py                  # Data template skill per divisi
├── requirements.txt
│
├── templates/
│   ├── base.html                 # Layout utama (sidebar + topbar adaptif)
│   │
│   ├── # ── Portal ──
│   ├── login.html                # Login dengan aurora animated background
│   ├── portal.html               # App launcher (aurora background)
│   ├── portal_settings.html      # Pengaturan akses app per user
│   ├── portal_users.html         # Kelola user (dengan info akses app)
│   ├── portal_user_form.html     # Form user + employee picker (autocomplete)
│   ├── portal_roles.html         # Role & Permission per aplikasi
│   │
│   ├── # ── TalentCore ──
│   ├── index.html                # Dashboard karyawan
│   ├── karyawan.html             # Daftar & manajemen karyawan
│   ├── employee_form.html        # Form tambah/edit karyawan
│   ├── eval_hardskill.html       # Penilaian hard skill
│   ├── eval_competency.html      # Penilaian kompetensi
│   ├── eval_ability.html         # Penilaian kemampuan (18 item)
│   ├── eval_project.html         # Penilaian project
│   ├── eval_summary.html         # Ringkasan evaluasi
│   ├── eval_self.html            # Self-assessment karyawan (tokenized)
│   ├── salary.html               # Tabel gaji + dashboard analitik
│   ├── contracts.html            # Dashboard kontrak karyawan
│   ├── reminder_log.html         # Log reminder terkirim
│   ├── admin.html                # Template evaluasi per divisi
│   ├── admin_roles.html          # Role & permission (legacy)
│   ├── settings.html             # Pengaturan notifikasi
│   └── profile.html              # Profil & keamanan MFA
│
├── Dockerfile
├── docker-compose.yml
├── gunicorn.conf.py
└── nginx/nginx.conf
```

---

## Fitur Portal (super-us)

### App Launcher
- Halaman portal dengan animated aurora background (macOS Sequoia style)
- Kartu aplikasi dengan glass-morphism effect
- Klik app → masuk ke aplikasi, menu sidebar berubah sesuai app

### Kelola User (`/portal/users`)
- Daftar user dengan kolom akses per aplikasi dan role-nya
- **Employee Picker** saat tambah user baru:
  - Cari karyawan via autocomplete (nama/jabatan/divisi)
  - Auto-fill username, nama, email, WhatsApp, Telegram dari data karyawan
  - Atau input manual tanpa link ke karyawan
- Reset password, reset MFA, nonaktifkan user

### Role & Permission (`/portal/roles`)
- Tab per aplikasi — setiap app kelola role-nya sendiri
- Editor visual permission dengan toggle per hak akses
- Role sistem (superadmin, admin, viewer) tidak bisa dihapus

### Akses Aplikasi (`/portal/settings`)
- Grid user × app — toggle akses on/off per user per app
- Set role user di dalam setiap aplikasi
- Superadmin selalu bypass — akses penuh ke semua app

### Keamanan
- **MFA wajib** — semua user harus setup Google Authenticator sebelum akses portal maupun app
- Password reset via Email / WhatsApp (WAHA) / Telegram
- Single-use token (1 jam TTL) untuk reset password
- XSS protection: confirm dialog pakai `data-confirm` attribute
- Open redirect protection: validasi `?next=` harus path relatif
- `SECRET_KEY` wajib diset via environment variable

---

## Fitur TalentCore

### Penilaian Kinerja
- **5 divisi** dengan template skill masing-masing: Programmer, Implementor/BPS, Helpdesk Support, Tester, IT Support
- **Formula scoring:**
  - Hard Skill: `(Σ(skor/4 × bobot) / Σbobot) × 100`
  - Kompetensi: `Σ(rating × 20 × bobot)`
  - Final: `PP × 0.3 + Kompetensi × 0.7`
- **18 item kemampuan** (Ability) dengan deskripsi A/B/C/D per item
- Penilaian project/target kerja terpisah
- **Self-assessment** via link token yang dikirim ke karyawan

### Manajemen Karyawan
- Data karyawan per divisi dengan level jabatan
- Tipe kepegawaian: **Tetap** dan **Kontrak**
- Tracking masa kontrak dengan countdown hari tersisa
- Badge status: kritis (≤14 hari), warning (≤30 hari), aman, expired
- Import data karyawan dari Excel (template tersedia)

### Tabel Gaji
- Input komponen gaji per tahun (gaji pokok, tunjangan, dll.)
- Persentase kenaikan & tanggal kenaikan gaji
- Dashboard analitik: total pengeluaran gaji, tren per tahun
- Import data gaji dari Excel

### Reminder Kontrak
- **APScheduler** — cek otomatis setiap hari pukul 08:00 WIB
- Laporan harian otomatis pukul 22:00 WIB
- Kirim via **Email (SMTP)**, **Telegram Bot**, dan/atau **WhatsApp (WAHA)**
- Trigger manual per karyawan atau semua sekaligus
- Log lengkap tiap pengiriman

### Administrasi TalentCore
- Pengaturan SMTP, Telegram, WhatsApp (WAHA/OpenWA)
- Test koneksi langsung dari UI
- Template skill dikustomisasi per divisi
- Manajemen divisi

---

## Role & Hak Akses

### Portal level (superadmin only)
| Aksi | Keterangan |
|------|------------|
| Kelola User | Tambah/edit/nonaktifkan user |
| Role & Permission | Buat role, set permission per app |
| Akses Aplikasi | Toggle akses user ke tiap app |

### TalentCore roles
| Role | Default Permission |
|------|-------------------|
| `superadmin` | Semua permission |
| `admin` | Kelola karyawan, evaluasi, divisi, template, kirim reminder |
| `viewer` | Lihat hasil evaluasi saja |

> Role dan permission dapat dikustomisasi di `/portal/roles`.

---

## Cara Menjalankan

### Development

```bash
git clone https://github.com/barangbaru/solid-apps.git
cd solid-apps

python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

export SECRET_KEY="dev-secret-key-ganti-di-production"
python3 app.py
```

Buka: **http://127.0.0.1:5000**

Login default:
```
Username : superadmin
Password : Admin@123
```
> **Segera ganti password** via Profil setelah login pertama.

---

### Production — aaPanel / Ubuntu

```bash
# Deploy dengan script
sudo bash /var/www/evaluasi/deploy-ubuntu.sh
```

Script deploy otomatis:
- Pull latest dari git
- Install/update dependencies
- Restart gunicorn
- DB migration & seed data dijalankan otomatis saat startup (termasuk update `superapp_apps`)

#### Environment Variables
```bash
export SECRET_KEY="random-string-min-32-karakter"
export DATABASE_PATH="/var/www/evaluasi/data/evaluasi.db"
export TZ="Asia/Jakarta"
```

Generate `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

#### Nginx
```nginx
location / {
    proxy_pass         http://unix:/tmp/evaluasi.sock;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
}
```

---

### Production — Docker Compose

```bash
cp .env.example .env
# Edit SECRET_KEY di .env

docker compose build
docker compose up -d
```

```
┌─────────────────────────────────────────────┐
│  Docker Compose                             │
│  ┌──────────────┐    ┌──────────────────┐  │
│  │    nginx     │───▶│    super-us      │  │
│  │  :80         │    │  gunicorn :5000  │  │
│  └──────────────┘    └────────┬─────────┘  │
│                    ┌──────────▼─────────┐  │
│                    │  volume: data/     │  │
│                    │  evaluasi.db       │  │
│                    └────────────────────┘  │
└─────────────────────────────────────────────┘
```

---

## Teknologi

| Komponen | Versi |
|----------|-------|
| Python | 3.11 |
| Flask | 3.1.x |
| Gunicorn | 21.2.x |
| APScheduler | 3.11.x |
| SQLite | bawaan Python |
| Bootstrap | 5.3.3 |
| Nginx | 1.27-alpine |

---

## Menambah Aplikasi Baru ke super-us

1. Edit list `_apps` di `init_db()` dalam `app.py`:

```python
_apps = [
    ('evaluasi', 'TalentCore', '...', 'clipboard2-check', '#4da8da', '#e8f4fd', '/', 1, 0, 0, ''),
    ('aset',     'AssetCore',  '...', 'box-seam',         '#6f42c1', '#f0ecff', '/aset/', 1, 0, 1, ''),
    # Tambah app baru di sini:
    ('finance',  'FinanceCore','...', 'cash-stack',       '#198754', '#e8f5e9', '/finance/', 1, 0, 2, ''),
]
```

2. Tambah permissions app di `APP_PERMISSIONS`:

```python
APP_PERMISSIONS = {
    'evaluasi': { ... },
    'finance':  {
        'view_reports':  'Lihat laporan keuangan',
        'manage_budget': 'Kelola anggaran',
    },
}
```

3. Deploy — DB diupdate otomatis saat restart.

---

*Developed by barangbaru & AI Team*
