from app import app, get_db

with app.app_context():
    db = get_db()
    print("Merapikan menu...")
    
    # 1. Cari semua parent PROJECTS
    projects_parents = db.execute("SELECT id FROM app_menus WHERE app_slug = 'support' AND title = 'PROJECTS' ORDER BY id ASC").fetchall()
    
    if projects_parents:
        main_id = projects_parents[0]['id']
        
        for p in projects_parents[1:]:
            old_id = p['id']
            # Pindahkan semua child ke main_id
            db.execute("UPDATE app_menus SET parent_id = ? WHERE parent_id = ?", (main_id, old_id))
            
            # Pindahkan role_menus
            roles = db.execute("SELECT role_name FROM role_menus WHERE menu_id = ?", (old_id,)).fetchall()
            for r in roles:
                db.execute("INSERT OR IGNORE INTO role_menus(role_name, menu_id) VALUES(?, ?)", (r['role_name'], main_id))
                
            # Hapus parent duplikat
            db.execute("DELETE FROM app_menus WHERE id = ?", (old_id,))
            print(f"Menghapus duplikat menu PROJECTS dengan ID: {old_id}")
            
        # 2. Perbaiki parent_id dari Menu Utama ex-project
        proj_child = db.execute("SELECT parent_id FROM app_menus WHERE app_slug = 'support' AND url LIKE '/project/%' AND parent_id IS NOT NULL LIMIT 1").fetchone()
        
        if proj_child and proj_child['parent_id']:
            menu_utama_id = proj_child['parent_id']
            db.execute("UPDATE app_menus SET parent_id = ? WHERE id = ?", (main_id, menu_utama_id))
            print(f"Memindahkan 'Menu Utama' (ID: {menu_utama_id}) ex-project ke bawah 'PROJECTS'")
                
        # 3. Perbaiki parent_id dari 'Master Data' ex-project
        support_master_data_child = db.execute("SELECT parent_id FROM app_menus WHERE app_slug = 'support' AND url = '/support/apps' AND parent_id IS NOT NULL").fetchone()
        real_support_master_data_id = support_master_data_child['parent_id'] if support_master_data_child else -1
            
        all_master_datas = db.execute("SELECT id, parent_id FROM app_menus WHERE app_slug = 'support' AND title = 'Master Data'").fetchall()
        for md in all_master_datas:
            if md['id'] != real_support_master_data_id and md['parent_id'] is None:
                db.execute("UPDATE app_menus SET parent_id = ? WHERE id = ?", (main_id, md['id']))
                print(f"Memindahkan 'Master Data' (ID: {md['id']}) ex-project ke bawah 'PROJECTS'")
                    
        db.commit()
        print("Selesai memperbaiki hierarki menu!")
    else:
        print("Menu PROJECTS tidak ditemukan. Tidak ada yang perlu diperbaiki.")
