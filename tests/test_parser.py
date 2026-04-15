"""Tests for the Liftosaur history record parser."""

from __future__ import annotations

from liftosaur2garmin.parser import _parse_header


HEADER_SUFFIX = " / exercises: {"


class TestParseHeader:
    def test_basic_header(self):
        header = '2024-01-15T10:30:00.000Z / program: "My Program" / dayName: "Push Day" / week: 3 / day: 1 / dayInWeek: 2 / duration: 3600s' + HEADER_SUFFIX
        result = _parse_header(header)
        assert result["date"] == "2024-01-15T10:30:00.000Z"
        assert result["program"] == "My Program"
        assert result["dayName"] == "Push Day"
        assert result["week"] == 3
        assert result["day"] == 1
        assert result["dayInWeek"] == 2
        assert result["duration_seconds"] == 3600

    def test_undefined_week_values(self):
        header = "2024-01-15T10:30:00.000Z / week: undefined / day: undefined / dayInWeek: undefined / duration: 1800s" + HEADER_SUFFIX
        result = _parse_header(header)
        assert "week" not in result
        assert "day" not in result
        assert "dayInWeek" not in result
        assert result["duration_seconds"] == 1800

    def test_mixed_undefined_and_valid(self):
        header = '2024-01-15T10:30:00.000Z / program: "Test" / week: undefined / day: 5 / duration: 900s' + HEADER_SUFFIX
        result = _parse_header(header)
        assert result["program"] == "Test"
        assert "week" not in result
        assert result["day"] == 5
        assert result["duration_seconds"] == 900
