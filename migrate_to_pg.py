#!/usr/bin/env python3
"""
migrate_to_pg.py — Migrasi data SQLite → PostgreSQL untuk Hive

Jalankan SEBELUM switch DB_TYPE=postgresql di .env:
  python3 migrate_to_pg.py [--sqlite path/to/evaluasi.db] [--dry-run]

Argumen opsional:
  --sqlite PATH   Path ke file SQLite (default: cari otomatis dari .env / direktori script)
  --pg-host HOST  Override PG_HOST
  --pg-port PORT  Override PG_PORT
  --pg-name NAME  Override PG_NAME
  --pg-user USER  Override PG_USER
  --pg-pass PASS  Override PG_PASS
  --dry-run       Tidak tulis apa pun ke PostgreSQL (cuma laporan)
  --truncate      Truncate tabel PG sebelum insert (hati-hati!)
  --skip-errors   Lanjut meski ada error per-baris (catat ke migrate_errors.log)

Contoh:
  python3 migrate_to_pg.py
  python3 migrate_to_pg.py --dry-run
  python3 migrate_to_pg.py --sqlite /var/lib/evaluasi/evaluasi.db --truncate
"""

import sys, os, re, sqlite3, argparse, time, traceback
from datetime import datetime

# ─── Warna terminal ──────────────────────────────────────────────────────────
def _c(code, text): return f'\033[{code}m{text}\033[0m'
ok   = lambda t: print(_c('32', f'  ✓  {t}'))
info = lambda t: print(_c('36', f'  →  {t}'))
warn = lambda t: print(_c('33', f'  ⚠  {t}'))
err  = lambda t: print(_c('31', f'  ✗  {t}'))
hdr  = lambda t: print(_c('1',  f'\n=== {t} ==='))


# ─── Urutan tabel (topological — parent sebelum child) ───────────────────────
# Tabel tanpa FK atau yang direferens duluan, lalu tabel dengan FK ke atas
TABLE_ORDER = [
    # Core / no FK
    'app_settings',
    'divisions',
    'roles',
    'role_permissions',
    'users',
    'employees',
    'superapp_apps',
    # Evaluasi
    'skill_categories',
    'skill_items',
    'ability_items',
    'competency_items',
    'evaluations',
    'project_entries',
    'skill_scores',
    'ability_scores',
    'competency_scores',
    'peer_reviews',
    'eval_tokens',
    'eval_reviews',
    'employee_salary',
    # Access & notifications
    'user_app_access',
    'reminder_logs',
    'password_reset_tokens',
    # Audit
    'audit_activity',
    'audit_errors',
    'audit_notifications',
    # SupportCore
    'sc_apps',
    'sc_modules',
    'sc_customers',
    'sc_services',
    'sc_support_types',
    'sc_sla_categories',
    'sc_contracts',
    'sc_contract_services',
    'sc_contract_support_types',
    'sc_tickets',
    'sc_ticket_history',
    'sc_ticket_assignees',
    'sc_presales_requests',
    'sc_presales_assignees',
    'sc_presales_history',
    # BookingCore
    'bk_resources',
    'bk_resource_images',
    'bk_items',
    'bk_bookings',
    'bk_booking_items',
    # AssetCore
    'ac_assets',
    'ac_asset_software',
    'ac_asset_history',
    'ac_infrastructure',
    'ac_licenses',
    'ac_license_assignments',
    'ac_subscriptions',
    'ac_software_requests',
    'ac_maintenance',
    'ac_maintenance_log',
    # AttendanceCore
    'attendance',
    'attendance_leaves',
    'attendance_overtime',
    'attendance_corrections',
]


