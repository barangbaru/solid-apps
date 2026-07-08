#!/bin/bash
# backup_db.sh — Script to backup PostgreSQL DB as postgres admin

set -e

# Export standard system paths in case calling environment has restricted PATH
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

# Path to script directory
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Load env variables manually from .env if it exists
if [ -f "$DIR/.env" ]; then
    export $(grep -v '^#' "$DIR/.env" | xargs)
fi

HOST="${PG_HOST:-localhost}"
PORT="${PG_PORT:-5432}"
DBNAME="${PG_NAME:-hive_db}"
USER="${PG_USER:-hive}"
PASSWORD="${PG_PASS:-}"

OUTPUT_PATH="$1"
if [ -z "$OUTPUT_PATH" ]; then
    echo "Usage: $0 <output_path>"
    exit 1
fi

TEMP_DUMP="/tmp/hive_db_dump.sql"

# Try running pg_dump. If local and we can use sudo as postgres user, do that.
# Otherwise use standard env credentials.
if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n -u postgres true >/dev/null 2>&1; then
        echo "Running pg_dump as system postgres admin..."
        sudo -u postgres pg_dump -d "$DBNAME" -F p -f "$TEMP_DUMP"
    else
        echo "Running pg_dump using env credentials..."
        PGPASSWORD="$PASSWORD" pg_dump -h "$HOST" -p "$PORT" -U "$USER" -F p -f "$TEMP_DUMP" "$DBNAME"
    fi
else
    echo "Running remote pg_dump using env credentials..."
    PGPASSWORD="$PASSWORD" pg_dump -h "$HOST" -p "$PORT" -U "$USER" -F p -f "$TEMP_DUMP" "$DBNAME"
fi

# Move the result to output path
mv "$TEMP_DUMP" "$OUTPUT_PATH"
echo "Backup database PostgreSQL selesai: $OUTPUT_PATH"
