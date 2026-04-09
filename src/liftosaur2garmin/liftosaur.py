"""Liftosaur API client with normalized workout output."""

from __future__ import annotations

import logging
import math
import os
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from liftosaur2garmin.env import load_local_env
from liftosaur2garmin.parser import parse_history_record

logger = logging.getLogger("liftosaur2garmin")


class LiftosaurAuthError(Exception):
    """Raised when the Liftosaur API rejects the API key."""

DEFAULT_BASE_URL = "https://www.liftosaur.com/api/v1"
API_CALL_DELAY = 0.5


class LiftosaurClient:
    """HTTP client for the Liftosaur REST API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        load_local_env()
        self.base_url = (base_url or os.environ.get("LIFTOSAUR_API_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        key = api_key or os.environ.get("LIFTOSAUR_API_KEY", "") or os.environ.get("HEVY_API_KEY", "")
        if not key:
            raise ValueError("Liftosaur API key required. Pass api_key= or set LIFTOSAUR_API_KEY.")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {key}",
                "api-key": key,
                "Accept": "application/json",
            }
        )
        retry = Retry(
            total=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _get(self, path: str, params: dict | None = None) -> dict:
        """Make a GET request with rate limiting."""
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        if resp.status_code in (401, 403):
            raise LiftosaurAuthError(
                "Liftosaur API key is invalid or expired. "
                "Create a fresh key in Liftosaur Settings > API Keys."
            )
        resp.raise_for_status()
        remaining = resp.headers.get("X-RateLimit-Remaining") or resp.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            try:
                rem = int(remaining)
                if rem < 10:
                    logger.warning("Liftosaur API rate limit low: %d requests remaining", rem)
            except ValueError:
                pass
        time.sleep(API_CALL_DELAY)
        return resp.json()

    def get_workout_count(self) -> int:
        """Count total history records by paging through the API."""
        total = 0
        cursor: str | None = None
        while True:
            data = self._get_history(limit=200, cursor=cursor)
            if "workout_count" in data:
                return int(data["workout_count"])
            records = data.get("records", [])
            total += len(records)
            if not data.get("hasMore"):
                return total
            cursor = data.get("nextCursor")
            if cursor is None:
                return total

    def get_workouts(self, page: int = 1, page_size: int = 10) -> dict:
        """Return normalized workouts using a page/page_size interface."""
        cursor: str | None = None
        current_page = 1
        page_data: dict | None = None
        while current_page <= page:
            page_data = self._get_history(limit=page_size, cursor=cursor)
            if current_page == page:
                break
            if not page_data.get("hasMore"):
                break
            cursor = page_data.get("nextCursor")
            current_page += 1

        if page_data is None:
            page_data = self._get_history(limit=page_size, cursor=None)

        if "workouts" in page_data:
            return page_data

        total_count = self.get_workout_count()
        workouts = [parse_history_record(record) for record in page_data.get("records", [])]
        return {
            "workouts": workouts,
            "page_count": max(1, math.ceil(total_count / page_size)) if total_count else 0,
            "has_more": bool(page_data.get("hasMore")),
            "next_cursor": page_data.get("nextCursor"),
            "total_count": total_count,
        }

    def get_all_workouts(self, since_page: int = 1, page_size: int = 10) -> list[dict]:
        """Fetch all normalized workouts."""
        all_workouts: list[dict] = []
        page = since_page
        while True:
            data = self.get_workouts(page, page_size)
            workouts = data.get("workouts", [])
            all_workouts.extend(workouts)
            logger.info("  Page %d/%d — %d workouts", page, data.get("page_count", "?"), len(workouts))
            if page >= data.get("page_count", page):
                break
            page += 1
        return all_workouts

    def get_history_record(self, record_id: str | int) -> dict:
        """Fetch and normalize a single history record."""
        data = self._get(f"/history/{record_id}")
        return parse_history_record(data["data"])

    def get_exercise_templates(self, page: int = 1, page_size: int = 10) -> dict:
        """Compatibility shim for older tests and callers."""
        return self._get("/exercise_templates", {"page": page, "pageSize": page_size})

    def get_workout_events(self, since: str, page: int = 1, page_size: int = 10) -> dict:
        """Compatibility shim for older tests and callers."""
        return self._get("/workouts/events", {"since": since, "page": page, "pageSize": page_size})

    def _get_history(
        self,
        limit: int = 50,
        cursor: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        payload: dict[str, object] = {"limit": limit}
        if cursor is not None:
            payload["cursor"] = cursor
        if start_date:
            payload["startDate"] = start_date
        if end_date:
            payload["endDate"] = end_date
        data = self._get("/history", payload)
        if "data" in data and isinstance(data["data"], dict):
            return data["data"]
        return data


HevyAuthError = LiftosaurAuthError
HevyClient = LiftosaurClient
