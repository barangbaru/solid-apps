# Evaluasi Kinerja IT

Aplikasi web untuk penilaian kinerja tim IT berbasis Flask + SQLite. Mendukung 5 divisi dengan template skill berbeda, manajemen kontrak karyawan, dan reminder otomatis via email & Telegram.

---

## Struktur Aplikasi

```
evaluasi-kinerja/
├── app.py                      # Core Flask app, routes, auth, scheduler
├── wsgi.py                     # Entry point Gunicorn (production)
├── seed_data.py                # Data template skill per divisi
├── requirements.txt            # Python dependencies
├── run.bat                     # Jalankan di Windows (development)
│
├── templates/
│   ├── base.html               # Layout utama + navbar
│   ├── login.html              # Halaman login
│   ├── index.html              # Dashboard daftar karyawan
│   ├── employee_form.html      # Form tambah/edit karyawan
│   ├── eval_hardskill.html     # Form penilaian hard skill
│   ├── eval_competency.html    # Form penilaian kompetensi
│   ├── eval_ability.html       # Form penilaian kemampuan (18 item)
│   ├── eval_project.html       # Form penilaian project
│   ├── eval_summary.html       # Ringkasan hasil evaluasi
│   ├── contracts.html          # Dashboard kontrak karyawan
│   ├── users.html              # Kelola user (superadmin)
│   ├── user_form.html          # Form tambah/edit user
│   ├── settings.html           # Pengaturan SMTP & Telegram
│   ├── reminder_log.html       # Log reminder yang terkirim
│   ├── profile.html            # Profil user login
│   ├── admin.html              # Admin template skill
│   └── admin_divisi.html       # Admin skill per divisi
│
├── Dockerfile                  # Multi-stage build (builder + runtime)
├── docker-compose.yml          # Orchestrasi evaluasi + nginx
├── gunicorn.conf.py            # Config Gunicorn untuk aaPanel (unix socket)
├── gunicorn.docker.conf.py     # Config Gunicorn untuk Docker (TCP)
├── nginx/
│   └── nginx.conf              # Reverse proxy ke app
├── build-push.bat              # Build & push image ke Nexus (Windows)
├── build-push.sh               # Build & push image ke Nexus (Linux/Mac)
├── .env.example                # Template environment variables
├── .dockerignore
└── .gitignore
```

---

## Fitur

### Penilaian Kinerja
- **5 divisi** dengan template skill masing-masing: Programmer, Implementor/BPS, Helpdesk Support, Tester, IT Support
- **Formula scoring** dari Excel:
  - Hard Skill (PP): `(Σ(skor/4 × bobot) / Σbobot) × 100`
  - Kompetensi: `Σ(rating × 20 × bobot)`
  - Final: `PP × 0.3 + Kompetensi × 0.7`
- **18 item kemampuan** (Ability) dengan deskripsi A/B/C/D per item
- Penilaian project/target kerja terpisah

### Manajemen Karyawan
- Data karyawan per divisi dengan level jabatan
- Tipe kepegawaian: **Tetap** dan **Kontrak**
- Tracking masa kontrak dengan countdown hari tersisa
- Badge status kontrak: kritis (≤14 hari), warning (≤30 hari), aman, expired

### Role & Autentikasi
| Role | Akses |
|------|-------|
| `superadmin` | Semua fitur + kelola user + pengaturan sistem |
| `admin` | Penilaian, lihat kontrak, lihat log reminder |

- Session-based login dengan password hashing (Werkzeug)
- Fitur **promote** karyawan menjadi admin/superadmin

### Reminder Kontrak
- **APScheduler** — reminder otomatis setiap hari pukul 08:00 WIB
- Kirim via **Email (SMTP)** dan/atau **Telegram Bot**
- Trigger manual per karyawan atau sekaligus semua
- Log lengkap setiap pengiriman reminder

### Administrasi
- Konfigurasi SMTP (host, port, TLS, user, password)
- Konfigurasi Telegram Bot (token + chat ID)
- Test koneksi email & Telegram langsung dari UI
- Template skill dapat dikustomisasi per divisi

---

## Cara Menjalankan

### 1. Development — Windows (run.bat)

**Prasyarat:** Python 3.10+ terinstall dan ada di PATH.

```bat
# Clone repo
git clone https://github.com/barangbaru/solid-apps.git
cd solid-apps

# Buat virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Jalankan aplikasi
run.bat
```

