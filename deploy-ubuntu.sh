#!/bin/bash
# deploy-ubuntu.sh — Install & Update Hive di Ubuntu 20.04/22.04/24.04
# Jalankan sebagai root: sudo bash deploy-ubuntu.sh
#
# Idempotent — aman dijalankan berulang:
#   Install baru  : setup lengkap dari nol
#   Update/redeploy: tarik kode baru, update deps, restart — database TIDAK tersentuh

set -e

# ── Warna output ──────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}  >>  $*${NC}"; }
success() { echo -e "${GREEN}  ✓   $*${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠   $*${NC}"; }
header()  { echo -e "\n${BOLD}=== $* ===${NC}"; }

# ── Konstanta ─────────────────────────────────────────────────────────────────
APP_DIR="/var/www/evaluasi"
DATA_DIR="/var/lib/evaluasi"
SERVICE_NAME="evaluasi"
REPO_URL="https://github.com/barangbaru/solid-apps.git"
REPO_SUBDIR="."

IS_UPDATE=false
[ -f "$APP_DIR/wsgi.py" ] && IS_UPDATE=true

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║         HIVE — Deploy Script             ║${NC}"
if $IS_UPDATE; then
echo -e "${BOLD}║         MODE: UPDATE APLIKASI            ║${NC}"
else
echo -e "${BOLD}║         MODE: INSTALL BARU               ║${NC}"
fi
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""

# ════════════════════════════════════════════════════════════════════════════
# BAGIAN 0 — Pilihan Database (hanya saat install baru atau paksa re-config)
# ════════════════════════════════════════════════════════════════════════════
DB_TYPE="sqlite"
PG_HOST="localhost"
PG_PORT="5432"
PG_NAME="hive_db"
PG_USER="hive"
PG_PASS=""

# Cek apakah .env sudah ada dengan config PostgreSQL
if [ -f "$APP_DIR/.env" ] && grep -q "^DB_TYPE=postgresql" "$APP_DIR/.env" 2>/dev/null; then
    DB_TYPE="postgresql"
    PG_HOST=$(grep '^PG_HOST=' "$APP_DIR/.env" | cut -d= -f2-)
    PG_PORT=$(grep '^PG_PORT=' "$APP_DIR/.env" | cut -d= -f2-)
    PG_NAME=$(grep '^PG_NAME=' "$APP_DIR/.env" | cut -d= -f2-)
    PG_USER=$(grep '^PG_USER=' "$APP_DIR/.env" | cut -d= -f2-)
    PG_PASS=$(grep '^PG_PASS=' "$APP_DIR/.env" | cut -d= -f2-)
    warn "Config PostgreSQL ditemukan di .env — menggunakan yang sudah ada."
    warn "  Host: $PG_HOST:$PG_PORT  DB: $PG_NAME  User: $PG_USER"
else

header "Pilihan Database"
echo ""
echo -e "  ${BOLD}[1]${NC} SQLite       — Simple, cocok untuk single-server (default)"
echo -e "  ${BOLD}[2]${NC} PostgreSQL   — Lebih robust, siap untuk multi-process / scale-up"
echo ""
read -rp "  Pilih [1/2] (default: 1): " DB_CHOICE
DB_CHOICE=${DB_CHOICE:-1}

