"""SQLite implementation of the Database interface."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from liftosaur2garmin.db_interface import Database


def _ts_newer(new_ts: str, old_ts: str) -> bool:
    """Compare ISO timestamps safely."""
    try:
        new_dt = datetime.fromisoformat(new_ts.replace("Z", "+00:00"))
        old_dt = datetime.fromisoformat(old_ts.replace("Z", "+00:00"))
        return new_dt > old_dt
    except (ValueError, TypeError):
        return new_ts > old_ts


DEFAULT_DB_PATH = Path("~/.liftosaur2garmin/sync.db").expanduser()
_LEGACY_PREFIX = "he" "vy"
_LEGACY_ID_COLUMN = f"{_LEGACY_PREFIX}_id"
_LEGACY_UPDATED_COLUMN = f"{_LEGACY_PREFIX}_updated_at"
_LEGACY_NAME_COLUMN = f"{_LEGACY_PREFIX}_name"
_LEGACY_PLATFORM = _LEGACY_PREFIX
_LEGACY_TOTAL_KEY = f"{_LEGACY_PREFIX}_total"
_LEGACY_PAGE_PREFIX = f"{_LEGACY_PREFIX}_workouts_page_"


class SQLiteDatabase(Database):
    """SQLite-backed storage for tracking synced workouts."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)

    def _get_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS synced_workouts (
                workout_id TEXT PRIMARY KEY,
                garmin_activity_id TEXT,
                title TEXT,
                synced_at TEXT DEFAULT (datetime('now')),
                calories INTEGER,
                avg_hr INTEGER,
                status TEXT DEFAULT 'success',
                source_updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT DEFAULT (datetime('now')),
                synced INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                trigger TEXT DEFAULT 'manual'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hr_cache (
                workout_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                cached_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_mappings (
                exercise_name TEXT PRIMARY KEY,
                category INTEGER NOT NULL,
                subcategory INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_credentials (
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
            CREATE TABLE IF NOT EXISTS app_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self._migrate_schema(conn)
        conn.commit()
        return conn

    def _column_names(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def _rename_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        old_name: str,
        new_name: str,
    ) -> None:
        columns = self._column_names(conn, table)
        if old_name in columns and new_name not in columns:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")

    def _rename_app_cache_key(
        self,
        conn: sqlite3.Connection,
        old_key: str,
        new_key: str,
    ) -> None:
        row = conn.execute("SELECT value FROM app_cache WHERE key = ?", (old_key,)).fetchone()
        if row is None:
            return
        existing = conn.execute("SELECT 1 FROM app_cache WHERE key = ?", (new_key,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO app_cache (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (new_key, row["value"]),
            )
        conn.execute("DELETE FROM app_cache WHERE key = ?", (old_key,))

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        self._rename_column(conn, "synced_workouts", _LEGACY_ID_COLUMN, "workout_id")
        self._rename_column(conn, "synced_workouts", _LEGACY_UPDATED_COLUMN, "source_updated_at")
        self._rename_column(conn, "hr_cache", _LEGACY_ID_COLUMN, "workout_id")
        self._rename_column(conn, "custom_mappings", _LEGACY_NAME_COLUMN, "exercise_name")

        row = conn.execute(
            "SELECT credentials FROM platform_credentials WHERE platform = ?",
            (_LEGACY_PLATFORM,),
        ).fetchone()
        if row is not None:
            existing = conn.execute(
                "SELECT 1 FROM platform_credentials WHERE platform = 'liftosaur'"
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                    VALUES ('liftosaur', 'api_key', ?, 'active')
                    """,
                    (row["credentials"],),
                )
            conn.execute("DELETE FROM platform_credentials WHERE platform = ?", (_LEGACY_PLATFORM,))

        self._rename_app_cache_key(conn, _LEGACY_TOTAL_KEY, "workout_total")

        page_rows = conn.execute(
            "SELECT key, value FROM app_cache WHERE key LIKE ?",
            (f"{_LEGACY_PAGE_PREFIX}%",),
        ).fetchall()
        for row in page_rows:
            new_key = row["key"].replace(_LEGACY_PAGE_PREFIX, "workouts_page_", 1)
            existing = conn.execute("SELECT 1 FROM app_cache WHERE key = ?", (new_key,)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO app_cache (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                    (new_key, row["value"]),
                )
            conn.execute("DELETE FROM app_cache WHERE key = ?", (row["key"],))

    def is_synced(self, workout_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM synced_workouts WHERE workout_id = ?",
            (workout_id,),
        ).fetchone()
        conn.close()
        return row is not None

    def get_synced_ids(self, workout_ids: list[str]) -> dict[str, str | None]:
        if not workout_ids:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in workout_ids)
        rows = conn.execute(
            f"SELECT workout_id, garmin_activity_id FROM synced_workouts WHERE workout_id IN ({placeholders})",
            workout_ids,
        ).fetchall()
        conn.close()
        return {row["workout_id"]: row["garmin_activity_id"] for row in rows}

    def get_garmin_id(self, workout_id: str) -> str | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT garmin_activity_id FROM synced_workouts WHERE workout_id = ?",
            (workout_id,),
        ).fetchone()
        conn.close()
        return row["garmin_activity_id"] if row else None

    def mark_synced(
        self,
        workout_id: str,
        garmin_activity_id: str | None = None,
        title: str = "",
        calories: int | None = None,
        avg_hr: int | None = None,
        source_updated_at: str | None = None,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO synced_workouts (
                workout_id, garmin_activity_id, title, calories, avg_hr, source_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workout_id, garmin_activity_id, title, calories, avg_hr, source_updated_at),
        )
        conn.commit()
        conn.close()

    def get_stale_synced(self, workouts: list[dict]) -> list[str]:
        """Return synced workout IDs edited in Liftosaur since sync."""
        if not workouts:
            return []
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in workouts)
        workout_ids = [workout.get("id", "") for workout in workouts]
        rows = conn.execute(
            f"""
            SELECT workout_id, source_updated_at
            FROM synced_workouts
            WHERE workout_id IN ({placeholders}) AND source_updated_at IS NOT NULL
            """,
            workout_ids,
        ).fetchall()
        conn.close()
        stored = {row["workout_id"]: row["source_updated_at"] for row in rows}
        stale: list[str] = []
        for workout in workouts:
            workout_id = workout.get("id", "")
            old_ts = stored.get(workout_id)
            new_ts = workout.get("updated_at") or ""
            if old_ts and new_ts and _ts_newer(new_ts, old_ts):
                stale.append(workout_id)
        return stale

    def unsync(self, workout_id: str) -> bool:
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM synced_workouts WHERE workout_id = ?", (workout_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def unsync_all(self) -> int:
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM synced_workouts")
        count = cur.rowcount
        conn.commit()
        conn.close()
        return count

    def get_synced_count(self) -> int:
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) AS count FROM synced_workouts").fetchone()["count"]
        conn.close()
        return count

    def get_recent_synced(self, limit: int = 10) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM synced_workouts ORDER BY synced_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def record_sync_log(
        self,
        synced: int = 0,
        skipped: int = 0,
        failed: int = 0,
        trigger: str = "manual",
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO sync_log (synced, skipped, failed, trigger) VALUES (?, ?, ?, ?)",
            (synced, skipped, failed, trigger),
        )
        conn.commit()
        conn.close()

    def get_sync_log(self, limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_cached_hr(self, workout_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT data FROM hr_cache WHERE workout_id = ?",
            (workout_id,),
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row["data"])
        return None

    def cache_hr(self, workout_id: str, data: dict) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO hr_cache (workout_id, data) VALUES (?, ?)",
            (workout_id, json.dumps(data)),
        )
        conn.commit()
        conn.close()

    def get_app_config(self, key: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute("SELECT value FROM app_cache WHERE key = ?", (key,)).fetchone()
        conn.close()
        if row:
            return json.loads(row["value"])
        return None

    def set_app_config(self, key: str, value: dict) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO app_cache (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            """,
            (key, json.dumps(value)),
        )
        conn.commit()
        conn.close()

    def get_custom_mappings(self) -> dict[str, tuple[int, int]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT exercise_name, category, subcategory FROM custom_mappings"
        ).fetchall()
        conn.close()
        return {row["exercise_name"]: (row["category"], row["subcategory"]) for row in rows}

    def save_custom_mapping(self, exercise_name: str, category: int, subcategory: int) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO custom_mappings (exercise_name, category, subcategory)
            VALUES (?, ?, ?)
            """,
            (exercise_name, category, subcategory),
        )
        conn.commit()
        conn.close()

    def delete_custom_mapping(self, exercise_name: str) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM custom_mappings WHERE exercise_name = ?", (exercise_name,))
        conn.commit()
        conn.close()
