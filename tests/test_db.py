"""Tests for database tracking layer."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from liftosaur2garmin.db_sqlite import SQLiteDatabase

LEGACY_PREFIX = "he" "vy"
LEGACY_ID_COLUMN = f"{LEGACY_PREFIX}_id"
LEGACY_UPDATED_COLUMN = f"{LEGACY_PREFIX}_updated_at"
LEGACY_NAME_COLUMN = f"{LEGACY_PREFIX}_name"
LEGACY_PLATFORM = LEGACY_PREFIX
LEGACY_TOTAL_KEY = f"{LEGACY_PREFIX}_total"
LEGACY_PAGE_KEY = f"{LEGACY_PREFIX}_workouts_page_1"


def _make_db(tmp_path):
    """Create a DB instance appropriate for the current environment."""
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        from liftosaur2garmin.db_postgres import PostgresDatabase
        db = PostgresDatabase(database_url)
        # Clean tables for test isolation
        with db._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM synced_workouts")
                cur.execute("DELETE FROM sync_log")
                cur.execute("DELETE FROM hr_cache")
            conn.commit()
        return db
    return SQLiteDatabase(tmp_path / "test.db")


class TestSyncTracking:
    def test_not_synced_initially(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        assert db.is_synced("unknown-id") is False

    def test_mark_then_check(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        db.mark_synced("w1", garmin_activity_id="123", title="Push")
        assert db.is_synced("w1") is True

    def test_count(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        assert db.get_synced_count() == 0
        db.mark_synced("w1", title="Push")
        db.mark_synced("w2", title="Pull")
        assert db.get_synced_count() == 2

    def test_recent_ordering(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        db.mark_synced("w1", title="First")
        import time; time.sleep(1.1)  # ensure different timestamp
        db.mark_synced("w2", title="Second")
        recent = db.get_recent_synced(limit=2)
        assert len(recent) == 2
        assert recent[0]["title"] == "Second"  # most recent first

    def test_idempotent_mark(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        db.mark_synced("w1", garmin_activity_id="100", title="Push")
        db.mark_synced("w1", garmin_activity_id="200", title="Push Updated")
        assert db.get_synced_count() == 1
        recent = db.get_recent_synced(limit=1)
        assert recent[0]["garmin_activity_id"] == "200"

    def test_db_auto_creates(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "dir" / "sync.db"
        db = SQLiteDatabase(db_path)
        db.mark_synced("w1", title="Test")
        assert db_path.exists()

    def test_stores_calories_and_hr(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        db.mark_synced("w1", title="Push", calories=250, avg_hr=95)
        recent = db.get_recent_synced(limit=1)
        assert recent[0]["calories"] == 250
        assert recent[0]["avg_hr"] == 95

    def test_unsync_single(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        db.mark_synced("w1", garmin_activity_id="100", title="Push")
        db.mark_synced("w2", garmin_activity_id="200", title="Pull")
        assert db.get_synced_count() == 2
        assert db.unsync("w1") is True
        assert db.get_synced_count() == 1
        assert db.is_synced("w1") is False
        assert db.is_synced("w2") is True

    def test_unsync_nonexistent(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        assert db.unsync("nonexistent") is False

    def test_unsync_all(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        db.mark_synced("w1", title="Push")
        db.mark_synced("w2", title="Pull")
        db.mark_synced("w3", title="Legs")
        count = db.unsync_all()
        assert count == 3
        assert db.get_synced_count() == 0

    def test_app_config_roundtrip(self, tmp_path: Path) -> None:
        db = SQLiteDatabase(tmp_path / "test.db")
        assert db.get_app_config("missing") is None
        db.set_app_config("settings", {"theme": "dark", "n": 42})
        assert db.get_app_config("settings") == {"theme": "dark", "n": 42}
        # Overwrite
        db.set_app_config("settings", {"theme": "light"})
        assert db.get_app_config("settings") == {"theme": "light"}

    def test_app_config_caches_workout_pages(self, tmp_path: Path) -> None:
        """The workouts-page cache key pattern used by the server."""
        db = SQLiteDatabase(tmp_path / "test.db")
        page_data = {"workouts": [{"id": "a"}, {"id": "b"}], "page_count": 3}
        db.set_app_config("workouts_page_1", page_data)
        got = db.get_app_config("workouts_page_1")
        assert got["page_count"] == 3
        assert len(got["workouts"]) == 2
        assert got["workouts"][0]["id"] == "a"

    def test_sqlite_migrates_legacy_schema(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            f"""
            CREATE TABLE synced_workouts (
                {LEGACY_ID_COLUMN} TEXT PRIMARY KEY,
                garmin_activity_id TEXT,
                title TEXT,
                synced_at TEXT DEFAULT (datetime('now')),
                calories INTEGER,
                avg_hr INTEGER,
                status TEXT DEFAULT 'success',
                {LEGACY_UPDATED_COLUMN} TEXT
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE hr_cache (
                {LEGACY_ID_COLUMN} TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                cached_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE custom_mappings (
                {LEGACY_NAME_COLUMN} TEXT PRIMARY KEY,
                category INTEGER NOT NULL,
                subcategory INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE platform_credentials (
                platform TEXT PRIMARY KEY,
                auth_type TEXT NOT NULL DEFAULT 'oauth',
                credentials TEXT NOT NULL DEFAULT '{}',
                connected_at TEXT,
                expires_at TEXT,
                status TEXT DEFAULT 'disconnected'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE app_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO synced_workouts
                ({LEGACY_ID_COLUMN}, garmin_activity_id, title, {LEGACY_UPDATED_COLUMN})
            VALUES
                ('legacy-1', '123', 'Legacy Push', '2026-04-01T10:00:00+00:00')
            """
        )
        conn.execute(
            f"""
            INSERT INTO hr_cache ({LEGACY_ID_COLUMN}, data)
            VALUES ('legacy-1', '{{"hr_samples":[{{"time":0,"hr":88}}]}}')
            """
        )
        conn.execute(
            f"""
            INSERT INTO custom_mappings ({LEGACY_NAME_COLUMN}, category, subcategory)
            VALUES ('Legacy Exercise', 5, 10)
            """
        )
        conn.execute(
            f"""
            INSERT INTO platform_credentials (platform, auth_type, credentials, status)
            VALUES ('{LEGACY_PLATFORM}', 'api_key', '{{"api_key":"legacy"}}', 'active')
            """
        )
        conn.execute(
            f"""
            INSERT INTO app_cache (key, value)
            VALUES
                ('{LEGACY_TOTAL_KEY}', '{{"count":7}}'),
                ('{LEGACY_PAGE_KEY}', '{{"workouts":[{{"id":"legacy-1"}}],"page_count":1}}')
            """
        )
        conn.commit()
        conn.close()

        db = SQLiteDatabase(db_path)

        assert db.is_synced("legacy-1") is True
        assert db.get_cached_hr("legacy-1")["hr_samples"][0]["hr"] == 88
        assert db.get_custom_mappings()["Legacy Exercise"] == (5, 10)
        assert db.get_app_config("workout_total") == {"count": 7}
        assert db.get_app_config("workouts_page_1")["workouts"][0]["id"] == "legacy-1"

        conn = sqlite3.connect(db_path)
        synced_columns = {row[1] for row in conn.execute("PRAGMA table_info(synced_workouts)").fetchall()}
        hr_columns = {row[1] for row in conn.execute("PRAGMA table_info(hr_cache)").fetchall()}
        mapping_columns = {row[1] for row in conn.execute("PRAGMA table_info(custom_mappings)").fetchall()}
        platforms = {row[0] for row in conn.execute("SELECT platform FROM platform_credentials").fetchall()}
        keys = {row[0] for row in conn.execute("SELECT key FROM app_cache").fetchall()}
        conn.close()

        assert "workout_id" in synced_columns
        assert "source_updated_at" in synced_columns
        assert LEGACY_ID_COLUMN not in synced_columns
        assert LEGACY_UPDATED_COLUMN not in synced_columns
        assert "workout_id" in hr_columns
        assert LEGACY_ID_COLUMN not in hr_columns
        assert "exercise_name" in mapping_columns
        assert LEGACY_NAME_COLUMN not in mapping_columns
        assert "liftosaur" in platforms
        assert LEGACY_PLATFORM not in platforms
        assert "workout_total" in keys
        assert LEGACY_TOTAL_KEY not in keys
        assert "workouts_page_1" in keys
        assert LEGACY_PAGE_KEY not in keys


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
class TestPostgresBackend:
    """Same tests as TestSyncTracking but against Postgres."""

    def test_not_synced_initially(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert db.is_synced("pg-unknown") is False

    def test_mark_then_check(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        db.mark_synced("pg-w1", garmin_activity_id="123", title="Push")
        assert db.is_synced("pg-w1") is True

    def test_count(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        assert db.get_synced_count() == 0
        db.mark_synced("pg-w1", title="Push")
        db.mark_synced("pg-w2", title="Pull")
        assert db.get_synced_count() == 2

    def test_idempotent_mark(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        db.mark_synced("pg-w1", garmin_activity_id="100", title="Push")
        db.mark_synced("pg-w1", garmin_activity_id="200", title="Push Updated")
        assert db.get_synced_count() == 1

    def test_stores_calories_and_hr(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        db.mark_synced("pg-w1", title="Push", calories=250, avg_hr=95)
        recent = db.get_recent_synced(limit=1)
        assert recent[0]["calories"] == 250
        assert recent[0]["avg_hr"] == 95

    def test_sync_log(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        db.record_sync_log(synced=5, skipped=2, failed=0, trigger="test")
        log = db.get_sync_log(limit=1)
        assert len(log) == 1
        assert log[0]["synced"] == 5

    def test_hr_cache(self, tmp_path: Path) -> None:
        db = _make_db(tmp_path)
        data = {"hr_samples": [{"time": 0, "hr": 85}]}
        db.cache_hr("pg-w1", data)
        cached = db.get_cached_hr("pg-w1")
        assert cached["hr_samples"][0]["hr"] == 85

    def test_postgres_migrates_legacy_schema(self, tmp_path: Path) -> None:
        import psycopg2

        database_url = os.environ["DATABASE_URL"]
        conn = psycopg2.connect(database_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS custom_mappings")
                cur.execute("DROP TABLE IF EXISTS platform_credentials")
                cur.execute("DROP TABLE IF EXISTS hr_cache")
                cur.execute("DROP TABLE IF EXISTS synced_workouts")
                cur.execute("DROP TABLE IF EXISTS app_cache")
                cur.execute(
                    f"""
                    CREATE TABLE synced_workouts (
                        {LEGACY_ID_COLUMN} TEXT PRIMARY KEY,
                        garmin_activity_id TEXT,
                        title TEXT,
                        synced_at TIMESTAMPTZ DEFAULT NOW(),
                        calories INTEGER,
                        avg_hr INTEGER,
                        status VARCHAR(20) DEFAULT 'success',
                        {LEGACY_UPDATED_COLUMN} TEXT
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE hr_cache (
                        {LEGACY_ID_COLUMN} TEXT PRIMARY KEY,
                        data JSONB NOT NULL,
                        cached_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE custom_mappings (
                        {LEGACY_NAME_COLUMN} TEXT PRIMARY KEY,
                        category INTEGER NOT NULL,
                        subcategory INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE platform_credentials (
                        platform VARCHAR(50) PRIMARY KEY,
                        auth_type VARCHAR(20) NOT NULL DEFAULT 'oauth',
                        credentials JSONB NOT NULL DEFAULT '{}',
                        connected_at TIMESTAMPTZ,
                        expires_at TIMESTAMPTZ,
                        status VARCHAR(20) DEFAULT 'disconnected'
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE app_cache (
                        key TEXT PRIMARY KEY,
                        value JSONB NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO synced_workouts
                        ({LEGACY_ID_COLUMN}, garmin_activity_id, title, {LEGACY_UPDATED_COLUMN})
                    VALUES
                        ('legacy-1', '123', 'Legacy Push', '2026-04-01T10:00:00+00:00')
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO hr_cache ({LEGACY_ID_COLUMN}, data)
                    VALUES ('legacy-1', '{"hr_samples":[{"time":0,"hr":88}]}'::jsonb)
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO custom_mappings ({LEGACY_NAME_COLUMN}, category, subcategory)
                    VALUES ('Legacy Exercise', 5, 10)
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                    VALUES ('{LEGACY_PLATFORM}', 'api_key', '{{"api_key":"legacy"}}'::jsonb, 'active')
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO app_cache (key, value)
                    VALUES
                        ('{LEGACY_TOTAL_KEY}', '{{"count":7}}'::jsonb),
                        ('{LEGACY_PAGE_KEY}', '{{"workouts":[{{"id":"legacy-1"}}],"page_count":1}}'::jsonb)
                    """
                )
        conn.close()

        db = _make_db(tmp_path)

        assert db.is_synced("legacy-1") is True
        assert db.get_cached_hr("legacy-1")["hr_samples"][0]["hr"] == 88
        assert db.get_custom_mappings()["Legacy Exercise"] == (5, 10)
        assert db.get_app_config("workout_total") == {"count": 7}
        assert db.get_app_config("workouts_page_1")["workouts"][0]["id"] == "legacy-1"

        conn = psycopg2.connect(database_url)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'synced_workouts'
                    """
                )
                synced_columns = {row[0] for row in cur.fetchall()}
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'hr_cache'
                    """
                )
                hr_columns = {row[0] for row in cur.fetchall()}
                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'custom_mappings'
                    """
                )
                mapping_columns = {row[0] for row in cur.fetchall()}
                cur.execute("SELECT platform FROM platform_credentials")
                platforms = {row[0] for row in cur.fetchall()}
                cur.execute("SELECT key FROM app_cache")
                keys = {row[0] for row in cur.fetchall()}
        conn.close()

        assert "workout_id" in synced_columns
        assert "source_updated_at" in synced_columns
        assert LEGACY_ID_COLUMN not in synced_columns
        assert LEGACY_UPDATED_COLUMN not in synced_columns
        assert "workout_id" in hr_columns
        assert LEGACY_ID_COLUMN not in hr_columns
        assert "exercise_name" in mapping_columns
        assert LEGACY_NAME_COLUMN not in mapping_columns
        assert "liftosaur" in platforms
        assert LEGACY_PLATFORM not in platforms
        assert "workout_total" in keys
        assert LEGACY_TOTAL_KEY not in keys
        assert "workouts_page_1" in keys
        assert LEGACY_PAGE_KEY not in keys


class TestDispatcher:
    def test_default_is_sqlite(self, monkeypatch, tmp_path: Path) -> None:
        """Without DATABASE_URL, get_db() returns SQLiteDatabase."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        from liftosaur2garmin import db
        db.reset()
        instance = db.get_db()
        assert isinstance(instance, SQLiteDatabase)
        db.reset()

    def test_reset_clears_singleton(self, monkeypatch) -> None:
        """reset() forces a fresh instance on next get_db()."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        from liftosaur2garmin import db
        db.reset()
        first = db.get_db()
        db.reset()
        second = db.get_db()
        assert first is not second
        db.reset()

    def test_module_wrappers_accept_db_path_kwarg(self, monkeypatch, tmp_path: Path) -> None:
        """Module-level functions silently accept db_path= for backwards compat."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        from liftosaur2garmin import db
        db.reset()
        # Patch the singleton to use tmp_path
        db._instance = SQLiteDatabase(tmp_path / "test.db")
        # These should not raise even with db_path= passed
        db.mark_synced("w1", title="Compat", db_path=tmp_path / "ignored.db")
        assert db.is_synced("w1", db_path=tmp_path / "ignored.db") is True
        assert db.get_synced_count(db_path=tmp_path / "ignored.db") == 1
        db.reset()
