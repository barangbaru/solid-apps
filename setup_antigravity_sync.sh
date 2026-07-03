#!/bin/bash

# Exit on error
set -e

ONEDRIVE_PATH="/Users/md/Library/CloudStorage/OneDrive-SharedLibraries-ONEDRIVE"
SYNC_DIR="$ONEDRIVE_PATH/antigravity_sync"
GEMINI_DIR="$HOME/.gemini"

echo "==============================================="
echo "   Antigravity Sync Setup (Laptop A / Utama)   "
echo "==============================================="
echo "OneDrive Path: $ONEDRIVE_PATH"
echo "Local .gemini: $GEMINI_DIR"
echo "Sync Target  : $SYNC_DIR"
echo "==============================================="
echo ""

# Periksa apakah user menjalankan sebagai root (tidak disarankan)
if [ "$EUID" -eq 0 ]; then
  echo "Peringatan: Jangan jalankan script ini menggunakan sudo."
  exit 1
fi

# Tanyakan konfirmasi penutupan aplikasi
echo "PENTING: Pastikan Anda telah menutup seluruh aplikasi Antigravity, AGY CLI, dan IDE sebelum melanjutkan."
read -p "Apakah Anda yakin ingin melanjutkan? (y/n): " confirm
if [[ $confirm != "y" && $confirm != "Y" ]]; then
  echo "Setup dibatalkan."
  exit 1
fi

# 1. Buat folder sync di OneDrive jika belum ada
if [ ! -d "$SYNC_DIR" ]; then
  echo "-> Membuat direktori sync di OneDrive..."
  mkdir -p "$SYNC_DIR"
fi

# 2. Backup folder .gemini lokal jika ada dan bukan symlink
if [ -d "$GEMINI_DIR" ]; then
  if [ -L "$GEMINI_DIR" ]; then
    echo "-> Peringatan: Folder $GEMINI_DIR sudah berupa symbolic link."
    echo "Apakah Anda ingin memperbarui link tersebut? (y/n)"
    read -p "> " link_confirm
    if [[ $link_confirm != "y" && $link_confirm != "Y" ]]; then
      echo "Setup dibatalkan."
      exit 0
    fi
    echo "-> Menghapus symbolic link lama..."
    rm "$GEMINI_DIR"
  else
    BACKUP_DIR="$HOME/.gemini_backup_$(date +%Y%m%d_%H%M%S)"
    echo "-> Membuat backup folder .gemini lokal ke $BACKUP_DIR..."
    cp -R "$GEMINI_DIR" "$BACKUP_DIR"
    
    # 3. Pindahkan data ke OneDrive jika target belum ada
    if [ ! -d "$SYNC_DIR/.gemini" ]; then
      echo "-> Memindahkan folder .gemini ke OneDrive..."
      mv "$GEMINI_DIR" "$SYNC_DIR/.gemini"
    else
      echo "-> Folder .gemini sudah ada di OneDrive. Menghapus folder lokal..."
      rm -rf "$GEMINI_DIR"
    fi
  fi
else
  echo "-> Folder .gemini lokal tidak ditemukan. Akan membuat baru di OneDrive..."
  mkdir -p "$SYNC_DIR/.gemini"
fi

# 4. Buat symbolic link ke OneDrive
echo "-> Membuat symbolic link dari ~/.gemini ke OneDrive..."
ln -s "$SYNC_DIR/.gemini" "$GEMINI_DIR"

# 5. Buat script helper untuk Laptop B (laptop kedua) di folder OneDrive sync
echo "-> Membuat script pembantu untuk laptop kedua (setup_other_laptop.sh)..."
cat << 'EOF' > "$SYNC_DIR/setup_other_laptop.sh"
#!/bin/bash

# Exit on error
set -e

GEMINI_DIR="$HOME/.gemini"

echo "==============================================="
echo "   Antigravity Sync Setup (Laptop Kedua / B)   "
echo "==============================================="
echo ""

