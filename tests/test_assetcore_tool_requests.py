def test_completed_tool_request_creates_laptop_asset(tmp_path, monkeypatch):
    import app as hive_app

    db_path = tmp_path / 'assetcore_tool_requests.db'
    monkeypatch.setattr(hive_app, 'DB_TYPE', 'sqlite')
    monkeypatch.setattr(hive_app, 'DB_PATH', str(db_path))
    hive_app.app.config.update(TESTING=True, SECRET_KEY='test-secret')

    with hive_app.app.app_context():
        hive_app.init_db()
        db = hive_app.get_db()
        cur = db.execute(
            'INSERT INTO employees(name,jabatan,divisi,is_active) VALUES(?,?,?,?)',
            ('User AssetCore Test', 'Staff', 'IT', 1),
        )
        employee_id = cur.lastrowid
        db.commit()

    client = hive_app.app.test_client()
    resp = client.post(
        '/login',
        data={'username': 'superadmin', 'password': 'Admin@123'},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    request_data = {
        'employee_id': str(employee_id),
        'request_date': '2026-07-09',
        'item_name': 'ASUS Vivobook Go 14 Ryzen 5 16GB 512GB',
        'item_category': 'Laptop',
        'admin_price': '8000000',
        'purchase_date': '2026-07-10',
        'received_date': '2026-07-11',
        'receipt_date': '2026-07-12',
        'pic_support': 'David',
        'admin_item_type': 'ASUS Vivobook Go 14',
        'admin_url': 'https://example.com/laptop',
        'asset_tag': 'TEST-001',
        'spec_cpu_type': 'Ryzen 5 7520U',
        'spec_ram': '16 GB',
        'spec_disk': '512 GB SSD',
        'spec_gpu': 'Radeon 610M',
        'serial_number': 'SNTEST001',
        'spec_screen': '14 inch FHD',
        'spec_os': 'Windows 11 Home',
        'spec_office': 'OHS',
        'ket': 'Asset IT',
        'reason': 'Kebutuhan onboarding',
        'admin_specs': 'Bundle standard Windows',
        'notes': 'Catatan test',
    }

    resp = client.post('/aset/tool-requests/new', data=request_data, follow_redirects=False)
    assert resp.status_code in (302, 303)

    with hive_app.app.app_context():
        db = hive_app.get_db()
        tool_request = db.execute(
            'SELECT * FROM ac_tool_requests ORDER BY id DESC LIMIT 1'
        ).fetchone()
        request_id = tool_request['id']
        assert tool_request['spec_cpu_type'] == 'Ryzen 5 7520U'

    complete_data = dict(request_data)
    complete_data.update({'status': 'Completed', 'create_asset': '1'})
    resp = client.post(
        f'/aset/tool-requests/{request_id}/status',
        data=complete_data,
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with hive_app.app.app_context():
        db = hive_app.get_db()
        tool_request = db.execute(
            'SELECT * FROM ac_tool_requests WHERE id=?',
            (request_id,),
        ).fetchone()
        asset = db.execute(
            'SELECT * FROM ac_assets WHERE id=?',
            (tool_request['asset_id'],),
        ).fetchone()

    assert tool_request['status'] == 'Completed'
    assert asset['employee_id'] == employee_id
    assert asset['device_type'] == 'Laptop'
    assert asset['brand'] == 'ASUS Vivobook Go 14'
    assert asset['processor'] == 'Ryzen 5 7520U'
    assert asset['ram'] == '16 GB'
    assert asset['disk'] == '512 GB SSD'
    assert asset['asset_tag'] == 'TEST-001'
    assert asset['serial_number'] == 'SNTEST001'
