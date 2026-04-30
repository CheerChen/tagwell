"""Tests for the audio quality report."""

from pathlib import Path

from tagwell.quality import generate_quality_report
from tagwell.schema import audio_file_record, scan_header_record, to_jsonl_line


def _audio(
    *,
    container: str,
    codec: str | None = None,
    lossless: bool | None,
    duration: float = 200.0,
    sample_rate: int = 44100,
    channels: int = 2,
    bit_depth: int | None = 16,
    bitrate: float | None = None,
    bitrate_mode: str | None = None,
    encoder_info: str | None = None,
    encoder_settings: str | None = None,
    sketchy: bool | None = None,
) -> dict:
    return {
        "container": container,
        "codec": codec or container,
        "duration_seconds": duration,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "bit_depth": bit_depth,
        "lossless": lossless,
        "estimated_bitrate_kbps": bitrate,
        "bitrate_mode": bitrate_mode,
        "encoder_info": encoder_info,
        "encoder_settings": encoder_settings,
        "sketchy": sketchy,
    }


def _snapshot(relative_path: str, audio: dict) -> dict:
    return audio_file_record(
        file_info={
            "relative_path": relative_path,
            "name": Path(relative_path).name,
            "stem": Path(relative_path).stem,
            "ext": Path(relative_path).suffix,
            "size_bytes": 1024,
            "mtime": "2026-01-01T00:00:00+00:00",
            "ctime": None,
        },
        audio_info=audio,
        tags_info={"format": "vorbis_comment", "raw": {}, "parsed": {}, "external_ids": {"musicbrainz": {}}, "pictures": []},
    )