if [ "$DB_CHOICE" = "2" ]; then
    DB_TYPE="postgresql"
    echo ""
    echo -e "  ${BOLD}[A]${NC} Install & setup PostgreSQL otomatis di server ini"
    echo -e "  ${BOLD}[B]${NC} Gunakan PostgreSQL yang sudah ada (input parameter)"
    echo ""
    read -rp "  Pilih [A/B] (default: A): " PG_SETUP
    PG_SETUP=${PG_SETUP:-A}
    PG_SETUP=$(echo "$PG_SETUP" | tr '[:lower:]' '[:upper:]')

    if [ "$PG_SETUP" = "A" ]; then
        # ── Auto install PostgreSQL ──────────────────────────────────────────
        header "Install PostgreSQL"
        apt-get update -qq
        apt-get install -y postgresql postgresql-contrib
        systemctl enable postgresql
        systemctl start postgresql

        # Generate password acak untuk user hive
        PG_PASS=$(python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(20)))")
        PG_HOST="localhost"
        PG_PORT="5432"

        read -rp "  Nama database (default: hive_db): " INPUT_PG_NAME
        PG_NAME=${INPUT_PG_NAME:-hive_db}
        read -rp "  Nama user PostgreSQL (default: hive): " INPUT_PG_USER
        PG_USER=${INPUT_PG_USER:-hive}

        info "Membuat user '$PG_USER' dan database '$PG_NAME'..."
        # Jalankan sebagai postgres — skip jika sudah ada
        sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'" | grep -q 1 || \
            sudo -u postgres psql -c "CREATE USER $PG_USER WITH PASSWORD '$PG_PASS';"
        sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$PG_NAME'" | grep -q 1 || \
            sudo -u postgres psql -c "CREATE DATABASE $PG_NAME OWNER $PG_USER;"
        sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $PG_NAME TO $PG_USER;"
        # PostgreSQL 15+: perlu grant schema public
        sudo -u postgres psql -d "$PG_NAME" -c "GRANT ALL ON SCHEMA public TO $PG_USER;" 2>/dev/null || true

        success "PostgreSQL siap: $PG_USER@$PG_HOST:$PG_PORT/$PG_NAME"
        echo ""
        echo -e "  ${YELLOW}Simpan kredensial ini:${NC}"
        echo -e "    Host     : $PG_HOST"
        echo -e "    Port     : $PG_PORT"
        echo -e "    Database : $PG_NAME"
        echo -e "    User     : $PG_USER"
        echo -e "    Password : $PG_PASS"
        echo ""

    else
        # ── Input parameter PostgreSQL manual ───────────────────────────────
        header "Konfigurasi PostgreSQL"
        echo ""
        read -rp "  Host PostgreSQL (default: localhost): " INPUT_HOST
        PG_HOST=${INPUT_HOST:-localhost}
        read -rp "  Port (default: 5432): " INPUT_PORT
        PG_PORT=${INPUT_PORT:-5432}
        read -rp "  Nama Database: " PG_NAME
        read -rp "  Username: " PG_USER
        read -srp "  Password: " PG_PASS
        echo ""

        # Test koneksi
        info "Menguji koneksi ke PostgreSQL..."
        apt-get install -y postgresql-client -qq 2>/dev/null || true
        if PGPASSWORD="$PG_PASS" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_NAME" -c '\q' 2>/dev/null; then
            success "Koneksi PostgreSQL berhasil!"
        else
            warn "Koneksi gagal — pastikan parameter benar dan PostgreSQL bisa diakses."
            warn "Melanjutkan install, tapi app mungkin tidak bisa start."
        fi
    fi
fi

fi  # end if .env belum ada

# ════════════════════════════════════════════════════════════════════════════
# [1] Sistem dependencies
# ════════════════════════════════════════════════════════════════════════════
header "[1/7] Install system dependencies"
if ! $IS_UPDATE; then
    apt-get update -qq
    PKGS="python3 python3-pip python3-venv nginx git rsync"
    if [ "$DB_TYPE" = "postgresql" ]; then
        PKGS="$PKGS libpq-dev python3-dev"
    fi
    apt-get install -y $PKGS
    success "System dependencies terpasang."
else
    # Pastikan libpq-dev ada jika PostgreSQL
    if [ "$DB_TYPE" = "postgresql" ]; then
        apt-get install -y libpq-dev python3-dev -qq
    fi
    info "Mode update — sistem dependencies dilewati."
fi

# ════════════════════════════════════════════════════════════════════════════
# [2] Tarik kode terbaru
# ════════════════════════════════════════════════════════════════════════════
header "[2/7] Tarik kode terbaru dari GitHub"
TMPDIR_DEPLOY=$(mktemp -d)
git clone --depth=1 "$REPO_URL" "$TMPDIR_DEPLOY/repo" -q
rsync -a --delete \
    --exclude='.env' \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.db' \
    "$TMPDIR_DEPLOY/repo/$REPO_SUBDIR/" "$APP_DIR/"
rm -rf "$TMPDIR_DEPLOY"
success "Kode berhasil diperbarui."

# ════════════════════════════════════════════════════════════════════════════
# [3] Virtual environment & dependencies
# ════════════════════════════════════════════════════════════════════════════
header "[3/7] Update virtual environment"
cd "$APP_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q

# Tambahkan psycopg2-binary ke requirements jika PostgreSQL
if [ "$DB_TYPE" = "postgresql" ]; then
    if ! grep -q "psycopg2" requirements.txt 2>/dev/null; then
        echo "psycopg2-binary" >> requirements.txt
    fi
fi

venv/bin/pip install -r requirements.txt -q
success "Dependencies up to date."

