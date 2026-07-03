"""
test_auth.py — Tests untuk autentikasi: login, logout, dan proteksi route.
"""
import pytest


class TestLoginPage:
    """Halaman /login dapat diakses tanpa login."""

    def test_get_login_page(self, client):
        resp = client.get('/login')
        assert resp.status_code == 200
        assert b'login' in resp.data.lower()

    def test_login_redirects_if_already_logged_in(self, auth_client):
        resp = auth_client.get('/login', follow_redirects=False)
        # Sudah login → redirect ke portal atau index
        assert resp.status_code in (301, 302)


class TestLoginPost:
    """POST /login dengan berbagai skenario."""

    def test_login_valid_credentials(self, client):
        resp = client.post('/login', data={
            'username': 'superadmin',
            'password': 'Admin@123',
        }, follow_redirects=True)
        assert resp.status_code == 200
        # Berhasil login → tidak tampil form login lagi, tampil portal/index
        assert b'Login' not in resp.data or b'Selamat datang' in resp.data or b'portal' in resp.data.lower()

    def test_login_wrong_password(self, client):
        resp = client.post('/login', data={
            'username': 'superadmin',
            'password': 'wrong-password',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b'salah' in resp.data.lower() or b'invalid' in resp.data.lower() or b'login' in resp.data.lower()

    def test_login_unknown_user(self, client):
        resp = client.post('/login', data={
            'username': 'nobody',
            'password': 'anything',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b'salah' in resp.data.lower() or b'login' in resp.data.lower()

    def test_login_empty_username(self, client):
        resp = client.post('/login', data={
            'username': '',
            'password': 'Admin@123',
        }, follow_redirects=True)
        assert resp.status_code == 200


class TestProtectedRoutes:
    """Rute yang membutuhkan login harus redirect ke /login saat belum authenticated."""

    @pytest.mark.parametrize('url', [
        '/portal',
        '/karyawan',
        '/support/',
        '/project/',
        '/aset/',
        '/booking/',
        '/chatbot',
    ])
    def test_redirect_to_login_when_not_authenticated(self, client, url):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code in (301, 302)
        location = resp.headers.get('Location', '')
        assert 'login' in location.lower()


class TestLogout:
    """Logout membersihkan session."""

    def test_logout_redirects(self, auth_client):
        resp = auth_client.get('/logout', follow_redirects=False)
        assert resp.status_code in (301, 302)

    def test_after_logout_cannot_access_protected(self, auth_client):
        auth_client.get('/logout', follow_redirects=True)
        resp = auth_client.get('/portal', follow_redirects=False)
        assert resp.status_code in (301, 302)
        location = resp.headers.get('Location', '')
        assert 'login' in location.lower()
