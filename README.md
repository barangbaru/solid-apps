# Hive — Platform Superapp Tim IT

Platform superapp berbasis **Flask + PostgreSQL** yang mengintegrasikan manajemen kinerja karyawan, support, booking, aset, dan proyek dalam satu portal terpusat — dengan sistem autentikasi terpusat, MFA, role per aplikasi, dan update center otomatis.

> Versi terkini: lihat [CHANGELOG.md](CHANGELOG.md) · Deploy: `sudo bash deploy-ubuntu.sh`

---

## Aplikasi yang Tersedia

| App | URL | Deskripsi | Status |
|-----|-----|-----------|--------|
| **TalentCore** | `/` | Penilaian kinerja, evaluasi, manajemen kontrak & karyawan | ✅ Aktif |
| **SupportCore** | `/support/` | Tiket support, kontrak, presales, SLA monitoring | ✅ Aktif |
| **BookingCore** | `/booking/` | Reservasi ruangan & kendaraan operasional | ✅ Aktif |
| **AssetCore** | `/aset/` | Inventaris aset IT, lisensi, infrastruktur, subscription | ✅ Aktif |
| **ProjectCore** | `/project/` | Manajemen proyek, milestone, issue, task | ✅ Aktif |
| **AttendanceCore** | `/attendance/` | Live daily attendance, rencana & kemajuan kerja, cuti, lembur, dan koreksi | ✅ Aktif |

---

## Fitur Utama per Modul

### 🎯 TalentCore — Penilaian Kinerja

**Formula Evaluasi:**
```
Final = PP_Score × 0.3 + Kompetensi × 0.7

PP_Score     : Skor Project Performance (manual, 0–5 → dikali 20)
Kompetensi   : Weighted avg Hard Skill + Soft Skill + Ability
Hard Skill   : Σ(skor/4 × bobot) / Σbobot × 100
Ability      : Avg level A/B/C/D × 25
```

**Komponen Penilaian:**
- **Project Performance** — input target & pencapaian kerja nyata
- **Hard Skill** — penilaian teknis per divisi (template per 5 divisi)
- **Soft Skill / Kompetensi** — rating 1–5 per item berbobot
- **Ability** — deskripsi level A/B/C/D per kemampuan
- **Self-Assessment** — link token dikirim ke karyawan (email/WA/Telegram)
- **Peer Review** — feedback reviewer eksternal

**Fitur Lain:**
- Manajemen karyawan: divisi, level, tipe kontrak, supervisor
- Countdown masa kontrak kritis (≤14 hari) / warning (≤30 hari)
- Tabel gaji & tren historis per karyawan
- Reminder kontrak otomatis via Email / Telegram / WhatsApp

---

### 🕒 AttendanceCore — Presensi & Waktu Kerja
- **Live Daily Attendance Wall:** Menampilkan status kehadiran seluruh karyawan hari ini beserta jam clock-in/out, lokasi GPS, durasi kerja, dan link #PLAN/#PROGRESS.
- **Telegram Bot Webhook:** Mendukung absensi (Clock In & Clock Out) via sharing location di grup maupun japri.
- **Sequential Attendance Constraint:** Mewajibkan Clock In -> Isi `#PLAN` (min 10 karakter) -> Isi `#PROGRESS` (min 10 karakter) -> Clock Out.
- **Auto-Register Fallback:** Otomatis mendaftarkan user dan karyawan baru jika pengirim absensi dari Telegram tidak dikenal.
- **Pengajuan Cuti, Lembur, & Koreksi:** Dilengkapi dengan alur approval admin IT.

---

### 📊 Kinerja Task — Scoring Otomatis

Skor kinerja dihitung **otomatis** dari data nyata di seluruh modul (Project, Support, POC). Tidak perlu input manual.

**Cara Kerja:**
```
Skor Task = min(Total Raw Points ÷ (Benchmark × Durasi Bulan) × 100, 100)
```

**Tabel Bobot (Best Practice IT):**

| Tipe Task | Base Points | Priority Mult | On-time |
|-----------|-------------|---------------|---------|
| Project — PIC / Lead | 15 pts | Critical ×2.0, High ×1.5, Low ×0.7 | Tepat ×1.1 / Telat ×0.9 |
| Project — Implementor | 10 pts | sama | sama |
| Project — Member Tim | 7 pts | sama | sama |
| Issue Project (Programmer) | 3 pts × difficulty | Hard ×2.0, Normal ×1.0, Easy ×0.5 | sama |
| Task ProjectCore (done) | 2 pts | Priority mult | — |
| POC / Presales (closed) | 5 pts | — | — |
| Tiket Support (closed) | 2 pts | Priority mult | Tepat ×1.1 / Telat ×0.9 |

