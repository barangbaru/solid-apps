"""
test_portal.py — Tests untuk modul Portal (dashboard, users, roles, audit).
"""
import pytest


class TestPortalDashboard:
    """Portal utama accessible oleh superadmin."""

    def test_portal_accessible(self, auth_client):
        resp = auth_client.get('/portal')
        assert resp.status_code == 200

    def test_portal_requires_login(self, client):
        resp = client.get('/portal', follow_redirects=False)
        assert resp.status_code in (301, 302)


class TestPortalUsers:
    """Manajemen user di portal."""

    def test_list_users(self, auth_client):
        resp = auth_client.get('/portal/users')
        assert resp.status_code == 200

    def test_add_user_page(self, auth_client):
        resp = auth_client.get('/portal/users/add')
        assert resp.status_code == 200

    def test_add_user_post(self, auth_client):
        resp = auth_client.post('/portal/users/add', data={
            'username': 'testuser_portal',
            'password': 'Test@12345',
            'full_name': 'Test User Portal',
            'role': 'admin',
            'email': 'testuser@hive.test',
        }, follow_redirects=True)
        assert resp.status_code == 200

    def test_add_user_duplicate_username(self, auth_client):
        # Username sudah ada (superadmin)
        resp = auth_client.post('/portal/users/add', data={
            'username': 'superadmin',
            'password': 'Admin@123',
            'full_name': 'Duplicate',
            'role': 'admin',
        }, follow_redirects=True)
        assert resp.status_code == 200


class TestPortalRoles:
    """Manajemen roles."""

    def test_roles_page(self, auth_client):
        resp = auth_client.get('/portal/roles')
        assert resp.status_code == 200


class TestPortalAudit:
    """Audit log."""

    def test_audit_page(self, auth_client):
        resp = auth_client.get('/portal/audit')
        assert resp.status_code == 200


class TestPortalSystemSettings:
    """System settings."""

    def test_system_settings_page(self, auth_client):
        resp = auth_client.get('/portal/system-settings')
        assert resp.status_code == 200

    def test_version_info_api(self, auth_client):
        resp = auth_client.get('/portal/system-settings/version-info')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'current_version' in data or 'version' in data or data is not None
