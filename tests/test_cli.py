"""Tests for CLI commands."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run liftosaur2garmin CLI and capture output."""
    return subprocess.run(
        [sys.executable, "-m", "liftosaur2garmin.cli", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestNoArgs:
    def test_shows_help(self) -> None:
        result = run_cli()
        assert result.returncode == 0
        assert "liftosaur2garmin" in result.stdout
        assert "sync" in result.stdout
        assert "init" in result.stdout
        assert "list" in result.stdout


class TestStatus:
    def test_without_config(self, tmp_path: Path, monkeypatch) -> None:
        """Status with no config should show 'not configured' — but subprocess reads real config.
        Test the function directly instead."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LIFTOSAUR_API_KEY", raising=False)
        monkeypatch.delenv("HEVY_API_KEY", raising=False)
        with patch("liftosaur2garmin.config.CONFIG_FILE", tmp_path / "nonexistent.json"):
            from liftosaur2garmin.config import is_configured
            assert is_configured() is False


class TestMap:
    def test_map_command_in_memory(self) -> None:
        from liftosaur2garmin.mapper import _custom_mappings, lookup_exercise

        _custom_mappings["CLI Test Exercise"] = (10, 20)
        cat, subcat, _ = lookup_exercise("CLI Test Exercise")
        assert cat == 10
        assert subcat == 20
        _custom_mappings.clear()


class TestSyncDryRun:
    def test_dry_run_flag(self) -> None:
        # Just verify the flag is accepted
        result = run_cli("sync", "--dry-run", "--help")
        assert result.returncode == 0
        assert "dry-run" in result.stdout

    def test_all_flag(self) -> None:
        result = run_cli("sync", "--all", "--help")
        assert result.returncode == 0

    def test_since_flag(self) -> None:
        result = run_cli("sync", "--since", "2026-01-01", "--help")
        assert result.returncode == 0
