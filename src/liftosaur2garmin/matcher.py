"""Match Hevy workouts to existing Garmin activities by start time.

Matching logic:
1. Time match: UTC start within ±30 minutes, closest wins (greedy)
2. Each Garmin activity matches at most ONE Hevy workout (1:1)
3. Date fallback: same calendar day ±1 + strength type for remaining unmatched
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta

logger = logging.getLogger("liftosaur2garmin")

# Cache to avoid hammering Garmin API on every page load
_garmin_activities_cache: list[dict] | None = None
_cache_count: int = 0
_cache_timestamp: float = 0
CACHE_TTL = 300  # 5 minutes

_matched_count_cache: int | None = None
_matched_count_timestamp: float = 0
MATCHED_COUNT_TTL = 600  # 10 minutes

STRENGTH_TYPES = {"strength_training", "indoor_cardio"}


def fetch_garmin_activities(client, count: int = 1000) -> list[dict]:
    """Fetch recent Garmin activities with caching."""
    global _garmin_activities_cache, _cache_count, _cache_timestamp

    cache_valid = (
        _garmin_activities_cache is not None
        and (_time.time() - _cache_timestamp) < CACHE_TTL
        and _cache_count >= count
    )
    if cache_valid:
        return _garmin_activities_cache  # type: ignore[return-value]

    try:
        from liftosaur2garmin.garmin import RateLimiter
        limiter = RateLimiter(delay=1.0)
        activities = limiter.call(client.get_activities, 0, count)
        _garmin_activities_cache = activities
        _cache_count = count
        _cache_timestamp = _time.time()
        return activities
    except Exception as e:
        logger.warning("Could not fetch Garmin activities: %s", e)
        return []


def count_matched_workouts(
    hevy_total: int,
    hevy_client,
    garmin_activities: list[dict],
) -> int:
    """Count how many Hevy workouts match a Garmin activity. Cached 10min."""
    global _matched_count_cache, _matched_count_timestamp

    if _matched_count_cache is not None and (_time.time() - _matched_count_timestamp) < MATCHED_COUNT_TTL:
        return _matched_count_cache

    all_workouts: list[dict] = []
    page = 1
    while True:
        data = hevy_client.get_workouts(page=page, page_size=10)
        workouts = data.get("workouts", [])
        if not workouts:
            break
        all_workouts.extend(workouts)
        page_count = data.get("page_count", 1)
        if page >= page_count:
            break
        page += 1

    matches = match_workouts_to_garmin(all_workouts, garmin_activities)
    _matched_count_cache = len(matches)
    _matched_count_timestamp = _time.time()
    return _matched_count_cache


def _parse_time(raw: str) -> datetime | None:
    """Parse various time formats to datetime."""
    if not raw:
        return None
    try:
        cleaned = raw.replace("Z", "+00:00")
        if "T" not in cleaned:
            cleaned = cleaned.replace(" ", "T")
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def match_workouts_to_garmin(
    workouts: list[dict],
    garmin_activities: list[dict],
    window_minutes: int = 30,
) -> dict[str, dict]:
    """Match Hevy workouts to Garmin activities.

    Strict 1:1: each Garmin activity matches at most one Hevy workout.
    Greedy best-match by time difference.
    Fallback: same calendar day ±1 with strength activity type.
    """
    # Pass 1: collect all time-based candidates
    candidates: list[tuple[str, int, float, dict]] = []  # (hevy_id, garmin_id, diff_s, act)

    for workout in workouts:
        hevy_id = workout.get("id", "")
        hevy_start_str = workout.get("start_time") or workout.get("startTime", "")
        hevy_start = _parse_time(hevy_start_str)
        if not hevy_start:
            continue
        hevy_naive = hevy_start.replace(tzinfo=None) if hevy_start.tzinfo else hevy_start

        for act in garmin_activities:
            act_start_str = act.get("startTimeGMT") or act.get("startTimeLocal", "")
            act_start = _parse_time(act_start_str)
            if not act_start:
                continue
            act_naive = act_start.replace(tzinfo=None) if act_start.tzinfo else act_start
            diff_seconds = abs((hevy_naive - act_naive).total_seconds())

            if diff_seconds < window_minutes * 60:
                candidates.append((hevy_id, act.get("activityId"), diff_seconds, act))

    # Greedy assignment: closest matches first, strict 1:1
    candidates.sort(key=lambda x: x[2])
    matches: dict[str, dict] = {}
    claimed_garmin: set[int] = set()
    claimed_hevy: set[str] = set()

    for hevy_id, garmin_id, diff_s, act in candidates:
        if hevy_id in claimed_hevy or garmin_id in claimed_garmin:
            continue
        matches[hevy_id] = {
            "garmin_id": garmin_id,
            "garmin_name": act.get("activityName", ""),
            "garmin_type": act.get("activityType", {}).get("typeKey", ""),
            "match_type": "time_match",
        }
        claimed_hevy.add(hevy_id)
        claimed_garmin.add(garmin_id)

    # Pass 2: date fallback for still-unmatched workouts
    garmin_by_date: dict[str, list[dict]] = {}
    for act in garmin_activities:
        gid = act.get("activityId")
        if gid in claimed_garmin:
            continue
        act_type = act.get("activityType", {}).get("typeKey", "")
        if act_type not in STRENGTH_TYPES:
            continue
        gmt = act.get("startTimeGMT", "")
        if gmt:
            garmin_by_date.setdefault(gmt[:10], []).append(act)

    for workout in workouts:
        hevy_id = workout.get("id", "")
        if hevy_id in claimed_hevy:
            continue
        hevy_start_str = workout.get("start_time") or workout.get("startTime", "")
        if not hevy_start_str:
            continue
        hevy_date = hevy_start_str[:10]
        hevy_dt = _parse_time(hevy_start_str)
        check_dates = {hevy_date}
        if hevy_dt:
            check_dates.add((hevy_dt - timedelta(days=1)).strftime("%Y-%m-%d"))
            check_dates.add((hevy_dt + timedelta(days=1)).strftime("%Y-%m-%d"))

        for d in check_dates:
            candidates_date = garmin_by_date.get(d, [])
            for act in candidates_date:
                gid = act.get("activityId")
                if gid in claimed_garmin:
                    continue
                matches[hevy_id] = {
                    "garmin_id": gid,
                    "garmin_name": act.get("activityName", ""),
                    "garmin_type": act.get("activityType", {}).get("typeKey", ""),
                    "match_type": "date_match",
                }
                claimed_hevy.add(hevy_id)
                claimed_garmin.add(gid)
                break
            if hevy_id in claimed_hevy:
                break

    return matches
