"""Tests for the `complete` Stage 1 enrichment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tagwell.complete import (
    build_release_snapshot,
    build_releases_jsonl,
    compute_completeness,
    detect_instrumental,
    match_local_files,
)
from tagwell.schema import audio_file_record, scan_header_record, to_jsonl_line


# ---------- Synthetic MB raw payload helpers ----------

def _mb_track(
    *,
    position: int,
    title: str,
    recording_id: str,
    release_track_id: str,
    length: int = 200_000,
    inst_via: str | None = None,
    relations: list[dict] | None = None,
    disambiguation: str = "",
):
    rels = list(relations or [])
    if inst_via == "work-rel":
        rels.append({
            "type": "performance",
            "target-type": "work",
            "attributes": ["instrumental"],
        })
    if inst_via == "karaoke-rel":
        rels.append({
            "type": "performance",
            "target-type": "work",
            "attributes": ["karaoke"],
        })
    if inst_via == "disambiguation":
        disambiguation = disambiguation or "instrumental version"
    return {
        "id": release_track_id,
        "position": position,
        "number": str(position),
        "title": title,
        "length": length,
        "recording": {
            "id": recording_id,
            "title": title,
            "length": length,
            "disambiguation": disambiguation,
            "relations": rels,
        },
    }


def _mb_release(release_id: str, *, title: str, tracks: list[dict], date: str = "2020-01-01", country: str = "JP"):
    return {
        "id": release_id,
        "title": title,
        "date": date,
        "country": country,
        "artist-credit": [{"name": "Test Artist", "artist": {"id": "art-1", "name": "Test Artist"}}],
        "release-group": {"id": "rg-1", "primary-type": "Album", "secondary-types": []},
        "label-info": [{"label": {"name": "Test Label"}, "catalog-number": "TST-001"}],
        "media": [{
            "position": 1,
            "format": "CD",
            "track-count": len(tracks),
            "tracks": tracks,
        }],
    }


# ---------- Local snapshot helpers ----------

def _snapshot(
    relative_path: str,
    *,
    release_id: str | None,
    recording_id: str | None = None,
    release_track_id: str | None = None,
    track_number: int | None = None,
    disc_number: int | None = 1,
):
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
        audio_info={"container": "flac", "codec": "flac", "lossless": True},
        tags_info={
            "format": "vorbis_comment",
            "raw": {},
            "parsed": {"track_number": track_number, "disc_number": disc_number},
            "external_ids": {
                "musicbrainz": {
                    "release_id": release_id,
                    "recording_id": recording_id,
                    "release_track_id": release_track_id,
                }
            },
            "pictures": [],
        },
    )


def _write_library(tmp_path: Path, snapshots: list[dict]) -> Path:
    path = tmp_path / "library.jsonl"
    records = [scan_header_record(scan_id="scan-1", started_at="2026-04-30T00:00:00+00:00", root="/music")] + snapshots
    path.write_text("".join(to_jsonl_line(r) + "\n" for r in records), encoding="utf-8")
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------- detect_instrumental ----------

class TestDetectInstrumental:
    def test_work_rel_attribute_wins(self):
        track = {"title": "Some Song"}
        recording = {"relations": [
            {"type": "performance", "target-type": "work", "attributes": ["instrumental"]}
        ]}
        assert detect_instrumental(track, recording) == (True, "work-rel")

    def test_karaoke_attribute_counted_as_instrumental(self):
        track = {"title": "Some Song"}
        recording = {"relations": [
            {"type": "performance", "target-type": "work", "attributes": ["karaoke"]}
        ]}
        assert detect_instrumental(track, recording) == (True, "work-rel")

    def test_disambiguation_fallback(self):
        track = {"title": "Pure Vocal Title"}
        recording = {"disambiguation": "Off Vocal"}
        assert detect_instrumental(track, recording) == (True, "disambiguation")

    def test_title_fallback_english(self):
        for title in ["My Song (Instrumental)", "My Song (Off Vocal)", "My Song (Inst.)", "Karaoke Mix"]:
            track = {"title": title}
            assert detect_instrumental(track, {}) == (True, "title"), title

    def test_title_fallback_japanese(self):
        for title in ["楽曲 (オフボーカル)", "楽曲 (インスト)", "楽曲 (カラオケ)", "楽曲 (オフ・ボーカル)"]:
            track = {"title": title}
            assert detect_instrumental(track, {}) == (True, "title"), title

    def test_non_instrumental(self):
        track = {"title": "Regular Song"}
        recording = {"relations": [], "disambiguation": ""}
        assert detect_instrumental(track, recording) == (False, None)

    def test_word_boundary_no_false_positive(self):
        # "Instinct" / "Karaokesque" should not match
        track = {"title": "Instinct of Survival"}
        assert detect_instrumental(track, {}) == (False, None)

    def test_non_performance_relation_ignored(self):
        track = {"title": "Song"}
        recording = {"relations": [
            {"type": "remix", "target-type": "recording", "attributes": ["instrumental"]}
        ]}
        assert detect_instrumental(track, recording) == (False, None)


# ---------- match_local_files priority ----------

class TestMatchPriority:
    def _release(self):
        raw = _mb_release("rel-1", title="Album", tracks=[
            _mb_track(position=1, title="Track A", recording_id="rec-A", release_track_id="rt-A"),
            _mb_track(position=2, title="Track B", recording_id="rec-B", release_track_id="rt-B"),
            _mb_track(position=3, title="Track C", recording_id="rec-C", release_track_id="rt-C"),
        ])
        return build_release_snapshot(raw, "rel-1")

    def test_recording_id_wins_over_others(self):
        rel = self._release()
        snaps = [_snapshot("a.flac", release_id="rel-1", recording_id="rec-A", release_track_id="rt-X", track_number=99)]
        match_local_files(snaps, rel)
        assert rel["media"][0]["tracks"][0]["local"] == [
            {"relative_path": "a.flac", "matched_by": "recording_id"}
        ]

    def test_release_track_id_fallback(self):
        rel = self._release()
        snaps = [_snapshot("b.flac", release_id="rel-1", recording_id=None, release_track_id="rt-B", track_number=99)]
        match_local_files(snaps, rel)
        assert rel["media"][0]["tracks"][1]["local"] == [
            {"relative_path": "b.flac", "matched_by": "release_track_id"}
        ]

    def test_position_fallback(self):
        rel = self._release()
        snaps = [_snapshot("c.flac", release_id="rel-1", recording_id=None, release_track_id=None, track_number=3)]
        match_local_files(snaps, rel)
        assert rel["media"][0]["tracks"][2]["local"] == [
            {"relative_path": "c.flac", "matched_by": "position"}
        ]

    def test_multiple_local_copies_listed(self):
        rel = self._release()
        snaps = [
            _snapshot("flac/01.flac", release_id="rel-1", recording_id="rec-A", track_number=1),
            _snapshot("mp3/01.mp3", release_id="rel-1", recording_id="rec-A", track_number=1),
        ]
        match_local_files(snaps, rel)
        assert rel["media"][0]["tracks"][0]["local"] == [
            {"relative_path": "flac/01.flac", "matched_by": "recording_id"},
            {"relative_path": "mp3/01.mp3", "matched_by": "recording_id"},
        ]

    def test_other_release_ignored(self):
        rel = self._release()
        snaps = [_snapshot("x.flac", release_id="rel-OTHER", recording_id="rec-A", track_number=1)]
        match_local_files(snaps, rel)
        for track in rel["media"][0]["tracks"]:
            assert track["local"] == []


# ---------- compute_completeness ----------

class TestCompleteness:
    def _build(self, tracks):
        raw = _mb_release("rel-1", title="Album", tracks=tracks)
        return build_release_snapshot(raw, "rel-1")

    def test_all_vocal_present(self):
        rel = self._build([
            _mb_track(position=1, title="A", recording_id="rec-A", release_track_id="rt-A"),
            _mb_track(position=2, title="B", recording_id="rec-B", release_track_id="rt-B"),
        ])
        rel["media"][0]["tracks"][0]["local"] = [{"relative_path": "01.flac", "matched_by": "recording_id"}]
        rel["media"][0]["tracks"][1]["local"] = [{"relative_path": "02.flac", "matched_by": "recording_id"}]
        compute_completeness(rel)
        c = rel["completeness"]
        assert c["ratio_vocal"] == 1.0
        assert c["ratio_naive"] == 1.0
        assert c["tracks_vocal_missing"] == 0

    def test_inst_tracks_excluded_from_main_ratio(self):
        rel = self._build([
            _mb_track(position=1, title="A", recording_id="rec-A", release_track_id="rt-A"),
            _mb_track(position=2, title="A (Off Vocal)", recording_id="rec-A2", release_track_id="rt-A2", inst_via="work-rel"),
            _mb_track(position=3, title="B", recording_id="rec-B", release_track_id="rt-B"),
            _mb_track(position=4, title="B (Karaoke)", recording_id="rec-B2", release_track_id="rt-B2", inst_via="karaoke-rel"),
        ])
        # User has only the vocal versions
        rel["media"][0]["tracks"][0]["local"] = [{"relative_path": "01.flac", "matched_by": "recording_id"}]
        rel["media"][0]["tracks"][2]["local"] = [{"relative_path": "03.flac", "matched_by": "recording_id"}]
        compute_completeness(rel)
        c = rel["completeness"]
        assert c["tracks_total"] == 4
        assert c["tracks_instrumental"] == 2
        assert c["tracks_vocal_total"] == 2
        assert c["tracks_vocal_present"] == 2
        assert c["ratio_vocal"] == 1.0
        assert c["ratio_naive"] == 0.5

    def test_partial_vocal_missing(self):
        rel = self._build([
            _mb_track(position=1, title="A", recording_id="rec-A", release_track_id="rt-A"),
            _mb_track(position=2, title="B", recording_id="rec-B", release_track_id="rt-B"),
            _mb_track(position=3, title="C", recording_id="rec-C", release_track_id="rt-C"),
            _mb_track(position=4, title="D", recording_id="rec-D", release_track_id="rt-D"),
        ])
        rel["media"][0]["tracks"][0]["local"] = [{"relative_path": "x.flac", "matched_by": "recording_id"}]
        compute_completeness(rel)
        c = rel["completeness"]
        assert c["ratio_vocal"] == 0.25
        assert c["tracks_vocal_missing"] == 3


# ---------- Orchestrator: end-to-end with fake fetcher ----------

def _make_fetcher(canned: dict[str, dict]):
    calls: list[str] = []

    def fetcher(rid: str) -> dict:
        calls.append(rid)
        if rid in canned:
            return canned[rid]
        raise RuntimeError(f"unknown release_id {rid}")

    fetcher.calls = calls  # type: ignore[attr-defined]
    return fetcher


class TestOrchestrator:
    def test_writes_header_snapshots_orphan_trailer(self, tmp_path: Path):
        library = _write_library(tmp_path, [
            _snapshot("alpha/01.flac", release_id="rel-A", recording_id="rec-A1", track_number=1),
            _snapshot("alpha/02.flac", release_id="rel-A", recording_id="rec-A2", track_number=2),
            _snapshot("orphan/x.mp3", release_id=None, track_number=1),
        ])
        canned = {
            "rel-A": _mb_release("rel-A", title="Alpha", tracks=[
                _mb_track(position=1, title="Track 1", recording_id="rec-A1", release_track_id="rt-A1"),
                _mb_track(position=2, title="Track 2", recording_id="rec-A2", release_track_id="rt-A2"),
            ])
        }
        out_path = tmp_path / "library_releases.jsonl"

        summary = build_releases_jsonl(
            library, out_path,
            delay=0, fetcher=_make_fetcher(canned), sleep=lambda _: None,
        )

        recs = _read_jsonl(out_path)
        assert recs[0]["record_type"] == "releases_header"
        assert recs[0]["schema_version"] == 2
        assert recs[0]["stage"]["incremental"] is True
        assert recs[1]["record_type"] == "release_snapshot"
        assert recs[1]["release_id"] == "rel-A"
        assert recs[1]["completeness"]["ratio_vocal"] == 1.0
        assert recs[-2]["record_type"] == "orphan_file"
        assert recs[-2]["count"] == 1
        assert recs[-1]["record_type"] == "releases_trailer"
        assert recs[-1]["stats"]["matched_by_recording_id"] == 2

        assert summary.fetched == 1
        assert summary.orphan_files == 1
        assert summary.matched_local_files == 2

    def test_incremental_skips_cached_release(self, tmp_path: Path):
        library = _write_library(tmp_path, [
            _snapshot("alpha/01.flac", release_id="rel-A", recording_id="rec-A1", track_number=1),
        ])
        canned = {
            "rel-A": _mb_release("rel-A", title="Alpha", tracks=[
                _mb_track(position=1, title="T1", recording_id="rec-A1", release_track_id="rt-A1"),
            ])
        }
        out_path = tmp_path / "library_releases.jsonl"

        # First run
        fetcher1 = _make_fetcher(canned)
        build_releases_jsonl(library, out_path, delay=0, fetcher=fetcher1, sleep=lambda _: None)
        assert fetcher1.calls == ["rel-A"]

        # Second run — same library, should hit cache
        fetcher2 = _make_fetcher(canned)
        summary = build_releases_jsonl(library, out_path, delay=0, fetcher=fetcher2, sleep=lambda _: None)
        assert fetcher2.calls == []
        assert summary.skipped_cached == 1
        assert summary.fetched == 0

    def test_refresh_flag_forces_refetch(self, tmp_path: Path):
        library = _write_library(tmp_path, [
            _snapshot("alpha/01.flac", release_id="rel-A", recording_id="rec-A1", track_number=1),
        ])
        canned = {
            "rel-A": _mb_release("rel-A", title="Alpha", tracks=[
                _mb_track(position=1, title="T1", recording_id="rec-A1", release_track_id="rt-A1"),
            ])
        }
        out_path = tmp_path / "library_releases.jsonl"

        fetcher1 = _make_fetcher(canned)
        build_releases_jsonl(library, out_path, delay=0, fetcher=fetcher1, sleep=lambda _: None)

        fetcher2 = _make_fetcher(canned)
        build_releases_jsonl(library, out_path, delay=0, fetcher=fetcher2, sleep=lambda _: None, refresh=True)
        assert fetcher2.calls == ["rel-A"]

    def test_errors_are_retried_on_next_run(self, tmp_path: Path):
        library = _write_library(tmp_path, [
            _snapshot("alpha/01.flac", release_id="rel-A", recording_id="rec-A1", track_number=1),
        ])
        out_path = tmp_path / "library_releases.jsonl"

        # First run: fetcher fails
        def fail(rid):
            raise RuntimeError("503")
        summary1 = build_releases_jsonl(library, out_path, delay=0, fetcher=fail, sleep=lambda _: None)
        assert summary1.failed == 1
        recs = _read_jsonl(out_path)
        assert any(r["record_type"] == "release_error" for r in recs)

        # Second run: fetcher succeeds — should retry, not skip
        canned = {
            "rel-A": _mb_release("rel-A", title="Alpha", tracks=[
                _mb_track(position=1, title="T1", recording_id="rec-A1", release_track_id="rt-A1"),
            ])
        }
        fetcher2 = _make_fetcher(canned)
        summary2 = build_releases_jsonl(library, out_path, delay=0, fetcher=fetcher2, sleep=lambda _: None)
        assert fetcher2.calls == ["rel-A"]
        assert summary2.fetched == 1
        recs = _read_jsonl(out_path)
        assert not any(r["record_type"] == "release_error" for r in recs)
        assert any(r["record_type"] == "release_snapshot" for r in recs)

    def test_stale_release_id_is_pruned(self, tmp_path: Path):
        # First run: library has rel-A and rel-B
        library_v1 = _write_library(tmp_path, [
            _snapshot("alpha/01.flac", release_id="rel-A", recording_id="rec-A1", track_number=1),
            _snapshot("beta/01.flac", release_id="rel-B", recording_id="rec-B1", track_number=1),
        ])
        canned = {
            "rel-A": _mb_release("rel-A", title="A", tracks=[_mb_track(position=1, title="t", recording_id="rec-A1", release_track_id="rt-A1")]),
            "rel-B": _mb_release("rel-B", title="B", tracks=[_mb_track(position=1, title="t", recording_id="rec-B1", release_track_id="rt-B1")]),
        }
        out_path = tmp_path / "library_releases.jsonl"
        build_releases_jsonl(library_v1, out_path, delay=0, fetcher=_make_fetcher(canned), sleep=lambda _: None)

        recs1 = _read_jsonl(out_path)
        rids1 = {r["release_id"] for r in recs1 if r["record_type"] == "release_snapshot"}
        assert rids1 == {"rel-A", "rel-B"}

        # Second run: rel-B no longer in library (file deleted)
        library_v2 = _write_library(tmp_path, [
            _snapshot("alpha/01.flac", release_id="rel-A", recording_id="rec-A1", track_number=1),
        ])
        fetcher2 = _make_fetcher(canned)
        summary = build_releases_jsonl(library_v2, out_path, delay=0, fetcher=fetcher2, sleep=lambda _: None)

        # rel-B should be gone, rel-A should still be cached
        assert fetcher2.calls == []
        recs2 = _read_jsonl(out_path)
        rids2 = {r["release_id"] for r in recs2 if r["record_type"] == "release_snapshot"}
        assert rids2 == {"rel-A"}
        assert summary.unique_release_ids == 1
        assert summary.skipped_cached == 1

    def test_no_orphan_record_when_all_files_have_release_id(self, tmp_path: Path):
        library = _write_library(tmp_path, [
            _snapshot("alpha/01.flac", release_id="rel-A", recording_id="rec-A1", track_number=1),
        ])
        canned = {
            "rel-A": _mb_release("rel-A", title="A", tracks=[_mb_track(position=1, title="t", recording_id="rec-A1", release_track_id="rt-A1")]),
        }
        out_path = tmp_path / "library_releases.jsonl"
        build_releases_jsonl(library, out_path, delay=0, fetcher=_make_fetcher(canned), sleep=lambda _: None)

        recs = _read_jsonl(out_path)
        assert not any(r["record_type"] == "orphan_file" for r in recs)
