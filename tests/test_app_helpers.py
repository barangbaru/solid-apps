import pytest
from unittest.mock import MagicMock, patch
from werkzeug.datastructures import FileStorage
from io import BytesIO

# Import the helpers from app.py
from app import (
    _tpl_from_json,
    _is_db_integrity_error,
    get_divisi_list,
    _DBWrapper,
    _save_upload_file,
    _save_upload,
    DIVISI_LIST,
    app
)

def test_tpl_from_json():
    # Valid JSON
    assert _tpl_from_json('{"key": "value"}') == {"key": "value"}
    assert _tpl_from_json('[1, 2, 3]') == [1, 2, 3]
    
    # Empty or None
    assert _tpl_from_json('') == []
    assert _tpl_from_json(None) == []
    
    # Invalid JSON
    assert _tpl_from_json('{invalid json}') == []

def test_is_db_integrity_error():
    class DummyError(Exception):
        pass
        
    assert _is_db_integrity_error(DummyError()) == False

    # Mock psycopg2.IntegrityError dynamically if not available
    try:
        import psycopg2
        error = psycopg2.IntegrityError("Unique violation")
        assert _is_db_integrity_error(error) == True
    except ImportError:
        pass

def test_get_divisi_list():
    # Mock db wrapper
    mock_db = MagicMock()
    
    # Test valid fetch
    mock_db.execute().fetchall.return_value = [{'name': 'IT'}, {'name': 'HR'}]
    assert get_divisi_list(mock_db) == ['IT', 'HR']
    
    # Test empty fetch (fallback to default)
    mock_db.execute().fetchall.return_value = []
    assert get_divisi_list(mock_db) == DIVISI_LIST
    
    # Test exception (fallback to default)
    mock_db.execute.side_effect = Exception("DB Error")
    assert get_divisi_list(mock_db) == DIVISI_LIST

def test_dbwrapper_fix_sqlite_to_pg():
    # Test the regex replacements for Postgres
    db_wrapper = _DBWrapper(conn=None, is_pg=True)
    
    # 1. INSERT OR REPLACE -> ON CONFLICT DO UPDATE
    sql = "INSERT OR REPLACE INTO my_table (id, name, val) VALUES (?, ?, ?)"
    fixed = db_wrapper._fix(sql)
    assert "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, val=EXCLUDED.val" in fixed
    assert "INSERT INTO" in fixed
    assert "%s" in fixed  # Placeholders converted
    
    # 2. INSERT OR IGNORE -> ON CONFLICT DO NOTHING
    sql = "INSERT OR IGNORE INTO logs (id) VALUES (?)"
    fixed = db_wrapper._fix(sql)
    assert "ON CONFLICT DO NOTHING" in fixed
    
    # 3. julianday math
    sql = "SELECT julianday(end_date) - julianday('now') FROM t"
    fixed = db_wrapper._fix(sql)
    assert "NULLIF(end_date,'')::date - CURRENT_DATE" in fixed
    
    sql = "SELECT julianday('now') - julianday(start_date) FROM t"
    fixed = db_wrapper._fix(sql)
    assert "CURRENT_DATE - NULLIF(start_date,'')::date" in fixed
    
    # 4. GROUP_CONCAT
    sql = "SELECT GROUP_CONCAT(name, ', ') FROM t"
    fixed = db_wrapper._fix(sql)
    assert "STRING_AGG(name::text, ', ')" in fixed
    
    sql = "SELECT GROUP_CONCAT(name) FROM t"
    fixed = db_wrapper._fix(sql)
    assert "STRING_AGG(name::text, ',')" in fixed
    
    # 5. SQLite specific funcs
    sql = "SELECT last_insert_rowid(), date('now'), datetime('now', 'localtime'), strftime('%Y-%m-%d', created_at) FROM t"
    fixed = db_wrapper._fix(sql)
    assert "lastval()" in fixed
    assert "CURRENT_DATE" in fixed
    assert "NOW()" in fixed
    assert "TO_CHAR(created_at::date, 'YYYY-MM-DD')" in fixed
    
def test_dbwrapper_no_pg():
    # Test that SQLite wrapper does not touch the SQL
    db_wrapper = _DBWrapper(conn=None, is_pg=False)
    sql = "INSERT OR IGNORE INTO logs (id) VALUES (?)"
    assert db_wrapper._fix(sql) == sql

def test_save_upload_file_dangerous_ext():
    # Setup flask app context since flash() is used in _save_upload_file
    with app.test_request_context():
        # Test missing filename
        file_obj = FileStorage(stream=BytesIO(b"dummy"), filename='')
        assert _save_upload_file(file_obj) is None
        
        # Test dangerous extension
        file_obj = FileStorage(stream=BytesIO(b"dummy"), filename='script.php')
        assert _save_upload_file(file_obj) is None
        
        # Test valid image extension but mocked save
        file_obj = FileStorage(stream=BytesIO(b"dummy"), filename='image.jpg')
        file_obj.save = MagicMock()
        
        # Mock get_settings to return local storage
        with patch('app.get_settings') as mock_get_settings:
            mock_get_settings.return_value = {'media_storage_type': 'local'}
            with patch('app.get_db'):
                # _save_upload wraps _save_upload_file with ALLOWED_IMAGE_EXT
                result = _save_upload(file_obj, subfolder='test')
                assert result.endswith('.jpg')
                assert 'test' in result
                file_obj.save.assert_called_once()
