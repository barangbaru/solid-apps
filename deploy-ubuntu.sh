#!/bin/bash
# deploy-ubuntu.sh — Setup awal Evaluasi Kinerja di Ubuntu 20.04/22.04
# Jalankan sebagai root: sudo bash deploy-ubuntu.sh

set -e

APP_DIR="/var/www/evaluasi"
SERVICE_NAME="evaluasi"
REPO_URL="https://github.com/barangbaru/solid-apps.git"
REPO_SUBDIR="PP/evaluasi"

echo "=== [1/7] Install dependencies ==="
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv nginx git

echo "=== [2/7] Clone / update repo ==="
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR"
    git pull origin main
else
    mkdir -p "$(dirname $APP_DIR)"
    # Clone full repo lalu pindahkan subfolder evaluasi
    TMPDIR=$(mktemp -d)
    git clone "$REPO_URL" "$TMPDIR/repo"
    rsync -a --delete "$TMPDIR/repo/$REPO_SUBDIR/" "$APP_DIR/"
    rm -rf "$TMPDIR"
fi

echo "=== [3/7] Setup virtual environment ==="
cd "$APP_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

echo "=== [4/7] Setup .env ==="
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|GANTI_DENGAN_RANDOM_STRING_PANJANG_DI_PRODUCTION|$SECRET|g" "$APP_DIR/.env"
    sed -i "s|DATABASE_PATH=.*|DATABASE_PATH=$APP_DIR/data/evaluasi.db|g" "$APP_DIR/.env"
    echo "  >> .env dibuat. Edit $APP_DIR/.env untuk konfigurasi SMTP/Telegram."
else
    echo "  >> .env sudah ada, dilewati."
fi

echo "=== [5/7] Setup folder data & log ==="
mkdir -p "$APP_DIR/data"
mkdir -p /var/log/evaluasi
chown -R www-data:www-data "$APP_DIR"
chmod 750 "$APP_DIR/data"

echo "=== [6/7] Install & aktifkan systemd service ==="
cp "$APP_DIR/evaluasi.service" /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl status "$SERVICE_NAME" --no-pager

echo "=== [7/7] Konfigurasi Nginx ==="
cat > /etc/nginx/sites-available/evaluasi << 'NGINXCONF'
server {
    listen 80;
    server_name _;          # Ganti dengan domain/IP server

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
nginx -t && systemctl reload nginx

echo ""
echo "======================================================"
echo " DEPLOY SELESAI"
echo "======================================================"
echo " App berjalan di : http://$(hostname -I | awk '{print $1}')"
echo " Log service     : journalctl -u evaluasi -f"
echo " Config .env     : $APP_DIR/.env"
echo " Restart app     : systemctl restart evaluasi"
echo "======================================================"
echo " PENTING: Edit .env untuk SMTP dan Telegram token!"
echo "======================================================"
