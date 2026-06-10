import sqlite3
db = sqlite3.connect(r"D:\workspace\PP\evaluasi\evaluasi.db")
db.row_factory = sqlite3.Row
print("=== USERS ===")
for r in db.execute("SELECT id,username,role,is_active FROM users ORDER BY id"):
    print(f"  {r['id']} | {r['username']} | {r['role']} | active={r['is_active']}")
print("=== EMPLOYEES ===")
for e in db.execute("SELECT id,name,employment_type,contract_end,email,telegram_id FROM employees ORDER BY id"):
    print(f"  {e['id']} | {e['name']} | {e['employment_type']} | end={e['contract_end']} | email={e['email']}")
print("=== CONTRACT DAYS ===")
from datetime import date
for e in db.execute("SELECT name,contract_end FROM employees WHERE employment_type='kontrak' AND contract_end!=''"):
    try:
        days = (date.fromisoformat(e['contract_end']) - date.today()).days
        print(f"  {e['name']} => {days} days left")
    except:
        print(f"  {e['name']} => invalid date")
db.close()
print("ALL OK")
