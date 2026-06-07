"""Discovery: given an artist MBID, score recordings (not yet in the library)
by overlap with the creative team behind that artist's library tracks.

Pipeline:
  1. Build a library profile from ``library_releases.jsonl``: for tracks where
     the target artist is a performer, group by (composer-set, arranger-set),
     take the top-N combos, flatten to a "team pool" of (artist_id, name).
  2. Browse all MB releases credited to the artist; fetch each release with
     the same inc as ``complete.py`` so we get work-level relations.
  3. Aggregate tracks across releases, dedupe by recording_id, drop those
     already in the library, score remaining by team-pool overlap (>= 1 hit).
  4. Render a Markdown report.

Lyricist credits are intentionally excluded. ``writer`` (used by MB when
composer/lyricist aren't separated) is folded into the composer side.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tagwell.complete import build_release_snapshot, fetch_release_full

_USER_AGENT = "tagwell/0.1.0 ( https://github.com/CheerChen/tagwell )"
_MB_API_BASE = "https://musicbrainz.org/ws/2"
_RELEASE_BROWSE_LIMIT = 100

# Composer pool absorbs MB's generic "writer" role; arrangers stay separate.
_COMPOSER_TYPES = {"composer", "writer"}
_ARRANGER_TYPES = {"arranger"}

VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

Person = tuple[str, str]  # (artist_id, name)
ReleaseFetcher = Callable[[str], dict[str, Any]]


# ---------- Artist name → MBID resolution ----------

_MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def looks_like_mbid(s: str) -> bool:
    return bool(_MBID_RE.match(s.strip()))


def search_artists(query: str, limit: int = 5) -> list[dict[str, Any]]:
    url = (
        f"{_MB_API_BASE}/artist?query={urllib.parse.quote(query)}"
        f"&limit={limit}&fmt=json"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("artists") or []


ArtistSearcher = Callable[[str, int], list[dict[str, Any]]]


class ArtistResolutionError(Exception):
    """Raised when a free-text query cannot be confidently mapped to a single MBID."""

    def __init__(self, query: str, candidates: list[dict[str, Any]], min_score: int):
        self.query = query
        self.candidates = candidates
        self.min_score = min_score
        super().__init__(f"no confident artist match for {query!r}")


def resolve_artist_mbid(
    query: str,
    *,
    min_score: int = 95,
    searcher: ArtistSearcher = search_artists,
) -> tuple[str, str]:
    """Return (mbid, resolved_name). Passes MBID through; otherwise hits MB search.

    Raises ArtistResolutionError if the top hit's score is below ``min_score`` or
    no hits at all.
    """
    if looks_like_mbid(query):
        return query.strip().lower(), ""
    artists = searcher(query, 5)
    if not artists:
        raise ArtistResolutionError(query, [], min_score)
    top = artists[0]
    score = int(top.get("score") or 0)
    if score < min_score:
        raise ArtistResolutionError(query, artists, min_score)
    return top["id"], top.get("name") or ""


# ---------- MB release browse ----------

def browse_release_ids(artist_id: str, offset: int, limit: int) -> dict[str, Any]:
    url = (
        f"{_MB_API_BASE}/release?artist={artist_id}"
        f"&offset={offset}&limit={limit}&fmt=json"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


Browser = Callable[[str, int, int], dict[str, Any]]


# ---------- Library-side profile ----------

def _credits_from_work_rels(work_rels: list[dict[str, Any]]) -> tuple[frozenset[Person], frozenset[Person]]:
    """Split a snapshot's track-level work_rels into (composers, arrangers)."""
    composers: set[Person] = set()
    arrangers: set[Person] = set()
    for wr in work_rels or []:
        rtype = (wr.get("type") or "").lower()
        artist = wr.get("artist") or {}
        aid = artist.get("id")
        if not aid:
            continue
        person = (aid, artist.get("name") or "")
        if rtype in _COMPOSER_TYPES:
            composers.add(person)
        elif rtype in _ARRANGER_TYPES:
            arrangers.add(person)
    return frozenset(composers), frozenset(arrangers)


@dataclass
class ComboExample:
    track_title: str
    release_title: str
    release_date: str


