#!/usr/bin/env python3
"""
Reseed salary dev DB — hapus data terenkripsi lama, isi ulang dengan key yang benar.
Key ini harus ditambahkan ke /var/www/evaluasi/.env di server:
  FIELD_ENCRYPT_KEY=VGcKCP3l81uH7QlueXEZCu79wwlSRbMf7dlZv7LXs2w=

Usage:
    python reseed_salary.py
"""
import os, sys, random

DEV_KEY = 'VGcKCP3l81uH7QlueXEZCu79wwlSRbMf7dlZv7LXs2w='
os.environ['FIELD_ENCRYPT_KEY'] = DEV_KEY
os.environ['DB_TYPE']  = 'postgresql'
os.environ['PG_HOST']  = '10.150.10.41'
os.environ['PG_PORT']  = '5432'
os.environ['PG_NAME']  = 'hive_db'
os.environ['PG_USER']  = 'hive'
os.environ['PG_PASS']  = '6RSxtzGk0ROSqb5glnzD'
os.environ.setdefault('SECRET_KEY', 'dev-seed-secret')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app, get_db, _fenc

# Performance profile (level 1-5)
PERF = {
    'Budi Santoso': 4, 'Rina Wulandari': 5,
    'Dewi Rahayu': 5, 'Andi Wijaya': 4, 'Irwan Fauzi': 4,
    'Nadia Rahma': 3, 'Reza Pratama': 1,
    'Fajar Nugroho': 5, 'Maya Sari': 4, 'Yuli Astuti': 3, 'Dimas Kurniawan': 2,
    'Hendra Kusuma': 5, 'Dian Permata': 3, 'Bagas Setiawan': 2,
    'Siti Nurhaliza': 5, 'Putri Handayani': 3,
}
INCREASE_MAP = {5: (9, 12), 4: (6, 9), 3: (3, 5), 2: (1, 3), 1: (0, 1)}

# Salary range per jabatan/grade (low, high)
SAL_RANGE = {
    'Manager':            (9_000_000,  14_000_000),
    'Project Manager':    (8_000_000,  12_000_000),
    'Senior Programmer':  (8_000_000,  12_000_000),
    'Junior Programmer':  (4_500_000,   7_000_000),
    'Programmer':         (5_500_000,   9_000_000),
    'Senior Implementor': (7_000_000,  10_000_000),
    'Implementor':        (5_000_000,   8_000_000),
    'Senior Helpdesk':    (6_000_000,   9_000_000),
    'Helpdesk':           (4_000_000,   6_500_000),
    'QA Lead':            (7_500_000,  11_000_000),
    'QA Tester':          (4_500_000,   7_000_000),
}
# Tunjangan sebagai rasio dari gaji pokok
TJ_RANGE = {
    'al_001': (0.15, 0.25),  # Tj. Jabatan
    'al_002': (0.05, 0.10),  # Tj. Komunikasi
    'al_003': (0.05, 0.15),  # Tj. Performance
    'al_004': (0.03, 0.08),  # Tj. Kehadiran
}

rng = random.Random(42)

def rp(lo, hi, step=500_000):
    return rng.randint(int(lo / step), int(hi / step)) * step

def rp_exact(lo, hi):
    return rng.randint(int(lo), int(hi))

with app.app_context():
    db = get_db()

    # Fetch semua karyawan aktif + non-aktif dengan jabatan
    emps = db.execute('''SELECT id, name, jabatan FROM employees ORDER BY id''').fetchall()
    print(f'Ditemukan {len(emps)} karyawan.')

    # Hapus salary lama
    db.execute('DELETE FROM employee_salary')
    db.commit()
    print('Data salary lama dihapus.')

    seeded = 0
    for emp in emps:
        name  = emp['name']
        perf  = PERF.get(name, 3)  # default AVERAGE
        jabatan = emp['jabatan'] or ''

        # Cari range gaji berdasarkan jabatan (keyword match)
        lo, hi = 4_000_000, 7_000_000
        for key, rng_val in SAL_RANGE.items():
            if key.lower() in jabatan.lower():
                lo, hi = rng_val
                break

        base_2024 = rp(lo, hi)
        tjs_2024  = {k: rp_exact(base_2024 * r[0], base_2024 * r[1]) for k, r in TJ_RANGE.items()}

        inc_2025  = round(rng.uniform(*INCREASE_MAP[perf]), 1)
        base_2025 = int(base_2024 * (1 + inc_2025 / 100) / 500_000) * 500_000
        tjs_2025  = {k: rp_exact(base_2025 * r[0], base_2025 * r[1]) for k, r in TJ_RANGE.items()}

        inc_2026  = round(rng.uniform(*INCREASE_MAP[perf]), 1)
        base_2026 = int(base_2025 * (1 + inc_2026 / 100) / 500_000) * 500_000
        tjs_2026  = {k: rp_exact(base_2026 * r[0], base_2026 * r[1]) for k, r in TJ_RANGE.items()}

        for year, base, tjs, inc_pct, inc_date in [
            (2024, base_2024, tjs_2024, 0.0,     '2024-01-01'),
            (2025, base_2025, tjs_2025, inc_2025, '2025-01-01'),
            (2026, base_2026, tjs_2026, inc_2026, '2026-01-01'),
        ]:
            gross = base + sum(tjs.values())
            note  = (f'Kenaikan {inc_pct}% dari evaluasi Q4-{year-1}'
                     if inc_pct > 0 else 'Tidak ada kenaikan')
            db.execute(
                '''INSERT INTO employee_salary
                   (employee_id, year, base_salary, al_001, al_002, al_003, al_004,
                    increase_pct, increase_date, notes)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(employee_id, year) DO UPDATE SET
                     base_salary=EXCLUDED.base_salary,
                     al_001=EXCLUDED.al_001, al_002=EXCLUDED.al_002,
                     al_003=EXCLUDED.al_003, al_004=EXCLUDED.al_004,
                     increase_pct=EXCLUDED.increase_pct,
                     increase_date=EXCLUDED.increase_date,
                     notes=EXCLUDED.notes''',
                (emp['id'], year,
                 _fenc(base),
                 _fenc(tjs['al_001']), _fenc(tjs['al_002']),
                 _fenc(tjs['al_003']), _fenc(tjs['al_004']),
                 inc_pct, inc_date, note)
            )
            perf_label = {5:'STAR',4:'SOLID',3:'AVG',2:'BELOW',1:'LAZY'}[perf]
            print(f'  [{perf_label}] {name:<22} {year}: base={base:>12,}  gross={gross:>13,}  naik={inc_pct}%')
        seeded += 1

    db.commit()
    print(f'\nOK — {seeded} karyawan × 3 tahun = {seeded*3} baris salary tersimpan.')
    print(f'\nTambahkan ke /var/www/evaluasi/.env di server:')
    print(f'  FIELD_ENCRYPT_KEY={DEV_KEY}')
