"""Tests for the releases-level Markdown report."""

from __future__ import annotations

import json
from pathlib import Path

from tagwell.complete import RELEASES_SCHEMA_VERSION, build_release_snapshot, compute_completeness
from tagwell.releases_report import generate_releases_report


# ---------- Synthetic fixture builders ----------

def _track(position: int, title: str, *, recording_id: str = "", release_track_id: str = "",
           is_inst: bool = False, inst_signal: str | None = None,
           local: list | None = None) -> dict:
    return {
        "position": position,
        "number": str(position),
        "title": title,
        "length_ms": 200_000,
        "release_track_id": release_track_id or f"rt-{position}",
        "recording_id": recording_id or f"rec-{position}",
        "is_instrumental": is_inst,
        "inst_signal": inst_signal,
        "local": local or [],
    }


def _release(release_id: str, title: str, *, artist: str = "Test Artist",
             country: str = "JP", date: str = "2020-01-01",
             primary_type: str = "Album", secondary_types: list | None = None,
             tracks: list | None = None, media: list | None = None) -> dict:
    if media is None:
        media = [{"position": 1, "format": "CD", "track_count": len(tracks or []), "tracks": tracks or []}]
    rec = {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "release_snapshot",
        "release_id": release_id,
        "title": title,
        "date": date,
        "country": country,
        "artist_credit": [{"name": artist, "id": f"art-{artist}"}],
        "release_group": {"id": "rg-1", "primary_type": primary_type, "secondary_types": secondary_types or []},
        "labels": [],
        "media": media,
        "completeness": {},
    }
    compute_completeness(rec)
    return rec


def _write(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "library_releases.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def _header(**stage_overrides) -> dict:
    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "releases_header",
        "scanner": {"name": "tagwell", "version": "0.1.0"},
        "stage": {
            "name": "complete",
            "started_at": "2026-04-30T13:00:00+09:00",
            "source_jsonl": "library.jsonl",
            "mb_api_base": "https://musicbrainz.org/ws/2",
            "mb_inc": "recordings+media+release-groups+artist-credits+labels+work-rels",
            "incremental": True,
            "delay_seconds": 1.0,
            **stage_overrides,
        },
    }


def _trailer(**stat_overrides) -> dict:
    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "releases_trailer",
        "completed_at": "2026-04-30T13:05:00+09:00",
        "elapsed_seconds": 300.0,
        "stats": {
            "unique_release_ids": 1,
            "fetched": 1,
            "skipped_cached": 0,
            "failed": 0,
            "orphan_files": 0,
            "total_local_files": 1,
            "matched_local_files": 1,
            "matched_by_recording_id": 1,
            "matched_by_release_track_id": 0,
            "matched_by_position": 0,
            **stat_overrides,
        },
    }


# ---------- Tests ----------

def test_empty_jsonl(tmp_path: Path):
    path = _write(tmp_path, _header(), _trailer())
    md = generate_releases_report(path)
    assert "No release records found" in md


def test_single_artist_below_100_lists_missing_tracks_truncated(tmp_path: Path):
    rel = _release("rel-1", "Album X", artist="Solo Artist", tracks=[
        _track(1, "Song A", local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track(2, "Song B"),
        _track(3, "Song C"),
        _track(4, "Song D"),
        _track(5, "Song E"),
        _track(6, "Song F"),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), rel, _trailer()))

    assert "## Below-100% releases" in md
    assert "### Single-artist releases below 100%" in md
    assert "Baseline completeness view" in md
    assert "| Album | Album artist | Vocal | Ratio | Missing tracks |" in md
    assert "Artist-weighted view" in md
    assert "Artist tracks in library" in md
    assert "Song B, Song C, Song D (+2 more)" in md


