import sys, io, re, openpyxl
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

wb = openpyxl.load_workbook(r'D:\Documents\Downloads\Dokumentasi IT Support by MMI.xlsx')

def esc(v):
    if v is None: return ''
    return str(v).strip().replace("'", "''").replace('\n', ' ')

def clean_ram(v):
    if v is None: return ''
    s = str(v).strip()
    m = re.match(r'^(\d+)', s)
    return (m.group(1) + ' GB') if m else s

def clean_disk(v):
    if v is None: return ''
    s = str(v).strip()
    if re.match(r'^\d+$', s): return s + ' GB'
    return s

def parse_os_lic(os_str):
    s = str(os_str) if os_str else ''
    if 'MAK' in s: return 'MAK (Volume)'
    if any(x in s for x in ('DLW','DLM','DL P','DL MA')): return 'Digital License OEM'
    if 'Digital' in s: return 'Digital License'
    if 'DL' in s: return 'Digital License'
    return 'OEM'

def parse_sw(apps_str):
    if not apps_str or str(apps_str).strip() in ('-', ''): return []
    return [l.strip().lstrip('- ').strip() for l in str(apps_str).split('\n') if l.strip().lstrip('- ').strip()]

def fmt_date(d):
    if d is None: return ''
    if hasattr(d, 'strftime'): return d.strftime('%Y-%m-%d')
    return str(d)[:10]

lines = ['-- AssetCore seed data from Dokumentasi IT Support by MMI.xlsx']
lines.append("DELETE FROM ac_asset_software WHERE asset_id IN (SELECT id FROM ac_assets WHERE notes='[seeded]');")
lines.append("DELETE FROM ac_assets WHERE notes='[seeded]';")
lines.append("DELETE FROM ac_infrastructure WHERE condition_notes='[seeded]';")
lines.append("DELETE FROM ac_licenses WHERE notes='[seeded]';")
lines.append("DELETE FROM ac_subscriptions WHERE notes='[seeded]';")
lines.append('')
lines.append('-- Laptop / PC')

ws = wb['Laptop']
for row in ws.iter_rows(min_row=2, max_row=43, values_only=True):
    r = (list(row) + [None]*12)[:12]
    no, nama, divisi, perangkat, os_val, lisensi, processor, ram, disk, office, apps, brand_col = r
    if not isinstance(no, int): continue
    if nama and str(nama).startswith('Ex-'): continue

    nama_e = esc(nama)
    brand_e = esc(brand_col) if brand_col else 'Lenovo'
    os_str = str(os_val).replace('\n', ' ') if os_val else ''
    os_lic = parse_os_lic(os_str)
    os_name = esc(re.sub(r'\s*\(.*?\)', '', os_str).strip() or os_str)
    emp_sql = f"(SELECT id FROM employees WHERE name='{nama_e}' LIMIT 1)" if nama_e else 'NULL'
    ram_e = esc(clean_ram(ram))
    disk_e = esc(clean_disk(disk))
    proc_e = esc(str(processor).replace('\n', ' + ')) if processor else ''
    office_e = esc(str(office).replace('\n', ' ')) if office else ''

    lines.append(f"INSERT INTO ac_assets(employee_id,device_type,brand,os,os_license_type,processor,ram,disk,office_version,condition,notes) VALUES({emp_sql},'Laptop','{brand_e}','{os_name}','{esc(os_lic)}','{proc_e}','{ram_e}','{disk_e}','{office_e}','Baik','[seeded]');")

    for sw in parse_sw(apps):
        lines.append(f"INSERT INTO ac_asset_software(asset_id,software_name) VALUES(last_insert_rowid(),'{esc(sw)}');")

lines.append('')
lines.append('-- Infrastruktur')

