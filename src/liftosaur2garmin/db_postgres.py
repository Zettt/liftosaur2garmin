"""PostgreSQL implementation of the Database interface."""

from __future__ import annotations

import json
from datetime import datetime

from liftosaur2garmin.db_interface import Database


def _ts_newer(new_ts: str, old_ts: str) -> bool:
    """Compare ISO timestamps safely."""
    try:
        new_dt = datetime.fromisoformat(new_ts.replace("Z", "+00:00"))
        old_dt = datetime.fromisoformat(old_ts.replace("Z", "+00:00"))
        return new_dt > old_dt
    except (ValueError, TypeError):
        return new_ts > old_ts


class PostgresDatabase(Database):
    """Postgres-backed storage for tracking synced workouts."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._conn_cache = None
        self._ensure_tables()

    def _get_conn(self):
        import psycopg2
        from psycopg2.extras import RealDictCursor

        if self._conn_cache is not None:
            try:
                self._conn_cache.cursor().execute("SELECT 1")
                return self._conn_cache
            except Exception:
                try:
                    self._conn_cache.close()
                except Exception:
                    pass
                self._conn_cache = None

        conn = psycopg2.connect(self.database_url, cursor_factory=RealDictCursor)
        self._conn_cache = conn
        return conn

    def _column_names(self, conn, table: str) -> set[str]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (table,),
            )
            return {row["column_name"] for row in cur.fetchall()}

    def _rename_column(self, conn, table: str, old_name: str, new_name: str) -> None:
        columns = self._column_names(conn, table)
        if old_name in columns and new_name not in columns:
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")

    def _rename_app_cache_key(self, conn, old_key: str, new_key: str) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_cache WHERE key = %s", (old_key,))
            row = cur.fetchone()
            if row is None:
                return
            cur.execute("SELECT 1 FROM app_cache WHERE key = %s", (new_key,))
            if cur.fetchone() is None:
                cur.execute(
                    """
                    INSERT INTO app_cache (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    (new_key, row["value"]),
                )
            cur.execute("DELETE FROM app_cache WHERE key = %s", (old_key,))

    def _migrate_schema(self) -> None:
        legacy_prefix = "he" "vy"
        legacy_id_column = f"{legacy_prefix}_id"
        legacy_updated_column = f"{legacy_prefix}_updated_at"
        legacy_name_column = f"{legacy_prefix}_name"
        legacy_total_key = f"{legacy_prefix}_total"
        legacy_page_prefix = f"{legacy_prefix}_workouts_page_"
        with self._get_conn() as conn:
            self._rename_column(conn, "synced_workouts", legacy_id_column, "workout_id")
            self._rename_column(conn, "synced_workouts", legacy_updated_column, "source_updated_at")
            self._rename_column(conn, "hr_cache", legacy_id_column, "workout_id")
            self._rename_column(conn, "custom_mappings", legacy_name_column, "exercise_name")

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT credentials FROM platform_credentials WHERE platform = %s LIMIT 1",
                    (legacy_prefix,),
                )
                legacy = cur.fetchone()
                if legacy is not None:
                    cur.execute(
                        "SELECT 1 FROM platform_credentials WHERE platform = 'liftosaur' LIMIT 1"
                    )
                    if cur.fetchone() is None:
                        cur.execute(
                            """
                            INSERT INTO platform_credentials (platform, auth_type, credentials, status)
                            VALUES ('liftosaur', 'api_key', %s, 'active')
                            """,
                            (legacy["credentials"],),
                        )
                    cur.execute("DELETE FROM platform_credentials WHERE platform = %s", (legacy_prefix,))

            self._rename_app_cache_key(conn, legacy_total_key, "workout_total")

            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM app_cache WHERE key LIKE %s", (f"{legacy_page_prefix}%",))
                page_rows = cur.fetchall()
                for row in page_rows:
                    new_key = row["key"].replace(legacy_page_prefix, "workouts_page_", 1)
                    cur.execute("SELECT 1 FROM app_cache WHERE key = %s", (new_key,))
                    if cur.fetchone() is None:
                        cur.execute(
                            """
                            INSERT INTO app_cache (key, value)
                            VALUES (%s, %s)
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                            """,
                            (new_key, row["value"]),
                        )
                    cur.execute("DELETE FROM app_cache WHERE key = %s", (row["key"],))
            conn.commit()

    def _ensure_tables(self) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS synced_workouts (
                        workout_id TEXT PRIMARY KEY,
                        garmin_activity_id TEXT,
                        title TEXT,
                        synced_at TIMESTAMPTZ DEFAULT NOW(),
                        calories INTEGER,
                        avg_hr INTEGER,
                        status VARCHAR(20) DEFAULT 'success',
                        source_updated_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sync_log (
                        id BIGSERIAL PRIMARY KEY,
                        time TIMESTAMPTZ DEFAULT NOW(),
                        synced INTEGER DEFAULT 0,
                        skipped INTEGER DEFAULT 0,
                        failed INTEGER DEFAULT 0,
                        trigger VARCHAR(50) DEFAULT 'manual'
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hr_cache (
                        workout_id TEXT PRIMARY KEY,
                        data JSONB NOT NULL,
                        cached_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS platform_credentials (
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
                    CREATE TABLE IF NOT EXISTS custom_mappings (
                        exercise_name TEXT PRIMARY KEY,
                        category INTEGER NOT NULL,
                        subcategory INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_cache (
                        key TEXT PRIMARY KEY,
                        value JSONB NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
            conn.commit()
        self._migrate_schema()

    def is_synced(self, workout_id: str) -> bool:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM synced_workouts WHERE workout_id = %s", (workout_id,))
                return cur.fetchone() is not None

    def get_synced_ids(self, workout_ids: list[str]) -> dict[str, str | None]:
        if not workout_ids:
            return {}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workout_id, garmin_activity_id
                    FROM synced_workouts
                    WHERE workout_id = ANY(%s)
                    """,
                    (workout_ids,),
                )
                return {row["workout_id"]: row["garmin_activity_id"] for row in cur.fetchall()}

    def get_garmin_id(self, workout_id: str) -> str | None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT garmin_activity_id FROM synced_workouts WHERE workout_id = %s",
                    (workout_id,),
                )
                row = cur.fetchone()
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
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO synced_workouts (
                        workout_id, garmin_activity_id, title, calories, avg_hr, source_updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (workout_id) DO UPDATE SET
                        garmin_activity_id = EXCLUDED.garmin_activity_id,
                        title = EXCLUDED.title,
                        calories = EXCLUDED.calories,
                        avg_hr = EXCLUDED.avg_hr,
                        source_updated_at = EXCLUDED.source_updated_at,
                        synced_at = NOW()
                    """,
                    (workout_id, garmin_activity_id, title, calories, avg_hr, source_updated_at),
                )
            conn.commit()

    def get_stale_synced(self, workouts: list[dict]) -> list[str]:
        """Return synced workout IDs edited in Liftosaur since sync."""
        if not workouts:
            return []
        workout_ids = [workout.get("id", "") for workout in workouts]
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workout_id, source_updated_at
                    FROM synced_workouts
                    WHERE workout_id = ANY(%s) AND source_updated_at IS NOT NULL
                    """,
                    (workout_ids,),
                )
                stored = {row["workout_id"]: row["source_updated_at"] for row in cur.fetchall()}
        stale: list[str] = []
        for workout in workouts:
            workout_id = workout.get("id", "")
            old_ts = stored.get(workout_id)
            new_ts = workout.get("updated_at") or ""
            if old_ts and new_ts and _ts_newer(new_ts, old_ts):
                stale.append(workout_id)
        return stale

    def unsync(self, workout_id: str) -> bool:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM synced_workouts WHERE workout_id = %s", (workout_id,))
                deleted = cur.rowcount > 0
            conn.commit()
        return deleted

    def unsync_all(self) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM synced_workouts")
                count = cur.rowcount
            conn.commit()
        return count

    def get_synced_count(self) -> int:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM synced_workouts")
                return cur.fetchone()["count"]

    def get_recent_synced(self, limit: int = 10) -> list[dict]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM synced_workouts ORDER BY synced_at DESC LIMIT %s",
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    def record_sync_log(
        self,
        synced: int = 0,
        skipped: int = 0,
        failed: int = 0,
        trigger: str = "manual",
    ) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sync_log (synced, skipped, failed, trigger) VALUES (%s, %s, %s, %s)",
                    (synced, skipped, failed, trigger),
                )
            conn.commit()

    def get_sync_log(self, limit: int = 20) -> list[dict]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT %s", (limit,))
                return [dict(row) for row in cur.fetchall()]

    def get_cached_hr(self, workout_id: str) -> dict | None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM hr_cache WHERE workout_id = %s", (workout_id,))
                row = cur.fetchone()
                if row:
                    data = row["data"]
                    return json.loads(data) if isinstance(data, str) else data
                return None

    def cache_hr(self, workout_id: str, data: dict) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO hr_cache (workout_id, data) VALUES (%s, %s)
                    ON CONFLICT (workout_id) DO UPDATE SET data = EXCLUDED.data, cached_at = NOW()
                    """,
                    (workout_id, json.dumps(data)),
                )
            conn.commit()

    def get_app_config(self, key: str) -> dict | None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_cache WHERE key = %s", (key,))
                row = cur.fetchone()
                if row:
                    value = row["value"]
                    return json.loads(value) if isinstance(value, str) else value
                return None

    def set_app_config(self, key: str, value: dict) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_cache (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    (key, json.dumps(value)),
                )
            conn.commit()

    def get_custom_mappings(self) -> dict[str, tuple[int, int]]:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT exercise_name, category, subcategory FROM custom_mappings")
                return {
                    row["exercise_name"]: (row["category"], row["subcategory"])
                    for row in cur.fetchall()
                }

    def save_custom_mapping(self, exercise_name: str, category: int, subcategory: int) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO custom_mappings (exercise_name, category, subcategory)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (exercise_name) DO UPDATE SET
                        category = EXCLUDED.category,
                        subcategory = EXCLUDED.subcategory
                    """,
                    (exercise_name, category, subcategory),
                )
            conn.commit()

    def delete_custom_mapping(self, exercise_name: str) -> None:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM custom_mappings WHERE exercise_name = %s", (exercise_name,))
            conn.commit()