def test_single_artist_below_100_ranks_by_artist_depth_score(tmp_path: Path):
    deep_complete = _release("rel-deep-1", "Deep Catalog", artist="Deep Artist", tracks=[
        _track(1, "Deep 1", local=[{"relative_path": "d1.flac", "matched_by": "recording_id"}]),
        _track(2, "Deep 2", local=[{"relative_path": "d2.flac", "matched_by": "recording_id"}]),
        _track(3, "Deep 3", local=[{"relative_path": "d3.flac", "matched_by": "recording_id"}]),
    ])
    deep_partial = _release("rel-deep-2", "Deep Candidate", artist="Deep Artist", tracks=[
        _track(1, "Owned", local=[{"relative_path": "d4.flac", "matched_by": "recording_id"}]),
        _track(2, "Missing"),
    ])
    shallow_partial = _release("rel-shallow", "Shallow Candidate", artist="Shallow Artist", tracks=[
        _track(1, "Owned", local=[{"relative_path": "s1.flac", "matched_by": "recording_id"}]),
        _track(2, "Miss 1"),
        _track(3, "Miss 2"),
        _track(4, "Miss 3"),
        _track(5, "Miss 4"),
    ])

    md = generate_releases_report(
        _write(tmp_path, _header(), deep_complete, deep_partial, shallow_partial, _trailer())
    )

    single_section = md.split("### Single-artist releases below 100%")[1].split("###")[0]
    baseline_section = single_section.split("Artist-weighted view:")[0]
    weighted_section = single_section.split("Artist-weighted view:")[1]

    assert "| Deep Candidate | Deep Artist | 1/2 | 50.0% | Missing |" in baseline_section
    assert "| Shallow Candidate | Shallow Artist | 1/5 | 20.0% | Miss 1, Miss 2, Miss 3 (+1 more) |" in baseline_section
    assert baseline_section.index("Shallow Candidate") < baseline_section.index("Deep Candidate")

    assert "| Deep Candidate | Deep Artist | 4 | 1/2 | 50.0% | Missing |" in weighted_section
    assert "| Shallow Candidate | Shallow Artist | 1 | 1/5 | 20.0% | Miss 1, Miss 2, Miss 3 (+1 more) |" in weighted_section
    assert weighted_section.index("Deep Candidate") < weighted_section.index("Shallow Candidate")


def test_compilation_routes_to_cherry_picked_section(tmp_path: Path):
    compilation = _release("rel-comp", "Best Of", artist="Various Artists",
                           primary_type="Album", secondary_types=["Compilation"], tracks=[
        _track(1, "T1", local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track(2, "T2"),
        _track(3, "T3"),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), compilation, _trailer()))

    assert "### Cherry-picked compilations / soundtracks" in md
    assert "Best Of" in md.split("Cherry-picked compilations")[1]
    # Should NOT appear in the single-artist section
    if "### Single-artist releases below 100%" in md:
        single_section = md.split("### Single-artist releases below 100%")[1].split("###")[0]
        assert "Best Of" not in single_section


def test_soundtrack_routes_to_cherry_picked_section(tmp_path: Path):
    ost = _release("rel-ost", "Game OST", artist="Composer X",
                   primary_type="Album", secondary_types=["Soundtrack"], tracks=[
        _track(1, "Theme", local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track(2, "BGM 1"),
        _track(3, "BGM 2"),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), ost, _trailer()))

    assert "### Cherry-picked compilations / soundtracks" in md
    assert "Game OST" in md.split("Cherry-picked compilations")[1]


def test_various_artists_album_routes_to_cherry_picked(tmp_path: Path):
    va = _release("rel-va", "VA Album", artist="Various Artists",
                  primary_type="Album", tracks=[
        _track(1, "T1", local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track(2, "T2"),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), va, _trailer()))

    assert "### Cherry-picked compilations / soundtracks" in md
    cherry_section = md.split("Cherry-picked compilations")[1]
    assert "VA Album" in cherry_section


def test_inst_audit_shows_signal_breakdown(tmp_path: Path):
    rel = _release("rel-1", "Album", tracks=[
        _track(1, "A", local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track(2, "A (Off Vocal)", is_inst=True, inst_signal="work-rel"),
        _track(3, "A (Inst.)", is_inst=True, inst_signal="title"),
        _track(4, "A v2", is_inst=True, inst_signal="disambiguation"),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), rel, _trailer()))

    assert "## Instrumental detection audit" in md
    assert "| work-rel | 1 |" in md
    assert "| disambiguation | 1 |" in md
    assert "| title | 1 |" in md
    assert "**Total instrumental tracks**: 3" in md


def test_match_audit_flags_position_only_releases(tmp_path: Path):
    weak_rel = _release("rel-weak", "Position Album", tracks=[
        _track(1, "T1", local=[{"relative_path": "a.flac", "matched_by": "position"}]),
        _track(2, "T2", local=[{"relative_path": "b.flac", "matched_by": "position"}]),
    ])
    strong_rel = _release("rel-strong", "MBID Album", tracks=[
        _track(1, "T1", local=[{"relative_path": "x.flac", "matched_by": "recording_id"}]),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), strong_rel, weak_rel, _trailer()))

    assert "## MB match audit" in md
    assert "| recording_id | 1 |" in md
    assert "| position | 2 |" in md
    assert "### Releases relying on position-match" in md
    assert "Position Album" in md
    # Strong release should NOT appear in the weak-releases subsection
    weak_section = md.split("### Releases relying on position-match")[1]
    assert "MBID Album" not in weak_section


