"""Patch missing MusicBrainz tags by querying the MusicBrainz API.

Patches release-level tags (script, releasecountry) and recording IDs.
Only writes to files that already have enough MusicBrainz IDs to resolve the
missing fields.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.aiff import AIFF
from mutagen.wave import WAVE

from tagwell.report import load_snapshots

_USER_AGENT = "tagwell/0.1.0 ( https://github.com/CheerChen/tagwell )"
_MB_API_BASE = "https://musicbrainz.org/ws/2"

Snapshot = dict[str, Any]
PatchMode = str


# ---------- MusicBrainz API ----------

@dataclass
class ReleaseInfo:
    script: str | None = None
    country: str | None = None
    track_recordings: dict[str, str] = field(default_factory=dict)


def fetch_release_info(release_id: str, *, include_recordings: bool = False) -> ReleaseInfo:
    """Fetch release metadata from the MusicBrainz release endpoint."""
    query = "inc=recordings&fmt=json" if include_recordings else "fmt=json"
    url = f"{_MB_API_BASE}/release/{release_id}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    text_rep = data.get("text-representation") or {}
    return ReleaseInfo(
        script=text_rep.get("script"),
        country=data.get("country"),
        track_recordings=_extract_track_recordings(data),
    )


def _extract_track_recordings(data: dict[str, Any]) -> dict[str, str]:
    """Return MusicBrainz release-track-id -> recording-id mappings."""
    track_recordings: dict[str, str] = {}
    for medium in data.get("media", []):
        for track in medium.get("tracks", []):
            track_id = track.get("id")
            recording_id = (track.get("recording") or {}).get("id")
            if track_id and recording_id:
                track_recordings[track_id] = recording_id
    return track_recordings


# ---------- Snapshot analysis ----------

@dataclass
class PatchTarget:
    """A file that needs patching."""
    relative_path: str
    absolute_path: Path
    tag_format: str
    release_id: str
    release_track_id: str | None = None
    needs_script: bool = False
    needs_country: bool = False
    needs_recording_id: bool = False


def _has_raw_tag(snapshot: Snapshot, field_name: str) -> bool:
    raw = snapshot.get("tags", {}).get("raw", {})
    keys = _RAW_KEY_ALIASES.get(field_name, [field_name])
    raw_lower = {k.lower() for k in raw}
    return any(alias.lower() in raw_lower for alias in keys)


# Mapping from logical field name to all possible raw key names across formats
_RAW_KEY_ALIASES: dict[str, list[str]] = {
    "script": ["script", "TXXX:SCRIPT", "----:com.apple.iTunes:SCRIPT"],
    "releasecountry": [
        "releasecountry",
        "TXXX:MusicBrainz Album Release Country",
        "----:com.apple.iTunes:MusicBrainz Album Release Country",
    ],
}


def find_patch_targets(
    header: Snapshot | None, snapshots: list[Snapshot], *, mode: PatchMode = "release-tags"
) -> list[PatchTarget]:
    """Identify files that can be patched in the requested mode."""
    root = Path(header["scan"]["root"]) if header else Path(".")
    targets: list[PatchTarget] = []

    for snap in snapshots:
        mb = snap.get("tags", {}).get("external_ids", {}).get("musicbrainz", {})
        release_id = mb.get("release_id")
        if not release_id:
            continue
        release_track_id = mb.get("release_track_id")

        tag_format = snap.get("tags", {}).get("format")
        if not tag_format:
            continue

        rel_path = snap.get("file", {}).get("relative_path", "")
        has_script = _has_raw_tag(snap, "script")
        has_country = _has_raw_tag(snap, "releasecountry")
        has_recording_id = bool(mb.get("recording_id"))

        needs_script = mode in ("release-tags", "all") and not has_script
        needs_country = mode in ("release-tags", "all") and not has_country
        needs_recording_id = (
            mode in ("recording-id", "all")
            and not has_recording_id
            and bool(release_track_id)
        )

        if not (needs_script or needs_country or needs_recording_id):
            continue

        targets.append(PatchTarget(
            relative_path=rel_path,
            absolute_path=root / rel_path,
            tag_format=tag_format,
            release_id=release_id,
            release_track_id=release_track_id,
            needs_script=needs_script,
            needs_country=needs_country,
            needs_recording_id=needs_recording_id,
        ))

    return targets


# ---------- Tag writing ----------

def _write_tags(target: PatchTarget, info: ReleaseInfo) -> list[str]:
    """Write missing tags to a single file. Returns list of fields written."""
    mf = mutagen.File(str(target.absolute_path))
    if mf is None:
        raise RuntimeError(f"mutagen cannot open: {target.absolute_path}")

    written: list[str] = []

    if target.tag_format == "vorbis_comment" and isinstance(mf, (FLAC, OggVorbis, OggOpus)):
        if target.needs_script and info.script:
            mf.tags["script"] = [info.script]
            written.append("script")
        if target.needs_country and info.country:
            mf.tags["releasecountry"] = [info.country]
            written.append("releasecountry")
        recording_id = _recording_id_for_target(target, info)
        if recording_id:
            mf.tags["musicbrainz_trackid"] = [recording_id]
            written.append("recording_id")

    elif target.tag_format == "id3":
        from mutagen.id3 import TXXX
        tags: ID3 = mf.tags  # type: ignore[assignment]
        if tags is None:
            raise RuntimeError(f"No ID3 tags: {target.absolute_path}")
        if target.needs_script and info.script:
            tags.add(TXXX(encoding=3, desc="SCRIPT", text=[info.script]))
            written.append("script")
        if target.needs_country and info.country:
            tags.add(TXXX(encoding=3, desc="MusicBrainz Album Release Country", text=[info.country]))
            written.append("releasecountry")
        recording_id = _recording_id_for_target(target, info)
        if recording_id:
            tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=[recording_id]))
            written.append("recording_id")

    elif target.tag_format == "mp4" and isinstance(mf, MP4):
        prefix = "----:com.apple.iTunes:"
        if target.needs_script and info.script:
            mf.tags[f"{prefix}SCRIPT"] = [info.script.encode("utf-8")]
            written.append("script")
        if target.needs_country and info.country:
            mf.tags[f"{prefix}MusicBrainz Album Release Country"] = [info.country.encode("utf-8")]
            written.append("releasecountry")
        recording_id = _recording_id_for_target(target, info)
        if recording_id:
            mf.tags[f"{prefix}MusicBrainz Track Id"] = [recording_id.encode("utf-8")]
            written.append("recording_id")

    if written:
        mf.save()

    return written


def _recording_id_for_target(target: PatchTarget, info: ReleaseInfo) -> str | None:
    if not target.needs_recording_id or not target.release_track_id:
        return None
    return info.track_recordings.get(target.release_track_id)


# ---------- Orchestrator ----------

@dataclass
class PatchSummary:
    total_targets: int = 0
    unique_releases: int = 0
    api_fetched: int = 0
    api_failed: int = 0
    files_patched: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    fields_written: int = 0


@dataclass
class PatchPlan:
    """Resolved plan: each target paired with its ReleaseInfo."""
    targets: list[PatchTarget] = field(default_factory=list)
    release_cache: dict[str, ReleaseInfo] = field(default_factory=dict)
    api_failures: dict[str, str] = field(default_factory=dict)


def build_plan(
    jsonl_path: Path,
    *,
    mode: PatchMode = "release-tags",
    delay: float = 1.0,
    on_progress: Any = None,
) -> PatchPlan:
    """Load JSONL → find targets → fetch MB API → return plan."""
    header, snapshots = load_snapshots(jsonl_path)
    targets = find_patch_targets(header, snapshots, mode=mode)

    # Deduplicate release_ids
    release_ids = sorted({t.release_id for t in targets})
    include_recordings = mode in ("recording-id", "all")

    release_cache: dict[str, ReleaseInfo] = {}
    api_failures: dict[str, str] = {}

    for i, rid in enumerate(release_ids, 1):
        if on_progress:
            on_progress(i, len(release_ids), rid)
        try:
            release_cache[rid] = fetch_release_info(rid, include_recordings=include_recordings)
        except Exception as exc:
            api_failures[rid] = str(exc)
        if i < len(release_ids):
            time.sleep(delay)

    return PatchPlan(
        targets=targets,
        release_cache=release_cache,
        api_failures=api_failures,
    )


def apply_plan(plan: PatchPlan) -> tuple[PatchSummary, list[tuple[str, list[str]]]]:
    """Execute the patch plan — actually write tags. Returns (summary, actions)."""
    summary = PatchSummary(
        total_targets=len(plan.targets),
        unique_releases=len(plan.release_cache) + len(plan.api_failures),
        api_fetched=len(plan.release_cache),
        api_failed=len(plan.api_failures),
    )
    actions: list[tuple[str, list[str]]] = []

    for target in plan.targets:
        info = plan.release_cache.get(target.release_id)
        if info is None:
            summary.files_skipped += 1
            continue
        # Check if there's actually something to write
        would_write_script = target.needs_script and info.script
        would_write_country = target.needs_country and info.country
        would_write_recording_id = _recording_id_for_target(target, info)
        if not would_write_script and not would_write_country and not would_write_recording_id:
            summary.files_skipped += 1
            continue
        try:
            written = _write_tags(target, info)
            if written:
                summary.files_patched += 1
                summary.fields_written += len(written)
                actions.append((target.relative_path, written))
            else:
                summary.files_skipped += 1
        except Exception:
            summary.files_failed += 1

    return summary, actions


def dry_run_plan(plan: PatchPlan) -> tuple[PatchSummary, list[tuple[str, list[str]]]]:
    """Simulate the patch plan — return summary + list of (path, fields) that would be written."""
    summary = PatchSummary(
        total_targets=len(plan.targets),
        unique_releases=len(plan.release_cache) + len(plan.api_failures),
        api_fetched=len(plan.release_cache),
        api_failed=len(plan.api_failures),
    )
    actions: list[tuple[str, list[str]]] = []

    for target in plan.targets:
        info = plan.release_cache.get(target.release_id)
        if info is None:
            summary.files_skipped += 1
            continue
        fields: list[str] = []
        if target.needs_script and info.script:
            fields.append(f"script = {info.script}")
        if target.needs_country and info.country:
            fields.append(f"releasecountry = {info.country}")
        recording_id = _recording_id_for_target(target, info)
        if recording_id:
            fields.append(f"recording_id = {recording_id}")
        if fields:
            actions.append((target.relative_path, fields))
            summary.files_patched += 1
            summary.fields_written += len(fields)
        else:
            summary.files_skipped += 1

    return summary, actions