ws2 = wb['Perangkat']
last_dtype = last_brand = last_model = ''
for row in ws2.iter_rows(min_row=2, max_row=30, values_only=True):
    r = (list(row) + [None]*16)[:16]
    no, perangkat, keterangan, serial_num, nickname = r[0], r[1], r[2], r[3], r[4]
    ups_group = r[6]

    if perangkat is None and nickname is None: continue

    # Additional nicknames for multi-unit devices
    if perangkat is None and nickname:
        nick = esc(nickname)
        if nick and last_dtype:
            lines.append(f"INSERT INTO ac_infrastructure(device_type,brand,model,description,serial_number,nickname,status,condition_notes) VALUES('{last_dtype}','{last_brand}','{last_model}','','','{nick}','Aktif','[seeded]');")
        continue

    ps = esc(perangkat)
    pl = ps.lower()
    if any(k in pl for k in ['proliant','dl380','dl320','dl3']): dtype, brand = 'Server', 'HP'
    elif any(k in pl for k in ['poweredge','r630','r730']): dtype, brand = 'Server', 'Dell'
    elif 'd-link' in pl or 'dlink' in pl: dtype, brand = 'Switch', 'D-Link'
    elif 'tp-link' in pl or 'access point' in pl: dtype, brand = 'Access Point', 'TP-Link'
    elif 'mikrotik' in pl or 'routerboard' in pl: dtype, brand = 'Router', 'Mikrotik'
    elif 'asus' in pl: dtype, brand = 'Router', 'Asus'
    elif 'monitor' in pl: dtype, brand = 'Monitor', 'Philips'
    elif 'keyboard' in pl: dtype, brand = 'Peripheral', 'Logitech'
    elif 'mouse' in pl: dtype, brand = 'Peripheral', 'Logitech'
    elif any(k in pl for k in ['tester','crimping','tang']): dtype, brand = 'Tools', ''
    else: dtype, brand = 'Lainnya', ''

    sn = esc(serial_num) if serial_num else ''
    nick = esc(nickname) if nickname else ''
    ups = esc(ups_group) if ups_group else ''
    desc = esc(keterangan) if keterangan else ''

    lines.append(f"INSERT INTO ac_infrastructure(device_type,brand,model,description,serial_number,nickname,ups_group,status,condition_notes) VALUES('{dtype}','{brand}','{ps}','{desc}','{sn}','{nick}','{ups}','Aktif','[seeded]');")
    last_dtype, last_brand, last_model = dtype, brand, ps

lines.append('')
lines.append('-- Lisensi Software')

ws3 = wb['Lisensi Software']
current_sw = None
for row in ws3.iter_rows(min_row=2, values_only=True):
    r = (list(row) + [None]*16)[:16]
    no_m, software, no2, lic_key, ket = r[0], r[1], r[2], r[3], r[4]
    year_val = r[7]
    provider, billing, start_d, end_d, username = r[10], r[11], r[12], r[13], r[14]

    if software: current_sw = esc(software)

    if lic_key and current_sw and no2 is not None:
        key_str = str(lic_key).strip()
        if key_str and '✅' not in key_str and len(key_str) > 4:
            key_e = esc(lic_key)
            year_sql = str(int(year_val)) if year_val and str(year_val).strip().isdigit() else 'NULL'
            ltype = 'Subscription' if '365' in current_sw and '@' in key_str else 'Perpetual'
            lines.append(f"INSERT OR IGNORE INTO ac_licenses(software_name,license_key,license_type,year,is_active,notes) VALUES('{current_sw}','{key_e}','{ltype}',{year_sql},1,'[seeded]');")

    # Only capture subscription rows where start_d / end_d are actual dates
    if provider and isinstance(provider, str) and provider.strip() not in ('Provider', '', 'Subscribe'):
        if hasattr(start_d, 'strftime') or hasattr(end_d, 'strftime'):
            prov = esc(provider)
            bil = esc(billing) if billing else 'Monthly'
            start_str = fmt_date(start_d)
            end_str = fmt_date(end_d)
            user_str = esc(username) if username else ''
            cat = 'ISP' if prov in ('MyRepublic','MyRepublic IP Static','MyRepublic WIFI','Orbit') else 'SaaS'
            lines.append(f"INSERT OR IGNORE INTO ac_subscriptions(provider,category,billing_cycle,start_date,end_date,username,is_active,notes) VALUES('{prov}','{cat}','{bil}','{start_str}','{end_str}','{user_str}',1,'[seeded]');")

print('\n'.join(lines))
