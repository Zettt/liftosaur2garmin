"""Tests for exercise mapper."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from liftosaur2garmin.mapper import (
    EXERCISE_TO_GARMIN,
    _UNKNOWN_CATEGORY,
    lookup_exercise,
    save_custom_mapping,
    _custom_mappings,
    _ensure_custom_loaded,
)


class TestLookupBuiltIn:
    def test_known_exercise(self) -> None:
        cat, subcat, name = lookup_exercise("Bench Press (Barbell)")
        assert cat == 0
        assert subcat == 1
        assert name == "Bench Press (Barbell)"

    def test_squat(self) -> None:
        cat, subcat, name = lookup_exercise("Squat (Barbell)")
        assert cat == 28
        assert name == "Squat (Barbell)"

    def test_unknown_exercise(self) -> None:
        cat, subcat, name = lookup_exercise("Made Up Exercise 12345")
        assert cat == _UNKNOWN_CATEGORY
        assert subcat == 0
        assert name == "Made Up Exercise 12345"

    def test_empty_string(self) -> None:
        cat, subcat, name = lookup_exercise("")
        assert cat == _UNKNOWN_CATEGORY
        assert name == ""

    def test_mapping_count_minimum(self) -> None:
        assert len(EXERCISE_TO_GARMIN) >= 400

    def test_preserves_original_name(self) -> None:
        _, _, name = lookup_exercise("Deadlift (Barbell)")
        assert name == "Deadlift (Barbell)"


class TestCustomMappings:
    def test_custom_overrides_builtin(self, tmp_path: Path) -> None:
        mappings_file = tmp_path / "custom_mappings.json"
        mappings_file.write_text(json.dumps({"Bench Press (Barbell)": [99, 88]}))

        # Reset custom state
        _custom_mappings.clear()
        import liftosaur2garmin.mapper as m
        m._custom_loaded = False

        with patch.object(Path, "expanduser", return_value=mappings_file):
            with patch("liftosaur2garmin.mapper._custom_loaded", False):
                # Force reload
                m._custom_loaded = False
                m._custom_mappings.clear()
                m._custom_mappings["Bench Press (Barbell)"] = (99, 88)
                cat, subcat, _ = lookup_exercise("Bench Press (Barbell)")
                assert cat == 99
                assert subcat == 88

        # Cleanup
        m._custom_mappings.clear()

    def test_custom_does_not_affect_other_exercises(self) -> None:
        import liftosaur2garmin.mapper as m
        m._custom_mappings["Only This One"] = (1, 2)
        cat, _, _ = lookup_exercise("Squat (Barbell)")
        assert cat == 28  # unchanged
        m._custom_mappings.clear()

    def test_save_custom_mapping_in_memory(self) -> None:
        import liftosaur2garmin.mapper as m
        m._custom_mappings["Test Exercise"] = (5, 10)
        cat, subcat, _ = lookup_exercise("Test Exercise")
        assert cat == 5
        assert subcat == 10
        m._custom_mappings.clear()

    def test_missing_custom_file_no_crash(self) -> None:
        import liftosaur2garmin.mapper as m
        m._custom_loaded = False
        m._custom_mappings.clear()
        # Should not crash when file doesn't exist
        _ensure_custom_loaded()

    def test_cloud_mapping_save_updates_lookup_without_reload(self, monkeypatch) -> None:
        import liftosaur2garmin.mapper as m
        from liftosaur2garmin import server

        class FakeDb:
            def __init__(self) -> None:
                self.saved: list[tuple[str, int, int]] = []

            def save_custom_mapping(self, name: str, category: int, subcategory: int) -> None:
                self.saved.append((name, category, subcategory))

        fake_db = FakeDb()
        m._custom_loaded = True
        m._custom_mappings.clear()
        server._is_configured_cache = True
        monkeypatch.setattr(server.db, "get_database_url", lambda: "postgres://example")
        monkeypatch.setattr(server.db, "get_db", lambda: fake_db)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        response = client.post(
            "/api/mapping",
            data={"exercise_name": "Cloud Only Exercise", "category": "7", "subcategory": "3"},
        )

        assert response.status_code == 200
        assert fake_db.saved == [("Cloud Only Exercise", 7, 3)]
        assert lookup_exercise("Cloud Only Exercise")[:2] == (7, 3)
