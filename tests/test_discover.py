"""Tests for the `discover` stage."""

from __future__ import annotations

import json
from pathlib import Path

from tagwell.discover import (
    ArtistResolutionError,
    Candidate,
    CandidateRelease,
    DiscoverSummary,
    LibraryProfile,
    LibraryTooSparse,
    _credits_from_work_rels,
    build_library_profile,
    discover,
    fetch_all_release_ids,
    looks_like_mbid,
    render_discover_report,
    resolve_artist_mbid,
    score_candidates,
)

import pytest


# ---------- Fixtures ----------

ARTIST_ID = "1a83b2c2-a848-496a-8220-3f22d0fb47db"  # Makino Yui
ARTIST_NAME = "牧野由依"

KUBOTA = ("kubota-id", "窪田ミナ")
KAJIURA = ("kajiura-id", "梶浦由記")
KANO = ("kano-id", "かの香織")
RANDOM = ("rand-id", "ランダム作曲家")
ARRANGER_X = ("arr-x", "編曲X")


def _wr(rtype: str, person: tuple[str, str]) -> dict:
    return {"type": rtype, "artist": {"id": person[0], "name": person[1]}}


def _track(*, recording_id: str, title: str, work_rels: list[dict], local: list | None = None,
           is_instrumental: bool = False) -> dict:
    return {
        "position": 1,
        "number": "1",
        "title": title,
        "length_ms": 200_000,
        "release_track_id": f"rt-{recording_id}",
        "recording_id": recording_id,
        "is_instrumental": is_instrumental,
        "inst_signal": None,
        "work_rels": work_rels,
        "local": local or [],
    }


