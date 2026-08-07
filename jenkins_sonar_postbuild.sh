#!/bin/bash

# Pastikan script ini dijalankan setelah proses SonarQube Scanner selesai.
# Tunggu sejenak agar SonarQube memproses hasil scan di background (jika perlu).
sleep 15

# 1. Eksekusi script Python penarik laporan
echo "Menjalankan script penarik laporan SonarQube..."
python3 fetch_sonar_issues.py

# 2. Commit file ke Git
# Pastikan Jenkins punya kredensial/akses SSH untuk push ke branch origin
git config --global user.name "Jenkins CI"
git config --global user.email "jenkins@mitramandiri.com"

# Cek apakah ada file SONAR_ISSUES.md yang diubah atau baru
if [[ -n $(git status -s SONAR_ISSUES.md) ]]; then
    git add SONAR_ISSUES.md
    git commit -m "chore: Update laporan otomatis SonarQube issues [skip ci]"
    
    # Push ke branch main
    # [skip ci] ditambahkan agar Jenkins tidak loop trigger build kembali
    git push origin HEAD:main
    echo "Laporan SonarQube berhasil di-push ke repositori."
else
    echo "Tidak ada perubahan pada issue SonarQube. Skip commit."
fi
