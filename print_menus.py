from app import app, get_db

with app.app_context():
    db = get_db()
    menus = db.execute("SELECT id, parent_id, title, app_slug, url FROM app_menus WHERE app_slug IN ('support', 'project')").fetchall()
    for m in menus:
        print(f"{m['id']} | {m['parent_id']} | {m['title']} | {m['app_slug']} | {m['url']}")
