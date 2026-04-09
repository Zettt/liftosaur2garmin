"""Parse Liftosaur history records into workout dicts used by the app."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

_HEADER_SPLIT_RE = re.compile(r' / (?=(?:[^"]*"[^"]*")*[^"]*$)')
_SET_RE = re.compile(
    r"""
    ^\s*
    (?P<count>\d+)x
    (?P<right>\d+)
    (?:
        \|(?P<left>\d+)
      | -(?P<range_max>\d+)
    )?
    (?P<amrap>\+)?
    (?:\s+(?P<weight>\d+(?:\.\d+)?)(?P<unit>kg|lb))?
    (?:\s+@(?P<rpe>\d+(?:\.\d+)?)(?P<logged>\+)?)?
    (?:\s+(?P<timer>\d+)s)?
    \s*$
    """,
    re.VERBOSE,
)
_EXERCISE_SECTION_RE = re.compile(r" / (?=(?:warmup|target): )")

_LB_TO_KG = 0.45359237


def parse_history_record(record: dict) -> dict:
    """Convert a Liftosaur history record to the normalized workout schema."""
    text = str(record.get("text", "")).strip()
    if not text:
        raise ValueError("History record is missing text")

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[-1].strip() == "}":
        raise ValueError("History record does not contain a valid exercises block")

    header = lines[0].strip()
    body_lines = [line.strip() for line in lines[1:-1] if not line.lstrip().startswith("//")]

    metadata = _parse_header(header)
    exercises = [_parse_exercise_line(line, index) for index, line in enumerate(body_lines) if not line.startswith("//")]
    start_time = _parse_datetime(metadata["date"])
    duration_seconds = metadata.get("duration_seconds") or _estimate_duration_seconds(exercises)
    end_time = start_time + timedelta(seconds=duration_seconds)

    title = _build_title(metadata)

    return {
        "id": str(record.get("id", metadata["date"])),
        "title": title,
        "start_time": _format_datetime(start_time),
        "end_time": _format_datetime(end_time),
        "updated_at": _format_datetime(start_time),
        "description": text,
        "source_text": text,
        "program_name": metadata.get("program"),
        "day_name": metadata.get("dayName") or "Workout",
        "duration_seconds": duration_seconds,
        "exercises": exercises,
    }


def _parse_header(header: str) -> dict:
    prefix = " / exercises: {"
    if not header.endswith(prefix):
        raise ValueError("History header is missing the exercises block start")

    parts = _HEADER_SPLIT_RE.split(header[: -len(prefix)])
    if not parts:
        raise ValueError("History header is empty")

    metadata: dict[str, object] = {"date": parts[0]}
    for part in parts[1:]:
        if ": " not in part:
            continue
        key, raw_value = part.split(": ", 1)
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            metadata[key] = value[1:-1]
        elif key == "duration" and value.endswith("s"):
            metadata["duration_seconds"] = int(value[:-1])
        elif key in {"week", "dayInWeek", "day"}:
            metadata[key] = int(value)
        else:
            metadata[key] = value
    return metadata


def _parse_exercise_line(line: str, index: int) -> dict:
    sections = line.split(" / ")
    if len(sections) < 2:
        raise ValueError(f"Invalid exercise line: {line}")

    title = sections[0].strip()
    completed_sets: list[dict] = []
    warmup_sets: list[dict] = []
    target_sets: list[dict] = []

    for section in sections[1:]:
        if section.startswith("warmup: "):
            warmup_sets.extend(_parse_sets(section[len("warmup: ") :], set_type="warmup"))
        elif section.startswith("target: "):
            target_sets.extend(_parse_sets(section[len("target: ") :], set_type="target"))
        else:
            completed_sets.extend(_parse_sets(section, set_type="normal"))

    merged_sets = _merge_sets(completed_sets, target_sets)
    return {
        "index": index,
        "title": title,
        "sets": warmup_sets + merged_sets,
    }


def _parse_sets(raw_sets: str, set_type: str) -> list[dict]:
    items = [item.strip() for item in raw_sets.split(",") if item.strip() and item.strip().lower() != "none"]
    sets: list[dict] = []
    for item in items:
        match = _SET_RE.match(item)
        if not match:
            continue
        count = int(match.group("count"))
        right_reps = int(match.group("right"))
        left_reps = match.group("left")
        range_max = match.group("range_max")
        reps = int(range_max or right_reps)
        if left_reps is not None:
            reps = max(right_reps, int(left_reps))

        weight_value = match.group("weight")
        weight_kg = None
        if weight_value is not None:
            weight = float(weight_value)
            weight_kg = weight if match.group("unit") == "kg" else weight * _LB_TO_KG

        timer_value = match.group("timer")
        timer_seconds = int(timer_value) if timer_value is not None else None

        for _ in range(count):
            set_index = len(sets)
            sets.append(
                {
                    "index": set_index,
                    "type": "warmup" if set_type == "warmup" else "normal",
                    "reps": reps,
                    "weight_kg": weight_kg,
                    "timer_seconds": timer_seconds,
                    "is_amrap": bool(match.group("amrap")),
                }
            )
    return sets


def _merge_sets(completed_sets: list[dict], target_sets: list[dict]) -> list[dict]:
    if not target_sets:
        return completed_sets
    if not completed_sets:
        return target_sets

    merged: list[dict] = []
    for index in range(max(len(completed_sets), len(target_sets))):
        completed = completed_sets[index] if index < len(completed_sets) else None
        target = target_sets[index] if index < len(target_sets) else None
        if completed and target:
            merged.append(
                {
                    **completed,
                    "target_reps": target.get("reps"),
                    "timer_seconds": target.get("timer_seconds") or completed.get("timer_seconds"),
                }
            )
        elif completed:
            merged.append(completed)
        elif target:
            merged.append(target)
    return merged


def _estimate_duration_seconds(exercises: list[dict]) -> int:
    if not exercises:
        return 30 * 60

    working_sets = 0
    warmup_sets = 0
    timed_rest_seconds = 0

    for exercise in exercises:
        for workout_set in exercise.get("sets", []):
            if workout_set.get("type") == "warmup":
                warmup_sets += 1
            else:
                working_sets += 1
            timed_rest_seconds += workout_set.get("timer_seconds") or 0

    rest_between_sets = max(working_sets + warmup_sets - len(exercises), 0) * 75
    rest_between_exercises = max(len(exercises) - 1, 0) * 120
    active_seconds = working_sets * 40 + warmup_sets * 25
    estimated = active_seconds + rest_between_sets + rest_between_exercises

    if timed_rest_seconds:
        estimated = max(estimated, active_seconds + timed_rest_seconds)

    return max(int(math.ceil(estimated)), 10 * 60)


def _build_title(metadata: dict) -> str:
    day_name = str(metadata.get("dayName") or "").strip()
    program = str(metadata.get("program") or "").strip()
    if day_name and program and day_name != program:
        return f"{program}: {day_name}"
    if day_name:
        return day_name
    if program:
        return program
    return "Workout"


def _parse_datetime(raw: str) -> datetime:
    cleaned = raw.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
