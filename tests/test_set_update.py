"""Tests for exercise set matching and updating on existing Garmin activities."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from liftosaur2garmin.sync import (
    _apply_liftosaur_values,
    _group_garmin_sets_by_exercise,
    _match_exercise_sets,
    _merge_consecutive_liftosaur_exercises,
    _remove_zero_rep_sets,
    update_existing_activity_sets,
)


def _active_set(category: str, name: str, reps: int, weight: int, start: str = "2026-04-09T19:16:52.0") -> dict:
    """Build a Garmin ACTIVE exercise set."""
    return {
        "exercises": [{"probability": 100, "category": category, "name": name}],
        "repetitionCount": reps,
        "duration": 25.0,
        "weight": weight,
        "setType": "ACTIVE",
        "startTime": start,
    }


def _rest_set() -> dict:
    """Build a Garmin REST set."""
    return {
        "setType": "REST",
        "duration": 90.0,
        "exercises": [],
        "repetitionCount": None,
        "weight": -1,
        "startTime": None,
    }


def _liftosaur_set(set_type: str, reps: int, weight_kg: float) -> dict:
    return {"type": set_type, "reps": reps, "weight_kg": weight_kg}


class TestRemoveZeroRepSets:
    def test_removes_zero_rep_active_and_following_rest(self) -> None:
        garmin_sets = [
            _active_set("CURL", "BICEP_CURL", 0, 0),
            _rest_set(),
            _active_set("CURL", "BICEP_CURL", 10, 12500),
            _rest_set(),
        ]
        result = _remove_zero_rep_sets(garmin_sets)
        assert len(result) == 2
        assert result[0]["repetitionCount"] == 10

    def test_removes_multiple_zero_rep_sets(self) -> None:
        garmin_sets = [
            _active_set("BP", "BENCH", 0, 0),
            _rest_set(),
            _active_set("BP", "BENCH", 0, 0),
            _rest_set(),
            _active_set("BP", "BENCH", 5, 60000),
            _rest_set(),
        ]
        result = _remove_zero_rep_sets(garmin_sets)
        assert len(result) == 2
        assert result[0]["repetitionCount"] == 5

    def test_preserves_all_when_no_zero_reps(self) -> None:
        garmin_sets = [
            _active_set("CURL", "BICEP_CURL", 10, 12500),
            _rest_set(),
        ]
        result = _remove_zero_rep_sets(garmin_sets)
        assert len(result) == 2

    def test_empty_list(self) -> None:
        assert _remove_zero_rep_sets([]) == []


class TestGroupGarminSetsByExercise:
    def test_groups_consecutive_same_exercise(self) -> None:
        garmin_sets = [
            _active_set("BP", "BENCH", 10, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 8, 60000),
            _rest_set(),
            _active_set("CURL", "BICEP_CURL", 12, 10000),
            _rest_set(),
        ]
        groups = _group_garmin_sets_by_exercise(garmin_sets)
        assert len(groups) == 2
        assert len(groups[0]) == 2  # 2 bench sets
        assert len(groups[1]) == 1  # 1 curl set

    def test_splits_non_consecutive_same_exercise(self) -> None:
        garmin_sets = [
            _active_set("BP", "BENCH", 10, 60000),
            _rest_set(),
            _active_set("CURL", "BICEP_CURL", 12, 10000),
            _rest_set(),
            _active_set("BP", "BENCH", 8, 60000),
            _rest_set(),
        ]
        groups = _group_garmin_sets_by_exercise(garmin_sets)
        assert len(groups) == 3

    def test_preserves_original_indices(self) -> None:
        garmin_sets = [
            _active_set("BP", "BENCH", 10, 60000),
            _rest_set(),
            _active_set("CURL", "BICEP_CURL", 12, 10000),
        ]
        groups = _group_garmin_sets_by_exercise(garmin_sets)
        assert groups[0][0]["index"] == 0
        assert groups[1][0]["index"] == 2

    def test_empty_list(self) -> None:
        assert _group_garmin_sets_by_exercise([]) == []


class TestMergeConsecutiveLiftosaurExercises:
    def test_merges_same_title(self) -> None:
        exercises = [
            {"title": "Bench Press", "sets": [_liftosaur_set("warmup", 12, 40)]},
            {"title": "Bench Press", "sets": [_liftosaur_set("normal", 10, 60)]},
        ]
        merged = _merge_consecutive_liftosaur_exercises(exercises)
        assert len(merged) == 1
        assert len(merged[0]["sets"]) == 2

    def test_does_not_merge_different_titles(self) -> None:
        exercises = [
            {"title": "Bench Press", "sets": [_liftosaur_set("normal", 10, 60)]},
            {"title": "Curl", "sets": [_liftosaur_set("normal", 12, 15)]},
        ]
        merged = _merge_consecutive_liftosaur_exercises(exercises)
        assert len(merged) == 2

    def test_does_not_merge_non_consecutive_same_title(self) -> None:
        exercises = [
            {"title": "Bench Press", "sets": [_liftosaur_set("normal", 10, 60)]},
            {"title": "Curl", "sets": [_liftosaur_set("normal", 12, 15)]},
            {"title": "Bench Press", "sets": [_liftosaur_set("normal", 8, 60)]},
        ]
        merged = _merge_consecutive_liftosaur_exercises(exercises)
        assert len(merged) == 3

    def test_does_not_mutate_input(self) -> None:
        original_sets = [_liftosaur_set("warmup", 12, 40)]
        exercises = [
            {"title": "Bench Press", "sets": original_sets},
            {"title": "Bench Press", "sets": [_liftosaur_set("normal", 10, 60)]},
        ]
        merged = _merge_consecutive_liftosaur_exercises(exercises)
        assert len(original_sets) == 1  # not mutated
        assert len(merged[0]["sets"]) == 2

    def test_empty_list(self) -> None:
        assert _merge_consecutive_liftosaur_exercises([]) == []


class TestMatchExerciseSets:
    def test_matches_all_warmup_and_normal(self) -> None:
        garmin = [
            {"index": 0, "set": {}},
            {"index": 2, "set": {}},
            {"index": 4, "set": {}},
        ]
        liftosaur = [
            _liftosaur_set("warmup", 12, 40),
            _liftosaur_set("normal", 10, 60),
            _liftosaur_set("normal", 8, 60),
        ]
        result = _match_exercise_sets(garmin, liftosaur)
        assert result is not None
        assert len(result) == 3

    def test_matches_normal_only_when_warmups_absent(self) -> None:
        garmin = [
            {"index": 0, "set": {}},
            {"index": 2, "set": {}},
        ]
        liftosaur = [
            _liftosaur_set("warmup", 12, 40),
            _liftosaur_set("normal", 10, 60),
            _liftosaur_set("normal", 8, 60),
        ]
        result = _match_exercise_sets(garmin, liftosaur)
        assert result is not None
        assert len(result) == 2
        # Should match to normal sets, not warmup
        assert result[0][1]["reps"] == 10
        assert result[1][1]["reps"] == 8

    def test_returns_none_on_count_mismatch(self) -> None:
        garmin = [{"index": 0, "set": {}}]
        liftosaur = [
            _liftosaur_set("normal", 10, 60),
            _liftosaur_set("normal", 8, 60),
        ]
        assert _match_exercise_sets(garmin, liftosaur) is None


class TestApplyLiftosaurValues:
    def test_updates_reps_and_weight(self) -> None:
        garmin_sets = [
            _active_set("BP", "BENCH", 0, 0),
            _rest_set(),
            _active_set("BP", "BENCH", 0, 0),
        ]
        matches = [
            ({"index": 0}, _liftosaur_set("normal", 10, 60)),
            ({"index": 2}, _liftosaur_set("normal", 8, 55.5)),
        ]
        _apply_liftosaur_values(garmin_sets, matches)
        assert garmin_sets[0]["repetitionCount"] == 10
        assert garmin_sets[0]["weight"] == 60000
        assert garmin_sets[2]["repetitionCount"] == 8
        assert garmin_sets[2]["weight"] == 55500

    def test_does_not_touch_rest_sets(self) -> None:
        garmin_sets = [
            _active_set("BP", "BENCH", 0, 0),
            _rest_set(),
        ]
        matches = [({"index": 0}, _liftosaur_set("normal", 10, 60))]
        _apply_liftosaur_values(garmin_sets, matches)
        assert garmin_sets[1]["weight"] == -1
        assert garmin_sets[1]["repetitionCount"] is None

    def test_skips_none_values(self) -> None:
        garmin_sets = [_active_set("BP", "BENCH", 5, 60000)]
        matches = [({"index": 0}, {"type": "normal", "reps": None, "weight_kg": None})]
        _apply_liftosaur_values(garmin_sets, matches)
        assert garmin_sets[0]["repetitionCount"] == 5  # unchanged
        assert garmin_sets[0]["weight"] == 60000  # unchanged


class TestUpdateExistingActivitySets:
    def _garmin_sets_for_workout(self) -> list[dict]:
        """Garmin sets matching sample_workout: 1 warmup + 3 normal bench, 2 normal shoulder."""
        return [
            _active_set("BP", "BENCH", 12, 40000),
            _rest_set(),
            _active_set("BP", "BENCH", 10, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 8, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 7, 60000),
            _rest_set(),
            _active_set("SP", "SHOULDER_PRESS", 12, 14000),
            _rest_set(),
            _active_set("SP", "SHOULDER_PRESS", 10, 14000),
            _rest_set(),
        ]

    def test_full_sync_flow(self, sample_workout: dict) -> None:
        client = MagicMock()
        with patch("liftosaur2garmin.sync.get_exercise_sets") as mock_get, \
             patch("liftosaur2garmin.sync.update_exercise_sets") as mock_update:
            mock_get.return_value = self._garmin_sets_for_workout()
            result = update_existing_activity_sets(client, 123, sample_workout)
            assert result is True
            mock_update.assert_called_once()
            updated_sets = mock_update.call_args[0][2]
            # Bench warmup set updated
            assert updated_sets[0]["repetitionCount"] == 12
            assert updated_sets[0]["weight"] == 40000
            # Bench working sets updated
            assert updated_sets[2]["repetitionCount"] == 10
            assert updated_sets[2]["weight"] == 60000
            # Shoulder press updated
            assert updated_sets[8]["repetitionCount"] == 12
            assert updated_sets[8]["weight"] == 14000

    def test_returns_false_when_no_garmin_sets(self, sample_workout: dict) -> None:
        client = MagicMock()
        with patch("liftosaur2garmin.sync.get_exercise_sets") as mock_get, \
             patch("liftosaur2garmin.sync.update_exercise_sets") as mock_update:
            mock_get.return_value = []
            result = update_existing_activity_sets(client, 123, sample_workout)
            assert result is False
            mock_update.assert_not_called()

    def test_returns_false_when_no_exercises(self) -> None:
        client = MagicMock()
        workout = {"exercises": []}
        with patch("liftosaur2garmin.sync.get_exercise_sets") as mock_get, \
             patch("liftosaur2garmin.sync.update_exercise_sets") as mock_update:
            mock_get.return_value = [_active_set("BP", "BENCH", 10, 60000)]
            result = update_existing_activity_sets(client, 123, workout)
            assert result is False
            mock_update.assert_not_called()

    def test_removes_zero_rep_sets_before_matching(self) -> None:
        client = MagicMock()
        workout = {
            "exercises": [{
                "title": "Bench Press (Barbell)",
                "sets": [_liftosaur_set("normal", 10, 60)],
            }],
        }
        garmin_sets = [
            _active_set("BP", "BENCH", 0, 0),  # zero-rep, should be removed
            _rest_set(),
            _active_set("BP", "BENCH", 10, 60000),
            _rest_set(),
        ]
        with patch("liftosaur2garmin.sync.get_exercise_sets") as mock_get, \
             patch("liftosaur2garmin.sync.update_exercise_sets") as mock_update:
            mock_get.return_value = garmin_sets
            result = update_existing_activity_sets(client, 123, workout)
            assert result is True
            updated_sets = mock_update.call_args[0][2]
            # Zero-rep set and its rest should be gone
            assert len(updated_sets) == 2
            assert updated_sets[0]["repetitionCount"] == 10
            assert updated_sets[0]["weight"] == 60000

    def test_merges_consecutive_exercises(self) -> None:
        client = MagicMock()
        workout = {
            "exercises": [
                {"title": "Bench Press", "sets": [
                    _liftosaur_set("warmup", 12, 40),
                    _liftosaur_set("normal", 5, 80),
                ]},
                {"title": "Bench Press", "sets": [
                    _liftosaur_set("normal", 5, 80),
                    _liftosaur_set("normal", 5, 80),
                ]},
            ],
        }
        garmin_sets = [
            _active_set("BP", "BENCH", 12, 40000),
            _rest_set(),
            _active_set("BP", "BENCH", 5, 80000),
            _rest_set(),
            _active_set("BP", "BENCH", 5, 80000),
            _rest_set(),
            _active_set("BP", "BENCH", 5, 80000),
            _rest_set(),
        ]
        with patch("liftosaur2garmin.sync.get_exercise_sets") as mock_get, \
             patch("liftosaur2garmin.sync.update_exercise_sets") as mock_update:
            mock_get.return_value = garmin_sets
            result = update_existing_activity_sets(client, 123, workout)
            assert result is True
            mock_update.assert_called_once()

    def test_warmups_absent_in_garmin(self) -> None:
        client = MagicMock()
        workout = {
            "exercises": [{
                "title": "Bench Press (Barbell)",
                "sets": [
                    _liftosaur_set("warmup", 12, 40),
                    _liftosaur_set("normal", 10, 60),
                    _liftosaur_set("normal", 8, 60),
                ],
            }],
        }
        # Garmin only has the 2 normal sets, no warmup
        garmin_sets = [
            _active_set("BP", "BENCH", 10, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 8, 60000),
            _rest_set(),
        ]
        with patch("liftosaur2garmin.sync.get_exercise_sets") as mock_get, \
             patch("liftosaur2garmin.sync.update_exercise_sets") as mock_update:
            mock_get.return_value = garmin_sets
            result = update_existing_activity_sets(client, 123, workout)
            assert result is True
            updated_sets = mock_update.call_args[0][2]
            assert updated_sets[0]["repetitionCount"] == 10
            assert updated_sets[0]["weight"] == 60000

    def test_skips_mismatched_exercise(self) -> None:
        client = MagicMock()
        workout = {
            "exercises": [{
                "title": "Bench Press (Barbell)",
                "sets": [
                    _liftosaur_set("normal", 10, 60),
                    _liftosaur_set("normal", 8, 60),
                    _liftosaur_set("normal", 6, 60),
                ],
            }],
        }
        # Garmin has 5 sets — doesn't match 3 normal or 3 total
        garmin_sets = [
            _active_set("BP", "BENCH", 10, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 8, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 6, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 4, 60000),
            _rest_set(),
            _active_set("BP", "BENCH", 2, 60000),
            _rest_set(),
        ]
        with patch("liftosaur2garmin.sync.get_exercise_sets") as mock_get, \
             patch("liftosaur2garmin.sync.update_exercise_sets") as mock_update:
            mock_get.return_value = garmin_sets
            result = update_existing_activity_sets(client, 123, workout)
            assert result is False
            mock_update.assert_not_called()
