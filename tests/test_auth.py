"""Tests for optional dashboard password auth."""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestPasswordAuthHelpers:
    def test_auth_disabled_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("L2G_PASSWORD", raising=False)

        from liftosaur2garmin.auth import auth_enabled, verify_session

        assert not auth_enabled()
        assert verify_session(None) is True

    def test_auth_enabled_when_password_set(self, monkeypatch) -> None:
        monkeypatch.setenv("L2G_PASSWORD", "dashboard-secret")

        from liftosaur2garmin.auth import auth_enabled

        assert auth_enabled()

    def test_signed_session_round_trip(self, monkeypatch) -> None:
        monkeypatch.setenv("L2G_PASSWORD", "dashboard-secret")

        from liftosaur2garmin.auth import sign_session, verify_session

        cookie = sign_session()

        assert cookie.startswith("v1.")
        assert verify_session(cookie) is True

    def test_rejects_tampered_session(self, monkeypatch) -> None:
        monkeypatch.setenv("L2G_PASSWORD", "dashboard-secret")

        from liftosaur2garmin.auth import sign_session, verify_session

        parts = sign_session().split(".")
        parts[2] = "0" * len(parts[2])

        assert verify_session(".".join(parts)) is False


class TestPasswordAuthRoutes:
    def _client(self, monkeypatch) -> TestClient:
        from liftosaur2garmin import server

        server._is_configured_cache = None
        monkeypatch.setenv("L2G_PASSWORD", "dashboard-secret")
        monkeypatch.setattr(server, "is_configured", lambda: True)
        monkeypatch.setattr(server, "load_config", lambda: {"user_profile": {}, "auto_sync": {}})
        monkeypatch.setattr(server.db, "get_synced_count", lambda: 0)
        monkeypatch.setattr(server.db, "get_recent_synced", lambda limit=5: [])
        return TestClient(server.app, follow_redirects=False)

    def test_unauthenticated_page_redirects_to_login(self, monkeypatch) -> None:
        client = self._client(monkeypatch)

        response = client.get("/")

        assert response.status_code in (302, 307)
        assert response.headers["location"] == "/login?next=/"

    def test_login_page_renders(self, monkeypatch) -> None:
        client = self._client(monkeypatch)

        response = client.get("/login")

        assert response.status_code == 200
        assert "liftosaur2garmin" in response.text
        assert "password" in response.text.lower()

    def test_wrong_password_returns_401(self, monkeypatch) -> None:
        client = self._client(monkeypatch)

        response = client.post("/login", data={"password": "wrong"})

        assert response.status_code == 401
        assert "Wrong password" in response.text

    def test_correct_password_sets_session_cookie(self, monkeypatch) -> None:
        client = self._client(monkeypatch)

        response = client.post("/login", data={"password": "dashboard-secret"})

        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "l2g_session" in response.cookies

    def test_authenticated_page_access_does_not_redirect_to_login(self, monkeypatch) -> None:
        client = self._client(monkeypatch)
        login = client.post("/login", data={"password": "dashboard-secret"})

        response = client.get("/", cookies={"l2g_session": login.cookies["l2g_session"]})

        assert response.status_code != 302 or "/login" not in response.headers.get("location", "")

    def test_unauthenticated_api_returns_401(self, monkeypatch) -> None:
        client = self._client(monkeypatch)

        response = client.get("/api/sync-one")

        assert response.status_code == 401

    def test_exempt_setup_route_still_renders(self, monkeypatch) -> None:
        client = self._client(monkeypatch)

        response = client.get("/setup")

        assert response.status_code == 200
        assert "Setup" in response.text

    def test_exempt_cron_route_uses_own_auth(self, monkeypatch) -> None:
        client = self._client(monkeypatch)

        response = client.get("/api/cron/sync")

        assert response.status_code != 302