# Cari lokasi OneDrive di CloudStorage secara otomatis
echo "Mendeteksi folder OneDrive..."
ONEDRIVE_DETECTED=""
for dir in ~/Library/CloudStorage/*; do
  if [[ "$dir" == *OneDrive* ]]; then
    ONEDRIVE_DETECTED="$dir"
    break
  fi
done

if [ -n "$ONEDRIVE_DETECTED" ]; then
  echo "Ditemukan OneDrive di: $ONEDRIVE_DETECTED"
else
  echo "Gagal mendeteksi folder OneDrive secara otomatis."
  read -p "Masukkan path absolut folder OneDrive Anda (contoh: /Users/username/Library/CloudStorage/OneDrive-SharedLibraries-ONEDRIVE): " ONEDRIVE_DETECTED
fi

SYNC_DIR="$ONEDRIVE_DETECTED/antigravity_sync"

if [ ! -d "$SYNC_DIR/.gemini" ]; then
  echo "EROR: Folder '.gemini' tidak ditemukan di $SYNC_DIR/.gemini."
  echo "Pastikan sinkronisasi OneDrive telah selesai di laptop ini."
  exit 1
fi

echo "PENTING: Pastikan Anda telah menutup seluruh aplikasi Antigravity, AGY CLI, dan IDE sebelum melanjutkan."
read -p "Apakah Anda yakin ingin melanjutkan? (y/n): " confirm
if [[ $confirm != "y" && $confirm != "Y" ]]; then
  echo "Setup dibatalkan."
  exit 1
fi

# Backup folder .gemini lokal di Laptop B jika ada
if [ -d "$GEMINI_DIR" ]; then
  if [ -L "$GEMINI_DIR" ]; then
    echo "-> Menghapus symbolic link lama..."
    rm "$GEMINI_DIR"
  else
    BACKUP_DIR="$HOME/.gemini_backup_$(date +%Y%m%d_%H%M%S)"
    echo "-> Membuat backup folder .gemini lokal ke $BACKUP_DIR..."
    mv "$GEMINI_DIR" "$BACKUP_DIR"
  fi
fi

# Buat symbolic link ke OneDrive
echo "-> Membuat symbolic link dari ~/.gemini ke OneDrive..."
ln -s "$SYNC_DIR/.gemini" "$GEMINI_DIR"

echo ""
echo "Setup selesai! Sekarang Laptop ini terhubung ke history dan memory Antigravity di OneDrive."
echo "==============================================="
EOF

chmod +x "$SYNC_DIR/setup_other_laptop.sh"

# 6. Buat script helper untuk Windows (setup_windows.bat) di folder OneDrive sync
echo "-> Membuat script pembantu untuk Windows (setup_windows.bat)..."
cat << 'EOF' > "$SYNC_DIR/setup_windows.bat"
@echo off
setlocal enabledelayedexpansion

echo ===============================================
echo    Antigravity Sync Setup (Windows Client)
echo ===============================================
echo.

:: 1. Deteksi folder OneDrive secara otomatis
echo Mendeteksi folder OneDrive...
set "ONEDRIVE_DETECTED="

:: Periksa folder OneDrive default
if exist "%USERPROFILE%\OneDrive" (
    set "ONEDRIVE_DETECTED=%USERPROFILE%\OneDrive"
)
:: Periksa folder OneDrive komersial/institusi
for /d %%d in ("%USERPROFILE%\OneDrive - *") do (
    set "ONEDRIVE_DETECTED=%%d"
)

if not "!ONEDRIVE_DETECTED!"=="" (
    echo Ditemukan OneDrive di: !ONEDRIVE_DETECTED!
) else (
    echo Gagal mendeteksi folder OneDrive secara otomatis.
    set /p "ONEDRIVE_DETECTED=Masukkan path folder OneDrive Anda (contoh: C:\Users\Username\OneDrive): "
)

set "SYNC_DIR=!ONEDRIVE_DETECTED!\antigravity_sync"
set "GEMINI_DIR=%USERPROFILE%\.gemini"

if not exist "!SYNC_DIR!\.gemini" (
    echo EROR: Folder '.gemini' tidak ditemukan di !SYNC_DIR!\.gemini.
    echo Pastikan sinkronisasi OneDrive telah selesai di PC ini.
    pause
    exit /b 1
)

echo PENTING: Pastikan Anda telah menutup seluruh aplikasi Antigravity, AGY CLI, dan IDE sebelum melanjutkan.
set /p "confirm=Apakah Anda yakin ingin melanjutkan? (y/n): "
if /i "!confirm!" neq "y" (
    echo Setup dibatalkan.
    pause
    exit /b 1
)

:: 2. Backup folder .gemini lokal di Windows jika ada
if exist "%GEMINI_DIR%" (
    echo -> Membackup folder .gemini lokal lama...
    set "timestamp=%date:~10,4%%date:~4,2%%date:~7,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
    set "timestamp=!timestamp: =0!"
    set "BACKUP_DIR=%USERPROFILE%\.gemini_backup_!timestamp!"
    
    move "%GEMINI_DIR%" "!BACKUP_DIR!" >nul 2>&1
    if errorlevel 1 (
        echo -> Folder lama berupa junction/symlink, menghapus link...
        rmdir "%GEMINI_DIR%"
    )
)

:: 3. Buat Junction Link ke OneDrive
echo -> Membuat directory junction dari %GEMINI_DIR% ke OneDrive...
mklink /J "%GEMINI_DIR%" "!SYNC_DIR!\.gemini"

if errorlevel 0 (
    echo.
    echo Setup selesai! Sekarang PC ini terhubung ke history dan memory Antigravity di OneDrive.
) else (
    echo.
    echo EROR: Gagal membuat junction link. Pastikan Anda memiliki izin akses.
)

echo ===============================================
pause
EOF

echo ""
echo "==============================================="
echo "Setup Laptop A Selesai!"
echo "Folder ~/.gemini sekarang tersambung ke OneDrive."
echo ""
echo "Untuk laptop kedua (Laptop B):"
echo "1. Tunggu sinkronisasi OneDrive selesai."
echo "2. Jika laptop kedua menggunakan macOS/Linux, jalankan:"
echo "   $SYNC_DIR/setup_other_laptop.sh"
echo "3. Jika laptop kedua menggunakan Windows, jalankan:"
echo "   $SYNC_DIR/setup_windows.bat"
echo "==============================================="

