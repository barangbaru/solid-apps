# Project-Scoped Rules for Hive Superapp

Setiap kali berinteraksi dalam workspace ini, asisten dan sub-agent wajib mematuhi aturan alur kerja kolaboratif berikut:

## 1. Cek & Tarik Update Git (Setiap Awal Chat/Turn)
* Sebelum menganalisis masalah atau memodifikasi kode, asisten **wajib** melakukan pengecekan ke repositori remote untuk melihat apakah ada pembaruan dari kolaborator lain:
  ```bash
  git fetch && git status
  ```
* Jika local branch berada di belakang (*behind*) remote branch, lakukan penarikan update terbaru terlebih dahulu:
  ```bash
  git pull
  ```

## 2. Commit, Tag, & Push (Setiap Selesai Modifikasi)
* Setelah melakukan perbaikan atau penyesuaian kode, lakukan langkah-langkah rilis berikut:
  1. **Update Versi**: Naikkan versi (*patch level*) di [version.py](file:///Users/md/Library/CloudStorage/OneDrive-SharedLibraries-ONEDRIVE/workspace/HIVE/version.py) (misalnya dari `2.2.4` ke `2.2.5`) dan tambahkan penjelasan singkat perubahan pada `RELEASE_NOTES`.
  2. **Commit Perubahan**: Lakukan commit semua berkas yang diubah:
     ```bash
     git add .
     git commit -m "Deskripsi perubahan dan perbaikan"
     ```
  3. **Buat Tag Versi**: Buat git tag baru sesuai versi di `version.py` dengan prefiks `v` (misal: `v2.2.5`):
     ```bash
     git tag v2.2.5
     ```
  4. **Push ke Remote**: Push commit dan tag baru tersebut ke repositori GitHub:
     ```bash
     git push origin main
     git push origin v2.2.5
     ```