@dataclass
class LibraryProfile:
    artist_id: str
    artist_name: str
    track_count: int
    tracks_with_credit: int
    top_combos: list[tuple[tuple[frozenset[Person], frozenset[Person]], int]]
    team_pool: set[Person]
    owned_recording_ids: set[str]
    owned_release_group_ids: set[str] = field(default_factory=set)
    library_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    combo_examples: dict[tuple[frozenset[Person], frozenset[Person]], list[ComboExample]] = field(default_factory=dict)

    @property
    def credit_coverage(self) -> float:
        return self.tracks_with_credit / self.track_count if self.track_count else 0.0


class LibraryTooSparse(Exception):
    """Raised when the library data is too thin to power a meaningful discovery run.

    Carries enough context for the CLI to render an informative error.
    """

    def __init__(self, *, profile: LibraryProfile, reason: str, threshold: float | None = None):
        self.profile = profile
        self.reason = reason
        self.threshold = threshold
        super().__init__(reason)


def build_library_profile(
    library_releases_jsonl: Path,
    artist_id: str,
    *,
    top_n: int = 3,
    include_self: bool = False,
) -> LibraryProfile:
    """Scan library_releases.jsonl twice in one pass:
    - For tracks where the target artist is in release.artist_credit (and not VA):
      collect (composer-set, arranger-set) combos for ranking.
    - For *every* track in the library: collect recording_ids that have at least
      one local file present — those are tracks the user already owns.
    - Stash all release_snapshots so the discover stage can skip re-fetching
      releases that are already in the library.
    """
    artist_name = ""
    combos: Counter = Counter()
    examples: dict[tuple[frozenset[Person], frozenset[Person]], list[ComboExample]] = {}
    track_count = 0
    tracks_with_credit = 0
    owned_recording_ids: set[str] = set()
    owned_release_group_ids: set[str] = set()
    library_snapshots: dict[str, dict[str, Any]] = {}
    _max_examples_per_combo = 3

    with open(library_releases_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("record_type") != "release_snapshot":
                continue
            rid = rec.get("release_id")
            if rid:
                library_snapshots[rid] = rec
            rg_id = (rec.get("release_group") or {}).get("id")

            release_has_local = False
            for medium in rec.get("media") or []:
                for track in medium.get("tracks") or []:
                    rec_id = track.get("recording_id")
                    if rec_id and (track.get("local") or []):
                        owned_recording_ids.add(rec_id)
                        release_has_local = True
            if release_has_local and rg_id:
                owned_release_group_ids.add(rg_id)

            ac = rec.get("artist_credit") or []
            ids = {a.get("id") for a in ac}
            if VA_MBID in ids or artist_id not in ids:
                continue
            for ac_entry in ac:
                if ac_entry.get("id") == artist_id and not artist_name:
                    artist_name = ac_entry.get("name") or ""
            rel_title = rec.get("title") or ""
            rel_date = rec.get("date") or ""
            for medium in rec.get("media") or []:
                for track in medium.get("tracks") or []:
                    composers, arrangers = _credits_from_work_rels(track.get("work_rels") or [])
                    key = (composers, arrangers)
                    combos[key] += 1
                    track_count += 1
                    if composers or arrangers:
                        tracks_with_credit += 1
                    bucket = examples.setdefault(key, [])
                    if len(bucket) < _max_examples_per_combo:
                        bucket.append(ComboExample(
                            track_title=track.get("title") or "",
                            release_title=rel_title,
                            release_date=rel_date,
                        ))

    top_pairs = combos.most_common(top_n)
    team_pool: set[Person] = set()
    for (composers, arrangers), _ in top_pairs:
        team_pool.update(composers)
        team_pool.update(arrangers)
    if not include_self:
        team_pool = {(aid, n) for aid, n in team_pool if aid != artist_id}

    return LibraryProfile(
        artist_id=artist_id,
        artist_name=artist_name,
        track_count=track_count,
        tracks_with_credit=tracks_with_credit,
        top_combos=top_pairs,
        team_pool=team_pool,
        owned_recording_ids=owned_recording_ids,
        owned_release_group_ids=owned_release_group_ids,
        library_snapshots=library_snapshots,
        combo_examples={key: examples.get(key, []) for key, _ in top_pairs},
    )


# ---------- Candidate aggregation + scoring ----------

@dataclass
class CandidateRelease:
    release_id: str
    title: str
    date: str
    primary_type: str | None


@dataclass
class Candidate:
    recording_id: str
    title: str
    length_ms: int | None
    composers: list[Person]
    arrangers: list[Person]
    releases: list[CandidateRelease]
    score: int = 0
    matched_people: list[str] = field(default_factory=list)
    is_instrumental: bool = False


@dataclass
class DiscoverSummary:
    release_ids_browsed: int = 0
    release_snapshots_total: int = 0
    release_snapshots_from_library: int = 0
    release_snapshots_from_cache: int = 0
    release_snapshots_fetched: int = 0
    release_fetch_failures: int = 0
    skipped_releases_owned_group: int = 0
    unique_recordings: int = 0
    candidates: int = 0
    skipped_already_owned: int = 0
    skipped_instrumental: int = 0
    skipped_no_overlap: int = 0
    miss_composer_counts: Counter = field(default_factory=Counter)
    miss_arranger_counts: Counter = field(default_factory=Counter)


# ---------- Release ID browsing ----------

def fetch_all_release_ids(
    artist_id: str,
    *,
    delay: float = 1.0,
    browser: Browser = browse_release_ids,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Page through MB release browse; return deduplicated release IDs."""
    seen: set[str] = set()
    ordered: list[str] = []
    offset = 0
    total: int | None = None
    while True:
        page = browser(artist_id, offset, _RELEASE_BROWSE_LIMIT)
        batch = page.get("releases") or []
        for r in batch:
            rid = r.get("id")
            if rid and rid not in seen:
                seen.add(rid)
                ordered.append(rid)
        if total is None:
            total = page.get("release-count") or len(batch)
        if on_progress:
            on_progress(len(ordered), total)
        if not batch or len(ordered) >= total:
            break
        offset += _RELEASE_BROWSE_LIMIT
        sleep(delay)
    return ordered


# ---------- Release fetching with cache + library reuse ----------

def fetch_release_snapshots(
    release_ids: list[str],
    *,
    library_snapshots: dict[str, dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    delay: float,
    summary: DiscoverSummary,
    fetcher: ReleaseFetcher = fetch_release_full,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """For each release ID, reuse a library snapshot if present, else cache,
    else fetch from MB. Mutates ``cache`` in place. Returns full snapshot dict.
    """
    snapshots: dict[str, dict[str, Any]] = {}
    fetched_this_run = 0
    to_fetch = [rid for rid in release_ids if rid not in library_snapshots and rid not in cache]
    fetch_idx = 0

    for rid in release_ids:
        if rid in library_snapshots:
            snapshots[rid] = library_snapshots[rid]
            summary.release_snapshots_from_library += 1
            continue
        if rid in cache:
            snapshots[rid] = cache[rid]
            summary.release_snapshots_from_cache += 1
            continue
        fetch_idx += 1
        if on_progress:
            on_progress(fetch_idx, len(to_fetch), rid)
        try:
            raw = fetcher(rid)
            snap = build_release_snapshot(raw, rid)
            snapshots[rid] = snap
            cache[rid] = snap
            summary.release_snapshots_fetched += 1
            fetched_this_run += 1
        except Exception:
            summary.release_fetch_failures += 1
        if fetch_idx < len(to_fetch):
            sleep(delay)

    return snapshots


# ---------- Score candidates from snapshots ----------

def score_candidates(
    snapshots: dict[str, dict[str, Any]],
    profile: LibraryProfile,
) -> tuple[list[Candidate], DiscoverSummary]:
    summary = DiscoverSummary()
    summary.release_snapshots_total = len(snapshots)
    pool_ids = {aid for aid, _ in profile.team_pool}
    pool_name = dict(profile.team_pool)

    by_recording: dict[str, Candidate] = {}

    for rid, snap in snapshots.items():
        rg = snap.get("release_group") or {}
        rg_id = rg.get("id")
        if rg_id and rg_id in profile.owned_release_group_ids and rid not in profile.library_snapshots:
            summary.skipped_releases_owned_group += 1
            continue
        rel_title = snap.get("title") or ""
        rel_date = snap.get("date") or ""
        rel_type = rg.get("primary_type")
        for medium in snap.get("media") or []:
            for track in medium.get("tracks") or []:
                rec_id = track.get("recording_id")
                if not rec_id:
                    continue
                if rec_id in by_recording:
                    by_recording[rec_id].releases.append(CandidateRelease(rid, rel_title, rel_date, rel_type))
                    continue
                composers_fs, arrangers_fs = _credits_from_work_rels(track.get("work_rels") or [])
                composers = sorted(composers_fs, key=lambda p: p[1])
                arrangers = sorted(arrangers_fs, key=lambda p: p[1])
                by_recording[rec_id] = Candidate(
                    recording_id=rec_id,
                    title=track.get("title") or "",
                    length_ms=track.get("length_ms"),
                    composers=composers,
                    arrangers=arrangers,
                    releases=[CandidateRelease(rid, rel_title, rel_date, rel_type)],
                    is_instrumental=bool(track.get("is_instrumental")),
                )

    summary.unique_recordings = len(by_recording)

    candidates: list[Candidate] = []
    for rec_id, cand in by_recording.items():
        if rec_id in profile.owned_recording_ids:
            summary.skipped_already_owned += 1
            continue
        if cand.is_instrumental:
            summary.skipped_instrumental += 1
            continue
        cand_ids = {aid for aid, _ in cand.composers} | {aid for aid, _ in cand.arrangers}
        matched_ids = cand_ids & pool_ids
        if not matched_ids:
            summary.skipped_no_overlap += 1
            for person in cand.composers:
                summary.miss_composer_counts[person] += 1
            for person in cand.arrangers:
                summary.miss_arranger_counts[person] += 1
            continue
        cand.score = len(matched_ids)
        cand.matched_people = sorted(pool_name[aid] for aid in matched_ids)
        candidates.append(cand)

    summary.candidates = len(candidates)
    candidates.sort(key=lambda c: (-c.score, _earliest_year(c.releases), c.title))
    return candidates, summary


def _earliest_year(releases: list[CandidateRelease]) -> str:
    years = [r.date[:4] for r in releases if r.date and r.date[:4].isdigit()]
    return min(years) if years else "9999"


# ---------- Report rendering ----------

def render_discover_report(
    profile: LibraryProfile,
    candidates: list[Candidate],
    summary: DiscoverSummary,
    *,
    top_n: int,
    include_self: bool,
    existing_only: bool,
    max_rows: int = 300,
) -> str:
    lines: list[str] = []
    lines.append(f"# Discover — {profile.artist_name or profile.artist_id}")
    lines.append("")
    lines.append(f"- Artist MBID: `{profile.artist_id}`")
    lines.append(f"- Library tracks (performer): **{profile.track_count}**")
    lines.append(f"- Top-N combos: **{top_n}**   include self: **{include_self}**   existing-only: **{existing_only}**")
    lines.append(f"- Releases browsed from MB: **{summary.release_ids_browsed}**")
    lines.append(f"- Release snapshots used: **{summary.release_snapshots_total}** "
                 f"(library: {summary.release_snapshots_from_library}, "
                 f"cache: {summary.release_snapshots_from_cache}, "
                 f"fetched: {summary.release_snapshots_fetched}, "
                 f"failed: {summary.release_fetch_failures})")
    lines.append(f"- Releases skipped (release_group already owned): **{summary.skipped_releases_owned_group}**")
    lines.append(f"- Unique recordings (after dedup): **{summary.unique_recordings}** "
                 f"(already owned: {summary.skipped_already_owned}, "
                 f"instrumental: {summary.skipped_instrumental}, "
                 f"no team overlap: {summary.skipped_no_overlap})")
    lines.append(f"- Candidates with score ≥ 1: **{summary.candidates}**")
    lines.append("")

    lines.append("## Top combos in library")
    lines.append("")
    if not profile.top_combos:
        lines.append("_No tracks matched the artist filter._")
    else:
        for i, ((composers, arrangers), freq) in enumerate(profile.top_combos, start=1):
            c = ", ".join(sorted(n for _, n in composers)) or "—"
            a = ", ".join(sorted(n for _, n in arrangers)) or "—"
            lines.append(f"**#{i}** — freq **{freq}** — composer: {c}   arranger: {a}")
            for ex in profile.combo_examples.get((composers, arrangers), []):
                year = ex.release_date[:4] if ex.release_date else ""
                rel_str = f"{ex.release_title} ({year})" if year else ex.release_title
                lines.append(f"  - {ex.track_title}  _on {rel_str}_")
            lines.append("")

    lines.append("## Team pool")
    lines.append("")
    if profile.team_pool:
        for aid, name in sorted(profile.team_pool, key=lambda p: p[1]):
            lines.append(f"- {name}  `{aid}`")
    else:
        lines.append("_(empty)_")
    lines.append("")

    lines.append("## Miss profile (no team overlap)")
    lines.append("")
    lines.append(f"Among the **{summary.skipped_no_overlap}** unique recordings rejected for no team overlap, "
                 "the most-frequent composers / arrangers are:")
    lines.append("")
    miss_top = 15
    has_miss = summary.miss_composer_counts or summary.miss_arranger_counts
    if not has_miss:
        lines.append("_(empty)_")
    else:
        lines.append("| Role | Person | MBID | Count |")
        lines.append("|---|---|---|---:|")
        for (aid, name), n in summary.miss_composer_counts.most_common(miss_top):
            lines.append(f"| composer | {name} | `{aid}` | {n} |")
        for (aid, name), n in summary.miss_arranger_counts.most_common(miss_top):
            lines.append(f"| arranger | {name} | `{aid}` | {n} |")
    lines.append("")

    lines.append("## Candidates grouped by release")
    lines.append("")
    if not candidates:
        lines.append("_No matching candidates._")
        return "\n".join(lines) + "\n"

    groups: dict[str, dict[str, Any]] = {}
    for c in candidates:
        primary = _primary_release(c.releases)
        g = groups.setdefault(primary.release_id, {"release": primary, "candidates": []})
        g["candidates"].append(c)

    ordered = sorted(
        groups.values(),
        key=lambda g: (-len(g["candidates"]), g["release"].date or "9999", g["release"].title),
    )
    for g in ordered:
        g["candidates"].sort(key=lambda c: (-c.score, c.title))

    rendered_rows = 0
    truncated = False
    for g in ordered:
        if rendered_rows >= max_rows:
            truncated = True
            break
        rel = g["release"]
        year = rel.date[:4] if rel.date else "—"
        type_str = rel.primary_type or "—"
        n = len(g["candidates"])
        lines.append(f"### {rel.title} ({year}) — _{type_str}_ — **{n}** hit{'s' if n != 1 else ''}")
        lines.append("")
        lines.append("| Score | Title | Composer | Arranger | Matched |")
        lines.append("|---:|---|---|---|---|")
        for c in g["candidates"]:
            if rendered_rows >= max_rows:
                truncated = True
                break
            comp = ", ".join(n for _, n in c.composers) or "—"
            arr = ", ".join(n for _, n in c.arrangers) or "—"
            matched = ", ".join(c.matched_people)
            lines.append(f"| {c.score} | {c.title} | {comp} | {arr} | {matched} |")
            rendered_rows += 1
        lines.append("")

    if truncated:
        lines.append(f"_(truncated at {max_rows} rows of {len(candidates)} candidates)_")
    return "\n".join(lines) + "\n"


def _primary_release(releases: list[CandidateRelease]) -> CandidateRelease:
    """Pick the most "canonical" release for a recording: prefer Album, then earliest date."""
    return min(releases, key=lambda r: (r.primary_type != "Album", r.date or "9999"))


# ---------- High-level orchestrator ----------

@dataclass
class _DiscoverCache:
    """On-disk shape: { 'artist_id': ..., 'release_ids': [...], 'snapshots': {rid: snapshot} }."""
    artist_id: str
    release_ids: list[str]
    snapshots: dict[str, dict[str, Any]]


def _load_cache(path: Path, artist_id: str) -> _DiscoverCache:
    if not path.exists():
        return _DiscoverCache(artist_id, [], {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _DiscoverCache(artist_id, [], {})
    if data.get("artist_id") != artist_id:
        return _DiscoverCache(artist_id, [], {})
    return _DiscoverCache(
        artist_id=artist_id,
        release_ids=list(data.get("release_ids") or []),
        snapshots=dict(data.get("snapshots") or {}),
    )


def _save_cache(path: Path, cache: _DiscoverCache) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "artist_id": cache.artist_id,
        "release_ids": cache.release_ids,
        "snapshots": cache.snapshots,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def discover(
    library_releases_jsonl: Path,
    artist_id: str,
    cache_path: Path,
    *,
    top_n: int = 3,
    include_self: bool = False,
    delay: float = 1.0,
    refresh: bool = False,
    existing_only: bool = False,
    min_credit_coverage: float = 0.3,
    force: bool = False,
    browser: Browser = browse_release_ids,
    fetcher: ReleaseFetcher = fetch_release_full,
    sleep: Callable[[float], None] = time.sleep,
    on_browse: Callable[[int, int], None] | None = None,
    on_fetch: Callable[[int, int, str], None] | None = None,
) -> tuple[LibraryProfile, list[Candidate], DiscoverSummary]:
    profile = build_library_profile(
        library_releases_jsonl, artist_id, top_n=top_n, include_self=include_self
    )

    if not force:
        if profile.track_count == 0:
            raise LibraryTooSparse(
                profile=profile,
                reason="no library tracks credit this artist as performer",
            )
        if not profile.team_pool:
            raise LibraryTooSparse(
                profile=profile,
                reason="team pool is empty (no usable composer/arranger credits beyond the artist themselves)",
            )
        if profile.credit_coverage < min_credit_coverage:
            raise LibraryTooSparse(
                profile=profile,
                reason=(
                    f"only {profile.tracks_with_credit}/{profile.track_count} library tracks "
                    f"have composer/arranger credits "
                    f"({profile.credit_coverage * 100:.0f}%, threshold {min_credit_coverage * 100:.0f}%)"
                ),
                threshold=min_credit_coverage,
            )

    cache = _load_cache(cache_path, artist_id) if not refresh else _DiscoverCache(artist_id, [], {})

    if existing_only:
        # Without hitting MB, fall back to: cached browse list if present,
        # otherwise every library release where the target artist is credited.
        if cache.release_ids:
            release_ids = list(cache.release_ids)
        else:
            release_ids = [
                rid for rid, snap in profile.library_snapshots.items()
                if any(a.get("id") == artist_id for a in (snap.get("artist_credit") or []))
            ]
    elif cache.release_ids and not refresh:
        release_ids = list(cache.release_ids)
    else:
        release_ids = fetch_all_release_ids(
            artist_id, delay=delay, browser=browser, sleep=sleep, on_progress=on_browse,
        )
        cache.release_ids = release_ids
        _save_cache(cache_path, cache)

    snapshots_summary = DiscoverSummary()
    snapshots_summary.release_ids_browsed = len(release_ids)

    if existing_only:
        snapshots: dict[str, dict[str, Any]] = {
            rid: profile.library_snapshots[rid]
            for rid in release_ids
            if rid in profile.library_snapshots
        }
        snapshots_summary.release_snapshots_from_library = len(snapshots)
    else:
        snapshots = fetch_release_snapshots(
            release_ids,
            library_snapshots=profile.library_snapshots,
            cache=cache.snapshots,
            delay=delay,
            summary=snapshots_summary,
            fetcher=fetcher,
            sleep=sleep,
            on_progress=on_fetch,
        )
        _save_cache(cache_path, cache)

    candidates, score_summary = score_candidates(snapshots, profile)

    score_summary.release_ids_browsed = snapshots_summary.release_ids_browsed
    score_summary.release_snapshots_from_library = snapshots_summary.release_snapshots_from_library
    score_summary.release_snapshots_from_cache = snapshots_summary.release_snapshots_from_cache
    score_summary.release_snapshots_fetched = snapshots_summary.release_snapshots_fetched
    score_summary.release_fetch_failures = snapshots_summary.release_fetch_failures

    return profile, candidates, score_summary
