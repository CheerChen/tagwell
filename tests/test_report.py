"""Tests for Markdown report generation."""

from pathlib import Path

from tagwell.report import generate_report
from tagwell.schema import audio_file_record, scan_header_record, to_jsonl_line


class TestReport:
    def _write_jsonl(self, tmp_path: Path, records: list[dict]) -> Path:
        path = tmp_path / "library.jsonl"
        path.write_text("".join(to_jsonl_line(record) + "\n" for record in records), encoding="utf-8")
        return path

    def _snapshot(
        self,
        relative_path: str,
        *,
        title: str,
        album: str,
        artists: list[str],
        album_artists: list[str] | None = None,
        release_id: str | None = None,
        release_group_id: str | None = None,
        recording_id: str | None = None,
        track_number: int = 1,
        track_total: int | None = 1,
        disc_number: int | None = 1,
        disc_total: int | None = 1,
        codec: str = "flac",
        container: str | None = None,
        lossless: bool = True,
        bitrate: float | None = None,
        duration: float = 180.0,
        release_year: int = 2024,
        script: str | None = None,
        releasecountry: str | None = None,
        mtime: str = "2026-01-01T00:00:00+00:00",
        has_cover: bool = True,
    ) -> dict:
        album_artists = album_artists or []
        container = container or codec

        raw: dict[str, list[str]] = {
            "title": [title],
            "album": [album],
            "artist": artists,
            "tracknumber": [str(track_number)],
        }
        if script:
            raw["script"] = [script]
        if releasecountry:
            raw["releasecountry"] = [releasecountry]

        return audio_file_record(
            file_info={
                "relative_path": relative_path,
                "name": Path(relative_path).name,
                "stem": Path(relative_path).stem,
                "ext": Path(relative_path).suffix,
                "size_bytes": 1024,
                "mtime": mtime,
                "ctime": None,
            },
            audio_info={
                "container": container,
                "codec": codec,
                "duration_seconds": duration,
                "sample_rate_hz": 44100,
                "channels": 2,
                "bit_depth": 16 if lossless else None,
                "lossless": lossless,
                "estimated_bitrate_kbps": bitrate,
            },
            tags_info={
                "format": "vorbis_comment",
                "reader": {"name": "mutagen", "version": "1.47.0"},
                "raw": raw,
                "parsed": {
                    "title": title,
                    "artists": artists,
                    "album": album,
                    "album_artists": album_artists,
                    "release_date": {
                        "raw": str(release_year),
                        "year": release_year,
                        "month": None,
                        "day": None,
                        "precision": "year",
                    },
                    "track_number": track_number,
                    "track_total": track_total,
                    "disc_number": disc_number,
                    "disc_total": disc_total,
                    "genres": [],
                    "labels": [],
                },
                "external_ids": {
                    "musicbrainz": {
                        "recording_id": recording_id,
                        "release_id": release_id,
                        "release_group_id": release_group_id,
                        "release_track_id": None,
                        "artist_ids": [],
                        "album_artist_ids": [],
                    }
                },
                "pictures": [{"type": "front"}] if has_cover else [],
            },
        )

    def test_report_reorganizes_sections_and_lists_recording_backlog(self, tmp_path: Path):
        records = [
            scan_header_record(
                scan_id="scan-1",
                started_at="2026-04-29T17:26:46+09:00",
                root="/music",
                reader={"name": "mutagen", "version": "1.47.0"},
            ),
            self._snapshot(
                "Alpha/01 - First.mp3",
                title="First",
                album="Alpha",
                artists=["Artist A"],
                album_artists=["Artist A"],
                release_id="alpha-rel",
                release_group_id="alpha-group",
                recording_id=None,
                track_number=1,
                track_total=2,
                disc_number=1,
                disc_total=1,
                codec="mp3",
                lossless=False,
                bitrate=320.0,
                duration=200.0,
                release_year=2021,
                script="Jpan",
                releasecountry="JP",
                mtime="2022-01-02T00:00:00+00:00",
            ),
            self._snapshot(
                "Alpha/02 - Second.mp3",
                title="Second",
                album="Alpha",
                artists=["Artist A"],
                album_artists=["Artist A"],
                release_id="alpha-rel",
                release_group_id="alpha-group",
                recording_id="alpha-rec-2",
                track_number=2,
                track_total=2,
                disc_number=1,
                disc_total=1,
                codec="mp3",
                lossless=False,
                bitrate=320.0,
                duration=210.0,
                release_year=2021,
                script="Jpan",
                releasecountry="JP",
                mtime="2022-01-02T00:00:00+00:00",
            ),
            self._snapshot(
                "Beta/Disc 1/01 - Wide.flac",
                title="Wide",
                album="Beta",
                artists=["Artist B", "Guest C"],
                album_artists=["Artist B"],
                release_id="beta-rel",
                release_group_id="beta-group",
                recording_id="beta-rec-1",
                track_number=1,
                track_total=2,
                disc_number=1,
                disc_total=2,
                duration=500.0,
                release_year=2023,
                script="Latn",
                releasecountry="US",
                mtime="2023-05-01T00:00:00+00:00",
            ),
            self._snapshot(
                "Beta/Disc 2/01 - Short.flac",
                title="Short",
                album="Beta",
                artists=["Artist B"],
                album_artists=["Artist B"],
                release_id="beta-rel",
                release_group_id="beta-group",
                recording_id="beta-rec-2",
                track_number=1,
                track_total=3,
                disc_number=2,
                disc_total=2,
                duration=90.0,
                release_year=2023,
                script="Hant",
                releasecountry="TW",
                mtime="2023-05-01T00:00:00+00:00",
            ),
        ]
        report = generate_report(self._write_jsonl(tmp_path, records))

        assert "## Data Quality" in report
        assert "## Library Shape" in report
        assert "### Missing recording_id backlog" in report
        assert "| Alpha | Artist A | 1 |" in report
        assert "- `Alpha/01 - First.mp3`" not in report  # detail listing removed
        assert "All 4 files have embedded cover art." in report
        assert "| No cover art |" not in report
        assert "### Encoding profile" not in report
        assert "Lossy bitrate distribution" not in report
        assert "| Beta | Artist B | 2 | 5 | 40.0% |" in report

    def test_report_merges_missing_release_id_track_into_single_album_group(self, tmp_path: Path):
        records = [
            scan_header_record(
                scan_id="scan-2",
                started_at="2026-04-29T17:26:46+09:00",
                root="/music",
            ),
            self._snapshot(
                "Gamma/01 - One.flac",
                title="One",
                album="Gamma",
                artists=["Artist G"],
                album_artists=["Artist G"],
                release_id="gamma-rel",
                release_group_id="gamma-group",
                recording_id="gamma-rec-1",
                track_number=1,
                track_total=2,
            ),
            self._snapshot(
                "Gamma/02 - Two.flac",
                title="Two",
                album="Gamma",
                artists=["Artist G"],
                album_artists=["Artist G"],
                release_id=None,
                release_group_id=None,
                recording_id="gamma-rec-2",
                track_number=2,
                track_total=2,
            ),
        ]
        report = generate_report(self._write_jsonl(tmp_path, records))

        assert "| Unique albums | 1 |" in report
        assert "| Gamma | Artist G | 2 |" in report

    def test_report_escapes_pipe_characters_in_table_cells(self, tmp_path: Path):
        records = [
            scan_header_record(
                scan_id="scan-3",
                started_at="2026-04-29T17:26:46+09:00",
                root="/music",
            ),
            self._snapshot(
                "A/Z/01 - Track.flac",
                title="Track",
                album="A/Z | aLIEz",
                artists=["SawanoHiroyuki[nZk]"],
                album_artists=["SawanoHiroyuki[nZk]"],
                release_id="az-rel",
                release_group_id="az-group",
                recording_id="az-rec-1",
                track_total=1,
            ),
        ]
        report = generate_report(self._write_jsonl(tmp_path, records))

        assert "A/Z \\| aLIEz" in report
