"""Tests for FIT file generator."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from liftosaur2garmin.fit import generate_fit


class TestFITGeneration:
    def test_generates_file(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "test.fit")
        result = generate_fit(sample_workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert os.path.exists(path)
        assert os.path.getsize(path) > 0

    def test_returns_correct_structure(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "test.fit")
        result = generate_fit(sample_workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert "exercises" in result
        assert "total_sets" in result
        assert "calories" in result
        assert "duration_s" in result
        assert "output_path" in result

    def test_exercise_count(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "test.fit")
        result = generate_fit(sample_workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert result["exercises"] == 2

    def test_set_count(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "test.fit")
        result = generate_fit(sample_workout, hr_samples=None, output_path=path, profile=sample_profile)
        # 4 bench sets + 2 shoulder sets = 6
        assert result["total_sets"] == 6

    def test_calories_positive(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "test.fit")
        result = generate_fit(sample_workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert result["calories"] > 0

    def test_duration_matches_workout(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "test.fit")
        result = generate_fit(sample_workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert result["duration_s"] == 45 * 60  # 45 minutes


class TestProfileOverride:
    def test_different_weight_changes_calories(self, sample_workout: dict, tmp_path: Path) -> None:
        path1 = str(tmp_path / "light.fit")
        path2 = str(tmp_path / "heavy.fit")

        light = {"weight_kg": 60, "birth_year": 1994, "vo2max": 45, "working_set_s": 40, "warmup_set_s": 25, "rest_sets_s": 75, "rest_exercises_s": 120}
        heavy = {"weight_kg": 100, "birth_year": 1994, "vo2max": 45, "working_set_s": 40, "warmup_set_s": 25, "rest_sets_s": 75, "rest_exercises_s": 120}

        r1 = generate_fit(sample_workout, hr_samples=None, output_path=path1, profile=light)
        r2 = generate_fit(sample_workout, hr_samples=None, output_path=path2, profile=heavy)

        assert r2["calories"] > r1["calories"]

    def test_default_profile_from_config(self, sample_workout: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "default.fit")
        # No profile param → reads from config (which returns defaults)
        result = generate_fit(sample_workout, hr_samples=None, output_path=path)
        assert result["calories"] > 0


class TestHRSamples:
    def test_with_hr_samples(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "hr.fit")
        hr = [90, 95, 100, 105, 110, 115, 120, 115, 110, 100]
        result = generate_fit(sample_workout, hr_samples=hr, output_path=path, profile=sample_profile)
        assert result["hr_samples"] == len(hr)
        assert result["avg_hr"] > 0

    def test_no_hr_uses_default(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path = str(tmp_path / "nohr.fit")
        result = generate_fit(sample_workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert result["hr_samples"] == 0

    def test_hr_changes_calories(self, sample_workout: dict, sample_profile: dict, tmp_path: Path) -> None:
        path1 = str(tmp_path / "low_hr.fit")
        path2 = str(tmp_path / "high_hr.fit")

        low_hr = [70] * 10
        high_hr = [150] * 10

        r1 = generate_fit(sample_workout, hr_samples=low_hr, output_path=path1, profile=sample_profile)
        r2 = generate_fit(sample_workout, hr_samples=high_hr, output_path=path2, profile=sample_profile)

        assert r2["calories"] > r1["calories"]


class TestEdgeCases:
    def test_single_exercise(self, sample_profile: dict, tmp_path: Path) -> None:
        workout = {
            "id": "single",
            "title": "Quick",
            "start_time": "2026-04-01T20:00:00+00:00",
            "end_time": "2026-04-01T20:10:00+00:00",
            "exercises": [
                {
                    "index": 0,
                    "title": "Bicep Curl (Dumbbell)",
                    "sets": [{"index": 0, "type": "normal", "weight_kg": 10, "reps": 12}],
                },
            ],
        }
        path = str(tmp_path / "single.fit")
        result = generate_fit(workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert result["exercises"] == 1
        assert result["total_sets"] == 1

    def test_empty_exercises(self, sample_profile: dict, tmp_path: Path) -> None:
        workout = {
            "id": "empty",
            "title": "Empty",
            "start_time": "2026-04-01T20:00:00+00:00",
            "end_time": "2026-04-01T20:10:00+00:00",
            "exercises": [],
        }
        path = str(tmp_path / "empty.fit")
        result = generate_fit(workout, hr_samples=None, output_path=path, profile=sample_profile)
        assert result["exercises"] == 0
        assert result["total_sets"] == 0
