@echo off
title Aplikasi Evaluasi Kinerja Tim IT
cd /d "%~dp0"

echo ============================================================
echo  Aplikasi Evaluasi Kinerja Tim IT
echo ============================================================

:: Matikan proses Python lama yang mungkin masih jalan di port 5000
echo [1/3] Memeriksa proses lama di port 5000...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":5000 "') do (
    if not "%%a"=="0" (
        echo        Mematikan proses PID %%a ...
        taskkill /PID %%a /F >nul 2>&1
    )
)
timeout /t 1 /nobreak >nul

:: Bersihkan bytecode cache Python
echo [2/3] Membersihkan cache Python...
if exist __pycache__ (
    rmdir /s /q __pycache__ 2>nul
)

:: Aktifkan venv jika ada, atau pakai Python sistem
echo [3/3] Menjalankan aplikasi...
echo.
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
    echo      [venv aktif]
) else (
    echo      [menggunakan Python sistem]
)

echo.
echo  Buka browser: http://127.0.0.1:5000
echo.
echo  Login default (ganti segera setelah masuk):
echo    Username : superadmin
echo    Password : Admin@123
echo ============================================================
echo  Tekan Ctrl+C untuk menghentikan aplikasi
echo ============================================================
echo.

python app.py
pause