# ════════════════════════════════════════════════════════════════════════════
# [4] File .env
# ════════════════════════════════════════════════════════════════════════════
header "[4/7] Konfigurasi .env"
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|GANTI_DENGAN_RANDOM_STRING_PANJANG_DI_PRODUCTION|$SECRET|g" "$APP_DIR/.env"

    if [ "$DB_TYPE" = "postgresql" ]; then
        # Hapus/update DATABASE_PATH untuk PostgreSQL
        sed -i "s|^DATABASE_PATH=.*|# DATABASE_PATH tidak digunakan saat DB_TYPE=postgresql|g" "$APP_DIR/.env"
        cat >> "$APP_DIR/.env" << PGENV

# ─── PostgreSQL ───────────────────────────────────────────────────────────────
DB_TYPE=postgresql
PG_HOST=$PG_HOST
PG_PORT=$PG_PORT
PG_NAME=$PG_NAME
PG_USER=$PG_USER
PG_PASS=$PG_PASS
PGENV
        success ".env baru dibuat dengan konfigurasi PostgreSQL."
    else
        sed -i "s|DATABASE_PATH=.*|DATABASE_PATH=$DATA_DIR/evaluasi.db|g" "$APP_DIR/.env"
        echo "DB_TYPE=sqlite" >> "$APP_DIR/.env"
        success ".env baru dibuat dengan SQLite."
    fi
    warn "Edit $APP_DIR/.env untuk SMTP/Telegram/konfigurasi lainnya."
else
    success ".env sudah ada — tidak diubah."

    # Jika user sebelumnya SQLite dan sekarang pilih PostgreSQL, tambahkan config PG
    if [ "$DB_TYPE" = "postgresql" ] && ! grep -q "^DB_TYPE=postgresql" "$APP_DIR/.env"; then
        warn "Menambahkan konfigurasi PostgreSQL ke .env yang sudah ada..."
        cat >> "$APP_DIR/.env" << PGENV

# ─── PostgreSQL (ditambahkan oleh deploy script) ──────────────────────────────
DB_TYPE=postgresql
PG_HOST=$PG_HOST
PG_PORT=$PG_PORT
PG_NAME=$PG_NAME
PG_USER=$PG_USER
PG_PASS=$PG_PASS
PGENV
        success "Konfigurasi PostgreSQL ditambahkan ke .env."
    fi

    # Migrasi DATABASE_PATH lama (SQLite) ke DATA_DIR jika masih ada
    if [ "$DB_TYPE" = "sqlite" ]; then
        CURRENT_DB=$(grep '^DATABASE_PATH=' "$APP_DIR/.env" | cut -d= -f2- || true)
        if [ -n "$CURRENT_DB" ] && [ "$CURRENT_DB" != "$DATA_DIR/evaluasi.db" ]; then
            mkdir -p "$DATA_DIR"
            [ -f "$CURRENT_DB" ] && mv "$CURRENT_DB" "$DATA_DIR/evaluasi.db" && \
                info "Database dipindahkan: $CURRENT_DB → $DATA_DIR/evaluasi.db"
            sed -i "s|DATABASE_PATH=.*|DATABASE_PATH=$DATA_DIR/evaluasi.db|g" "$APP_DIR/.env"
            success "DATABASE_PATH diperbarui."
        fi
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# [4b] Migrasi SQLite → PostgreSQL (otomatis, sekali saja)
# ════════════════════════════════════════════════════════════════════════════
MIGRATION_FLAG="$DATA_DIR/.pg_migration_done"

if [ "$DB_TYPE" = "postgresql" ] && [ -f "$DATA_DIR/evaluasi.db" ]; then
    if [ -f "$MIGRATION_FLAG" ]; then
        info "Migrasi SQLite→PostgreSQL sudah pernah dijalankan — dilewati."
        info "  (hapus $MIGRATION_FLAG untuk paksa migrasi ulang)"
    else
        echo ""
        header "[4b] Migrasi data SQLite → PostgreSQL"
        warn "Ditemukan database SQLite: $DATA_DIR/evaluasi.db"
        warn "Data akan dimigrasikan ke PostgreSQL secara otomatis."
        echo ""

        # Backup dulu
        cp "$DATA_DIR/evaluasi.db" "$DATA_DIR/evaluasi.db.bak"
        success "Backup disimpan: $DATA_DIR/evaluasi.db.bak"

        # Jalankan migrate_to_pg.py dengan venv
        info "Menjalankan migrasi data..."
        cd "$APP_DIR"

        # Export PG env vars agar migrate_to_pg.py bisa baca
        export PG_HOST PG_PORT PG_NAME PG_USER PG_PASS

        if "$APP_DIR/venv/bin/python3" "$APP_DIR/migrate_to_pg.py" \
            --sqlite "$DATA_DIR/evaluasi.db" \
            --truncate \
            --skip-errors; then
            touch "$MIGRATION_FLAG"
            echo "$(date '+%Y-%m-%d %H:%M:%S') Migrated from $DATA_DIR/evaluasi.db" >> "$MIGRATION_FLAG"
            success "Migrasi selesai! Flag disimpan: $MIGRATION_FLAG"
        else
            echo ""
            warn "Migrasi selesai dengan beberapa error."
            warn "Cek $APP_DIR/migrate_errors.log untuk detail."
            warn "App tetap akan dijalankan — data yang berhasil sudah di PostgreSQL."
            touch "$MIGRATION_FLAG"
            echo "$(date '+%Y-%m-%d %H:%M:%S') Migrated with errors from $DATA_DIR/evaluasi.db" >> "$MIGRATION_FLAG"
        fi
        echo ""
    fi
