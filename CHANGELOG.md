# Changelog — Hive

Format: [Semantic Versioning](https://semver.org) — `MAJOR.MINOR.PATCH`
- **MAJOR**: breaking change (migrasi data, perubahan schema besar)
- **MINOR**: fitur baru
- **PATCH**: bug fix / perbaikan kecil

---

## [1.7.6] — 2026-06-27
### Fitur: Prioritas Tiket Support
- Kolom `priority` ditambahkan ke tabel `sc_tickets` (migrasi otomatis, default `'Medium'`)
- Pilihan: Critical / High / Medium / Low dengan badge warna di daftar & detail tiket
- Form tambah/edit tiket: dropdown Prioritas di antara Tipe Support dan Kategori SLA
- Skor kinerja tiket support kini mempertimbangkan priority multiplier (sesuai `task_perf_config`)

---

## [1.7.5] — 2026-06-27
### Bug Fix
- Fix error `column t.priority does not exist` di `sc_tickets`: tabel tiket support tidak memiliki kolom `priority`
- Hapus `t.priority` dari query `calc_task_perf` dan `calc_task_analytics` untuk support tickets; gunakan nilai default `'Medium'` sebagai multiplier prioritas

---

## [1.7.4] — 2026-06-27
### Bug Fix & Peningkatan
- Fix error 500 tidak tercatat di `audit_errors`: error handler kini membuka koneksi DB baru (fresh) untuk logging, menghindari koneksi yang stuck di *aborted transaction* setelah psycopg2 error
- Audit Trail tab "Log Error": tambah toggle switch **Pesan Error** dan **Traceback (Stack Trace)** — setting tersimpan di localStorage untuk troubleshoot tanpa perlu ke server

---

## [1.7.3] — 2026-06-27
### Bug Fix
- Fix error `column i.due_date does not exist` di PostgreSQL: tabel `pc_issues` tidak memiliki kolom `due_date` (hanya ada `resolved_date`, `issued_date`, `created_at`)
- Perbaikan di `calc_task_perf()` dan `calc_task_analytics()`: hapus `i.due_date` dari SELECT, gunakan `resolved_date` sebagai proxy timeliness untuk issue project

---

## [1.7.2] — 2026-06-27
### Fitur: Analitik Kinerja Divisi (`/kinerja/analitik`)
- `calc_task_analytics()`: fungsi baru — analitik detail per karyawan mencakup timeliness, concurrency, breakdown per tipe task
- **Timeliness breakdown**: done_ontime / done_delay / open_ontime / open_overtime (task masih open & sudah lewat due date)
- **Concurrency**: maks & rata-rata task aktif bersamaan (via event-point scan pada interval [start, done])
- **Tabel perbandingan**: semua metrik dalam satu baris per karyawan — volume, project vs support, timeliness, concurrency, % on-time, skor
- **4 chart**: stacked bar volume per tipe, stacked bar timeliness, bar concurrency dengan line avg, bubble chart volume vs skor (ukuran bubble = maks concurrent)
- **Rangkuman per divisi**: aggregate on-time, delay, overtime, avg skor
- Menu "Analitik Divisi" ditambahkan di sidebar TalentCore

---

## [1.7.1] — 2026-06-27
### Dokumentasi & Transparansi
- README.md diperbarui lengkap: nama app (Hive), stack PostgreSQL, semua modul, Scoring Framework tabel, arsitektur file, deploy guide
- Panel **"Cara Sistem Menilai Kinerja Task"** (collapsible) ditambahkan di dashboard Kinerja Individu & Kinerja Tim: formula, tabel base points, priority multiplier, on-time multiplier, kategori skor, syarat task terhitung

---

## [1.7.0] — 2026-06-27
### Fitur: Kinerja Task
- **Scoring otomatis** dari seluruh sumber data: Project (PIC/Implementor/Member), Issue Project, Task ProjectCore, POC/Presales, Tiket Support
- **Bobot best practice**: base points per tipe × priority multiplier × on-time multiplier, dinormalisasi ke 0–100 vs benchmark (default 100 pts/bulan)
- **Tabel `task_perf_config`**: bobot dapat dikonfigurasi per tipe task (Admin Sistem)
- **Dashboard Individu** `/kinerja/individu/<id>`: skor, pie chart distribusi, bar riwayat evaluasi, tabel detail semua task dihitung, filter periode & benchmark
- **Dashboard Tim** `/kinerja/tim`: ranking seluruh anggota, bar chart perbandingan, pie chart distribusi beban, breakdown mini per orang, filter divisi & periode
- **API endpoint** `/api/kinerja/task-score/<emp_id>` untuk integrasi data
- Menu **"Kinerja Task"** ditambahkan di sidebar TalentCore

---

## [1.4.9] — 2026-06-20
### Peningkatan
- Update Center: halaman auto-refresh setelah deploy selesai dengan countdown (`✓ Selesai — reload (4s)`)
- Banner hijau "Update berhasil!" muncul otomatis setelah reload (via sessionStorage)
- Jika server restart (SSE putus), countdown 6 detik lalu reload otomatis
- Fallback polling: jika log tidak ada aktivitas 30 detik, halaman reload otomatis untuk cek status terbaru

---

## [1.4.8] — 2026-06-20
### Bug Fix
- Fix in-app update trigger tidak berjalan: `ExecStartPre` multi-line bash di systemd service file tidak di-parse benar → VERSION kosong, output `date` kacau
- Solusi: ganti `hive-update.service` agar memanggil wrapper script `/usr/local/bin/hive-update-run.sh` yang di-install otomatis saat `deploy-ubuntu.sh` dijalankan
- Fix `TAG_COUNT` integer error (line 92): `grep -c` no-match exit 1 menyebabkan double-output `0\n0` → fix dengan `${TAG_COUNT:-0}` terpisah

---

## [1.4.7] — 2026-06-20
### Peningkatan
- Riwayat Versi: tampilkan 5 versi terbaru saja

---

## [1.4.6] — 2026-06-20
### Bug Fix
- Fix status badge Riwayat Versi: sebelumnya semua versi selain `latest_version` dibadge "Lama", padahal bisa saja ada versi lebih baru dari yang terpasang tapi bukan latest
- Status sekarang dihitung via perbandingan semver di Python:
  - `Terpasang` — versi yang sedang berjalan
  - `Terbaru` — versi terbaru di GitHub (lebih baru dari terpasang)
  - `Belum Terpasang` — lebih baru dari terpasang tapi bukan yang terbaru (versi antara)
  - `Lama` — lebih lama dari yang terpasang

---

## [1.4.5] — 2026-06-20
### Peningkatan
- Riwayat Versi di Update Center: setiap baris tag sekarang dapat diklik untuk melihat release notes dari GitHub Releases
- `check_for_updates()`: fetch semua releases sekaligus (`/releases?per_page=50`) lalu gabungkan ke `update_all_tags` sebagai `[{tag, notes}]`
- Template: collapse row per tag dengan render Markdown (headers, bold, code, list)
- Backward-compatible: format `update_all_tags` lama (list of string) tetap diproses dengan benar

---

## [1.4.4] — 2026-06-20
### Bug Fix
- Fix "Memeriksa update..." tidak hilang: cek update dijalankan synchronous (blocking) bukan di thread terpisah, sehingga hasil langsung tersedia saat redirect
- Fix riwayat versi kosong: efek samping dari error `column id` yang sudah fix di v1.4.3 — setelah fix ini, klik "Cek Sekarang" langsung menampilkan daftar semua tag dari GitHub
- Flash message lebih informatif: tampilkan "Update tersedia: vX.Y.Z" atau "Sudah versi terbaru" sesuai hasil cek

---

## [1.4.3] — 2026-06-20
### Bug Fix
- Fix error `column "id" does not exist` saat INSERT ke tabel tanpa kolom `id` (misal `app_settings` yang pakai `key` sebagai PK)
- `_DBWrapper.execute()`: jika `RETURNING id` gagal karena tabel tidak punya kolom `id`, retry otomatis tanpa `RETURNING id`
- Memperbaiki fitur Update Center (cek update manual/otomatis) yang gagal karena `app_settings` tidak punya kolom `id`

---

## [1.4.2] — 2026-06-20
### Bug Fix
- Fix `last_insert_rowid()` SQLite → `lastval()` PostgreSQL di `_fix()`
  Berlaku untuk: tambah kontrak baru (SupportCore) & booking recurring (BookingCore)
- Audit lengkap seluruh query: tidak ada lagi `IFNULL`, `GROUP_CONCAT`, `GLOB`, `PRINTF` SQLite-specific
- `datetime DEFAULT` di SCHEMA sudah dihandle `_pg_adapt_schema()` → `NOW()`

---

## [1.4.1] — 2026-06-20
### Bug Fix
- Fix image resource BookingCore tidak bisa dibuka: `date(col)` SQLite tidak dikenal PostgreSQL
- Tambah konversi `date(col)` → `(col)::date` di `_DBWrapper._fix()` — berlaku untuk semua query booking yang filter by date

---

## [1.4.0] — 2026-06-20
### Update Center
- Notifikasi update otomatis: APScheduler cek GitHub tags setiap 6 jam
- Badge "NEW" di topbar & sidebar untuk role yang berhak (superadmin/admin)
- Halaman `/portal/update`: status versi, release notes, daftar semua tag
- Trigger update dari dalam app — tulis flag file → systemd `hive-update.path` deteksi → jalankan `deploy-ubuntu.sh --auto` sebagai root
- Log deploy streaming realtime via SSE (Server-Sent Events) ke browser
- Browser auto-reload saat deploy selesai / server restart
- Role control: `update_notify_roles` (siapa lihat notif) & `update_trigger_roles` (siapa bisa trigger)
- Systemd unit files: `hive-update.path` + `hive-update.service` (di-install otomatis via deploy script)
- `deploy-ubuntu.sh --auto`: skip semua prompt interaktif untuk triggered update

---

## [1.3.0] — 2026-06-20
### Migrasi & Infrastruktur
- Migrasi database dari SQLite ke PostgreSQL
- `_DBWrapper` kompatibilitas dual-database (otomatis convert `?`→`%s`, `julianday`→date arithmetic, dll)
- `deploy-ubuntu.sh`: dialog pilihan database (SQLite / PostgreSQL auto-install / PostgreSQL existing)
- `migrate_to_pg.py`: script migrasi data SQLite→PostgreSQL dengan verifikasi row count
- Auto-migrasi saat deploy pertama kali ke PostgreSQL

### BookingCore
- Image slider + lightbox untuk resource (ruangan/kendaraan)
- Edit resource: nama, tipe, kapasitas, lokasi, fasilitas, warna, ikon, foto
- Item tambahan dalam booking: minuman, makanan (Air Mineral, Kopi, Teh, Jus, dll)
- Menu sidebar dinamis dari database

### Portal
- Rename aplikasi dari "super-us" ke **Hive**
- Tambah ProjectCore, DocsCore, FinanceCore, HelpdeskCore (label "Segera Hadir")
- Semua user lihat semua app di portal; badge "Tidak Memiliki Akses" untuk yang belum punya akses

### Bug Fix
- Fix AssetCore: permission `ac_view` tidak muncul di system roles (admin/viewer)
- Fix system roles (admin/superadmin/viewer) tidak muncul di dropdown per-app
- Fix BookingCore: resource duplikat ratusan kali setiap restart
- Fix TalentCore: update role otomatis grant akses ke semua user
- Fix kontrak kadaluarsa menampilkan sisa hari negatif → tampilkan 0

---

## [1.2.0] — 2026-05-15
### Fitur
- **AssetCore**: pencatatan & tracking aset, infrastruktur, lisensi, software, subscription
- **TalentCore**: proteksi password untuk ubah stok item tambahan
- Item tambahan (air mineral/snack) dengan pembukuan terpisah
- Laporan harian otomatis 22:00 WIB + export dashboard ke PDF
- Master Terapis tanpa password

---

## [1.1.0] — 2026-04-10
### Fitur
- **BookingCore**: pemesanan ruangan & kendaraan
- **SupportCore**: monitoring technical support, SLA, presales
- Multi-app portal dengan manajemen akses per-user per-app
- Sistem role per-aplikasi + role system global (superadmin/admin/viewer)
- Google OAuth login
- MFA (TOTP) untuk keamanan akun

---

## [1.0.0] — 2026-03-01
### Rilis Pertama
- **TalentCore**: penilaian & review kinerja karyawan
- Evaluasi karyawan: project points, hard skill, soft skill, ability
- Manajemen divisi, jabatan, kontrak karyawan
- Reminder kontrak via email, Telegram, WhatsApp
- Portal superapp dengan manajemen user & role
