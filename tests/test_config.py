"""Tests for configuration system."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from liftosaur2garmin.config import (
    DEFAULT_CONFIG,
    is_configured,
    load_config,
    save_config,
)


class TestLoadConfig:
    def test_returns_defaults_when_no_file(self, tmp_path: Path) -> None:
        with patch("liftosaur2garmin.config.CONFIG_FILE", tmp_path / "missing.json"):
            config = load_config()
            assert config["user_profile"]["weight_kg"] == 80.0
            assert config["timing"]["working_set_seconds"] == 40

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        with patch("liftosaur2garmin.config.CONFIG_DIR", tmp_path), \
             patch("liftosaur2garmin.config.CONFIG_FILE", config_file):
            original = load_config()
            original["hevy_api_key"] = "test-key-123"
            original["user_profile"]["weight_kg"] = 75.5
            save_config(original)

            loaded = load_config()
            assert loaded["hevy_api_key"] == "test-key-123"
            assert loaded["user_profile"]["weight_kg"] == 75.5

    def test_deep_merge_preserves_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        # Save partial config (missing timing)
        config_file.write_text(json.dumps({"hevy_api_key": "key", "user_profile": {"weight_kg": 90}}))

        with patch("liftosaur2garmin.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config["hevy_api_key"] == "key"
            assert config["user_profile"]["weight_kg"] == 90
            # Defaults preserved for unset values
            assert config["user_profile"]["birth_year"] == 1990
            assert config["timing"]["working_set_seconds"] == 40

    def test_corrupt_file_returns_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text("{corrupt json!!!")

        with patch("liftosaur2garmin.config.CONFIG_FILE", config_file):
            config = load_config()
            assert config["user_profile"]["weight_kg"] == 80.0

    def test_reads_local_dotenv_file(self, tmp_path: Path, monkeypatch) -> None:
        config_file = tmp_path / "missing.json"
        (tmp_path / ".env").write_text(
            "LIFTOSAUR_API_KEY=dotenv-key\n"
            "GARMIN_EMAIL=dotenv@example.com\n"
            "GARMIN_PASSWORD=dotenv-password\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LIFTOSAUR_API_KEY", raising=False)
        monkeypatch.delenv("GARMIN_EMAIL", raising=False)
        monkeypatch.delenv("GARMIN_PASSWORD", raising=False)

        with patch("liftosaur2garmin.config.CONFIG_FILE", config_file):
            config = load_config()

        assert config["liftosaur_api_key"] == "dotenv-key"
        assert config["garmin_email"] == "dotenv@example.com"
        assert config["garmin_password"] == "dotenv-password"

    def test_exported_env_keeps_precedence_over_dotenv(self, tmp_path: Path, monkeypatch) -> None:
        config_file = tmp_path / "missing.json"
        (tmp_path / ".env").write_text("LIFTOSAUR_API_KEY=dotenv-key\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LIFTOSAUR_API_KEY", "exported-key")

        with patch("liftosaur2garmin.config.CONFIG_FILE", config_file):
            config = load_config()

        assert config["liftosaur_api_key"] == "exported-key"


class TestIsConfigured:
    def test_false_without_api_key(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LIFTOSAUR_API_KEY", raising=False)
        monkeypatch.delenv("HEVY_API_KEY", raising=False)
        with patch("liftosaur2garmin.config.CONFIG_FILE", tmp_path / "missing.json"):
            assert is_configured() is False

    def test_true_with_api_key(self, tmp_path: Path, monkeypatch) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"hevy_api_key": "some-key"}))

        # When DATABASE_URL is set, is_configured also checks for Garmin tokens.
        # Clear it so this test only validates the API key check.
        monkeypatch.delenv("DATABASE_URL", raising=False)

        with patch("liftosaur2garmin.config.CONFIG_FILE", config_file):
            assert is_configured() is True

    def test_false_with_empty_api_key(self, tmp_path: Path, monkeypatch) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"hevy_api_key": ""}))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LIFTOSAUR_API_KEY", raising=False)
        monkeypatch.delenv("HEVY_API_KEY", raising=False)

        with patch("liftosaur2garmin.config.CONFIG_FILE", config_file):
            assert is_configured() is False
