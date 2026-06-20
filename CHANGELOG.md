# Changelog — Hive

Format: [Semantic Versioning](https://semver.org) — `MAJOR.MINOR.PATCH`
- **MAJOR**: breaking change (migrasi data, perubahan schema besar)
- **MINOR**: fitur baru
- **PATCH**: bug fix / perbaikan kecil

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
