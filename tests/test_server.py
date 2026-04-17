"""Tests for server helpers and Garmin setup APIs."""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from typing import Any

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


class TestFavicon:
    def test_favicon_ico_is_served(self) -> None:
        client = TestClient(app)

        response = client.get("/favicon.ico")

        assert response.status_code == 200

    def test_setup_includes_favicon_links(self, monkeypatch) -> None:
        client = TestClient(app)

        monkeypatch.setattr(
            "liftosaur2garmin.server.load_config",
            lambda: {"garmin_email": "", "user_profile": {}},
            raising=False,
        )
        monkeypatch.setattr(
            "liftosaur2garmin.server.db.get_database_url",
            lambda: None,
            raising=False,
        )
        monkeypatch.setattr(
            "liftosaur2garmin.server._has_garmin_tokens",
            lambda config=None: False,
            raising=False,
        )

        response = client.get("/setup")

        assert response.status_code == 200
        assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in response.text
        assert '<link rel="alternate icon" href="/favicon.ico" sizes="any">' in response.text


class TestLiftosaurSetupApis:
    def test_validate_liftosaur_endpoint(self, monkeypatch) -> None:
        client = TestClient(app)

        fake_client = MagicMock()
        fake_client.get_workout_count.return_value = 12
        monkeypatch.setattr(
            "liftosaur2garmin.liftosaur.LiftosaurClient",
            lambda api_key: fake_client,
        )

        response = client.get("/api/validate-liftosaur?key=test-key")

        assert response.status_code == 200
        assert response.json() == {"valid": True, "workout_count": 12}

    def test_workouts_page_removes_legacy_source_link(self, monkeypatch) -> None:
        client = TestClient(app)
        db_mock = MagicMock()
        db_mock.get_app_config.return_value = {
            "workouts": [
                {
                    "id": "w1",
                    "title": "Push",
                    "start_time": "2026-04-01T20:00:00+00:00",
                    "end_time": "2026-04-01T20:45:00+00:00",
                    "exercises": [],
                }
            ],
            "page_count": 1,
        }
        monkeypatch.setattr("liftosaur2garmin.server.is_configured", lambda: True, raising=False)
        monkeypatch.setattr(
            "liftosaur2garmin.server.load_config",
            lambda: {
                "liftosaur_api_key": "test-key",
                "garmin_email": "user@example.com",
                "user_profile": {},
                "hr_fusion": {"enabled": False},
            },
            raising=False,
        )
        monkeypatch.setattr("liftosaur2garmin.server.db.get_db", lambda: db_mock)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_synced_count", lambda: 0)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_recent_synced", lambda *_args, **_kwargs: [])
        monkeypatch.setattr("liftosaur2garmin.server.db.get_sync_log", lambda *_args, **_kwargs: [])
        monkeypatch.setattr("liftosaur2garmin.server.db.get_database_url", lambda: None)
        monkeypatch.setattr("liftosaur2garmin.server._has_garmin_tokens", lambda config=None: True, raising=False)

        response = client.get("/workouts")

        assert response.status_code == 200
        assert "he" "vy.com/workout" not in response.text


