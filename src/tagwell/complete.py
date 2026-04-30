"""Stage 1 enrichment: fetch MusicBrainz release-level data and emit
``library_releases.jsonl`` annotated with local-file presence and
release-level completeness (excluding instrumental/karaoke tracks).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from tagwell._report_helpers import _relative_path, load_snapshots
from tagwell.schema import SCANNER_NAME, SCANNER_VERSION

RELEASES_SCHEMA_VERSION = 2

_USER_AGENT = "tagwell/0.1.0 ( https://github.com/CheerChen/tagwell )"
_MB_API_BASE = "https://musicbrainz.org/ws/2"
_INC = "recordings+media+release-groups+artist-credits+labels+work-rels"

_INST_REL_ATTRS = {"instrumental", "karaoke"}

# Title / disambiguation heuristic: covers EN and JP conventions for instrumental
# and karaoke tracks. Lowercase input; word-bounded so "instinct" doesn't match.
_INST_PATTERN = re.compile(
    r"\boff[\s\-_]*vocal\b"
    r"|\binstrumental\b"
    r"|\binst\.?\b"
    r"|\bkaraoke\b"
    r"|オフ[\s・]*ボーカル"
    r"|インスト(?:ゥルメンタル)?"
    r"|カラオケ",
    re.IGNORECASE,
)

Snapshot = dict[str, Any]
Fetcher = Callable[[str], dict[str, Any]]


# ---------- MusicBrainz API ----------

def fetch_release_full(release_id: str) -> dict[str, Any]:
    """Fetch a release with everything we need to enrich it."""
    url = f"{_MB_API_BASE}/release/{release_id}?inc={_INC}&fmt=json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ---------- Instrumental detection ----------

def detect_instrumental(track: dict[str, Any], recording: dict[str, Any]) -> tuple[bool, str | None]:
    """Cascade: work-rel attribute → recording.disambiguation → track title."""
    for rel in recording.get("relations") or []:
        if rel.get("type") != "performance" or rel.get("target-type") != "work":
            continue
        attrs = set(rel.get("attributes") or [])
        if attrs & _INST_REL_ATTRS:
            return True, "work-rel"

    disamb = recording.get("disambiguation") or ""
    if _INST_PATTERN.search(disamb):
        return True, "disambiguation"

    title = track.get("title") or ""
    if _INST_PATTERN.search(title):
        return True, "title"

    return False, None


# ---------- Build a release_snapshot record from raw MB JSON ----------

def build_release_snapshot(raw: dict[str, Any], release_id: str) -> dict[str, Any]:
    media = []
    for medium in raw.get("media") or []:
        tracks = []
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            is_inst, signal = detect_instrumental(track, recording)
            tracks.append({
                "position": track.get("position"),
                "number": track.get("number"),
                "title": track.get("title"),
                "length_ms": track.get("length"),
                "release_track_id": track.get("id"),
                "recording_id": recording.get("id"),
                "is_instrumental": is_inst,
                "inst_signal": signal,
                "local": [],
            })
        media.append({
            "position": medium.get("position"),
            "format": medium.get("format"),
            "track_count": medium.get("track-count") or len(tracks),
            "tracks": tracks,
        })

    artist_credit = []
    for ac in raw.get("artist-credit") or []:
        artist = ac.get("artist") or {}
        artist_credit.append({
            "name": ac.get("name") or artist.get("name"),
            "id": artist.get("id"),
        })

    rg = raw.get("release-group") or {}
    release_group = {
        "id": rg.get("id"),
        "primary_type": rg.get("primary-type"),
        "secondary_types": rg.get("secondary-types") or [],
    }

    labels = []
    for li in raw.get("label-info") or []:
        label = li.get("label") or {}
        labels.append({
            "name": label.get("name"),
            "catalog_number": li.get("catalog-number"),
        })

    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "release_snapshot",
        "release_id": release_id,
        "title": raw.get("title"),
        "date": raw.get("date"),
        "country": raw.get("country"),
        "artist_credit": artist_credit,
        "release_group": release_group,
        "labels": labels,
        "media": media,
        "completeness": _empty_completeness(),
    }


# ---------- Local file matching ----------

def match_local_files(snapshots: list[Snapshot], release_record: dict[str, Any]) -> dict[str, int]:
    """Attach local-file matches to each track. Mutates ``release_record`` in place.

    For each local snapshot whose release_id matches this release, find the strongest
    canonical-track match (recording_id → release_track_id → (disc, track) position)
    and append to that track's ``local`` list. Returns counts by match method.
    """
    release_id = release_record["release_id"]

    track_by_recording: dict[str, dict] = {}
    track_by_release_track: dict[str, dict] = {}
    track_by_pos: dict[tuple[int, int], dict] = {}
    for medium in release_record["media"]:
        for track in medium["tracks"]:
            if track["recording_id"]:
                track_by_recording.setdefault(track["recording_id"], track)
            if track["release_track_id"]:
                track_by_release_track.setdefault(track["release_track_id"], track)
            if track["position"] is not None:
                track_by_pos.setdefault((medium["position"] or 1, track["position"]), track)

    method_counts = {"recording_id": 0, "release_track_id": 0, "position": 0}

    for snap in snapshots:
        if _release_id_of(snap) != release_id:
            continue

        target, method = _pick_track_for_snapshot(
            snap, track_by_recording, track_by_release_track, track_by_pos
        )
        if target is None:
            continue
        target["local"].append({
            "relative_path": _relative_path(snap),
            "matched_by": method,
        })
        method_counts[method] += 1

    # Stable order within each track's local list
    for medium in release_record["media"]:
        for track in medium["tracks"]:
            track["local"].sort(key=lambda entry: entry["relative_path"])

    return method_counts


def _pick_track_for_snapshot(
    snap: Snapshot,
    by_recording: dict[str, dict],
    by_release_track: dict[str, dict],
    by_pos: dict[tuple[int, int], dict],
) -> tuple[dict | None, str | None]:
    mb = _mb_of(snap)
    rid = mb.get("recording_id")
    if rid and rid in by_recording:
        return by_recording[rid], "recording_id"

    rtid = mb.get("release_track_id")
    if rtid and rtid in by_release_track:
        return by_release_track[rtid], "release_track_id"

    parsed = (snap.get("tags") or {}).get("parsed") or {}
    disc = parsed.get("disc_number") or 1
    track_no = parsed.get("track_number")
    if track_no is not None and (disc, track_no) in by_pos:
        return by_pos[(disc, track_no)], "position"

    return None, None


# ---------- Completeness ----------

def compute_completeness(release_record: dict[str, Any]) -> None:
    total = inst = vocal_present = inst_present = 0
    for medium in release_record["media"]:
        for track in medium["tracks"]:
            total += 1
            present = bool(track["local"])
            if track["is_instrumental"]:
                inst += 1
                if present:
                    inst_present += 1
            elif present:
                vocal_present += 1

    vocal_total = total - inst
    release_record["completeness"] = {
        "tracks_total": total,
        "tracks_instrumental": inst,
        "tracks_vocal_total": vocal_total,
        "tracks_vocal_present": vocal_present,
        "tracks_vocal_missing": vocal_total - vocal_present,
        "tracks_inst_present": inst_present,
        "ratio_vocal": (vocal_present / vocal_total) if vocal_total else None,
        "ratio_naive": ((vocal_present + inst_present) / total) if total else None,
    }


def _empty_completeness() -> dict[str, Any]:
    return {
        "tracks_total": 0,
        "tracks_instrumental": 0,
        "tracks_vocal_total": 0,
        "tracks_vocal_present": 0,
        "tracks_vocal_missing": 0,
        "tracks_inst_present": 0,
        "ratio_vocal": None,
        "ratio_naive": None,
    }


# ---------- Cache ----------

def _read_cached_snapshots(out_path: Path) -> dict[str, dict[str, Any]]:
    """Load existing release_snapshot records keyed by release_id. Errors are
    intentionally NOT cached so they'll be retried on the next run.
    """
    cached: dict[str, dict[str, Any]] = {}
    if not out_path.exists():
        return cached
    with open(out_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("record_type") == "release_snapshot":
                rid = rec.get("release_id")
                if rid:
                    cached[rid] = rec
    return cached


# ---------- Record builders ----------

def _build_header_record(
    *,
    started_at: str,
    library_jsonl: Path,
    library_header: dict | None,
    delay: float,
    refresh: bool,
) -> dict[str, Any]:
    source_scan_id = None
    if library_header:
        source_scan_id = (library_header.get("scan") or {}).get("scan_id")
    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "releases_header",
        "scanner": {"name": SCANNER_NAME, "version": SCANNER_VERSION},
        "stage": {
            "name": "complete",
            "started_at": started_at,
            "source_jsonl": library_jsonl.name,
            "source_scan_id": source_scan_id,
            "mb_api_base": _MB_API_BASE,
            "mb_inc": _INC,
            "incremental": not refresh,
            "delay_seconds": delay,
        },
    }


def _build_trailer_record(
    *,
    completed_at: str,
    elapsed_seconds: float,
    summary: "CompleteSummary",
) -> dict[str, Any]:
    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "releases_trailer",
        "completed_at": completed_at,
        "elapsed_seconds": round(elapsed_seconds, 2),
        "stats": {
            "unique_release_ids": summary.unique_release_ids,
            "fetched": summary.fetched,
            "skipped_cached": summary.skipped_cached,
            "failed": summary.failed,
            "orphan_files": summary.orphan_files,
            "total_local_files": summary.total_local_files,
            "matched_local_files": summary.matched_local_files,
            "matched_by_recording_id": summary.matched_by_recording_id,
            "matched_by_release_track_id": summary.matched_by_release_track_id,
            "matched_by_position": summary.matched_by_position,
        },
    }


def _build_orphan_record(orphan_paths: list[str]) -> dict[str, Any]:
    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "orphan_file",
        "count": len(orphan_paths),
        "files": [{"relative_path": p} for p in sorted(orphan_paths)],
    }


def _build_error_record(release_id: str, exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": RELEASES_SCHEMA_VERSION,
        "record_type": "release_error",
        "release_id": release_id,
        "error": {
            "stage": "fetch_release",
            "kind": type(exc).__name__,
            "message": str(exc),
            "attempted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    }


# ---------- Orchestrator ----------

@dataclass
class CompleteSummary:
    unique_release_ids: int = 0
    fetched: int = 0
    skipped_cached: int = 0
    failed: int = 0
    orphan_files: int = 0
    total_local_files: int = 0
    matched_local_files: int = 0
    matched_by_recording_id: int = 0
    matched_by_release_track_id: int = 0
    matched_by_position: int = 0


def build_releases_jsonl(
    library_jsonl: Path,
    out_path: Path,
    *,
    delay: float = 1.0,
    refresh: bool = False,
    fetcher: Fetcher = fetch_release_full,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> CompleteSummary:
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    t0 = time.monotonic()

    library_header, snapshots = load_snapshots(library_jsonl)

    desired_ids: list[str] = sorted({
        rid for rid in (_release_id_of(s) for s in snapshots) if rid
    })
    desired_set = set(desired_ids)
    orphans = [_relative_path(s) for s in snapshots if not _release_id_of(s)]

    cached = {} if refresh else _read_cached_snapshots(out_path)
    cached = {rid: rec for rid, rec in cached.items() if rid in desired_set}

    summary = CompleteSummary(
        unique_release_ids=len(desired_ids),
        skipped_cached=len(cached),
        orphan_files=len(orphans),
        total_local_files=len(snapshots),
    )

    new_snapshots: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    to_fetch = [rid for rid in desired_ids if rid not in cached]

    for i, rid in enumerate(to_fetch):
        if on_progress:
            on_progress(i + 1, len(to_fetch), rid)
        try:
            raw = fetcher(rid)
            new_snapshots[rid] = build_release_snapshot(raw, rid)
            summary.fetched += 1
        except Exception as exc:
            errors.append(_build_error_record(rid, exc))
            summary.failed += 1
        if i + 1 < len(to_fetch):
            sleep(delay)

    all_snapshots = {**cached, **new_snapshots}

    for rec in all_snapshots.values():
        for medium in rec["media"]:
            for track in medium["tracks"]:
                track["local"] = []
        method_counts = match_local_files(snapshots, rec)
        compute_completeness(rec)
        summary.matched_local_files += sum(method_counts.values())
        summary.matched_by_recording_id += method_counts["recording_id"]
        summary.matched_by_release_track_id += method_counts["release_track_id"]
        summary.matched_by_position += method_counts["position"]

    elapsed = time.monotonic() - t0
    completed_at = datetime.now().astimezone().isoformat(timespec="seconds")

    header = _build_header_record(
        started_at=started_at,
        library_jsonl=library_jsonl,
        library_header=library_header,
        delay=delay,
        refresh=refresh,
    )
    trailer = _build_trailer_record(
        completed_at=completed_at,
        elapsed_seconds=elapsed,
        summary=summary,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(_json_line(header))
        for rid in sorted(all_snapshots):
            f.write(_json_line(all_snapshots[rid]))
        for err in errors:
            f.write(_json_line(err))
        if orphans:
            f.write(_json_line(_build_orphan_record(orphans)))
        f.write(_json_line(trailer))

    return summary


# ---------- Tiny snapshot accessors ----------

def _release_id_of(snap: Snapshot) -> str | None:
    return _mb_of(snap).get("release_id")


def _mb_of(snap: Snapshot) -> dict[str, Any]:
    return ((snap.get("tags") or {}).get("external_ids") or {}).get("musicbrainz") or {}


def _json_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
