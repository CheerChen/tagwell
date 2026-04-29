"""Tests for tag parsing helpers."""

from tagwell.tags import parse_date_tag, _parse_track_field, _coerce_list


class TestParseDateTag:
    def test_full_date(self):
        result = parse_date_tag("1996-10-25")
        assert result == {
            "raw": "1996-10-25",
            "year": 1996,
            "month": 10,
            "day": 25,
            "precision": "day",
        }

    def test_year_month(self):
        result = parse_date_tag("1996-10")
        assert result == {
            "raw": "1996-10",
            "year": 1996,
            "month": 10,
            "day": None,
            "precision": "month",
        }

    def test_year_only(self):
        result = parse_date_tag("1996")
        assert result == {
            "raw": "1996",
            "year": 1996,
            "month": None,
            "day": None,
            "precision": "year",
        }

    def test_invalid_date(self):
        result = parse_date_tag("not-a-date")
        assert result == {
            "raw": "not-a-date",
            "year": None,
            "month": None,
            "day": None,
            "precision": "unknown",
        }

    def test_none(self):
        assert parse_date_tag(None) is None

    def test_empty(self):
        assert parse_date_tag("") is None

    def test_whitespace(self):
        assert parse_date_tag("  ") is None

    def test_leading_trailing_whitespace(self):
        result = parse_date_tag("  1996-10-25  ")
        assert result["year"] == 1996
        assert result["precision"] == "day"


class TestParseTrackField:
    def test_simple_number(self):
        assert _parse_track_field("1", None) == (1, None)

    def test_zero_padded(self):
        assert _parse_track_field("01", None) == (1, None)

    def test_slash_format(self):
        assert _parse_track_field("1/12", None) == (1, 12)

    def test_slash_zero_padded(self):
        assert _parse_track_field("01/12", None) == (1, 12)

    def test_separate_total(self):
        assert _parse_track_field("5", "12") == (5, 12)

    def test_slash_overrides_separate_total(self):
        # If slash provides a total, it takes precedence
        assert _parse_track_field("5/10", "12") == (5, 10)

    def test_none_value(self):
        assert _parse_track_field(None, None) == (None, None)

    def test_invalid(self):
        assert _parse_track_field("abc", None) == (None, None)


class TestCoerceList:
    def test_string(self):
        assert _coerce_list("hello") == ["hello"]

    def test_list(self):
        assert _coerce_list(["a", "b"]) == ["a", "b"]

    def test_tuple(self):
        assert _coerce_list(("x", "y")) == ["x", "y"]

    def test_int(self):
        assert _coerce_list(42) == ["42"]

    def test_single_element_list(self):
        assert _coerce_list(["only"]) == ["only"]

    def test_empty_list(self):
        assert _coerce_list([]) == []