class TestSyncOneApi:
    def test_sync_one_uploads_when_update_existing_disabled(self, monkeypatch) -> None:
        client = TestClient(app)
        workout = {
            "id": "w1",
            "title": "Push",
            "start_time": "2026-04-01T20:00:00+00:00",
            "updated_at": "2026-04-01T21:00:00+00:00",
            "exercises": [],
        }
        db_mock = MagicMock()
        liftosaur_client = MagicMock()
        liftosaur_client.get_workout_count.return_value = 1
        liftosaur_client.get_workouts.return_value = {"workouts": [workout], "page_count": 1}

        monkeypatch.setattr(
            "liftosaur2garmin.server.load_config",
            lambda: {
                "liftosaur_api_key": "liftosaur-key",
                "garmin_email": "user@example.com",
                "update_existing": {"enabled": False},
            },
        )
        monkeypatch.setattr("liftosaur2garmin.server._failed_ids", set())
        monkeypatch.setattr("liftosaur2garmin.server.db.get_db", lambda: db_mock)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_synced_count", lambda: 0)
        monkeypatch.setattr("liftosaur2garmin.server.db.is_synced", lambda workout_id: False)
        monkeypatch.setattr("liftosaur2garmin.server.db.mark_synced", db_mock.mark_synced)
        monkeypatch.setattr("liftosaur2garmin.liftosaur.LiftosaurClient", lambda api_key: liftosaur_client)
        monkeypatch.setattr("liftosaur2garmin.garmin.get_client", lambda email: object())
        monkeypatch.setattr(
            "liftosaur2garmin.garmin.find_activity_by_start_time",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dedup lookup should be skipped")),
        )
        monkeypatch.setattr(
            "liftosaur2garmin.sync.update_existing_activity_sets",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("set update should be skipped")),
        )
        monkeypatch.setattr("liftosaur2garmin.fit.generate_fit", lambda *_args, **_kwargs: {"calories": 100, "avg_hr": 90})
        monkeypatch.setattr("liftosaur2garmin.garmin.upload_fit", lambda *_args, **_kwargs: {"activity_id": 456})
        monkeypatch.setattr("liftosaur2garmin.garmin.rename_activity", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("liftosaur2garmin.garmin.generate_description", lambda *_args, **_kwargs: "desc")
        monkeypatch.setattr("liftosaur2garmin.garmin.set_description", lambda *_args, **_kwargs: None)

        response = client.post("/api/sync-one")

        assert response.status_code == 200
        assert response.json()["synced"] == 1
        db_mock.mark_synced.assert_called_once()

    def test_sync_one_updates_existing_when_enabled(self, monkeypatch) -> None:
        client = TestClient(app)
        workout = {
            "id": "w1",
            "title": "Push",
            "start_time": "2026-04-01T20:00:00+00:00",
            "updated_at": "2026-04-01T21:00:00+00:00",
            "exercises": [],
        }
        db_mock = MagicMock()
        liftosaur_client = MagicMock()
        liftosaur_client.get_workout_count.return_value = 1
        liftosaur_client.get_workouts.return_value = {"workouts": [workout], "page_count": 1}
        find_calls: list[str] = []
        update_calls: list[tuple[int, dict]] = []

        monkeypatch.setattr(
            "liftosaur2garmin.server.load_config",
            lambda: {
                "liftosaur_api_key": "liftosaur-key",
                "garmin_email": "user@example.com",
                "update_existing": {"enabled": True},
            },
        )
        monkeypatch.setattr("liftosaur2garmin.server._failed_ids", set())
        monkeypatch.setattr("liftosaur2garmin.server.db.get_db", lambda: db_mock)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_synced_count", lambda: 0)
        monkeypatch.setattr("liftosaur2garmin.server.db.is_synced", lambda workout_id: False)
        monkeypatch.setattr("liftosaur2garmin.server.db.mark_synced", db_mock.mark_synced)
        monkeypatch.setattr("liftosaur2garmin.liftosaur.LiftosaurClient", lambda api_key: liftosaur_client)
        monkeypatch.setattr("liftosaur2garmin.garmin.get_client", lambda email: object())
        monkeypatch.setattr(
            "liftosaur2garmin.garmin.find_activity_by_start_time",
            lambda *_args, **_kwargs: find_calls.append("called") or 999,
        )
        monkeypatch.setattr(
            "liftosaur2garmin.sync.update_existing_activity_sets",
            lambda _client, activity_id, synced_workout: update_calls.append((activity_id, synced_workout)),
        )
        monkeypatch.setattr(
            "liftosaur2garmin.garmin.upload_fit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("upload should be skipped")),
        )
        monkeypatch.setattr("liftosaur2garmin.fit.generate_fit", lambda *_args, **_kwargs: {"calories": 100, "avg_hr": 90})
        monkeypatch.setattr("liftosaur2garmin.garmin.rename_activity", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("liftosaur2garmin.garmin.generate_description", lambda *_args, **_kwargs: "desc")
        monkeypatch.setattr("liftosaur2garmin.garmin.set_description", lambda *_args, **_kwargs: None)

        response = client.post("/api/sync-one")

        assert response.status_code == 200
        assert response.json()["synced"] == 1
        assert find_calls == ["called"]
        assert update_calls == [(999, workout)]
        db_mock.mark_synced.assert_called_once()

    def test_sync_one_does_not_search_beyond_bounded_recent_pages(self, monkeypatch) -> None:
        client = TestClient(app)
        recent_workout = {
            "id": "recent-synced",
            "title": "Recent Push",
            "start_time": "2026-04-01T20:00:00+00:00",
            "updated_at": "2026-04-01T21:00:00+00:00",
            "exercises": [],
        }
        old_workout = {
            "id": "old-w1",
            "title": "Very Old Push",
            "start_time": "2025-01-01T20:00:00+00:00",
            "updated_at": "2025-01-01T21:00:00+00:00",
            "exercises": [],
        }
        db_mock = MagicMock()
        hevy_client = MagicMock()
        total_count = 1000
        searched_pages: list[int] = []

        def fake_get_workouts(page: int, page_size: int) -> dict[str, Any]:
            searched_pages.append(page)
            if page == 11:
                return {"workouts": [old_workout], "page_count": 100}
            return {"workouts": [recent_workout], "page_count": 100}

        hevy_client.get_workout_count.return_value = total_count
        hevy_client.get_workouts.side_effect = fake_get_workouts

        monkeypatch.setattr(
            "liftosaur2garmin.server.load_config",
            lambda: {
                "hevy_api_key": "hevy-key",
                "garmin_email": "user@example.com",
                "update_existing": {"enabled": False},
            },
        )
        monkeypatch.setattr("liftosaur2garmin.server._failed_ids", set())
        monkeypatch.setattr("liftosaur2garmin.server.db.get_db", lambda: db_mock)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_synced_count", lambda: 0)
        monkeypatch.setattr("liftosaur2garmin.server.db.is_synced", lambda workout_id: workout_id != "old-w1")
        monkeypatch.setattr("liftosaur2garmin.server.db.mark_synced", db_mock.mark_synced)
        monkeypatch.setattr("liftosaur2garmin.hevy.HevyClient", lambda api_key: hevy_client)
        monkeypatch.setattr(
            "liftosaur2garmin.garmin.get_client",
            lambda email: (_ for _ in ()).throw(AssertionError("Garmin client should not be used when no workout is found")),
        )

        response = client.post("/api/sync-one")

        assert response.status_code == 200
        assert response.json() == {"synced": 0, "remaining": 0, "done": True}
        assert searched_pages == list(range(1, 11))
        db_mock.mark_synced.assert_not_called()


class TestSettingsSave:
    """Tests for POST /settings endpoint."""

    def _post_settings(self, client, monkeypatch, form_overrides=None):
        """Helper: mock config/save, POST /settings, return saved config dict."""
        saved_config = {}
        monkeypatch.setattr(
            "liftosaur2garmin.server.load_config",
            lambda: {
                "liftosaur_api_key": "old-key",
                "garmin_email": "old@example.com",
                "user_profile": {"weight_kg": 80, "birth_year": 1990, "sex": "male", "vo2max": 45},
                "timing": {"working_set_seconds": 40, "warmup_set_seconds": 25, "rest_between_sets_seconds": 75, "rest_between_exercises_seconds": 120},
                "hr_fusion": {"enabled": True},
                "update_existing": {"enabled": True, "match_window_minutes": 30},
            },
        )
        monkeypatch.setattr("liftosaur2garmin.server.save_config", lambda c: saved_config.update(c))
        monkeypatch.setattr("liftosaur2garmin.server._persist_cloud_credentials", lambda **kw: None)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_database_url", lambda: None)

        defaults = {
            "liftosaur_api_key": "test-key",
            "garmin_email": "user@example.com",
            "weight_kg": "75.5", "birth_year": "1992", "sex": "female", "vo2max": "50",
            "working_set_seconds": "35", "warmup_set_seconds": "20",
            "rest_between_sets_seconds": "90", "rest_between_exercises_seconds": "150",
            "hr_fusion_enabled": "on",
            "update_existing_enabled": "on",
            "match_window_minutes": "30",
        }
        if form_overrides:
            defaults.update(form_overrides)

        response = client.post("/settings", data=defaults, follow_redirects=False)
        return response, saved_config

    def test_redirects_to_settings(self, monkeypatch) -> None:
        client = TestClient(app)
        response, _ = self._post_settings(client, monkeypatch)
        assert response.status_code == 303

    def test_saves_user_profile(self, monkeypatch) -> None:
        client = TestClient(app)
        _, saved = self._post_settings(client, monkeypatch)
        assert saved["user_profile"]["weight_kg"] == 75.5
        assert saved["user_profile"]["birth_year"] == 1992
        assert saved["user_profile"]["sex"] == "female"
        assert saved["user_profile"]["vo2max"] == 50

    def test_saves_timing(self, monkeypatch) -> None:
        client = TestClient(app)
        _, saved = self._post_settings(client, monkeypatch)
        assert saved["timing"]["working_set_seconds"] == 35
        assert saved["timing"]["warmup_set_seconds"] == 20
        assert saved["timing"]["rest_between_sets_seconds"] == 90
        assert saved["timing"]["rest_between_exercises_seconds"] == 150

    def test_saves_api_key_and_email(self, monkeypatch) -> None:
        client = TestClient(app)
        _, saved = self._post_settings(client, monkeypatch)
        assert saved["liftosaur_api_key"] == "test-key"
        assert saved["garmin_email"] == "user@example.com"

    def test_saves_hr_fusion(self, monkeypatch) -> None:
        client = TestClient(app)
        _, saved = self._post_settings(client, monkeypatch, {"hr_fusion_enabled": "off"})
        assert saved["hr_fusion"]["enabled"] is False

    def test_saves_update_existing(self, monkeypatch) -> None:
        client = TestClient(app)
        _, saved = self._post_settings(client, monkeypatch, {
            "update_existing_enabled": "on",
            "match_window_minutes": "45",
        })
        assert saved["update_existing"]["enabled"] is True
        assert saved["update_existing"]["match_window_minutes"] == 45

    def test_update_existing_disabled(self, monkeypatch) -> None:
        client = TestClient(app)
        _, saved = self._post_settings(client, monkeypatch, {
            "update_existing_enabled": "off",
        })
        assert saved["update_existing"]["enabled"] is False

    def test_match_window_clamped(self, monkeypatch) -> None:
        client = TestClient(app)
        _, saved = self._post_settings(client, monkeypatch, {"match_window_minutes": "9999"})
        assert saved["update_existing"]["match_window_minutes"] == 1440

    def test_garmin_ticket_rejects_bad_payload(self) -> None:
        client = TestClient(app)
        response = client.post("/api/garmin-ticket", json={"tokens": {"di_token": "one"}})
        assert response.status_code == 400
        assert response.json()["error"].startswith("Invalid tokens")

    def test_garmin_ticket_stores_valid_payload(self, monkeypatch) -> None:
        client = TestClient(app)
        saved: dict[str, Any] = {}

        monkeypatch.setattr(
            "liftosaur2garmin.server.load_config",
            lambda: {"garmin_email": "", "user_profile": {}},
            raising=False,
        )
        monkeypatch.setattr(
            "liftosaur2garmin.server.save_config",
            lambda config: saved.setdefault("config", config),
            raising=False,
        )
        monkeypatch.setattr(
            "liftosaur2garmin.server._persist_cloud_credentials",
            lambda **kwargs: saved.setdefault("persist", kwargs),
            raising=False,
        )
        monkeypatch.setattr(
            "liftosaur2garmin.garmin.save_token_payload",
            lambda payload, **kwargs: saved.setdefault("payload", payload),
        )

        response = client.post(
            "/api/garmin-ticket",
            json={
                "garmin_email": "user@example.com",
                "tokens": {
                    "di_token": "one",
                    "di_refresh_token": "two",
                    "di_client_id": "cid",
                },
            },
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert saved["payload"]["email"] == "user@example.com"
        assert saved["payload"]["auth"]["di_token"] == "one"
        assert saved["config"]["garmin_email"] == "user@example.com"
        assert saved["persist"] == {"garmin_email": "user@example.com"}

    def test_setup_cloud_with_worker_renders_inline_login(self, monkeypatch) -> None:
        client = TestClient(app)

        monkeypatch.setenv("GARMIN_AUTH_WORKER_BASE_URL", "https://worker.example")
        monkeypatch.setattr("liftosaur2garmin.server.load_config", lambda: {"garmin_email": "", "user_profile": {}}, raising=False)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_database_url", lambda: "postgres://example", raising=False)
        monkeypatch.setattr("liftosaur2garmin.server._has_garmin_tokens", lambda config=None: False, raising=False)

        response = client.get("/setup")

        assert response.status_code == 200
        assert "https://worker.example" in response.text
        assert "garminConnect()" in response.text
        assert "garminSubmitMfa()" in response.text

    def test_setup_cloud_without_worker_renders_token_upload(self, monkeypatch) -> None:
        client = TestClient(app)

        monkeypatch.delenv("GARMIN_AUTH_WORKER_BASE_URL", raising=False)
        monkeypatch.setattr("liftosaur2garmin.server.load_config", lambda: {"garmin_email": "", "user_profile": {}}, raising=False)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_database_url", lambda: "postgres://example", raising=False)
        monkeypatch.setattr("liftosaur2garmin.server._has_garmin_tokens", lambda config=None: False, raising=False)

        response = client.get("/setup")

        assert response.status_code == 200
        assert "Upload Garmin Token File" in response.text
        assert "garminConnect()" not in response.text

    def test_settings_cloud_with_worker_renders_reconnect_ui(self, monkeypatch) -> None:
        client = TestClient(app)

        monkeypatch.setenv("GARMIN_AUTH_WORKER_BASE_URL", "https://worker.example")
        monkeypatch.setattr("liftosaur2garmin.server.is_configured", lambda: True, raising=False)
        monkeypatch.setattr("liftosaur2garmin.server.load_config", lambda: {"garmin_email": "", "user_profile": {}, "hr_fusion": {}}, raising=False)
        monkeypatch.setattr("liftosaur2garmin.server.db.get_database_url", lambda: "postgres://example", raising=False)
        monkeypatch.setattr("liftosaur2garmin.server._has_garmin_tokens", lambda config=None: False, raising=False)
        monkeypatch.setattr("liftosaur2garmin.server._get_unmapped_exercises", lambda: [], raising=False)

        response = client.get("/settings")

        assert response.status_code == 200
        assert "https://worker.example" in response.text
        assert "Connect Garmin" in response.text
