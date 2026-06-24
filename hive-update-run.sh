#!/bin/bash
TRIGGER=/tmp/hive_update_trigger
LOG=/tmp/hive_update.log
DEPLOY=/var/www/evaluasi/deploy-ubuntu.sh

VERSION=""
if [ -f "$TRIGGER" ]; then
    VERSION=$(cat "$TRIGGER" | tr -d '[:space:]')
    rm -f "$TRIGGER"
fi

{
    echo ""
    echo "=== Hive Auto Update ==="
    echo "Target versi : ${VERSION:-latest}"
    echo "Waktu        : $(date '+%Y-%m-%d %H:%M:%S')"
    echo ""
} > "$LOG"

if [ -n "$VERSION" ]; then
    bash "$DEPLOY" --auto --version "$VERSION" >> "$LOG" 2>&1
else
    bash "$DEPLOY" --auto >> "$LOG" 2>&1
fi

if [ $? -eq 0 ]; then
    echo "HIVE_DEPLOY_DONE" >> "$LOG"
else
    echo "HIVE_DEPLOY_FAILED" >> "$LOG"
fi