**Multiplier Priority:**

| Priority | Multiplier |
|----------|-----------|
| Critical / Blocker | ×2.0 |
| High | ×1.5 |
| Medium | ×1.0 |
| Low | ×0.7 |

**Interpretasi Skor:**

| Skor | Kategori |
|------|----------|
| ≥ 80 | 🟢 Sangat Baik |
| 60–79 | 🔵 Baik |
| 40–59 | 🟡 Cukup |
| < 40 | 🔴 Perlu Perhatian |

**Benchmark default: 100 pts/bulan** (configurable via filter di dashboard).

> Bobot per tipe task dapat dikonfigurasi oleh Admin di tabel `task_perf_config`.

**Dashboard:**
- `/kinerja/tim` — ranking tim, bar chart perbandingan, distribusi beban, filter per divisi
- `/kinerja/individu/<id>` — pie distribusi, riwayat evaluasi, tabel detail setiap task dihitung

---

### 🎫 SupportCore — Tiket & Kontrak

- Kontrak support tahunan per customer, multi-PIC, multi-layanan
- Tiket support: tracking status, SLA timer, multi-assignee
- Presales & POC: workflow request → approved/rejected
- SLA Monitoring: dashboard kepatuhan, trend bulanan, per customer
- Laporan filter date range / customer / tipe / assignee

---

### 📁 ProjectCore — Manajemen Proyek

- Project dengan fase, milestone, task, issue
- Multi-member tim (PIC, Implementor, Co-Leader, Member)
- Issue tracker: difficulty, priority, programmer/tester PIC, MD days
- Task board per milestone dengan assignee
- Integrasi dengan scoring kinerja otomatis

---

### 📅 BookingCore — Reservasi Sumber Daya

- Ruangan & kendaraan operasional
- Kalender harian, cek slot real-time, deteksi konflik otomatis
- Approval / cancel booking
- Slider foto + detail fasilitas per resource

---

### 💻 AssetCore — Inventaris IT

| Kategori | Isi |
|----------|-----|
| Aset (Laptop/PC) | OS, RAM, disk, Office, software terpasang, assigned ke karyawan |
| Infrastruktur | Server, router, AP, switch, monitor, tools |
| Lisensi Software | License key, seat, tipe (Perpetual/Subscription/Volume/OEM) |
| Subscription & ISP | SaaS, ISP — alert 30 hari sebelum berakhir |
| Request Alat Kerja | Workflow laptop purchase/checking, PIC IT Support, spec hardware, mandatori upload BAST & bukti serah terima |

---

## Portal & Infrastruktur

### 🏠 App Launcher (`/portal`)
- Aurora animated background (macOS Sequoia style)
- Glass-morphism card per app — klik → masuk, sidebar otomatis berubah
- Badge akses per role

### 👤 Manajemen User
- Employee Picker: cari karyawan → auto-fill data
- Reset password via Email / WhatsApp / Telegram
- Reset MFA, nonaktifkan user
- Deteksi & merge duplikat (Google SSO ↔ manual)

### 🔐 Keamanan
- MFA wajib (Google Authenticator / TOTP)
- Session timeout, open redirect protection
- Single-use token (1 jam TTL) untuk reset password
- Audit log semua aksi sensitif (`/portal/audit`)

### 🔔 Notifikasi
- Email (SMTP), Telegram Bot (dengan background thread non-blocking), WhatsApp (WAHA/OpenWA)
- Cron harian 08:00 WIB (kontrak, subscription)
- Laporan harian 22:00 WIB

### 🔄 Update Center (`/portal/update`)
- Notifikasi update otomatis dari GitHub (cek setiap 6 jam)
- Trigger update langsung dari browser — deploy via systemd
- Log deploy streaming realtime (SSE)
- Riwayat versi dengan release notes per tag
- Auto-reload setelah deploy selesai

---

## Stack Teknologi

| Komponen | Detail |
|----------|--------|
| Python | 3.11 |
| Flask | 3.1.x |
| Database | **PostgreSQL** (production & development) |
| ORM | Custom `_DBWrapper` — PostgreSQL adapter |
| Gunicorn | 21.2.x + gthread workers + unix socket |
| APScheduler | 3.11.x — cron jobs reminder & update check |
| Bootstrap | 5.3.3 + Bootstrap Icons |
| Chart.js / ApexCharts | Dashboard analytics |
| Font | Nunito / Inter (Google Fonts) |
| Nginx | Reverse proxy |
| OpenWA / WAHA | WhatsApp notification |
| Telegram Bot | Notifikasi & Absensi harian |
| systemd | Service management + in-app update trigger |

