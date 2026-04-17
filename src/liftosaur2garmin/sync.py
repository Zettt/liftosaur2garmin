"""Sync orchestrator that pulls source workouts, builds FIT files, and uploads them to Garmin."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from liftosaur2garmin import db
from liftosaur2garmin.config import get_update_existing, load_config
from liftosaur2garmin.fit import generate_fit
from liftosaur2garmin.garmin import (
    find_activity_by_start_time,
    generate_description,
    get_client,
    get_exercise_sets,
    rename_activity,
    set_description,
    update_exercise_sets,
    upload_fit,
)
from liftosaur2garmin.liftosaur import LiftosaurClient
from liftosaur2garmin.mapper import lookup_exercise

logger = logging.getLogger("liftosaur2garmin")


def _remove_zero_rep_sets(garmin_sets: list[dict], workout: dict) -> list[dict]:
    """Remove ACTIVE sets with 0 reps that look like false watch detections.

    A 0-rep set is a false-detection candidate if it's short (<10s) and not
    present in the Liftosaur exercise. Per exercise, we walk Garmin's sets
    in order and pair each 0-rep Garmin set with a 0-rep Liftosaur set.

    The first K Garmin 0-rep sets are kept (matching Liftosaur's K 0-rep sets
    in order). Any further 0-rep Garmin sets are removed if they qualify as short.
    REST sets that immediately follow a removed ACTIVE set are also removed.
    """
    garmin_groups = _group_garmin_sets_by_exercise(garmin_sets)
    liftosaur_exercises = _merge_consecutive_liftosaur_exercises(workout.get("exercises", []))

    to_remove: set[int] = set()
    for ex_idx, group in enumerate(garmin_groups):
        liftosaur_zero_count = 0
        if ex_idx < len(liftosaur_exercises):
            liftosaur_zero_count = sum(
                1 for s in liftosaur_exercises[ex_idx].get("sets", [])
                if s.get("reps") == 0
            )

        matched = 0
        for entry in group:
            gs = entry["set"]
            if gs.get("repetitionCount", -1) != 0:
                continue
            if matched < liftosaur_zero_count:
                matched += 1
                continue
            if gs.get("duration", 0) >= 10:
                continue
            idx = entry["index"]
            to_remove.add(idx)
            if idx + 1 < len(garmin_sets) and garmin_sets[idx + 1].get("setType") == "REST":
                to_remove.add(idx + 1)

    if to_remove:
        count = sum(1 for i in to_remove if garmin_sets[i].get("setType") == "ACTIVE")
        logger.info("  Removing %d zero-rep sets from Garmin", count)
    return [gs for i, gs in enumerate(garmin_sets) if i not in to_remove]


def _group_garmin_sets_by_exercise(garmin_sets: list[dict]) -> list[list[dict]]:
    """Group consecutive ACTIVE Garmin sets by exercise identity.

    Returns a list of exercise groups, where each group is a list of
    dicts with 'index' (position in garmin_sets) and 'set' (the set dict).
    """
    groups: list[list[dict]] = []
    current_key: str | None = None
    current_group: list[dict] = []

    for i, gs in enumerate(garmin_sets):
        if gs.get("setType") != "ACTIVE":
            continue
        exercises = gs.get("exercises") or []
        key = (
            f"{exercises[0].get('category')}:{exercises[0].get('name')}"
            if exercises
            else f"__unknown_{i}"
        )
        if key != current_key:
            if current_group:
                groups.append(current_group)
            current_group = []
            current_key = key
        current_group.append({"index": i, "set": gs})

    if current_group:
        groups.append(current_group)

    return groups


def _merge_consecutive_liftosaur_exercises(exercises: list[dict]) -> list[dict]:
    """Merge consecutive Liftosaur exercises with the same title.

    Liftosaur can split the same exercise into multiple entries (e.g. warmup
    block + working block). Garmin sees them as one continuous exercise group.
    """
    if not exercises:
        return []
    merged: list[dict] = []
    for ex in exercises:
        title = ex.get("title") or ex.get("name", "")
        if merged and (merged[-1].get("title") or merged[-1].get("name", "")) == title:
            merged[-1]["sets"] = merged[-1].get("sets", []) + ex.get("sets", [])
        else:
            merged.append({**ex, "sets": list(ex.get("sets", []))})
    return merged


def _match_exercise_sets(
    garmin_active: list[dict],
    liftosaur_sets: list[dict],
) -> list[tuple[dict, dict]] | None:
    """Match Garmin active sets to Liftosaur sets for a single exercise.

    Tries two strategies:
      1. All sets (warmup + normal) match 1:1
      2. Only normal sets match (warmups absent in Garmin)

    Returns list of (garmin_entry, liftosaur_set) pairs, or None if no match.
    """
    warmup = [s for s in liftosaur_sets if s.get("type") == "warmup"]
    normal = [s for s in liftosaur_sets if s.get("type") != "warmup"]
    all_ordered = warmup + normal

    if len(garmin_active) == len(all_ordered):
        return list(zip(garmin_active, all_ordered))
    if len(garmin_active) == len(normal):
        return list(zip(garmin_active, normal))
    return None


def _apply_liftosaur_values(garmin_sets: list[dict], matches: list[tuple[dict, dict]]) -> None:
    """Update reps and weight in-place on the garmin_sets list."""
    for garmin_entry, liftosaur_set in matches:
        idx = garmin_entry["index"]
        reps = liftosaur_set.get("reps")
        weight_kg = liftosaur_set.get("weight_kg")
        if reps is not None:
            garmin_sets[idx]["repetitionCount"] = int(reps)
        if weight_kg is not None:
            garmin_sets[idx]["weight"] = round(float(weight_kg) * 1000)


def update_existing_activity_sets(garmin_client, activity_id: int, workout: dict) -> bool:
    """Fetch existing Garmin sets, match to Liftosaur, and update reps/weight.

    Returns True if sets were updated, False otherwise.
    """
    garmin_sets = get_exercise_sets(garmin_client, activity_id)
    if not garmin_sets:
        logger.info("  No exercise sets found on Garmin activity %s", activity_id)
        return False

    # Remove false-detection sets (0 reps)
    garmin_sets = _remove_zero_rep_sets(garmin_sets, workout)

    garmin_groups = _group_garmin_sets_by_exercise(garmin_sets)
    liftosaur_exercises = _merge_consecutive_liftosaur_exercises(workout.get("exercises", []))

    if not liftosaur_exercises:
        return False

    any_matched = False
    for ex_idx, liftosaur_ex in enumerate(liftosaur_exercises):
        ex_name = liftosaur_ex.get("title") or liftosaur_ex.get("name", "Unknown")
        if ex_idx >= len(garmin_groups):
            logger.warning("  No Garmin exercise group for Liftosaur exercise #%d '%s'", ex_idx, ex_name)
            continue

        garmin_group = garmin_groups[ex_idx]
        liftosaur_sets = liftosaur_ex.get("sets", [])
        matches = _match_exercise_sets(garmin_group, liftosaur_sets)

        if matches is None:
            warmup_count = sum(1 for s in liftosaur_sets if s.get("type") == "warmup")
            normal_count = len(liftosaur_sets) - warmup_count
            logger.warning(
                "  Set count mismatch for '%s': Garmin has %d, Liftosaur has %d (%d warmup + %d normal) — skipping",
                ex_name, len(garmin_group), len(liftosaur_sets), warmup_count, normal_count,
            )
            continue

        _apply_liftosaur_values(garmin_sets, matches)
        any_matched = True
        logger.info("  Matched %d sets for '%s'", len(matches), ex_name)

    if any_matched:
        update_exercise_sets(garmin_client, activity_id, garmin_sets)
        return True

    logger.warning("  No sets could be matched for activity %s", activity_id)
    return False


def fetch_workouts(
    client: LiftosaurClient,
    limit: int | None = None,
    since: str | None = None,
    fetch_all: bool = False,
) -> list[dict]:
    """Fetch workouts with optional limit, date filter, or full history.

    Args:
        client: Source client instance.
        limit: Max workouts to fetch (None = use default or all).
        since: ISO date string — stop fetching at this date.
        fetch_all: If True, paginate through entire history.
    """
    if not fetch_all and limit and limit <= 10:
        data = client.get_workouts(page=1, page_size=limit)
        return data.get("workouts", [])[:limit]

    all_workouts: list[dict] = []
    page = 1
    while True:
        page_size = min(10, limit - len(all_workouts)) if limit else 10
        if page_size <= 0:
            break
        data = client.get_workouts(page=page, page_size=page_size)
        workouts = data.get("workouts", [])
        if not workouts:
            break
        for w in workouts:
            start = w.get("start_time") or w.get("startTime", "")
            if since and start < since:
                logger.info("Reached date boundary (%s), stopping", since)
                return all_workouts
            all_workouts.append(w)
            if limit and len(all_workouts) >= limit:
                return all_workouts
        logger.info("  Fetched %d workouts so far...", len(all_workouts))
        if page >= data.get("page_count", page):
            break
        page += 1
    return all_workouts


def sync(
    config: dict[str, Any] | None = None,
    limit: int | None = None,
    since: str | None = None,
    fetch_all: bool = False,
    dry_run: bool = False,
    **overrides: Any,
) -> dict:
    """Sync Liftosaur workouts to Garmin Connect.

    Args:
        config: Config dict (loaded from file if None).
        limit: Max workouts to sync.
        since: ISO date — sync workouts after this date.
        fetch_all: Sync the entire workout history.
        dry_run: Generate FIT files but don't upload.
        **overrides: Override config values (liftosaur_api_key, garmin_email, garmin_password).

    Returns:
        Dict with sync stats: synced, skipped, failed, total, unmapped.
    """
    cfg = config or load_config()
    liftosaur_api_key = overrides.get("liftosaur_api_key") or cfg.get("liftosaur_api_key")
    garmin_email = overrides.get("garmin_email") or cfg.get("garmin_email")
    garmin_password = overrides.get("garmin_password") or cfg.get("garmin_password", "")
    garmin_token_dir = cfg.get("garmin_token_dir", "~/.garminconnect")
    skip_existing = cfg.get("sync", {}).get("skip_existing", True)
    update_existing, match_window = get_update_existing(cfg)

    if not limit and not fetch_all and not since:
        limit = cfg.get("sync", {}).get("default_limit", 10)

    client = LiftosaurClient(api_key=liftosaur_api_key)
    total_count = client.get_workout_count()
    logger.info("Liftosaur reports %d total workouts", total_count)

    workouts = fetch_workouts(client, limit=limit, since=since, fetch_all=fetch_all)
    logger.info("Fetched %d workouts to process", len(workouts))

    garmin_client = None
    if not dry_run:
        logger.info("Authenticating with Garmin Connect...")
        garmin_client = get_client(garmin_email, garmin_password, garmin_token_dir)
        logger.info("Authenticated successfully")

    stats = {"synced": 0, "skipped": 0, "failed": 0, "total": len(workouts), "unmapped": []}

    for workout in workouts:
        wid = workout.get("id", "unknown")
        title = workout.get("title", "Workout")
        start_time = workout.get("start_time") or workout.get("startTime", "")

        if skip_existing and db.is_synced(wid):
            logger.debug("Skipping %s (%s) — already synced", wid, title)
            stats["skipped"] += 1
            continue

        logger.info("Syncing: %s (%s)", title, wid)

        # Track unmapped exercises
        for ex in workout.get("exercises", []):
            ex_name = ex.get("title") or ex.get("name", "")
            cat, _, _ = lookup_exercise(ex_name)
            if cat == 65534 and ex_name not in stats["unmapped"]:
                stats["unmapped"].append(ex_name)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                fit_path = str(Path(tmp) / f"{wid}.fit")
                result = generate_fit(workout, hr_samples=None, output_path=fit_path)
                logger.info(
                    "  FIT: %d exercises, %d sets, %d cal",
                    result["exercises"], result["total_sets"], result["calories"],
                )

                if dry_run:
                    logger.info("  [DRY RUN] Would upload %s", fit_path)
                    stats["synced"] += 1
                    continue

                # Dedup: check if activity already exists on Garmin
                existing_id = find_activity_by_start_time(garmin_client, start_time, window_minutes=match_window) if (update_existing and start_time) else None
                if existing_id:
                    logger.info("  Activity already on Garmin (%s), updating sets", existing_id)
                    activity_id = existing_id
                    update_existing_activity_sets(garmin_client, activity_id, workout)
                else:
                    upload_result = upload_fit(garmin_client, fit_path, workout_start=start_time)
                    activity_id = upload_result.get("activity_id")

                if activity_id:
                    rename_activity(garmin_client, activity_id, title)
                    desc = generate_description(
                        workout,
                        calories=result.get("calories"),
                        avg_hr=result.get("avg_hr"),
                    )
                    set_description(garmin_client, activity_id, desc)

                db.mark_synced(
                    workout_id=wid,
                    garmin_activity_id=str(activity_id) if activity_id else None,
                    title=title,
                    calories=result.get("calories"),
                    avg_hr=result.get("avg_hr"),
                    source_updated_at=workout.get("updated_at"),
                )
                stats["synced"] += 1
                logger.info("  ✓ Synced → Garmin activity %s", activity_id)

        except Exception as e:
            logger.error("  ✗ Failed to sync %s: %s", wid, e)
            stats["failed"] += 1

    if stats["unmapped"]:
        logger.warning("\nUnmapped exercises: %s", ", ".join(stats["unmapped"]))
        logger.warning("Add custom mappings: liftosaur2garmin map \"Exercise Name\" --category N --subcategory N")

    # Record to sync log (shows up in dashboard)
    trigger = "cli"
    if os.environ.get("GITHUB_ACTIONS"):
        trigger = "github-actions"
    db.record_sync_log(
        synced=stats["synced"],
        skipped=stats["skipped"],
        failed=stats["failed"],
        trigger=trigger,
    )

    return stats
