"""Patch missing release-level tags by querying the MusicBrainz API.

Currently patches: script, releasecountry.
Only writes to files that already have a release_id but are missing these fields.
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


# ---------- MusicBrainz API ----------

@dataclass
class ReleaseInfo:
    script: str | None = None
    country: str | None = None


def fetch_release_info(release_id: str) -> ReleaseInfo:
    """Fetch script and country from the MusicBrainz release endpoint."""
    url = f"{_MB_API_BASE}/release/{release_id}?fmt=json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    text_rep = data.get("text-representation") or {}
    return ReleaseInfo(
        script=text_rep.get("script"),
        country=data.get("country"),
    )


# ---------- Snapshot analysis ----------

@dataclass
class PatchTarget:
    """A file that needs patching."""
    relative_path: str
    absolute_path: Path
    tag_format: str
    release_id: str
    needs_script: bool = False
    needs_country: bool = False


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
    header: Snapshot | None, snapshots: list[Snapshot]
) -> list[PatchTarget]:
    """Identify files that have release_id but are missing script/releasecountry."""
    root = Path(header["scan"]["root"]) if header else Path(".")
    targets: list[PatchTarget] = []

    for snap in snapshots:
        mb = snap.get("tags", {}).get("external_ids", {}).get("musicbrainz", {})
        release_id = mb.get("release_id")
        if not release_id:
            continue

        tag_format = snap.get("tags", {}).get("format")
        if not tag_format:
            continue

        rel_path = snap.get("file", {}).get("relative_path", "")
        has_script = _has_raw_tag(snap, "script")
        has_country = _has_raw_tag(snap, "releasecountry")

        if has_script and has_country:
            continue

        targets.append(PatchTarget(
            relative_path=rel_path,
            absolute_path=root / rel_path,
            tag_format=tag_format,
            release_id=release_id,
            needs_script=not has_script,
            needs_country=not has_country,
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

    elif target.tag_format == "mp4" and isinstance(mf, MP4):
        prefix = "----:com.apple.iTunes:"
        if target.needs_script and info.script:
            mf.tags[f"{prefix}SCRIPT"] = [info.script.encode("utf-8")]
            written.append("script")
        if target.needs_country and info.country:
            mf.tags[f"{prefix}MusicBrainz Album Release Country"] = [info.country.encode("utf-8")]
            written.append("releasecountry")

    if written:
        mf.save()

    return written


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
    delay: float = 1.0,
    on_progress: Any = None,
) -> PatchPlan:
    """Load JSONL → find targets → fetch MB API → return plan."""
    header, snapshots = load_snapshots(jsonl_path)
    targets = find_patch_targets(header, snapshots)

    # Deduplicate release_ids
    release_ids = sorted({t.release_id for t in targets})

    release_cache: dict[str, ReleaseInfo] = {}
    api_failures: dict[str, str] = {}

    for i, rid in enumerate(release_ids, 1):
        if on_progress:
            on_progress(i, len(release_ids), rid)
        try:
            release_cache[rid] = fetch_release_info(rid)
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
        if not would_write_script and not would_write_country:
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
        if fields:
            actions.append((target.relative_path, fields))
            summary.files_patched += 1
            summary.fields_written += len(fields)
        else:
            summary.files_skipped += 1

    return summary, actions