def load_dotenv(path='.env'):
    """Baca variabel dari .env tanpa dependency tambahan."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k not in os.environ:
                os.environ[k] = v


def find_sqlite(args_path=None):
    """Cari file SQLite dari argumen, .env, atau lokasi default."""
    if args_path and os.path.exists(args_path):
        return args_path
    # Dari .env
    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(script_dir, '.env'))
    env_path = os.environ.get('DATABASE_PATH', '')
    if env_path and os.path.exists(env_path):
        return env_path
    # Default lokasi deployment
    candidates = [
        '/var/lib/evaluasi/evaluasi.db',
        os.path.join(script_dir, 'evaluasi.db'),
        os.path.join(script_dir, '../evaluasi.db'),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def pg_connect(args):
    """Koneksi ke PostgreSQL dari args + env vars."""
    try:
        import psycopg2
    except ImportError:
        err("psycopg2 tidak terinstall. Jalankan: pip install psycopg2-binary")
        sys.exit(1)

    host = args.pg_host or os.environ.get('PG_HOST', 'localhost')
    port = int(args.pg_port or os.environ.get('PG_PORT', 5432))
    name = args.pg_name or os.environ.get('PG_NAME', 'hive_db')
    user = args.pg_user or os.environ.get('PG_USER', 'hive')
    pw   = args.pg_pass or os.environ.get('PG_PASS', '')

    info(f"Koneksi PostgreSQL: {user}@{host}:{port}/{name}")
    conn = psycopg2.connect(host=host, port=port, dbname=name, user=user, password=pw)
    conn.autocommit = False
    return conn


def adapt_schema(schema_sql):
    """Konversi DDL SQLite → PostgreSQL."""
    s = schema_sql
    s = re.sub(r'\bINTEGER PRIMARY KEY AUTOINCREMENT\b', 'SERIAL PRIMARY KEY', s, flags=re.IGNORECASE)
    s = re.sub(r"datetime\('now',\s*'localtime'\)", 'NOW()', s, flags=re.IGNORECASE)
    return s


def get_sqlite_tables(sqlite_conn):
    """Ambil daftar tabel yang ada di SQLite."""
    cur = sqlite_conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return {r[0] for r in cur.fetchall()}


def get_sqlite_columns(sqlite_conn, table):
    """Ambil nama kolom dari tabel SQLite."""
    cur = sqlite_conn.execute(f'PRAGMA table_info({table})')
    return [r[1] for r in cur.fetchall()]


def get_pg_columns(pg_cur, table):
    """Ambil nama kolom dari tabel PostgreSQL via information_schema."""
    pg_cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position",
        (table,))
    return [r[0] for r in pg_cur.fetchall()]


def pg_table_exists(pg_cur, table):
    pg_cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name=%s", (table,))
    return pg_cur.fetchone() is not None


def pg_seq_name(table):
    """Nama sequence SERIAL default PostgreSQL untuk tabel."""
    return f'{table}_id_seq'


def reset_sequence(pg_cur, table):
    """Reset sequence id ke max(id) setelah insert manual."""
    pg_cur.execute(f"SELECT MAX(id) FROM {table}")
    row = pg_cur.fetchone()
    max_id = row[0] if row and row[0] else 0
    seq = pg_seq_name(table)
    pg_cur.execute(f"SELECT setval('{seq}', %s)", (max(max_id, 1),))


def migrate_table(sqlite_conn, pg_conn, table, dry_run=False, truncate=False, skip_errors=False, error_log=None):
    """Pindahkan satu tabel dari SQLite ke PostgreSQL."""
    pg_cur = pg_conn.cursor()

    # Ambil data dari SQLite
    try:
        sqlite_conn.row_factory = sqlite3.Row
        rows = sqlite_conn.execute(f'SELECT * FROM {table}').fetchall()
    except Exception as e:
        warn(f"  {table}: gagal baca SQLite — {e}")
        return 0, 0

    total = len(rows)
    if total == 0:
        info(f"  {table}: kosong (0 baris) — dilewati")
        return 0, 0

    # Ambil kolom dari SQLite
    sq_cols = get_sqlite_columns(sqlite_conn, table)

    # Cek apakah tabel ada di PG
    if not pg_table_exists(pg_cur, table):
        warn(f"  {table}: tidak ditemukan di PostgreSQL — dilewati (jalankan init_db dulu?)")
        return 0, total

    # Kolom yang ada di PG
    pg_cols = get_pg_columns(pg_cur, table)
    # Hanya migrasi kolom yang ada di KEDUA sisi
    common_cols = [c for c in sq_cols if c in pg_cols]
    if not common_cols:
        warn(f"  {table}: tidak ada kolom yang cocok")
        return 0, total

    if dry_run:
        ok(f"  {table}: {total} baris siap dimigrasikan (dry-run)")
        return total, total

    if truncate:
        pg_cur.execute(f'TRUNCATE TABLE {table} CASCADE')
        info(f"  {table}: truncated")

    # Siapkan INSERT dengan OVERRIDING SYSTEM VALUE untuk preserve id
    has_id = 'id' in common_cols
    col_list = ', '.join(common_cols)
    placeholders = ', '.join(['%s'] * len(common_cols))

    if has_id:
        insert_sql = (f'INSERT INTO {table} ({col_list}) '
                      f'OVERRIDING SYSTEM VALUE VALUES ({placeholders}) '
                      f'ON CONFLICT DO NOTHING')
    else:
        insert_sql = (f'INSERT INTO {table} ({col_list}) '
                      f'VALUES ({placeholders}) ON CONFLICT DO NOTHING')

    inserted = 0
    skipped  = 0
    errors   = 0

    for row in rows:
        values = []
        for col in common_cols:
            val = row[col]
            # Konversi tipe: SQLite INTEGER 0/1 untuk kolom boolean tetap integer di PG
            # String None → NULL sudah dihandle psycopg2
            values.append(val)

        try:
            pg_cur.execute(insert_sql, values)
            if pg_cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            msg = f"[{table}] row id={row['id'] if 'id' in sq_cols else '?'}: {e}"
            if error_log:
                error_log.write(msg + '\n')
            pg_conn.rollback()
            if not skip_errors:
                err(f"  ERROR: {msg}")
                raise
            pg_cur = pg_conn.cursor()  # cursor baru setelah rollback

    pg_conn.commit()

    # Reset sequence id
    if has_id:
        try:
            reset_sequence(pg_cur, table)
            pg_conn.commit()
        except Exception:
            pass  # tabel tanpa sequence (mis. app_settings)

    status = f"{inserted} inserted"
    if skipped:  status += f", {skipped} skipped (conflict)"
    if errors:   status += f", {errors} ERRORS"
    ok(f"  {table}: {status}")
    return inserted, total


def print_summary(results):
    hdr("RINGKASAN MIGRASI")
    total_rows  = sum(r[1] for r in results.values())
    total_ok    = sum(r[0] for r in results.values())
    failed_tbl  = [t for t, (ins, tot) in results.items() if ins < tot and tot > 0]

    print(f"\n  Total baris dimigrasi : {total_ok:,} / {total_rows:,}")
    if failed_tbl:
        warn(f"  Tabel dengan masalah  : {', '.join(failed_tbl)}")
    else:
        ok(f"  Semua tabel berhasil!")
    print()


def main():
    parser = argparse.ArgumentParser(description='Migrasi data SQLite → PostgreSQL untuk Hive')
    parser.add_argument('--sqlite',    help='Path ke file SQLite')
    parser.add_argument('--pg-host',   help='PostgreSQL host')
    parser.add_argument('--pg-port',   help='PostgreSQL port')
    parser.add_argument('--pg-name',   help='PostgreSQL database name')
    parser.add_argument('--pg-user',   help='PostgreSQL user')
    parser.add_argument('--pg-pass',   help='PostgreSQL password')
    parser.add_argument('--dry-run',   action='store_true', help='Tidak tulis ke PG')
    parser.add_argument('--truncate',  action='store_true', help='Truncate tabel PG sebelum insert')
    parser.add_argument('--skip-errors', action='store_true', help='Lanjut meski ada error per-baris')
    parser.add_argument('--tables',    help='Hanya migrasi tabel tertentu (koma-pisah)')
    args = parser.parse_args()

    # Load .env
    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(script_dir, '.env'))

    print()
    print('╔══════════════════════════════════════════════╗')
    print('║    Hive — Migrasi Data SQLite → PostgreSQL   ║')
    if args.dry_run:
        print('║               [ DRY-RUN MODE ]               ║')
    print('╚══════════════════════════════════════════════╝')
    print()

    # ── 1. Buka SQLite ─────────────────────────────────────────────────────────
    sqlite_path = find_sqlite(args.sqlite)
    if not sqlite_path:
        err("File SQLite tidak ditemukan. Gunakan --sqlite /path/ke/evaluasi.db")
        sys.exit(1)
    info(f"SQLite source : {sqlite_path}")
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    sq_tables = get_sqlite_tables(sqlite_conn)
    info(f"Tabel di SQLite: {len(sq_tables)}")

    # ── 2. Koneksi PostgreSQL ──────────────────────────────────────────────────
    if args.dry_run:
        pg_conn = None
        warn("Dry-run mode — tidak ada koneksi ke PostgreSQL")
    else:
        pg_conn = pg_connect(args)
        ok("Koneksi PostgreSQL berhasil")

    # ── 3. Buat schema di PG (jika belum ada) ─────────────────────────────────
    if not args.dry_run:
        hdr("[1/3] Setup schema PostgreSQL")
        try:
            # Import SCHEMA dari app.py di direktori yang sama
            sys.path.insert(0, script_dir)
            from app import SCHEMA as APP_SCHEMA, MIGRATIONS as APP_MIGRATIONS
            pg_schema = adapt_schema(APP_SCHEMA)
            pg_cur = pg_conn.cursor()
            # Eksekusi per-statement (psycopg2 tidak support executescript)
            for stmt in pg_schema.split(';'):
                stmt = stmt.strip()
                if stmt:
                    try:
                        pg_cur.execute(stmt)
                    except Exception as e:
                        # Abaikan "already exists" — tabel/constraint sudah ada
                        if 'already exists' not in str(e).lower() and 'duplicate' not in str(e).lower():
                            warn(f"Schema stmt: {e}")
                        pg_conn.rollback()
                        pg_cur = pg_conn.cursor()
            pg_conn.commit()

            # Jalankan MIGRATIONS (ALTER TABLE ADD COLUMN)
            for table, col, col_def in APP_MIGRATIONS:
                col_def_pg = re.sub(r"datetime\('now',\s*'localtime'\)", 'NOW()', col_def, flags=re.IGNORECASE)
                pg_cur.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
                    (table, col))
                if not pg_cur.fetchone():
                    try:
                        pg_cur.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_def_pg}')
                        pg_conn.commit()
                        info(f"  ALTER TABLE {table} ADD COLUMN {col}")
                    except Exception as e:
                        pg_conn.rollback()
                        warn(f"  ALTER TABLE {table} ADD COLUMN {col}: {e}")
                        pg_cur = pg_conn.cursor()

            ok("Schema PostgreSQL siap")
        except ImportError:
            warn("Tidak bisa import SCHEMA dari app.py — asumsikan schema sudah ada di PG")

    # ── 4. Migrasi data ────────────────────────────────────────────────────────
    hdr("[2/3] Migrasi data")

    # Tentukan tabel yang akan dimigrasikan
    if args.tables:
        target_tables = [t.strip() for t in args.tables.split(',')]
    else:
        # Gabungkan TABLE_ORDER + tabel di SQLite yang tidak ada di daftar
        extra = [t for t in sorted(sq_tables) if t not in TABLE_ORDER and not t.startswith('sqlite_')]
        target_tables = TABLE_ORDER + extra

    # Hanya yang ada di SQLite
    target_tables = [t for t in target_tables if t in sq_tables]

    if args.truncate and not args.dry_run:
        print()
        warn("PERINGATAN: --truncate akan menghapus semua data tabel yang ada di PostgreSQL!")
        confirm = input("  Ketik 'ya' untuk lanjut: ").strip().lower()
        if confirm != 'ya':
            err("Dibatalkan.")
            sys.exit(1)

    error_log_file = None
    if args.skip_errors:
        error_log_path = os.path.join(script_dir, 'migrate_errors.log')
        error_log_file = open(error_log_path, 'w')
        warn(f"Mode skip-errors aktif — error dicatat ke: {error_log_path}")

    results = {}
    start_time = time.time()

    for table in target_tables:
        if pg_conn is None:
            # dry-run: ambil count saja
            try:
                count = sqlite_conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                ok(f"  {table}: {count} baris (dry-run)")
                results[table] = (count, count)
            except Exception as e:
                warn(f"  {table}: {e}")
                results[table] = (0, 0)
        else:
            try:
                ins, tot = migrate_table(
                    sqlite_conn, pg_conn, table,
                    dry_run=args.dry_run,
                    truncate=args.truncate,
                    skip_errors=args.skip_errors,
                    error_log=error_log_file)
                results[table] = (ins, tot)
            except Exception as e:
                err(f"  {table}: GAGAL — {e}")
                if not args.skip_errors:
                    traceback.print_exc()
                    break
                results[table] = (0, -1)

    if error_log_file:
        error_log_file.close()

    elapsed = time.time() - start_time

    # ── 5. Verifikasi ─────────────────────────────────────────────────────────
    if pg_conn and not args.dry_run:
        hdr("[3/3] Verifikasi row count")
        pg_cur = pg_conn.cursor()
        mismatch = []
        for table in target_tables:
            try:
                sq_count = sqlite_conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                pg_cur.execute(f'SELECT COUNT(*) FROM {table}')
                pg_count = pg_cur.fetchone()[0]
                if sq_count != pg_count:
                    warn(f"  {table}: SQLite={sq_count}  PG={pg_count}  (SELISIH {sq_count-pg_count})")
                    mismatch.append(table)
                else:
                    ok(f"  {table}: {pg_count} baris ✓")
            except Exception as e:
                warn(f"  {table}: tidak bisa verifikasi — {e}")

        if mismatch:
            warn(f"\nTabel dengan selisih: {', '.join(mismatch)}")
            warn("Kemungkinan: conflict unique constraint (data duplicate di SQLite) — normal jika skip-errors aktif")

    print_summary(results)
    print(f"  Selesai dalam {elapsed:.1f} detik\n")

    if not args.dry_run and pg_conn:
        print("  LANGKAH SELANJUTNYA:")
        print("  1. Edit .env → ubah DB_TYPE=postgresql")
        print("  2. Pastikan PG_HOST/PORT/NAME/USER/PASS sudah benar di .env")
        print("  3. Restart service: sudo systemctl restart evaluasi")
        print("  4. Cek log: journalctl -u evaluasi -f")
        print()
        print("  Jika ada masalah, rollback dengan ubah kembali DB_TYPE=sqlite")
        print()

    if pg_conn:
        pg_conn.close()
    sqlite_conn.close()


if __name__ == '__main__':
    main()