fi

# ════════════════════════════════════════════════════════════════════════════
# [5] Direktori data & permission
# ════════════════════════════════════════════════════════════════════════════
header "[5/7] Setup direktori & permission"
mkdir -p "$DATA_DIR"
mkdir -p /var/log/evaluasi
mkdir -p "$APP_DIR/static/uploads"
chown -R www-data:www-data "$APP_DIR"
chown -R www-data:www-data "$DATA_DIR"
chmod 750 "$DATA_DIR"
find "$APP_DIR/venv/bin" -type f -exec chmod +x {} \;
success "Direktori & permission siap."

# ════════════════════════════════════════════════════════════════════════════
# [6] Systemd service
# ════════════════════════════════════════════════════════════════════════════
header "[6/7] Install & restart service"
cp "$APP_DIR/evaluasi.service" /etc/systemd/system/${SERVICE_NAME}.service

if ! grep -q "$DATA_DIR" /etc/systemd/system/${SERVICE_NAME}.service; then
    sed -i "s|ReadWritePaths=.*|ReadWritePaths=$APP_DIR $DATA_DIR /var/log/evaluasi|" \
        /etc/systemd/system/${SERVICE_NAME}.service
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl status "$SERVICE_NAME" --no-pager -l

# ════════════════════════════════════════════════════════════════════════════
# [7] Nginx
# ════════════════════════════════════════════════════════════════════════════
header "[7/7] Konfigurasi Nginx"
if [ ! -f /etc/nginx/sites-available/evaluasi ]; then
    cat > /etc/nginx/sites-available/evaluasi << 'NGINXCONF'
server {
    listen 80;
    server_name _;

    client_max_body_size 20M;

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
    [ -f /etc/nginx/sites-enabled/default ] && rm /etc/nginx/sites-enabled/default && \
        info "Site 'default' dinonaktifkan."
    success "Config Nginx baru dibuat."
else
    info "Config Nginx sudah ada, dilewati."
fi
nginx -t && systemctl reload nginx
success "Nginx reloaded."

# ════════════════════════════════════════════════════════════════════════════
# Ringkasan
# ════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
if $IS_UPDATE; then
echo -e "${BOLD}║      ✓  UPDATE SELESAI                   ║${NC}"
else
echo -e "${BOLD}║      ✓  INSTALL SELESAI                  ║${NC}"
fi
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  URL       : ${CYAN}http://$(hostname -I | awk '{print $1}')${NC}"
if [ "$DB_TYPE" = "postgresql" ]; then
echo -e "  Database  : ${CYAN}PostgreSQL — $PG_USER@$PG_HOST:$PG_PORT/$PG_NAME${NC}"
else
echo -e "  Database  : ${CYAN}SQLite — $DATA_DIR/evaluasi.db${NC}"
fi
echo -e "  Config    : ${CYAN}$APP_DIR/.env${NC}"
echo -e "  Log       : ${CYAN}journalctl -u evaluasi -f${NC}"
echo -e "  Update    : ${CYAN}sudo bash $APP_DIR/deploy-ubuntu.sh${NC}"
echo ""
if ! $IS_UPDATE; then
    echo -e "  ${YELLOW}LOGIN AWAL:${NC}"
    echo -e "    Username : superadmin"
    echo -e "    Password : Admin@123"
    echo -e "  ${RED}!! Segera ganti password setelah login !!${NC}"
    echo ""
    echo -e "  ${YELLOW}PENTING: Edit .env untuk SMTP & Telegram!${NC}"
fi
echo ""
