"""Tests for server helpers."""

from __future__ import annotations

import asyncio
import json

from liftosaur2garmin.server import _parse_autosync_interval, garmin_ticket_store


class TestParseAutosyncInterval:
    def test_accepts_known_interval(self) -> None:
        assert _parse_autosync_interval("240") == 240

    def test_defaults_on_undefined(self) -> None:
        assert _parse_autosync_interval("undefined") == 120

    def test_defaults_on_invalid_value(self) -> None:
        assert _parse_autosync_interval("15") == 120

    def test_defaults_on_missing_value(self) -> None:
        assert _parse_autosync_interval(None) == 120


class TestGarminTicketEndpoint:
    def test_rejects_missing_ticket_and_tokens(self) -> None:
        class FakeRequest:
            async def json(self) -> dict:
                return {}

        response = asyncio.run(garmin_ticket_store(FakeRequest()))

        assert response.status_code == 400
        assert json.loads(response.body) == {"error": "Missing Garmin ticket or tokens"}

    def test_exchanges_raw_ticket_locally(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        class FakeRequest:
            async def json(self) -> dict:
                return {"ticket": "ST-123"}

        def fake_exchange(ticket: str) -> dict[str, dict]:
            captured["ticket"] = ticket
            return {
                "oauth1_token.json": {"oauth_token": "one"},
                "oauth2_token.json": {"access_token": "two"},
            }

        def fake_store(tokens: dict[str, dict]) -> None:
            captured["tokens"] = tokens

        monkeypatch.setattr("liftosaur2garmin.server._exchange_garmin_ticket", fake_exchange)
        monkeypatch.setattr("liftosaur2garmin.server._store_garmin_tokens", fake_store)

        response = asyncio.run(garmin_ticket_store(FakeRequest()))

        assert response.status_code == 200
        assert json.loads(response.body) == {"ok": True}
        assert captured["ticket"] == "ST-123"
        assert captured["tokens"] == {
            "oauth1_token.json": {"oauth_token": "one"},
            "oauth2_token.json": {"access_token": "two"},
        }
