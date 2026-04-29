"""Tests for scan helpers, schema, extension filtering, and JSONL output."""

import json
import io
from pathlib import Path

from tagwell.schema import (
    SUPPORTED_EXTENSIONS,
    audio_file_record,
    error_record,
    scan_header_record,
    to_jsonl_line,
    make_scan_id,
)


class TestExtensionFiltering:
    def test_supported_extensions_include_common(self):
        for ext in (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".aac"):
            assert ext in SUPPORTED_EXTENSIONS, f"{ext} should be supported"

    def test_unsupported_extensions(self):
        for ext in (".txt", ".jpg", ".py", ".pdf", ".doc", ".cue", ".log"):
            assert ext not in SUPPORTED_EXTENSIONS, f"{ext} should not be supported"

    def test_case_sensitivity(self):
        # Extensions in the set are lowercase; the scanner lowercases before checking
        assert ".mp3" in SUPPORTED_EXTENSIONS
        assert ".MP3" not in SUPPORTED_EXTENSIONS  # set itself is lowercase


class TestScanHeader:
    def test_scan_header_structure(self):
        header = scan_header_record(
            scan_id="abc-123",
            started_at="2026-01-01T00:00:00+00:00",
            root="/music",
        )
        assert header["schema_version"] == 1
        assert header["record_type"] == "scan_header"
        assert header["scanner"]["name"] == "tagwell"
        assert header["scan"]["scan_id"] == "abc-123"
        assert header["scan"]["started_at"] == "2026-01-01T00:00:00+00:00"
        assert header["scan"]["root"] == "/music"
        assert header["scan"]["root_id"] == "main"

    def test_scan_header_serializes(self):
        header = scan_header_record(
            scan_id="abc-123",
            started_at="2026-01-01T00:00:00+00:00",
            root="/music",
        )
        line = to_jsonl_line(header)
        parsed = json.loads(line)
        assert parsed["record_type"] == "scan_header"


class TestJsonlWriter:
    def _make_record(self) -> dict:
        return audio_file_record(
            file_info={
                "relative_path": "test/song.mp3",
                "name": "song.mp3",
                "stem": "song",
                "ext": ".mp3",
                "size_bytes": 1024,
                "mtime": "2026-01-01T00:00:00+00:00",
                "ctime": None,
            },
            audio_info={
                "container": "mp3",
                "codec": "mp3",
                "duration_seconds": 180.5,
                "sample_rate_hz": 44100,
                "channels": 2,
                "bit_depth": None,
                "lossless": False,
                "estimated_bitrate_kbps": 320.0,
            },
            tags_info={
                "format": "id3",
                "reader": {"name": "mutagen", "version": "1.47.0"},
                "raw": {"TIT2": ["Test Song"]},
                "parsed": {
                    "title": "Test Song",
                    "artists": [],
                    "album": None,
                    "album_artists": [],
                    "release_date": None,
                    "track_number": None,
                    "track_total": None,
                    "disc_number": None,
                    "disc_total": None,
                    "genres": [],
                    "labels": [],
                },
                "external_ids": {"musicbrainz": {
                    "recording_id": None,
                    "release_id": None,
                    "release_group_id": None,
                    "release_track_id": None,
                    "artist_ids": [],
                    "album_artist_ids": [],
                }},
                "pictures": [],
            },
        )

    def test_single_line_valid_json(self):
        record = self._make_record()
        line = to_jsonl_line(record, pretty=False)
        # Must be single line
        assert "\n" not in line
        # Must be valid JSON
        parsed = json.loads(line)
        assert parsed["record_type"] == "audio_file_snapshot"

    def test_no_scanner_or_scan_in_snapshot(self):
        record = self._make_record()
        assert "scanner" not in record
        assert "scan" not in record

    def test_multiple_records_valid_jsonl(self):
        buf = io.StringIO()
        for _ in range(3):
            record = self._make_record()
            buf.write(to_jsonl_line(record) + "\n")
        buf.seek(0)
        lines = buf.readlines()
        assert len(lines) == 3
        for line in lines:
            parsed = json.loads(line)
            assert "schema_version" in parsed

    def test_pretty_mode(self):
        record = self._make_record()
        line = to_jsonl_line(record, pretty=True)
        # Pretty mode produces multi-line JSON
        assert "\n" in line
        parsed = json.loads(line)
        assert parsed["record_type"] == "audio_file_snapshot"

    def test_raw_tags_always_arrays(self):
        record = self._make_record()
        for key, val in record["tags"]["raw"].items():
            assert isinstance(val, list), f"raw tag '{key}' must be a list"

    def test_schema_version(self):
        record = self._make_record()
        assert record["schema_version"] == 1

    def test_record_type(self):
        record = self._make_record()
        assert record["record_type"] == "audio_file_snapshot"


class TestErrorRecord:
    def test_error_record_structure(self):
        rec = error_record(
            relative_path="bad/file.mp3",
            stage="read_tags",
            kind="MutagenError",
            exception_type="MutagenError",
            message="file is corrupt",
            recoverable=True,
        )
        assert rec["record_type"] == "file_error"
        assert rec["error"]["stage"] == "read_tags"
        assert rec["error"]["recoverable"] is True
        assert rec["file"]["relative_path"] == "bad/file.mp3"
        assert "scanner" not in rec
        assert "scan" not in rec
        line = to_jsonl_line(rec)
        parsed = json.loads(line)
        assert parsed["record_type"] == "file_error"


class TestMakeScanId:
    def test_returns_tuple(self):
        scan_id, started_at = make_scan_id()
        assert isinstance(scan_id, str)
        assert isinstance(started_at, str)
        # scan_id should be UUID4 format
        assert len(scan_id) == 36
        assert scan_id.count("-") == 4
        # started_at should be ISO format
        assert "T" in started_at