def _write_jsonl(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "library.jsonl"
    path.write_text("".join(to_jsonl_line(record) + "\n" for record in records), encoding="utf-8")
    return path


def test_quality_report_buckets_flac_by_encoder_and_flags_lavf(tmp_path: Path):
    records = [
        scan_header_record(scan_id="s", started_at="2026-04-30T00:00:00+00:00", root="/m"),
        _snapshot("A/01.flac", _audio(
            container="flac", lossless=True, bitrate=900.0,
            encoder_info="reference libFLAC 1.3.4 20220220",
        )),
        _snapshot("B/01.flac", _audio(
            container="flac", lossless=True, bitrate=850.0,
            encoder_info="Lavf57.71.100",
        )),
        _snapshot("C/01.flac", _audio(
            container="flac", lossless=True, bitrate=820.0,
            encoder_info="Lavf57.83.100",
        )),
        _snapshot("D/01.flac", _audio(
            container="flac", lossless=True, bitrate=900.0,
            encoder_info=None,
        )),
    ]
    md = generate_quality_report(_write_jsonl(tmp_path, records))

    assert "## Lossless integrity (FLAC)" in md
    assert "| reference libFLAC | 1 | 25.0% |" in md
    assert "| Lavf (FFmpeg) | 2 | 50.0% |" in md
    assert "| Missing | 1 | 25.0% |" in md
    # Red flag: Lavf-muxed FLACs listed
    assert "### FLAC files muxed by FFmpeg (2)" in md
    assert "| B/01.flac | Lavf57.71.100 |" in md
    assert "| C/01.flac | Lavf57.83.100 |" in md


def test_quality_report_mp3_breakdown_modes_and_encoders(tmp_path: Path):
    records = [
        scan_header_record(scan_id="s", started_at="2026-04-30T00:00:00+00:00", root="/m"),
        _snapshot("A/01.mp3", _audio(
            container="mp3", lossless=False, bitrate=320.0,
            bitrate_mode="cbr", encoder_info="LAME 3.100.0+", encoder_settings="-b 320",
            sketchy=False,
        )),
        _snapshot("A/02.mp3", _audio(
            container="mp3", lossless=False, bitrate=192.0,
            bitrate_mode="vbr", encoder_info="LAME 3.99.5",
            sketchy=False,
        )),
        _snapshot("A/03.mp3", _audio(
            container="mp3", lossless=False, bitrate=128.0,
            bitrate_mode=None, encoder_info=None, sketchy=True,
        )),
    ]
    md = generate_quality_report(_write_jsonl(tmp_path, records))

    assert "## MP3 encoder breakdown" in md
    assert "| CBR | 1 | 33.3% |" in md
    assert "| VBR | 1 | 33.3% |" in md
    assert "| unknown | 1 | 33.3% |" in md.lower()
    assert "| LAME 3.100.0+ | 1 | 33.3% |" in md
    assert "| (missing — no LAME/Xing header) | 1 | 33.3% |" in md
    # Sketchy red flag
    assert "### MP3 files with sketchy frame parsing (1)" in md
    assert "- `A/03.mp3`" in md
    # Lossy bitrate table appears (multiple bitrates)
    assert "## Lossy bitrate distribution" in md
    assert "| 320 kbps | 1 | 33.3% | CBR |" in md
    assert "| 192 kbps | 1 | 33.3% | VBR |" in md


def test_quality_report_flags_suspect_low_bitrate_lossless(tmp_path: Path):
    records = [
        scan_header_record(scan_id="s", started_at="2026-04-30T00:00:00+00:00", root="/m"),
        # Genuine: bitrate above threshold — not flagged
        _snapshot("ok/01.flac", _audio(
            container="flac", lossless=True, bitrate=900.0,
            encoder_info="reference libFLAC 1.3.4 20220220",
        )),
        # Suspiciously low bitrate at 16/44.1 stereo — flagged
        _snapshot("bad/01.flac", _audio(
            container="flac", lossless=True, bitrate=420.0,
            encoder_info="reference libFLAC 1.3.4 20220220",
        )),
        # Hi-res FLAC at low bitrate — NOT flagged (heuristic skips >16-bit)
        _snapshot("hires/01.flac", _audio(
            container="flac", lossless=True, bitrate=600.0, bit_depth=24, sample_rate=96000,
            encoder_info="reference libFLAC 1.3.4 20220220",
        )),
    ]
    md = generate_quality_report(_write_jsonl(tmp_path, records))

    assert "### Lossless files with suspiciously low bitrate (1)" in md
    assert "| bad/01.flac | 420 |" in md
    assert "hires/01.flac" not in md.split("Lossless files with suspiciously low bitrate")[1]


def test_quality_report_clean_library_says_no_red_flags(tmp_path: Path):
    records = [
        scan_header_record(scan_id="s", started_at="2026-04-30T00:00:00+00:00", root="/m"),
        _snapshot("A/01.flac", _audio(
            container="flac", lossless=True, bitrate=900.0,
            encoder_info="reference libFLAC 1.3.4 20220220",
        )),
        _snapshot("A/02.mp3", _audio(
            container="mp3", lossless=False, bitrate=320.0,
            bitrate_mode="cbr", encoder_info="LAME 3.100.0+", sketchy=False,
        )),
    ]
    md = generate_quality_report(_write_jsonl(tmp_path, records))

    assert "## Red flags" in md
    assert "No quality red flags detected." in md


def test_quality_report_collapses_single_bitrate_summary(tmp_path: Path):
    records = [
        scan_header_record(scan_id="s", started_at="2026-04-30T00:00:00+00:00", root="/m"),
        _snapshot("A/01.mp3", _audio(
            container="mp3", lossless=False, bitrate=320.0,
            bitrate_mode="cbr", encoder_info="LAME 3.100.0+", sketchy=False,
        )),
        _snapshot("A/02.mp3", _audio(
            container="mp3", lossless=False, bitrate=320.0,
            bitrate_mode="cbr", encoder_info="LAME 3.100.0+", sketchy=False,
        )),
    ]
    md = generate_quality_report(_write_jsonl(tmp_path, records))

    assert "All 2 lossy files are 320 kbps (CBR)." in md


def test_quality_report_empty_jsonl(tmp_path: Path):
    records = [
        scan_header_record(scan_id="s", started_at="2026-04-30T00:00:00+00:00", root="/m"),
    ]
    md = generate_quality_report(_write_jsonl(tmp_path, records))
    assert "No audio files found in JSONL." in md