def test_release_group_composition_with_secondary_types(tmp_path: Path):
    a = _release("rel-a", "A", primary_type="Album")
    b = _release("rel-b", "B", primary_type="Album", secondary_types=["Compilation"])
    c = _release("rel-c", "C", primary_type="EP")
    md = generate_releases_report(_write(tmp_path, _header(), a, b, c, _trailer()))

    assert "## Release-group composition" in md
    assert "| Album | 1 |" in md
    assert "| Album + Compilation | 1 |" in md
    assert "| EP | 1 |" in md


def test_country_profile(tmp_path: Path):
    md = generate_releases_report(_write(
        tmp_path,
        _header(),
        _release("rel-jp", "A", country="JP"),
        _release("rel-jp2", "B", country="JP"),
        _release("rel-us", "C", country="US"),
        _release("rel-no", "D", country=""),
        _trailer(),
    ))

    assert "## Release country profile" in md
    assert "JP (Japan)" in md
    assert "US (United States)" in md
    assert "**Missing country**: 1" in md


def test_decade_profile(tmp_path: Path):
    md = generate_releases_report(_write(
        tmp_path,
        _header(),
        _release("rel-1", "A", date="1985-06-01"),
        _release("rel-2", "B", date="2007-08-15"),
        _release("rel-3", "C", date="2008-01-01"),
        _release("rel-4", "D", date=""),
        _trailer(),
    ))

    assert "## Release timeline" in md
    assert "| 1980s | 1 |" in md
    assert "| 2000s | 2 |" in md
    assert "**Missing date**: 1" in md


def test_failed_releases_section(tmp_path: Path):
    error = {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "release_error",
        "release_id": "deadbeef-1234-5678",
        "error": {
            "stage": "fetch_release",
            "kind": "HTTPError",
            "message": "503 Service Unavailable",
            "attempted_at": "2026-04-30T13:32:14+09:00",
        },
    }
    rel = _release("rel-ok", "OK Album")
    md = generate_releases_report(_write(tmp_path, _header(), rel, error, _trailer()))

    assert "## Failed releases" in md
    assert "deadbeef-1234-5678" in md
    assert "HTTPError: 503 Service Unavailable" in md


def test_multi_disc_section_condensed(tmp_path: Path):
    rel = _release("rel-1", "Big Box", media=[
        {"position": 1, "format": "CD", "track_count": 1, "tracks": [_track(1, "A")]},
        {"position": 2, "format": "CD", "track_count": 1, "tracks": [_track(1, "B")]},
        {"position": 3, "format": "CD", "track_count": 1, "tracks": [_track(1, "C")]},
    ])
    md = generate_releases_report(_write(tmp_path, _header(), rel, _trailer()))

    assert "## Multi-disc releases" in md
    assert "**1** releases span multiple media" in md
    assert "Big Box (3 discs)" in md
    # Old verbose table should NOT exist
    assert "| Album | Album artist | Discs |" not in md


def test_no_below_100_section_when_all_complete(tmp_path: Path):
    rel = _release("rel-1", "Complete Album", artist="Solo", tracks=[
        _track(1, "A", local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track(2, "B", local=[{"relative_path": "b.flac", "matched_by": "recording_id"}]),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), rel, _trailer()))

    assert "## Below-100% releases" in md
    assert "All releases are at 100% vocal completeness." in md


def test_overview_counts_releases_at_100(tmp_path: Path):
    a = _release("a", "A", artist="Solo", tracks=[
        _track(1, "T1", local=[{"relative_path": "1.flac", "matched_by": "recording_id"}]),
        _track(2, "T2", local=[{"relative_path": "2.flac", "matched_by": "recording_id"}]),
    ])
    b = _release("b", "B", artist="Solo", tracks=[
        _track(1, "T1", local=[]),
        _track(2, "T (Inst)", is_inst=True, inst_signal="title"),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), a, b, _trailer()))

    assert "## Overview" in md
    # A is 100%, B is 0% → 1 of 2 is fully complete (50%)
    assert "Releases at 100% vocal completeness | 1 (50.0%)" in md
    # The misleading aggregate ratio should be gone
    assert "Library-wide vocal completeness" not in md


