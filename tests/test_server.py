"""Tests for server helpers and Garmin setup APIs."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from liftosaur2garmin.garmin import GarminNeedsMFA
from liftosaur2garmin.server import _parse_autosync_interval, app


class TestParseAutosyncInterval:
    def test_accepts_known_interval(self) -> None:
        assert _parse_autosync_interval("240") == 240

    def test_defaults_on_undefined(self) -> None:
        assert _parse_autosync_interval("undefined") == 120

    def test_defaults_on_invalid_value(self) -> None:
        assert _parse_autosync_interval("15") == 120

    def test_defaults_on_missing_value(self) -> None:
        assert _parse_autosync_interval(None) == 120


class TestGarminSetupApis:
    def test_login_start_rejects_missing_credentials(self) -> None:
        client = TestClient(app)
        response = client.post("/api/garmin/login/start", json={})
        assert response.status_code == 400
        assert response.json() == {"error": "Garmin email and password are required"}

    def test_login_start_returns_mfa(self, monkeypatch) -> None:
        client = TestClient(app)

        class DummyState:
            pass

        def fake_start_login(email: str, password: str, token_dir: str):
            raise GarminNeedsMFA(DummyState())

        monkeypatch.setattr("liftosaur2garmin.server.load_config", lambda: {"garmin_email": "", "user_profile": {}})
        monkeypatch.setattr("liftosaur2garmin.server.save_config", lambda config: None)
        monkeypatch.setattr("liftosaur2garmin.garmin.start_login", fake_start_login)

        response = client.post(
            "/api/garmin/login/start",
            json={"email": "user@example.com", "password": "secret"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "needs_mfa"
        assert response.json()["login_id"]

    def test_login_finish_requires_code(self) -> None:
        client = TestClient(app)
        response = client.post("/api/garmin/login/finish", json={"login_id": "missing", "mfa_code": ""})
        assert response.status_code == 400

    def test_import_token_file_accepts_valid_payload(self, monkeypatch) -> None:
        client = TestClient(app)
        saved: dict[str, object] = {}

        class DummyAuth:
            def load_payload(self, payload):
                saved["validated"] = payload

        monkeypatch.setattr("liftosaur2garmin.garmin.GarminAuthSession", lambda: DummyAuth())
        monkeypatch.setattr("liftosaur2garmin.garmin.save_token_payload", lambda payload: saved.setdefault("payload", payload))
        monkeypatch.setattr("liftosaur2garmin.server.save_config", lambda config: saved.setdefault("config", config), raising=False)
        monkeypatch.setattr("liftosaur2garmin.server.load_config", lambda: {"garmin_email": "", "user_profile": {}}, raising=False)
        monkeypatch.setattr("liftosaur2garmin.server._persist_cloud_credentials", lambda **kwargs: saved.setdefault("persist", kwargs), raising=False)

        payload = {
            "schema_version": 1,
            "kind": "garmin_native_auth",
            "email": "user@example.com",
            "auth": {"di_token": "one", "di_refresh_token": "two", "di_client_id": "cid"},
        }
        response = client.post(
            "/api/garmin/import-token-file",
            data={"garmin_email": "user@example.com"},
            files={"token_file": ("garmin_tokens.json", json.dumps(payload), "application/json")},
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert saved["payload"]["email"] == "user@example.com"

    def test_import_token_file_rejects_bad_payload(self) -> None:
        client = TestClient(app)
        response = client.post(
            "/api/garmin/import-token-file",
            files={"token_file": ("garmin_tokens.json", "{}", "application/json")},
        )
        assert response.status_code == 400
