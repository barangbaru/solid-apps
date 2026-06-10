@echo off
setlocal EnableDelayedExpansion

:: ── Konfigurasi ──────────────────────────────────────────────────────────────
set REGISTRY=nexus.devops.mmi-pt.com
set IMAGE_NAME=evaluasi-kinerja
set IMAGE_TAG=latest
set NEXUS_USER=developer
set NEXUS_PASS=nop4ssword

:: Baca tag dari argumen pertama jika ada (contoh: build-push.bat v1.2.0)
if not "%~1"=="" set IMAGE_TAG=%~1

set FULL_IMAGE=%REGISTRY%/%IMAGE_NAME%:%IMAGE_TAG%

echo.
echo ============================================================
echo   BUILD ^& PUSH: %FULL_IMAGE%
echo ============================================================
echo.

:: ── 1. Build Docker image ────────────────────────────────────────────────────
echo [1/4] Building Docker image...
docker build -t %FULL_IMAGE% .
if errorlevel 1 (
    echo [ERROR] Docker build gagal!
    exit /b 1
)
echo [OK] Build selesai.
echo.

:: ── 2. Tag tambahan :latest jika IMAGE_TAG bukan latest ─────────────────────
if not "%IMAGE_TAG%"=="latest" (
    set LATEST_IMAGE=%REGISTRY%/%IMAGE_NAME%:latest
    echo [2/4] Menambah tag latest: !LATEST_IMAGE!
    docker tag %FULL_IMAGE% !LATEST_IMAGE!
) else (
    echo [2/4] Tag sudah latest, skip.
)
echo.

:: ── 3. Login ke Nexus Registry ───────────────────────────────────────────────
echo [3/4] Login ke %REGISTRY%...
echo %NEXUS_PASS% | docker login %REGISTRY% -u %NEXUS_USER% --password-stdin
if errorlevel 1 (
    echo [ERROR] Login ke Nexus gagal! Cek kredensial atau koneksi.
    exit /b 1
)
echo [OK] Login berhasil.
echo.

:: ── 4. Push image ────────────────────────────────────────────────────────────
echo [4/4] Pushing %FULL_IMAGE%...
docker push %FULL_IMAGE%
if errorlevel 1 (
    echo [ERROR] Push gagal!
    exit /b 1
)

if not "%IMAGE_TAG%"=="latest" (
    echo Pushing !LATEST_IMAGE!...
    docker push !LATEST_IMAGE!
)

echo.
echo ============================================================
echo   SUKSES! Image tersedia di:
echo   %FULL_IMAGE%
echo ============================================================
echo.
echo Untuk deploy, jalankan di server:
echo   docker-compose pull ^&^& docker-compose up -d
echo.

endlocal