def test_completeness_by_type_table(tmp_path: Path):
    album_complete = _release("a", "Album A", artist="Solo", primary_type="Album", tracks=[
        _track(1, "T", local=[{"relative_path": "1.flac", "matched_by": "recording_id"}]),
    ])
    album_partial = _release("b", "Album B", artist="Solo", primary_type="Album", tracks=[
        _track(1, "T1", local=[{"relative_path": "2.flac", "matched_by": "recording_id"}]),
        _track(2, "T2"),
    ])
    compilation = _release("c", "Comp", artist="Various Artists",
                           primary_type="Album", secondary_types=["Compilation"], tracks=[
        _track(1, "T1", local=[{"relative_path": "3.flac", "matched_by": "recording_id"}]),
        _track(2, "T2"),
        _track(3, "T3"),
        _track(4, "T4"),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), album_complete, album_partial, compilation, _trailer()))

    assert "## Vocal completeness by release type" in md
    # Album bucket: 2 releases, ratios 1.0 and 0.5 → avg 75%, 1 at 100%
    assert "| Album | 2 | 75.0% | 1 (50.0%) |" in md
    # Compilation bucket: 1 release at 25%
    assert "| Album + Compilation | 1 | 25.0% | 0 (0.0%) |" in md


def _track_with_credits(position: int, title: str, *,
                        work_rels: list | None = None,
                        local: list | None = None) -> dict:
    t = _track(position, title, local=local)
    t["work_rels"] = work_rels or []
    return t


def test_top_composers_section(tmp_path: Path):
    rel = _release("rel-1", "Sakamichi Album", artist="坂本真綾", tracks=[
        _track_with_credits(1, "Song A", work_rels=[
            {"type": "composer", "artist": {"id": "art-c", "name": "菅野よう子"}},
        ], local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track_with_credits(2, "Song B", work_rels=[
            {"type": "composer", "artist": {"id": "art-c", "name": "菅野よう子"}},
            {"type": "lyricist", "artist": {"id": "art-l", "name": "岩里祐穂"}},
        ], local=[{"relative_path": "b.flac", "matched_by": "recording_id"}]),
        _track_with_credits(3, "Song C"),  # no work_rels
    ])
    md = generate_releases_report(_write(tmp_path, _header(), rel, _trailer()))

    assert "## Top composers — release-level" in md
    assert "菅野よう子" in md
    assert "| 菅野よう子 | 2 |" in md

    assert "## Top lyricists — release-level" in md
    assert "岩里祐穂" in md

    # Library-level should also appear
    assert "## Top composers — library-level" in md
    assert "## Top lyricists — library-level" in md


def test_composer_artist_matrix(tmp_path: Path):
    rel1 = _release("rel-1", "Album A", artist="坂本真綾", tracks=[
        _track_with_credits(1, "S1", work_rels=[
            {"type": "composer", "artist": {"id": "art-c", "name": "菅野よう子"}},
        ], local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track_with_credits(2, "S1b", work_rels=[
            {"type": "composer", "artist": {"id": "art-c", "name": "菅野よう子"}},
        ], local=[{"relative_path": "a2.flac", "matched_by": "recording_id"}]),
    ])
    rel2 = _release("rel-2", "Album B", artist="KOTOKO", tracks=[
        _track_with_credits(1, "S2", work_rels=[
            {"type": "composer", "artist": {"id": "art-c2", "name": "中坪淳彦"}},
        ], local=[{"relative_path": "b.flac", "matched_by": "recording_id"}]),
        _track_with_credits(2, "S2b", work_rels=[
            {"type": "composer", "artist": {"id": "art-c2", "name": "中坪淳彦"}},
        ], local=[{"relative_path": "b2.flac", "matched_by": "recording_id"}]),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), rel1, rel2, _trailer()))

    assert "## Composer × album-artist connections" in md
    assert "菅野よう子" in md
    assert "坂本真綾" in md
    assert "中坪淳彦" in md


def test_composer_coverage_section(tmp_path: Path):
    rel = _release("rel-1", "Album", tracks=[
        _track_with_credits(1, "Has composer", work_rels=[
            {"type": "composer", "artist": {"id": "art-c", "name": "X"}},
        ], local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
        _track_with_credits(2, "No composer", local=[{"relative_path": "b.flac", "matched_by": "recording_id"}]),
        _track_with_credits(3, "Inst", work_rels=[], local=[{"relative_path": "c.flac", "matched_by": "recording_id"}]),
    ])
    # make track 3 instrumental
    rel["media"][0]["tracks"][2]["is_instrumental"] = True
    compute_completeness(rel)
    md = generate_releases_report(_write(tmp_path, _header(), rel, _trailer()))

    assert "## Composer coverage" in md
    assert "Has composer" in md
    assert "No composer" in md


def test_no_creator_section_when_no_work_rels(tmp_path: Path):
    rel = _release("rel-1", "Album", tracks=[
        _track(1, "Song", local=[{"relative_path": "a.flac", "matched_by": "recording_id"}]),
    ])
    md = generate_releases_report(_write(tmp_path, _header(), rel, _trailer()))

    assert "## Top composers" not in md
    assert "## Composer ×" not in md
