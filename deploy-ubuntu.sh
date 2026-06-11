#!/bin/bash
# deploy-ubuntu.sh — Install & Update Evaluasi Kinerja di Ubuntu 20.04/22.04
# Jalankan sebagai root: sudo bash deploy-ubuntu.sh
#
# Script ini aman dijalankan berulang (idempotent):
#   - Install baru  : setup lengkap dari nol
#   - Update/redeploy : tarik kode baru, update deps, restart — database TIDAK tersentuh

set -e

APP_DIR="/var/www/evaluasi"
DATA_DIR="/var/lib/evaluasi"          # Database di luar app dir — aman saat update
SERVICE_NAME="evaluasi"
REPO_URL="https://github.com/barangbaru/solid-apps.git"
REPO_SUBDIR="PP/evaluasi"

IS_UPDATE=false
[ -f "$APP_DIR/wsgi.py" ] && IS_UPDATE=true

if $IS_UPDATE; then
    echo "========================================"
    echo " MODE: UPDATE APLIKASI"
    echo "========================================"
else
    echo "========================================"
    echo " MODE: INSTALL BARU"
    echo "========================================"
fi

# ─── [1] Sistem dependencies ──────────────────────────────────────────────────
if ! $IS_UPDATE; then
    echo "=== [1/7] Install system dependencies ==="
    apt-get update -qq
    apt-get install -y python3 python3-pip python3-venv nginx git rsync
else
    echo "=== [1/7] Lewati install system (mode update) ==="
fi

# ─── [2] Tarik kode terbaru ───────────────────────────────────────────────────
echo "=== [2/7] Tarik kode terbaru dari GitHub ==="
TMPDIR=$(mktemp -d)
git clone --depth=1 "$REPO_URL" "$TMPDIR/repo" -q

# rsync: salin kode baru, tapi JANGAN sentuh .env dan venv
rsync -a --delete \
    --exclude='.env' \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$TMPDIR/repo/$REPO_SUBDIR/" "$APP_DIR/"

rm -rf "$TMPDIR"
echo "  >> Kode berhasil diperbarui."

# ─── [3] Virtual environment & dependencies ───────────────────────────────────
echo "=== [3/7] Update virtual environment ==="
cd "$APP_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "  >> Dependencies up to date."

# ─── [4] File .env (hanya saat install baru) ──────────────────────────────────
echo "=== [4/7] Konfigurasi .env ==="
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|GANTI_DENGAN_RANDOM_STRING_PANJANG_DI_PRODUCTION|$SECRET|g" "$APP_DIR/.env"
    sed -i "s|DATABASE_PATH=.*|DATABASE_PATH=$DATA_DIR/evaluasi.db|g" "$APP_DIR/.env"
    echo "  >> .env baru dibuat. Edit $APP_DIR/.env untuk SMTP/Telegram."
else
    echo "  >> .env sudah ada, dilewati (database aman)."
fi

# ─── [5] Direktori data & permission ──────────────────────────────────────────
echo "=== [5/7] Setup direktori & permission ==="
mkdir -p "$DATA_DIR"
mkdir -p /var/log/evaluasi
chown -R www-data:www-data "$APP_DIR"
chown -R www-data:www-data "$DATA_DIR"
chmod 750 "$DATA_DIR"
# Pastikan venv tetap executable setelah chown
find "$APP_DIR/venv/bin" -type f -exec chmod +x {} \;
echo "  >> Data dir: $DATA_DIR (database di sini, aman saat update)"

# ─── [6] Systemd service ──────────────────────────────────────────────────────
echo "=== [6/7] Install & restart service ==="
cp "$APP_DIR/evaluasi.service" /etc/systemd/system/${SERVICE_NAME}.service

# Tambah ReadWritePaths untuk DATA_DIR di service jika belum ada
if ! grep -q "$DATA_DIR" /etc/systemd/system/${SERVICE_NAME}.service; then
    sed -i "s|ReadWritePaths=.*|ReadWritePaths=$APP_DIR $DATA_DIR /var/log/evaluasi|" \
        /etc/systemd/system/${SERVICE_NAME}.service
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl status "$SERVICE_NAME" --no-pager -l

# ─── [7] Nginx (hanya saat install baru) ──────────────────────────────────────
echo "=== [7/7] Konfigurasi Nginx ==="
if [ ! -f /etc/nginx/sites-available/evaluasi ] || ! $IS_UPDATE; then
    cat > /etc/nginx/sites-available/evaluasi << 'NGINXCONF'
server {
    listen 80;
    server_name _;

    client_max_body_size 10M;

    location / {
        proxy_pass         http://unix:/run/evaluasi/evaluasi.sock;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120;
    }

    location /static/ {
        alias /var/www/evaluasi/static/;
        expires 7d;
        add_header Cache-Control "public";
    }
}
NGINXCONF
    ln -sf /etc/nginx/sites-available/evaluasi /etc/nginx/sites-enabled/evaluasi
    [ -f /etc/nginx/sites-enabled/default ] && rm /etc/nginx/sites-enabled/default && echo "  >> Site 'default' dinonaktifkan."
    echo "  >> Config Nginx baru dibuat."
fi
nginx -t && systemctl reload nginx
echo "  >> Nginx reloaded."

# ─── Ringkasan ────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
if $IS_UPDATE; then
    echo " UPDATE SELESAI"
else
    echo " INSTALL SELESAI"
fi
echo "========================================"
echo " URL          : http://$(hostname -I | awk '{print $1}')"
echo " Database     : $DATA_DIR/evaluasi.db"
echo " Config .env  : $APP_DIR/.env"
echo " Log          : journalctl -u evaluasi -f"
echo " Update app   : sudo bash $APP_DIR/deploy-ubuntu.sh"
echo "========================================"
if ! $IS_UPDATE; then
    echo " LOGIN AWAL:"
    echo "   Username : superadmin"
    echo "   Password : Admin@123"
    echo " !! Segera ganti password setelah login !!"
    echo "========================================"
    echo " PENTING: Edit .env untuk SMTP & Telegram!"
    echo "========================================"
fi
