@echo off
title Hive — Local Dev
cd /d "%~dp0"

echo ============================================================
echo  Hive — Local Development Server
echo ============================================================

:: Matikan proses lama di port 5000
echo [1/4] Memeriksa proses lama di port 5000...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":5000 "') do (
    if not "%%a"=="0" (
        echo        Mematikan PID %%a ...
        taskkill /PID %%a /F >nul 2>&1
    )
)
timeout /t 1 /nobreak >nul

:: Bersihkan bytecode cache
echo [2/4] Membersihkan cache Python...
if exist __pycache__ rmdir /s /q __pycache__ 2>nul

:: Set env vars untuk development lokal
echo [3/4] Menyiapkan environment...
set FLASK_DEBUG=0
set PORT=5000

:: SECRET_KEY — generate sekali atau isi manual di bawah
if "%SECRET_KEY%"=="" set SECRET_KEY=dev-secret-key-ganti-di-production

:: FIELD_ENCRYPT_KEY — diperlukan untuk akses tabel gaji
:: Jika belum punya key, generate dengan perintah:
::   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
:: lalu isi di bawah ini:
if "%FIELD_ENCRYPT_KEY%"=="" (
    echo.
    echo  [PERINGATAN] FIELD_ENCRYPT_KEY tidak diset.
    echo  Data gaji tidak akan bisa didekripsi.
    echo  Set env var FIELD_ENCRYPT_KEY sebelum menjalankan run.bat
    echo  atau isi langsung di baris: set FIELD_ENCRYPT_KEY=...
    echo.
)

:: DATABASE_URL dikosongkan agar pakai SQLite lokal (bukan PostgreSQL)
set DATABASE_URL=

:: Aktifkan venv jika ada
echo [4/4] Menjalankan aplikasi...
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
    echo      [venv aktif]
) else (
    echo      [menggunakan Python sistem]
)

echo.
echo  Buka browser: http://127.0.0.1:%PORT%
echo  Database    : SQLite lokal (evaluasi.db)
echo  Mode        : Development
echo ============================================================
echo  Tekan Ctrl+C untuk menghentikan
echo ============================================================
echo.

python app.py
pause
