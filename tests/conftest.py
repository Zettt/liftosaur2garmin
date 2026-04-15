"""Shared fixtures for liftosaur2garmin tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Temp directory for config files."""
    d = tmp_path / ".liftosaur2garmin"
    d.mkdir()
    return d


@pytest.fixture
def sample_workout() -> dict:
    """A realistic Liftosaur workout dict."""
    return {
        "id": "test-workout-123",
        "title": "Push",
        "start_time": "2026-04-01T20:00:00+00:00",
        "end_time": "2026-04-01T20:45:00+00:00",
        "exercises": [
            {
                "index": 0,
                "title": "Bench Press (Barbell)",
                "exercise_template_id": "79D0BB3A",
                "sets": [
                    {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 12},
                    {"index": 1, "type": "normal", "weight_kg": 60, "reps": 10},
                    {"index": 2, "type": "normal", "weight_kg": 60, "reps": 8},
                    {"index": 3, "type": "normal", "weight_kg": 60, "reps": 7},
                ],
            },
            {
                "index": 1,
                "title": "Shoulder Press (Dumbbell)",
                "exercise_template_id": "878CD1D0",
                "sets": [
                    {"index": 0, "type": "normal", "weight_kg": 14, "reps": 12},
                    {"index": 1, "type": "normal", "weight_kg": 14, "reps": 10},
                ],
            },
        ],
    }


@pytest.fixture
def sample_workout_unmapped() -> dict:
    """Workout with an unmapped exercise."""
    return {
        "id": "test-unmapped-456",
        "title": "Custom Day",
        "start_time": "2026-04-02T18:00:00+00:00",
        "end_time": "2026-04-02T18:30:00+00:00",
        "exercises": [
            {
                "index": 0,
                "title": "Invented Exercise 99",
                "sets": [
                    {"index": 0, "type": "normal", "weight_kg": 20, "reps": 10},
                ],
            },
        ],
    }


@pytest.fixture
def sample_profile() -> dict:
    """User profile for FIT generation."""
    return {
        "weight_kg": 78.0,
        "birth_year": 1994,
        "vo2max": 50.0,
        "working_set_s": 40,
        "warmup_set_s": 25,
        "rest_sets_s": 75,
        "rest_exercises_s": 120,
    }
