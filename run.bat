@echo off
title Aplikasi Evaluasi Kinerja Tim IT
cd /d "%~dp0"
echo ============================================================
echo  Aplikasi Evaluasi Kinerja Tim IT
echo  Buka browser: http://127.0.0.1:5000
echo.
echo  Login default (ganti segera setelah masuk):
echo    Username : superadmin
echo    Password : Admin@123
echo ============================================================
python app.py
pause