---

## Arsitektur File

```
hive/
├── app.py                   # Flask app (~15000+ baris) — semua route, auth, scheduler
├── version.py               # VERSION, RELEASE_DATE, RELEASE_NOTES
├── wsgi.py                  # Entry point Gunicorn
├── seed_data.py             # Template skill per divisi (TalentCore)
├── requirements.txt
├── deploy-ubuntu.sh         # Deploy/update script (idempotent, support --auto --version, reset password)
├── hive-update.path         # systemd: watcher trigger in-app update
├── hive-update.service      # systemd: runner deploy --auto
├── CHANGELOG.md
│
└── templates/
    ├── base.html                        # Layout utama
    ├── portal.html / portal_*.html      # Portal & admin
    ├── update_center.html               # Update Center
    │
    ├── # ── TalentCore ───────────────────────────────────────────
    ├── index.html / karyawan.html / employee_form.html
    ├── eval_*.html                      # Form evaluasi per komponen
    ├── kinerja_individu.html            # Dashboard kinerja task individu
    ├── kinerja_tim.html                 # Dashboard kinerja task tim/divisi
    │
    ├── # ── SupportCore ──────────────────────────────────────────
    ├── sc_*.html
    │
    ├── # ── ProjectCore ──────────────────────────────────────────
    ├── pc_*.html
    │
    ├── # ── BookingCore ──────────────────────────────────────────
    ├── booking_*.html
    │
    └── # ── AssetCore ────────────────────────────────────────────
        └── ac_*.html
```

---

## Database Schema (ringkasan prefix)

| Prefix | Modul | Tabel Utama |
|--------|-------|-------------|
| *(tanpa prefix)* | TalentCore | `employees`, `evaluations`, `skill_items`, `competency_items`, `ability_items`, `task_perf_config` |
| `sc_` | SupportCore | `sc_customers`, `sc_contracts`, `sc_tickets`, `sc_presales_requests`, `sc_sla_categories` |
| `pc_` | ProjectCore | `pc_projects`, `pc_members`, `pc_tasks`, `pc_issues`, `pc_milestones` |
| `bk_` | BookingCore | `bk_resources`, `bk_bookings` |
| `ac_` | AssetCore | `ac_assets`, `ac_infrastructure`, `ac_licenses`, `ac_subscriptions` |
| *(portal)* | Auth & Portal | `users`, `user_app_access`, `roles`, `audit_activity`, `app_settings` |

---

## Deploy

### Development

```bash
git clone https://github.com/barangbaru/solid-apps.git
cd solid-apps
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="dev-secret-ganti"
python3 app.py
# Buka: http://127.0.0.1:5000  |  Login: superadmin / Admin@123
```

### Production — Ubuntu + systemd

```bash
# Install baru atau update (idempotent)
sudo bash deploy-ubuntu.sh

# Update ke versi spesifik
sudo bash deploy-ubuntu.sh --auto --version 1.7.0

# Atau trigger langsung dari browser: /portal/update
```

Script otomatis: clone GitHub → venv → `.env` → systemd → Nginx → verifikasi.

### Environment Variables (`.env`)

```bash
SECRET_KEY=random-min-32-char
DB_TYPE=postgresql
PG_HOST=localhost
PG_PORT=5432
PG_NAME=hive_db
PG_USER=hive
PG_PASS=password
```

---

## Permission per Modul

| Modul | Permission |
|-------|-----------|
| TalentCore | `view_eval`, `manage_eval`, `view_salary`, `manage_salary`, `manage_employees`, `manage_divisions`, `manage_settings`, `manage_roles`, `send_reminders` |
| SupportCore | `sc_view`, `sc_manage_tickets`, `sc_manage_contracts`, `sc_manage_master`, `sc_manage_presales`, `sc_view_reports` |
| ProjectCore | `pc_view`, `pc_manage` |
| BookingCore | `booking_view`, `booking_create`, `booking_manage` |
| AssetCore | `ac_view`, `ac_manage_assets`, `ac_manage_infra`, `ac_manage_licenses`, `ac_manage_subs`, `ac_manage_requests` |

---

*Developed for PT MMI IT Team — internal use*
