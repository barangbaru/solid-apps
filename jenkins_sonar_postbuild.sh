#!/bin/bash

# Pastikan script ini dijalankan setelah proses SonarQube Scanner selesai.
# Tunggu sejenak agar SonarQube memproses hasil scan di background (jika perlu).
sleep 15

# 1. Eksekusi script Python penarik laporan
echo "Menjalankan script penarik laporan SonarQube..."
python3 fetch_sonar_issues.py

# 2. Commit file ke Git
# Set config name & email secara lokal (temporary) hanya untuk eksekusi di workspace ini
git config user.name "Jenkins CI"
git config user.email "jenkins@mitramandiri.com"

# Catatan: Proses push akan otomatis menggunakan credential git (SSH Key atau Git Token) 
# yang sedang aktif / di-inject oleh Jenkins saat job ini dieksekusi.

# Cek apakah ada file SONAR_ISSUES.md yang diubah atau baru
if [[ -n $(git status -s SONAR_ISSUES.md) ]]; then
    git add SONAR_ISSUES.md
    git commit -m "chore: Update laporan otomatis SonarQube issues [skip ci]"
    
    # Push ke branch main menggunakan URL dengan credentials
    # [skip ci] ditambahkan agar Jenkins tidak loop trigger build kembali
    git push https://${GITHUB_TOKEN}@github.com/barangbaru/solid-apps.git HEAD:main
    echo "Laporan SonarQube berhasil di-push ke repositori."
else
    echo "Tidak ada perubahan pada issue SonarQube. Skip commit."
fi
