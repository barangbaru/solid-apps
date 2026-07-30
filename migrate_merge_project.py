import os
from app import app, get_db

with app.app_context():
    db = get_db()
    
    # 1. Rename support app to "Support & Project Core"
    db.execute("UPDATE superapp_apps SET name = 'Support & Project Core', icon = 'headset', color = '#198754' WHERE slug = 'support'")
    
    # 2. Mark project app as coming soon
    db.execute("UPDATE superapp_apps SET is_coming_soon = 1 WHERE slug = 'project'")
    
    # 3. Get support main menu items to determine sort_order
    support_menus = db.execute("SELECT id, sort_order FROM app_menus WHERE app_slug = 'support' AND parent_id IS NULL ORDER BY sort_order DESC").fetchall()
    max_sort_order = support_menus[0]['sort_order'] if support_menus else 0
    
    # Create a new parent menu in 'support' called 'PROJECTS'
    db.execute("""
        INSERT INTO app_menus(app_slug, parent_id, title, url, icon, sort_order, is_active)
        VALUES('support', NULL, 'PROJECTS', '#', 'kanban', ?, 1)
    """, (max_sort_order + 1,))
    db.commit()
    
    # Get the ID of the new 'PROJECTS' menu
    project_parent = db.execute("SELECT id FROM app_menus WHERE app_slug = 'support' AND title = 'PROJECTS'").fetchone()
    project_parent_id = project_parent['id']
    
    # Update all top-level menus of 'project' to be children of 'PROJECTS' under 'support'
    db.execute("""
        UPDATE app_menus 
        SET app_slug = 'support', parent_id = ?
        WHERE app_slug = 'project' AND parent_id IS NULL
    """, (project_parent_id,))
    
    # Update all child menus of 'project' to 'support' (they keep their existing parent_id)
    db.execute("""
        UPDATE app_menus 
        SET app_slug = 'support'
        WHERE app_slug = 'project'
    """)
    
    # 4. Migrate role_menus and user_app_access
    # For every user that has 'project' access, ensure they have 'support' access
    users_with_project = db.execute("SELECT user_id, app_role FROM user_app_access WHERE app_slug = 'project' AND is_active = 1").fetchall()
    for u in users_with_project:
        user_id = u['user_id']
        app_role = u['app_role']
        
        # Check if they already have support access
        has_support = db.execute("SELECT id FROM user_app_access WHERE user_id = ? AND app_slug = 'support'", (user_id,)).fetchone()
        if not has_support:
            db.execute("INSERT INTO user_app_access(user_id, app_slug, app_role, is_active) VALUES(?, 'support', ?, 1)", (user_id, app_role))
            
    # Move all role_menus from project to support (for custom roles)
    project_role_menus = db.execute("""
        SELECT rm.id, rm.role_name, rm.menu_id 
        FROM role_menus rm
        JOIN app_menus am ON rm.menu_id = am.id
        WHERE am.app_slug = 'support' AND am.parent_id = ? 
    """, (project_parent_id,)).fetchall()
    # Wait, the menus are already updated to app_slug = 'support', so role_menus just points to menu_id.
    # What about roles defined with app_slug='project'?
    db.execute("UPDATE roles SET app_slug = 'support' WHERE app_slug = 'project'")
    
    # Add role_menus for the new parent 'PROJECTS' for roles that had access to any of its children
    # We find all roles that have access to children of 'PROJECTS'
    roles_with_project_menus = db.execute("""
        SELECT DISTINCT role_name FROM role_menus rm
        JOIN app_menus am ON rm.menu_id = am.id
        WHERE am.parent_id = ?
    """, (project_parent_id,)).fetchall()
    for r in roles_with_project_menus:
        role_name = r['role_name']
        db.execute("INSERT INTO role_menus(role_name, menu_id) VALUES(?, ?)", (role_name, project_parent_id))
    
    db.commit()
    print("Migrasi sukses: ProjectCore berhasil digabung ke SupportCore!")
