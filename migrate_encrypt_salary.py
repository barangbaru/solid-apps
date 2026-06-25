#!/usr/bin/env python3
"""
One-time migration: enkripsi kolom gaji existing di employee_salary.
Jalankan sekali setelah FIELD_ENCRYPT_KEY sudah diset di environment.

Usage:
    FIELD_ENCRYPT_KEY=<key> python3 migrate_encrypt_salary.py [--db /path/to/evaluasi.db]

Generate key baru:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import sqlite3, os, sys, argparse
from cryptography.fernet import Fernet, InvalidToken

SALARY_ENC_FIELDS = ['base_salary', 'al_001', 'al_002', 'al_003', 'al_004']
DEFAULT_DB = '/var/lib/evaluasi/evaluasi.db'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--dry-run', action='store_true', help='Tampilkan tanpa simpan')
    args = parser.parse_args()

    key = os.environ.get('FIELD_ENCRYPT_KEY', '')
    if not key:
        print('ERROR: FIELD_ENCRYPT_KEY tidak diset di environment.')
        print('Generate key: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
        sys.exit(1)

    try:
        fernet = Fernet(key.encode())
    except Exception as e:
        print(f'ERROR: FIELD_ENCRYPT_KEY tidak valid: {e}')
        sys.exit(1)

    if not os.path.exists(args.db):
        print(f'ERROR: Database tidak ditemukan: {args.db}')
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur  = conn.cursor()

    rows = cur.execute('SELECT * FROM employee_salary').fetchall()
    print(f'Total baris: {len(rows)}')

    updated = 0
    skipped = 0

    for row in rows:
        updates = {}
        for field in SALARY_ENC_FIELDS:
            val = row[field]
            if val is None:
                continue
            if isinstance(val, (int, float)):
                # Plain numeric — perlu dienkripsi
                updates[field] = fernet.encrypt(str(float(val)).encode()).decode()
            elif isinstance(val, str):
                # Cek apakah sudah terenkripsi
                try:
                    fernet.decrypt(val.encode())
                    skipped += 1  # sudah terenkripsi, skip
                except InvalidToken:
                    # String tapi bukan Fernet token — coba parse sebagai float
                    try:
                        updates[field] = fernet.encrypt(str(float(val)).encode()).decode()
                    except (ValueError, TypeError):
                        pass  # abaikan nilai tidak valid

        if updates:
            set_clause = ', '.join(f'{f}=?' for f in updates)
            vals = list(updates.values()) + [row['id']]
            if not args.dry_run:
                cur.execute(f'UPDATE employee_salary SET {set_clause} WHERE id=?', vals)
            else:
                print(f'  [DRY] id={row["id"]} emp={row["employee_id"]} year={row["year"]}: {list(updates.keys())}')
            updated += 1

    if not args.dry_run:
        conn.commit()
        print(f'Selesai: {updated} baris dienkripsi, {skipped} kolom sudah terenkripsi sebelumnya.')
    else:
        print(f'[DRY RUN] {updated} baris akan dienkripsi.')

    conn.close()

if __name__ == '__main__':
    main()