def _release_snapshot(*, release_id: str, title: str, tracks: list[dict],
                     artist=(ARTIST_ID, ARTIST_NAME), date: str = "2008-01-01",
                     release_group_id: str = "rg-default") -> dict:
    return {
        "schema_version": 2,
        "record_type": "release_snapshot",
        "release_id": release_id,
        "title": title,
        "date": date,
        "country": "JP",
        "artist_credit": [{"id": artist[0], "name": artist[1]}],
        "release_group": {"id": release_group_id, "primary_type": "Album", "secondary_types": []},
        "labels": [],
        "media": [{"position": 1, "format": "CD", "track_count": len(tracks), "tracks": tracks}],
        "completeness": {},
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    header = {"record_type": "releases_header", "stage": {"name": "complete"}}
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- Unit tests: credits extraction ----------

def test_credits_from_work_rels_splits_composer_and_arranger():
    composers, arrangers = _credits_from_work_rels([
        _wr("composer", KUBOTA),
        _wr("lyricist", KUBOTA),
        _wr("arranger", ARRANGER_X),
        _wr("writer", KAJIURA),
    ])
    assert composers == frozenset({KUBOTA, KAJIURA})
    assert arrangers == frozenset({ARRANGER_X})


def test_credits_from_work_rels_ignores_unknown_and_missing_id():
    composers, arrangers = _credits_from_work_rels([
        _wr("translator", KUBOTA),
        {"type": "composer", "artist": {"id": None, "name": "anon"}},
    ])
    assert composers == frozenset()
    assert arrangers == frozenset()


# ---------- Unit tests: library profile ----------

def test_build_library_profile_top_combos_and_team_pool(tmp_path):
    album_a = [
        _track(recording_id="r1", title="A1",
               work_rels=[_wr("composer", KUBOTA), _wr("lyricist", KUBOTA)],
               local=[{"relative_path": "a.mp3", "matched_by": "recording_id"}]),
        _track(recording_id="r2", title="A2", work_rels=[_wr("composer", KUBOTA)],
               local=[{"relative_path": "b.mp3", "matched_by": "recording_id"}]),
        _track(recording_id="r3", title="A3", work_rels=[_wr("composer", KAJIURA)],
               local=[{"relative_path": "c.mp3", "matched_by": "recording_id"}]),
    ]
    album_b = [
        _track(recording_id="r4", title="B1", work_rels=[_wr("composer", KAJIURA)]),
        _track(recording_id="r5", title="B2", work_rels=[_wr("composer", KANO)]),
        _track(recording_id="r6", title="B3", work_rels=[_wr("composer", (ARTIST_ID, ARTIST_NAME))]),
        _track(recording_id="r7", title="B4", work_rels=[_wr("composer", RANDOM)]),
    ]
    jsonl = tmp_path / "library_releases.jsonl"
    _write_jsonl(jsonl, [
        _release_snapshot(release_id="rel-a", title="Album A", tracks=album_a),
        _release_snapshot(release_id="rel-b", title="Album B", tracks=album_b),
    ])

    profile = build_library_profile(jsonl, ARTIST_ID, top_n=3)

    assert profile.artist_id == ARTIST_ID
    assert profile.artist_name == ARTIST_NAME
    assert profile.track_count == 7
    # owned_recording_ids = tracks with local non-empty
    assert profile.owned_recording_ids == {"r1", "r2", "r3"}
    # library_snapshots indexed by release id
    assert set(profile.library_snapshots.keys()) == {"rel-a", "rel-b"}
    # Top combos: KUBOTA x2, KAJIURA x2, then a tie among singletons
    assert len(profile.top_combos) == 3
    top_composer_sets = [composers for (composers, _), _ in profile.top_combos]
    assert frozenset({KUBOTA}) in top_composer_sets
    assert frozenset({KAJIURA}) in top_composer_sets
    # Self excluded by default
    assert (ARTIST_ID, ARTIST_NAME) not in profile.team_pool


def test_build_library_profile_include_self(tmp_path):
    tracks = [_track(recording_id="r1", title="T",
                     work_rels=[_wr("composer", (ARTIST_ID, ARTIST_NAME))])]
    jsonl = tmp_path / "lr.jsonl"
    _write_jsonl(jsonl, [_release_snapshot(release_id="rel", title="Album", tracks=tracks)])
    profile = build_library_profile(jsonl, ARTIST_ID, top_n=3, include_self=True)
    assert (ARTIST_ID, ARTIST_NAME) in profile.team_pool


def test_build_library_profile_skips_va_and_other_artists(tmp_path):
    tracks = [_track(recording_id="r1", title="T", work_rels=[_wr("composer", KUBOTA)])]
    jsonl = tmp_path / "lr.jsonl"
    _write_jsonl(jsonl, [
        _release_snapshot(release_id="va", title="VA", tracks=tracks,
                          artist=("89ad4ac3-39f7-470e-963a-56509c546377", "Various Artists")),
        _release_snapshot(release_id="other", title="Other", tracks=tracks,
                          artist=("other-id", "Other")),
    ])
    profile = build_library_profile(jsonl, ARTIST_ID, top_n=3)
    assert profile.track_count == 0
    assert profile.team_pool == set()
    # owned_recording_ids still tracks library-wide local presence even on filtered releases
    assert profile.owned_recording_ids == set()


# ---------- Unit tests: scoring ----------

def test_score_candidates_dedupes_recordings_across_releases_and_filters():
    profile = LibraryProfile(
        artist_id=ARTIST_ID,
        artist_name=ARTIST_NAME,
        track_count=5,
        tracks_with_credit=5,
        top_combos=[],
        team_pool={KUBOTA, KAJIURA},
        owned_recording_ids={"already-have"},
    )
    # rec-x appears on two releases: dedupe to one candidate, merge release list
    rel_one = _release_snapshot(
        release_id="r1", title="Album One", date="2010-01-01",
        tracks=[
            _track(recording_id="already-have", title="Owned", work_rels=[_wr("composer", KUBOTA)]),
            _track(recording_id="rec-x", title="Shared", work_rels=[_wr("composer", KUBOTA), _wr("arranger", KAJIURA)]),
            _track(recording_id="rec-miss", title="Miss", work_rels=[_wr("composer", RANDOM)]),
        ],
    )
    rel_two = _release_snapshot(
        release_id="r2", title="Comp Two", date="2011-06-01",
        tracks=[
            _track(recording_id="rec-x", title="Shared (Compilation)", work_rels=[_wr("composer", KUBOTA), _wr("arranger", KAJIURA)]),
            _track(recording_id="rec-y", title="Only One Match", work_rels=[_wr("composer", KAJIURA)]),
        ],
    )
    snapshots = {"r1": rel_one, "r2": rel_two}

    cands, summary = score_candidates(snapshots, profile)

    assert summary.unique_recordings == 4  # already-have, rec-x, rec-miss, rec-y
    assert summary.skipped_already_owned == 1
    assert summary.skipped_no_overlap == 1
    assert summary.candidates == 2

    by_rid = {c.recording_id: c for c in cands}
    assert by_rid["rec-x"].score == 2
    assert by_rid["rec-x"].matched_people == ["梶浦由記", "窪田ミナ"]
    assert len(by_rid["rec-x"].releases) == 2  # both releases tracked
    assert by_rid["rec-y"].score == 1

    # Sorted: rec-x (score 2) first, then rec-y (score 1)
    assert cands[0].recording_id == "rec-x"
    assert cands[1].recording_id == "rec-y"


# ---------- Integration: release-ID browsing ----------

def test_fetch_all_release_ids_paginates_and_dedupes():
    pages = [
        {"release-count": 3, "releases": [{"id": "a"}, {"id": "b"}]},
        {"release-count": 3, "releases": [{"id": "b"}, {"id": "c"}]},  # b dup, then c, total reached
    ]
    calls: list[tuple[int, int]] = []

    def fake_browser(artist_id, offset, limit):
        calls.append((offset, limit))
        idx = offset // limit
        return pages[idx]

    ids = fetch_all_release_ids(ARTIST_ID, delay=0.0, browser=fake_browser, sleep=lambda _: None)
    assert ids == ["a", "b", "c"]
    assert calls == [(0, 100), (100, 100)]


# ---------- Integration: discover orchestration ----------

def test_discover_reuses_library_snapshots_without_fetching(tmp_path):
    """If MB browse returns a release already in library, no fetch needed."""
    tracks = [
        _track(recording_id="owned-1", title="T1", work_rels=[_wr("composer", KUBOTA)],
               local=[{"relative_path": "p.mp3", "matched_by": "recording_id"}]),
    ]
    library_rel = _release_snapshot(release_id="lib-rel", title="In Library", tracks=tracks)
    jsonl = tmp_path / "library_releases.jsonl"
    _write_jsonl(jsonl, [library_rel])

    cache_path = tmp_path / "cache" / f"discover-{ARTIST_ID}.json"

    def browser(aid, offset, limit):
        return {"release-count": 1, "releases": [{"id": "lib-rel"}]}

    def fetcher(rid):
        raise AssertionError(f"should not fetch {rid}; already in library")

    profile, cands, summary = discover(
        jsonl, ARTIST_ID, cache_path,
        delay=0.0, browser=browser, fetcher=fetcher, sleep=lambda _: None,
    )
    assert summary.release_snapshots_from_library == 1
    assert summary.release_snapshots_fetched == 0
    # Library track is already owned, no candidates
    assert cands == []


def test_discover_fetches_new_releases_and_writes_cache(tmp_path):
    library_track = _track(
        recording_id="owned-1", title="L1",
        work_rels=[_wr("composer", KUBOTA)],
        local=[{"relative_path": "p.mp3", "matched_by": "recording_id"}],
    )
    jsonl = tmp_path / "library_releases.jsonl"
    _write_jsonl(jsonl, [_release_snapshot(release_id="lib-rel", title="L",
                                          tracks=[library_track], release_group_id="rg-owned")])

    cache_path = tmp_path / "cache" / f"discover-{ARTIST_ID}.json"

    new_release = _release_snapshot(
        release_id="new-rel", title="Sound Track",
        date="2012-06-01", release_group_id="rg-new",
        tracks=[
            _track(recording_id="new-rec", title="Discovery Hit",
                   work_rels=[_wr("composer", KUBOTA)]),
        ],
    )

    def browser(aid, offset, limit):
        return {"release-count": 2, "releases": [{"id": "lib-rel"}, {"id": "new-rel"}]}

    fetch_calls: list[str] = []

    def fetcher(rid):
        fetch_calls.append(rid)
        # complete.build_release_snapshot expects MB raw shape; we shortcut by
        # returning a snapshot-shaped object and bypassing build via a patched fetcher.
        return _MB_RAW_FOR_SNAPSHOT[rid]

    # Patch build_release_snapshot to passthrough for the test
    import tagwell.discover as _disc
    original_build = _disc.build_release_snapshot
    _disc.build_release_snapshot = lambda raw, rid: raw  # type: ignore[assignment]
    global _MB_RAW_FOR_SNAPSHOT
    _MB_RAW_FOR_SNAPSHOT = {"new-rel": new_release}

    try:
        profile, cands, summary = discover(
            jsonl, ARTIST_ID, cache_path,
            delay=0.0, browser=browser, fetcher=fetcher, sleep=lambda _: None,
        )
    finally:
        _disc.build_release_snapshot = original_build

    assert fetch_calls == ["new-rel"]
    assert summary.release_snapshots_from_library == 1
    assert summary.release_snapshots_fetched == 1
    assert len(cands) == 1
    assert cands[0].recording_id == "new-rec"
    assert cache_path.exists()

    # Re-run should NOT fetch again (cache hit)
    def boom(rid):
        raise AssertionError("must not fetch on second run")

    profile2, cands2, summary2 = discover(
        jsonl, ARTIST_ID, cache_path,
        delay=0.0, browser=browser, fetcher=boom, sleep=lambda _: None,
    )
    assert summary2.release_snapshots_from_cache == 1
    assert summary2.release_snapshots_fetched == 0
    assert len(cands2) == 1


def test_discover_existing_only_uses_only_library_snapshots(tmp_path):
    library_track = _track(
        recording_id="owned-1", title="L1",
        work_rels=[_wr("composer", KUBOTA)],
        local=[{"relative_path": "p.mp3", "matched_by": "recording_id"}],
    )
    jsonl = tmp_path / "library_releases.jsonl"
    _write_jsonl(jsonl, [_release_snapshot(release_id="lib-rel", title="L", tracks=[library_track])])

    cache_path = tmp_path / "cache" / f"discover-{ARTIST_ID}.json"
    # No cache file: existing-only should fall back to library releases for this artist

    def boom_browser(aid, offset, limit):
        raise AssertionError("must not browse in existing-only mode")

    def boom_fetcher(rid):
        raise AssertionError("must not fetch in existing-only mode")

    profile, cands, summary = discover(
        jsonl, ARTIST_ID, cache_path,
        existing_only=True,
        delay=0.0, browser=boom_browser, fetcher=boom_fetcher, sleep=lambda _: None,
    )
    # Only lib-rel resolves (from library), uncached-rel can't be used
    assert summary.release_snapshots_from_library == 1
    assert summary.release_snapshots_fetched == 0
    assert cands == []


# ---------- Report smoke test ----------

def test_score_candidates_records_miss_profile():
    profile = LibraryProfile(
        artist_id=ARTIST_ID, artist_name=ARTIST_NAME, track_count=1, tracks_with_credit=1,
        top_combos=[], team_pool={KUBOTA},
        owned_recording_ids=set(),
    )
    snapshots = {
        "r1": _release_snapshot(release_id="r1", title="A", release_group_id="rg-a", tracks=[
            _track(recording_id="hit", title="Hit", work_rels=[_wr("composer", KUBOTA)]),
            _track(recording_id="miss1", title="Miss1",
                   work_rels=[_wr("composer", RANDOM), _wr("arranger", ARRANGER_X)]),
            _track(recording_id="miss2", title="Miss2",
                   work_rels=[_wr("composer", RANDOM)]),
            _track(recording_id="miss3", title="Miss3", work_rels=[_wr("composer", KANO)]),
        ]),
    }
    _, summary = score_candidates(snapshots, profile)
    assert summary.skipped_no_overlap == 3
    assert summary.miss_composer_counts[RANDOM] == 2
    assert summary.miss_composer_counts[KANO] == 1
    assert summary.miss_arranger_counts[ARRANGER_X] == 1


def test_score_candidates_skips_instrumental_tracks():
    profile = LibraryProfile(
        artist_id=ARTIST_ID, artist_name=ARTIST_NAME, track_count=1, tracks_with_credit=1,
        top_combos=[], team_pool={KUBOTA},
        owned_recording_ids=set(),
    )
    snapshots = {
        "r1": _release_snapshot(
            release_id="r1", title="A", release_group_id="rg-a",
            tracks=[
                _track(recording_id="vocal", title="Vocal Hit", work_rels=[_wr("composer", KUBOTA)]),
                _track(recording_id="inst", title="Same -instrumental-",
                       work_rels=[_wr("composer", KUBOTA)], is_instrumental=True),
            ],
        ),
    }
    cands, summary = score_candidates(snapshots, profile)
    assert {c.recording_id for c in cands} == {"vocal"}
    assert summary.skipped_instrumental == 1


def test_score_candidates_skips_releases_in_owned_release_group():
    """Different regional release of an album we already own should be skipped wholesale."""
    profile = LibraryProfile(
        artist_id=ARTIST_ID, artist_name=ARTIST_NAME, track_count=1, tracks_with_credit=1,
        top_combos=[], team_pool={KUBOTA},
        owned_recording_ids=set(),  # MB gave the regional version different recording_ids
        owned_release_group_ids={"rg-album-X"},
        library_snapshots={"jp-release": {"release_group": {"id": "rg-album-X"}}},
    )
    snapshots = {
        # Different release_id, same release_group as the owned one — must skip
        "tw-release": _release_snapshot(
            release_id="tw-release", title="Album X (TW)",
            release_group_id="rg-album-X",
            tracks=[_track(recording_id="rec-tw-1", title="Track A",
                           work_rels=[_wr("composer", KUBOTA)])],
        ),
        # Different release_group — keep
        "other-rel": _release_snapshot(
            release_id="other-rel", title="Other Album",
            release_group_id="rg-other",
            tracks=[_track(recording_id="rec-other", title="Track B",
                           work_rels=[_wr("composer", KUBOTA)])],
        ),
    }
    cands, summary = score_candidates(snapshots, profile)
    assert {c.recording_id for c in cands} == {"rec-other"}
    assert summary.skipped_releases_owned_group == 1


def test_build_library_profile_collects_owned_release_group_ids(tmp_path):
    tracks_a = [_track(recording_id="a1", title="A1", work_rels=[_wr("composer", KUBOTA)],
                       local=[{"relative_path": "p.mp3", "matched_by": "recording_id"}])]
    tracks_b = [_track(recording_id="b1", title="B1", work_rels=[_wr("composer", KUBOTA)])]  # no local
    jsonl = tmp_path / "lr.jsonl"
    _write_jsonl(jsonl, [
        _release_snapshot(release_id="rel-a", title="A", tracks=tracks_a, release_group_id="rg-have"),
        _release_snapshot(release_id="rel-b", title="B", tracks=tracks_b, release_group_id="rg-not-have"),
    ])
    profile = build_library_profile(jsonl, ARTIST_ID, top_n=3)
    assert profile.owned_release_group_ids == {"rg-have"}


def test_looks_like_mbid_accepts_valid_rejects_garbage():
    assert looks_like_mbid("b6c18308-82c7-4ec1-a42d-e8488bce6618")
    assert looks_like_mbid("B6C18308-82C7-4EC1-A42D-E8488BCE6618")
    assert not looks_like_mbid("坂本真綾")
    assert not looks_like_mbid("b6c18308-82c7-4ec1-a42d")  # too short
    assert not looks_like_mbid("")


def test_resolve_artist_mbid_passes_mbid_through():
    mbid = "b6c18308-82c7-4ec1-a42d-e8488bce6618"

    def boom(q, limit):
        raise AssertionError("must not call MB search when input is an MBID")

    resolved_id, resolved_name = resolve_artist_mbid(mbid, searcher=boom)
    assert resolved_id == mbid
    assert resolved_name == ""


def test_resolve_artist_mbid_takes_top_high_score_hit():
    def searcher(query, limit):
        assert query == "坂本真綾"
        return [
            {"id": "b6c18308-82c7-4ec1-a42d-e8488bce6618", "name": "坂本真綾", "score": 100},
            {"id": "other-id", "name": "Other", "score": 60},
        ]

    resolved_id, resolved_name = resolve_artist_mbid("坂本真綾", searcher=searcher)
    assert resolved_id == "b6c18308-82c7-4ec1-a42d-e8488bce6618"
    assert resolved_name == "坂本真綾"


def test_resolve_artist_mbid_raises_when_no_results():
    def searcher(query, limit):
        return []

    with pytest.raises(ArtistResolutionError) as exc:
        resolve_artist_mbid("nonexistent garbage 12345", searcher=searcher)
    assert exc.value.candidates == []


def test_resolve_artist_mbid_raises_below_threshold():
    def searcher(query, limit):
        return [
            {"id": "a1", "name": "Vaguely Similar", "score": 70},
            {"id": "a2", "name": "Other", "score": 50},
        ]

    with pytest.raises(ArtistResolutionError) as exc:
        resolve_artist_mbid("ambiguous", searcher=searcher, min_score=95)
    assert len(exc.value.candidates) == 2
    assert exc.value.min_score == 95


def test_discover_refuses_when_artist_absent_from_library(tmp_path):
    jsonl = tmp_path / "lr.jsonl"
    _write_jsonl(jsonl, [])  # empty library
    cache_path = tmp_path / "cache.json"

    def boom_browser(*a, **k): raise AssertionError("must not browse")
    def boom_fetcher(*a, **k): raise AssertionError("must not fetch")

    with pytest.raises(LibraryTooSparse) as exc:
        discover(jsonl, ARTIST_ID, cache_path,
                 browser=boom_browser, fetcher=boom_fetcher, sleep=lambda _: None)
    assert "no library tracks" in str(exc.value)


def test_discover_refuses_when_credit_coverage_too_low(tmp_path):
    """11/12 tracks have no work_rels — should refuse below default 30% threshold."""
    tracks = [_track(recording_id=f"r{i}", title=f"T{i}", work_rels=[]) for i in range(11)]
    tracks.append(_track(recording_id="r11", title="Cover", work_rels=[_wr("composer", KUBOTA)]))
    jsonl = tmp_path / "lr.jsonl"
    _write_jsonl(jsonl, [_release_snapshot(release_id="rel", title="Album", tracks=tracks)])
    cache_path = tmp_path / "cache.json"

    def boom_browser(*a, **k): raise AssertionError("must not browse")

    with pytest.raises(LibraryTooSparse) as exc:
        discover(jsonl, ARTIST_ID, cache_path,
                 browser=boom_browser, fetcher=lambda _: {}, sleep=lambda _: None)
    assert "composer/arranger" in exc.value.reason
    assert exc.value.profile.tracks_with_credit == 1
    assert exc.value.profile.track_count == 12


def test_discover_refuses_when_team_pool_empty(tmp_path):
    """All credits are the artist themselves — team pool is empty after exclude-self."""
    tracks = [_track(recording_id="r1", title="T",
                     work_rels=[_wr("composer", (ARTIST_ID, ARTIST_NAME))])]
    jsonl = tmp_path / "lr.jsonl"
    _write_jsonl(jsonl, [_release_snapshot(release_id="rel", title="Album", tracks=tracks)])
    cache_path = tmp_path / "cache.json"

    with pytest.raises(LibraryTooSparse) as exc:
        discover(jsonl, ARTIST_ID, cache_path,
                 browser=lambda *a, **k: {}, fetcher=lambda _: {}, sleep=lambda _: None)
    assert "team pool is empty" in exc.value.reason


def test_discover_force_bypasses_guard(tmp_path):
    """--force lets sparse libraries through (no exception)."""
    tracks = [_track(recording_id="r1", title="T", work_rels=[])]
    jsonl = tmp_path / "lr.jsonl"
    _write_jsonl(jsonl, [_release_snapshot(release_id="rel", title="Album", tracks=tracks)])
    cache_path = tmp_path / "cache.json"

    def browser(*a, **k):
        return {"release-count": 0, "releases": []}

    profile, cands, summary = discover(
        jsonl, ARTIST_ID, cache_path,
        force=True,
        browser=browser, fetcher=lambda _: {}, sleep=lambda _: None,
    )
    assert cands == []
    assert profile.team_pool == set()


def test_render_report_basic_smoke():
    profile = LibraryProfile(
        artist_id=ARTIST_ID,
        artist_name=ARTIST_NAME,
        track_count=10,
        tracks_with_credit=10,
        top_combos=[((frozenset({KUBOTA}), frozenset()), 3)],
        team_pool={KUBOTA},
        owned_recording_ids=set(),
    )
    cand = Candidate(
        recording_id="rec-1",
        title="A Discovery",
        length_ms=200_000,
        composers=[KUBOTA],
        arrangers=[ARRANGER_X],
        releases=[CandidateRelease("rel-1", "Best of Kubota", "2010-05-01", "Album")],
        score=1,
        matched_people=["窪田ミナ"],
    )
    summary = DiscoverSummary(release_ids_browsed=5, release_snapshots_total=5,
                              unique_recordings=10, candidates=1)
    md = render_discover_report(profile, [cand], summary,
                                top_n=3, include_self=False, existing_only=False)
    assert ARTIST_NAME in md
    assert "窪田ミナ" in md
    assert "Best of Kubota (2010)" in md
    assert "**1** hit" in md  # release-grouped section header


def test_render_report_groups_by_release_count_desc():
    """Releases with more hits should appear first; ties broken by earlier date."""
    profile = LibraryProfile(
        artist_id=ARTIST_ID, artist_name=ARTIST_NAME, track_count=10, tracks_with_credit=10,
        top_combos=[], team_pool={KUBOTA},
        owned_recording_ids=set(),
    )
    big_release = CandidateRelease("big", "Big Album", "2015-01-01", "Album")
    small_release = CandidateRelease("small", "Small Single", "2010-01-01", "Single")

    candidates = [
        Candidate(recording_id="c1", title="Aaa", length_ms=None,
                  composers=[KUBOTA], arrangers=[], releases=[big_release],
                  score=1, matched_people=["窪田ミナ"]),
        Candidate(recording_id="c2", title="Bbb", length_ms=None,
                  composers=[KUBOTA], arrangers=[], releases=[big_release],
                  score=2, matched_people=["窪田ミナ"]),
        Candidate(recording_id="c3", title="Ccc", length_ms=None,
                  composers=[KUBOTA], arrangers=[], releases=[small_release],
                  score=1, matched_people=["窪田ミナ"]),
    ]
    summary = DiscoverSummary(candidates=3)
    md = render_discover_report(profile, candidates, summary,
                                top_n=3, include_self=False, existing_only=False)

    big_idx = md.index("Big Album")
    small_idx = md.index("Small Single")
    assert big_idx < small_idx  # 2 hits beats 1

    # Within big_release, higher score (Bbb=2) before lower (Aaa=1)
    big_section = md[big_idx:small_idx]
    assert big_section.index("Bbb") < big_section.index("Aaa")


def test_render_report_primary_release_prefers_album_over_single():
    """Recording that appears on both single and album should land under the album."""
    profile = LibraryProfile(
        artist_id=ARTIST_ID, artist_name=ARTIST_NAME, track_count=1, tracks_with_credit=1,
        top_combos=[], team_pool={KUBOTA},
        owned_recording_ids=set(),
    )
    candidate = Candidate(
        recording_id="c1", title="Shared Track", length_ms=None,
        composers=[KUBOTA], arrangers=[],
        releases=[
            CandidateRelease("sgl", "Early Single", "2010-01-01", "Single"),
            CandidateRelease("alb", "Later Album", "2012-06-01", "Album"),
        ],
        score=1, matched_people=["窪田ミナ"],
    )
    md = render_discover_report(profile, [candidate],
                                DiscoverSummary(candidates=1),
                                top_n=3, include_self=False, existing_only=False)
    assert "Later Album" in md
    # The candidate row falls under the album section, not the single section
    assert md.index("Shared Track") > md.index("Later Album")
    assert "Early Single" not in md