Buka browser: **http://127.0.0.1:5000**

Login default:
```
Username : superadmin
Password : Admin@123
```
> **Ganti password segera** setelah login pertama via menu Profil.

---

### 2. Production — aaPanel

#### Persiapan Server
1. Install **Python Project** di aaPanel
2. Upload seluruh file ke `/www/wwwroot/evaluasi/`
3. Buat virtual environment dan install dependencies:

```bash
cd /www/wwwroot/evaluasi
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### Konfigurasi Gunicorn
File `gunicorn.conf.py` sudah tersedia dengan konfigurasi unix socket:

```python
bind = "unix:/tmp/evaluasi.sock"
workers = 1        # SQLite tidak support multi-writer
threads = 4
```

Jalankan via Supervisor (aaPanel → App Store → Supervisor):
```bash
# Command
/www/wwwroot/evaluasi/venv/bin/gunicorn --config gunicorn.conf.py wsgi:app

# Working directory
/www/wwwroot/evaluasi
```

#### Konfigurasi Nginx (aaPanel)
Tambahkan di konfigurasi site Nginx:

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

#### Environment Variables (opsional)
```bash
export SECRET_KEY="random-string-panjang-min-32-karakter"
export DATABASE_PATH="/www/wwwroot/evaluasi/data/evaluasi.db"
export TZ="Asia/Jakarta"
```

---

### 3. Production — Docker Compose

#### Prasyarat
- Docker Engine 24+
- Docker Compose v2

#### Langkah Deploy

```bash
# 1. Clone repo
git clone https://github.com/barangbaru/solid-apps.git
cd solid-apps

# 2. Buat file .env dari template
cp .env.example .env

# 3. Edit SECRET_KEY (wajib diganti!)
nano .env
```

Isi minimal di `.env`:
```env
SECRET_KEY=isi-dengan-random-string-minimal-32-karakter
TZ=Asia/Jakarta
```

```bash
# 4. Login ke Nexus registry (jika pull dari private registry)
docker login nexus.domain.com -u user -p userpassword

# 5. Build image lokal (atau pull dari registry)
docker compose build

# 6. Jalankan
docker compose up -d

# 7. Cek status
docker compose ps
docker compose logs -f evaluasi
```

Aplikasi berjalan di **http://localhost** (via Nginx port 80).

#### Perintah Berguna

```bash
# Lihat log realtime
docker compose logs -f

# Restart service
docker compose restart evaluasi

# Update ke versi terbaru
docker compose pull && docker compose up -d

# Stop semua service
docker compose down

# Hapus semua termasuk volume (DATA HILANG!)
docker compose down -v
```

#### Build & Push ke Nexus Registry

```bat
# Windows
build-push.bat

# Dengan tag versi spesifik
build-push.bat v1.2.0
```

```bash
# Linux / macOS
chmod +x build-push.sh
./build-push.sh

# Dengan tag versi spesifik
./build-push.sh v1.2.0
```

Image akan di-push ke:
```
nexus.devops.mmi-pt.com/evaluasi-kinerja:latest
```

---

## Arsitektur Docker

```
┌─────────────────────────────────────────────┐
│  Docker Compose                             │
│                                             │
│  ┌──────────────┐    ┌──────────────────┐  │
│  │    nginx     │───▶│    evaluasi      │  │
│  │  :80         │    │  gunicorn :5000  │  │
│  └──────────────┘    └────────┬─────────┘  │
│                               │            │
│                    ┌──────────▼─────────┐  │
│                    │  volume: data/     │  │
│                    │  evaluasi.db       │  │
│                    └────────────────────┘  │
└─────────────────────────────────────────────┘
```

| Service | Image | Port |
|---------|-------|------|
| `evaluasi` | `nexus.devops.mmi-pt.com/evaluasi-kinerja:latest` | 5000 (internal) |
| `nginx` | `nginx:1.27-alpine` | 80 (public) |

---

## Teknologi

| Komponen | Versi |
|----------|-------|
| Python | 3.11 |
| Flask | 3.1.3 |
| Gunicorn | 21.2.0 |
| APScheduler | 3.11.2 |
| SQLite | bawaan Python |
| Nginx | 1.27-alpine |
| Docker base | python:3.11-slim |

---

## Lisensi

Internal use — MMI DevOps Team
