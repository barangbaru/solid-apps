from app import app, get_db

with app.app_context():
    db = get_db()
    
    print("Memulai rollback database untuk memisahkan Project Core dari Support Core...")

    # 1. Update app_slug back to 'project' for any menu with url starting with '/project'
    db.execute("UPDATE app_menus SET app_slug = 'project' WHERE url LIKE '/project%'")
    print("Berhasil mengembalikan app_slug='project' untuk URL spesifik Project Core.")
    
    # 2. Kembalikan Menu Utama
    proj_menu_utama = db.execute("SELECT parent_id FROM app_menus WHERE url LIKE '/project%' AND parent_id IS NOT NULL LIMIT 1").fetchone()
    if proj_menu_utama and proj_menu_utama['parent_id']:
        menu_utama_id = proj_menu_utama['parent_id']
        db.execute("UPDATE app_menus SET app_slug = 'project', parent_id = NULL WHERE id = ?", (menu_utama_id,))
        print("Berhasil mengembalikan 'Menu Utama' Project Core menjadi top-level menu.")
        
    # 3. Kembalikan Master Data (jika ada) dan hapus PROJECTS parent
    projects_parents = db.execute("SELECT id FROM app_menus WHERE app_slug = 'support' AND title = 'PROJECTS'").fetchall()
    
    for p in projects_parents:
        main_id = p['id']
        
        # Cari Master Data di bawah PROJECTS ini
        md_under_projects = db.execute("SELECT id FROM app_menus WHERE parent_id = ? AND title = 'Master Data'", (main_id,)).fetchall()
        for md in md_under_projects:
            db.execute("UPDATE app_menus SET app_slug = 'project', parent_id = NULL WHERE id = ?", (md['id'],))
            db.execute("UPDATE app_menus SET app_slug = 'project' WHERE parent_id = ?", (md['id'],))
            print(f"Berhasil mengembalikan 'Master Data' (ID: {md['id']}) ke Project Core.")
            
        # Cari Menu Utama di bawah PROJECTS (jika belum kena query di poin 2)
        mu_under_projects = db.execute("SELECT id FROM app_menus WHERE parent_id = ? AND title = 'Menu Utama'", (main_id,)).fetchall()
        for mu in mu_under_projects:
            db.execute("UPDATE app_menus SET app_slug = 'project', parent_id = NULL WHERE id = ?", (mu['id'],))
            db.execute("UPDATE app_menus SET app_slug = 'project' WHERE parent_id = ?", (mu['id'],))
            
        # Hapus parent PROJECTS
        db.execute("DELETE FROM app_menus WHERE id = ?", (main_id,))
        print("Berhasil menghapus parent menu 'PROJECTS' dari Support Core.")

    # 4. Kembalikan status aplikasi di Portal
    db.execute("UPDATE superapp_apps SET is_coming_soon = 0 WHERE slug = 'project'")
    print("Berhasil mengembalikan status Project Core di portal (menghapus label Segera Hadir).")
    
    db.commit()
    print("Rollback database selesai!")
