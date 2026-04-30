"""Release-level Markdown report generator for tagwell library_releases.jsonl."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from tagwell._report_helpers import (
    Snapshot,
    WriteLine,
    _md,
    _pct,
    _sort_text,
)

_COUNTRY_NAMES = {
    "AF": "Afghanistan", "CN": "China", "JP": "Japan", "KR": "South Korea",
    "TW": "Taiwan", "US": "United States", "GB": "United Kingdom", "DE": "Germany",
    "FR": "France", "XW": "Worldwide",
}

_LOWEST_COMPLETENESS_LIMIT = 20
_MISSING_TRACK_TRUNCATE = 3
_ORPHAN_LIST_LIMIT = 50
_MULTI_DISC_LIMIT = 10


# ---------- Loading ----------

def load_releases_jsonl(jsonl_path: Path) -> dict[str, Any]:
    """Parse a library_releases.jsonl into its component records."""
    out: dict[str, Any] = {
        "header": None,
        "trailer": None,
        "snapshots": [],
        "errors": [],
        "orphan": None,
    }
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rt = rec.get("record_type")
            if rt == "releases_header":
                out["header"] = rec
            elif rt == "releases_trailer":
                out["trailer"] = rec
            elif rt == "release_snapshot":
                out["snapshots"].append(rec)
            elif rt == "release_error":
                out["errors"].append(rec)
            elif rt == "orphan_file":
                out["orphan"] = rec
    return out


# ---------- Top-level entry ----------

def generate_releases_report(jsonl_path: Path) -> str:
    parsed = load_releases_jsonl(jsonl_path)
    snapshots: list[Snapshot] = parsed["snapshots"]

    if not snapshots and not parsed["errors"]:
        return "# Tagwell Releases Report\n\nNo release records found in JSONL.\n"

    lines: list[str] = []
    write = lines.append

    write("# Tagwell Releases Report\n")
    _render_source_info(write, jsonl_path, parsed["header"], parsed["trailer"])
    _render_overview(write, parsed)
    _render_completeness_by_type(write, snapshots)
    _render_match_audit(write, snapshots)
    _render_inst_audit(write, snapshots)
    _render_release_group_composition(write, snapshots)
    _render_country_profile(write, snapshots)
    _render_decade_profile(write, snapshots)
    _render_below_full_completeness(write, snapshots)
    _render_multi_disc(write, snapshots)
    _render_failed_releases(write, parsed["errors"])

    return "\n".join(lines) + "\n"


# ---------- Sections ----------

def _render_source_info(write: WriteLine, jsonl_path: Path, header: dict | None, trailer: dict | None) -> None:
    write(f"- **Source**: `{jsonl_path.name}`")
    if header:
        stage = header.get("stage", {})
        scanner = header.get("scanner", {})
        write(f"- **Source library**: `{stage.get('source_jsonl', '?')}`")
        write(f"- **Stage started**: {stage.get('started_at', '?')}")
        write(f"- **Scanner**: {scanner.get('name', '?')} {scanner.get('version', '?')}")
        write(f"- **MB API**: `{stage.get('mb_api_base', '?')}` (inc=`{stage.get('mb_inc', '?')}`)")
    if trailer:
        write(f"- **Stage completed**: {trailer.get('completed_at', '?')} ({trailer.get('elapsed_seconds', 0)}s)")
    write("")


def _render_overview(write: WriteLine, parsed: dict) -> None:
    snapshots = parsed["snapshots"]
    trailer = parsed["trailer"] or {}
    stats = trailer.get("stats", {})

    fully_complete = sum(
        1 for s in snapshots
        if (_completeness(s).get("ratio_vocal") or 0) >= 1.0
    )

    write("## Overview\n")
    write("| Metric | Count |")
    write("|--------|-------|")
    write(f"| Releases (snapshots) | {len(snapshots)} |")
    write(f"| Releases at 100% vocal completeness | {fully_complete} ({_pct(fully_complete, len(snapshots))}) |")
    write(f"| Releases failed | {len(parsed['errors'])} |")
    write(f"| Total local files | {stats.get('total_local_files', '?')} |")
    write(f"| Matched local files | {stats.get('matched_local_files', '?')} |")
    write(f"| Orphan files (no release_id) | {stats.get('orphan_files', '?')} |")
    write("")


def _render_completeness_by_type(write: WriteLine, snapshots: list[Snapshot]) -> None:
    """Bucket vocal completeness by release-group type. Compilations & soundtracks
    naturally show low ratios because users typically cherry-pick from them; this
    split makes that obvious instead of dragging down a misleading library-wide number.
    """
    by_bucket: dict[str, list[float]] = defaultdict(list)
    at_100: dict[str, int] = defaultdict(int)
    for snap in snapshots:
        ratio = _completeness(snap).get("ratio_vocal")
        if ratio is None:
            continue
        bucket = _release_group_label(snap)
        by_bucket[bucket].append(ratio)
        if ratio >= 1.0:
            at_100[bucket] += 1

    if not by_bucket:
        return

    write("## Vocal completeness by release type\n")
    write(
        "Average vocal-track ratio per release-group type. Buckets use MusicBrainz canonical "
        "primary/secondary types verbatim, so labels like `Album + Compilation` reflect MB data, "
        "not tagwell inference. Compilations and soundtracks are typically partial by design — "
        "low ratios there are usually intentional.\n"
    )
    write("| Type | Releases | Avg vocal ratio | At 100% |")
    write("|------|----------|-----------------|---------|")
    for bucket in sorted(by_bucket, key=lambda k: (-len(by_bucket[k]), _sort_text(k))):
        ratios = by_bucket[bucket]
        avg = sum(ratios) / len(ratios)
        complete = at_100[bucket]
        write(
            f"| {_md(bucket)} | {len(ratios)} | {avg * 100:.1f}% | "
            f"{complete} ({_pct(complete, len(ratios))}) |"
        )
    write("")
    _render_other_primary_type_note(write, snapshots)


def _render_match_audit(write: WriteLine, snapshots: list[Snapshot]) -> None:
    method_counter: Counter[str] = Counter()
    weak_releases: list[tuple[str, str, int, int]] = []  # (album, artist, weak_count, total)

    for snap in snapshots:
        local_method_counts: Counter[str] = Counter()
        for medium in snap.get("media", []):
            for track in medium.get("tracks", []):
                for entry in track.get("local", []):
                    method = entry.get("matched_by") or "unknown"
                    method_counter[method] += 1
                    local_method_counts[method] += 1
        weak = local_method_counts.get("position", 0)
        total_matched = sum(local_method_counts.values())
        if weak and total_matched and weak / total_matched >= 0.5:
            weak_releases.append((
                snap.get("title") or snap.get("release_id"),
                _artist_label(snap),
                weak,
                total_matched,
            ))

    write("## MB match audit\n")
    write("How each local file was matched to a canonical MB track.\n")

    total = sum(method_counter.values())
    if total == 0:
        write("No matched local files.\n")
        return

    write("| Method | Count | % of matched |")
    write("|--------|-------|--------------|")
    for method in ["recording_id", "release_track_id", "position"]:
        count = method_counter.get(method, 0)
        write(f"| {method} | {count} | {_pct(count, total)} |")
    write("")

    write(
        "These counts are winner-takes-all after tagwell's current priority order, not raw key-availability counts. "
        "The matcher tries `recording_id` first, then `release_track_id`, then disc/track position, so a 100% "
        "`recording_id` result means the first key was sufficient for every matched file — not that the lower-priority "
        "paths are broken.\n"
    )

    if method_counter.get("position", 0):
        write("Position-only matches mean the local file lacked an MBID strong enough to lock the track. "
              "Run `tagwell patch --mode recording-id --apply` to backfill recording IDs.\n")

    if weak_releases:
        write("### Releases relying on position-match for ≥50% of tracks\n")
        write("| Album | Album artist | Position-matched / total |")
        write("|-------|--------------|--------------------------|")
        for album, artist, weak, total_matched in sorted(weak_releases, key=lambda r: (-r[2], _sort_text(r[0])))[:15]:
            write(f"| {_md(album)} | {_md(artist)} | {weak} / {total_matched} |")
        write("")


def _render_inst_audit(write: WriteLine, snapshots: list[Snapshot]) -> None:
    signal_counter: Counter[str] = Counter()
    inst_total = 0
    for snap in snapshots:
        for medium in snap.get("media", []):
            for track in medium.get("tracks", []):
                if track.get("is_instrumental"):
                    inst_total += 1
                    signal = track.get("inst_signal") or "(none)"
                    signal_counter[signal] += 1

    write("## Instrumental detection audit\n")
    if inst_total == 0:
        write("No instrumental tracks detected across the library.\n")
        return

    write(
        "Confidence breakdown for tracks flagged as instrumental. This is a cascade audit — work-rel, then "
        "recording disambiguation, then title regex — so it behaves more like a health check of MusicBrainz editorial "
        "coverage in this domain than a quality score for tagwell itself.\n"
    )
    write("| Signal | Count | % of inst | Confidence |")
    write("|--------|-------|-----------|------------|")
    confidence_label = {
        "work-rel": "high (MB editorial)",
        "disambiguation": "medium (free-text editorial)",
        "title": "low (regex)",
    }
    for signal in ["work-rel", "disambiguation", "title"]:
        count = signal_counter.get(signal, 0)
        if count == 0:
            continue
        write(f"| {signal} | {count} | {_pct(count, inst_total)} | {confidence_label[signal]} |")
    write("")
    if signal_counter.get("title", 0) / inst_total >= 0.75:
        write(
            "Heavy `title` usage usually means the canonical MB work relationship is sparsely edited for these releases. "
            "In other words, this table is primarily telling you about MB data coverage in the anison-heavy part of the library, "
            "not that tagwell had to make a suspicious fallback.\n"
        )
    write(f"**Total instrumental tracks**: {inst_total}\n")


def _render_release_group_composition(write: WriteLine, snapshots: list[Snapshot]) -> None:
    bucket_counter: Counter[str] = Counter()
    for snap in snapshots:
        bucket_counter[_release_group_label(snap)] += 1

    if not bucket_counter:
        return

    write("## Release-group composition (MB canonical)\n")
    write("Primary + secondary types are shown verbatim from MusicBrainz canonical release-group data.\n")
    write("| Type | Count | % |")
    write("|------|-------|---|")
    for label, count in sorted(bucket_counter.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
        write(f"| {_md(label)} | {count} | {_pct(count, len(snapshots))} |")
    write("")


def _render_country_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    country_counter: Counter[str] = Counter()
    missing = 0
    for snap in snapshots:
        country = snap.get("country")
        if not country:
            missing += 1
            continue
        country_counter[country] += 1

    if not country_counter:
        return

    write("## Release country profile (MB canonical)\n")
    if missing:
        write(f"- **Missing country**: {missing} release(s)\n")
    write("| Country | Count | % |")
    write("|---------|-------|---|")
    for code, count in sorted(country_counter.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
        label = f"{code} ({_COUNTRY_NAMES[code]})" if code in _COUNTRY_NAMES else code
        write(f"| {_md(label)} | {count} | {_pct(count, len(snapshots))} |")
    write("")


def _render_decade_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    decade_counter: Counter[str] = Counter()
    no_date = 0
    for snap in snapshots:
        year = _release_year(snap)
        if year is None:
            no_date += 1
            continue
        decade_counter[f"{(year // 10) * 10}s"] += 1

    if not decade_counter:
        return

    write("## Release timeline (MB canonical)\n")
    if no_date:
        write(f"- **Missing date**: {no_date} release(s)\n")
    write("| Decade | Releases | % |")
    write("|--------|----------|---|")
    for decade in sorted(decade_counter, key=lambda value: int(value[:-1])):
        count = decade_counter[decade]
        write(f"| {decade} | {count} | {_pct(count, len(snapshots))} |")
    write("")


def _render_below_full_completeness(write: WriteLine, snapshots: list[Snapshot]) -> None:
    """Split below-100% releases into actionable single-artist albums vs cherry-picked
    compilations/soundtracks/VA. Mixing them in one ranking buries the few real
    candidates under expected partial-ownership noise."""
    actionable: list[dict[str, Any]] = []
    cherry: list[dict[str, Any]] = []
    artist_track_counts = _artist_track_counts(snapshots)

    for snap in snapshots:
        c = _completeness(snap)
        ratio = c.get("ratio_vocal")
        if ratio is None or ratio >= 1.0:
            continue
        artist = _artist_label(snap)
        artist_tracks = artist_track_counts.get(artist, 0)
        row = {
            "album": snap.get("title") or snap.get("release_id"),
            "artist": artist,
            "artist_tracks": artist_tracks,
            "mb_type": _release_group_label(snap),
            "ratio": ratio,
            "priority": (1.0 - ratio) * artist_tracks,
            "present": c.get("tracks_vocal_present", 0),
            "total": c.get("tracks_vocal_total", 0),
            "missing_titles": _collect_missing_vocal_titles(snap),
        }
        if _is_cherry_pick(snap):
            cherry.append(row)
        else:
            actionable.append(row)

    write("## Below-100% releases\n")
    if not actionable and not cherry:
        write("All releases are at 100% vocal completeness.\n")
        return

    if actionable:
        write(f"### Single-artist releases below 100% ({len(actionable)})\n")
        write(
            "Likely actionable — non-compilation albums where some vocal tracks are missing. "
            "Keep the plain completeness ranking first, then add an artist-weighted view so "
            '"low-ratio one-offs" and "deep-artist backlog" do not get collapsed into one list.\n'
        )
        write(
            f"Baseline completeness view: sorted ascending by vocal ratio, showing up to {_LOWEST_COMPLETENESS_LIMIT}.\n"
        )
        write("| Album | Album artist | Vocal | Ratio | Missing tracks |")
        write("|-------|--------------|-------|-------|----------------|")
        for row in sorted(actionable, key=lambda r: (r["ratio"], _sort_text(r["album"])))[:_LOWEST_COMPLETENESS_LIMIT]:
            missing = row["missing_titles"]
            if len(missing) > _MISSING_TRACK_TRUNCATE:
                shown = missing[:_MISSING_TRACK_TRUNCATE]
                missing_label = ", ".join(shown) + f" (+{len(missing) - _MISSING_TRACK_TRUNCATE} more)"
            else:
                missing_label = ", ".join(missing) or "—"
            write(
                f"| {_md(row['album'])} | {_md(row['artist'])} | {row['present']}/{row['total']} | "
                f"{row['ratio'] * 100:.1f}% | {_md(missing_label)} |"
            )
        write("")

        write(
            "Artist-weighted view: ranked by discovery value = (1 - vocal ratio) x artist tracks in library, so "
            f"deeper artist relationships rise above one-off pickups. Showing up to {_LOWEST_COMPLETENESS_LIMIT}.\n"
        )
        write("| Album | Album artist | Artist tracks in library | Vocal | Ratio | Missing tracks |")
        write("|-------|--------------|--------------------------|-------|-------|----------------|")
        for row in sorted(
            actionable,
            key=lambda r: (-r["priority"], -r["artist_tracks"], r["ratio"], _sort_text(r["album"])),
        )[:_LOWEST_COMPLETENESS_LIMIT]:
            missing = row["missing_titles"]
            if len(missing) > _MISSING_TRACK_TRUNCATE:
                shown = missing[:_MISSING_TRACK_TRUNCATE]
                missing_label = ", ".join(shown) + f" (+{len(missing) - _MISSING_TRACK_TRUNCATE} more)"
            else:
                missing_label = ", ".join(missing) or "—"
            write(
                f"| {_md(row['album'])} | {_md(row['artist'])} | {row['artist_tracks']} | "
                f"{row['present']}/{row['total']} | {row['ratio'] * 100:.1f}% | {_md(missing_label)} |"
            )
        write("")

    if cherry:
        write(f"### Cherry-picked compilations / soundtracks ({len(cherry)})\n")
        write(
            "Below 100% by intent — Various Artists releases, compilations, and soundtracks where partial ownership is normal. "
            "This grouping follows MusicBrainz canonical release-group types and Various Artists credits verbatim, so an entry can "
            "look surprising if MB itself marks it as `Compilation` or `Soundtrack`. Listed by tracks owned (descending) in case any "
            "of these are candidates for completion.\n"
        )
        write("| Album | Album artist | MB type | Vocal | Ratio |")
        write("|-------|--------------|---------|-------|-------|")
        for row in sorted(cherry, key=lambda r: (-r["present"], _sort_text(r["album"])))[:10]:
            write(
                f"| {_md(row['album'])} | {_md(row['artist'])} | {_md(row['mb_type'])} | "
                f"{row['present']}/{row['total']} | {row['ratio'] * 100:.1f}% |"
            )
        if len(cherry) > 10:
            write(f"\n…and {len(cherry) - 10} more cherry-picked releases.")
        write("")


def _is_cherry_pick(snap: Snapshot) -> bool:
    rg = snap.get("release_group") or {}
    primary = rg.get("primary_type") or ""
    secondary = rg.get("secondary_types") or []
    if "Compilation" in secondary or "Soundtrack" in secondary:
        return True
    if primary in ("Compilation", "Soundtrack"):
        return True
    artists = snap.get("artist_credit") or []
    if any((a.get("name") or "").casefold() == "various artists" for a in artists):
        return True
    return False


def _render_multi_disc(write: WriteLine, snapshots: list[Snapshot]) -> None:
    multi_rows = _multi_disc_rows(snapshots)
    if not multi_rows:
        return

    largest = sorted(multi_rows, key=lambda row: (-row["discs_total"], _sort_text(row["album"])))[:3]
    top_label = ", ".join(f"{row['album']} ({row['discs_total']} discs)" for row in largest)
    cross_disc = [row for row in multi_rows if row["discs_touched"] >= 2]
    single_disc = [row for row in multi_rows if row["discs_touched"] == 1]

    write("## Multi-disc releases\n")
    write(f"**{len(multi_rows)}** releases span multiple media (per MB canonical). Largest: {top_label}.\n")
    write(
        "Here `discs touched` means the number of media with at least one local file. That is not full-disc completion, "
        "but it is enough to separate releases you are collecting across the box from releases you only touched on one disc.\n"
    )
    write(f"- **Cross-disc collecting signal**: {len(cross_disc)} release(s) touched on 2+ discs")
    write(f"- **Single-disc touch signal**: {len(single_disc)} release(s) touched on exactly 1 disc\n")

    if cross_disc:
        write(f"### Cross-disc collecting signals ({len(cross_disc)})\n")
        write("Likely systematic collecting — your files are spread across multiple discs of the same release.\n")
        write("| Album | Album artist | Discs touched | Vocal | Ratio |")
        write("|-------|--------------|---------------|-------|-------|")
        for row in sorted(
            cross_disc,
            key=lambda item: (-item["discs_touched"], -item["ratio"], -item["present"], _sort_text(item["album"])),
        )[:_MULTI_DISC_LIMIT]:
            write(
                f"| {_md(row['album'])} | {_md(row['artist'])} | {row['discs_touched']}/{row['discs_total']} | "
                f"{row['present']}/{row['total']} | {row['ratio'] * 100:.1f}% |"
            )
        if len(cross_disc) > _MULTI_DISC_LIMIT:
            write(f"\n…and {len(cross_disc) - _MULTI_DISC_LIMIT} more cross-disc releases.")
        write("")

    if single_disc:
        write(f"### Single-disc touches inside larger releases ({len(single_disc)})\n")
        write("More like sampling or targeted cherry-picking — the release is multi-disc, but your files only land on one disc.\n")
        write("| Album | Album artist | Discs touched | Vocal | Ratio |")
        write("|-------|--------------|---------------|-------|-------|")
        for row in sorted(
            single_disc,
            key=lambda item: (-item["discs_total"], item["ratio"], -item["present"], _sort_text(item["album"])),
        )[:_MULTI_DISC_LIMIT]:
            write(
                f"| {_md(row['album'])} | {_md(row['artist'])} | {row['discs_touched']}/{row['discs_total']} | "
                f"{row['present']}/{row['total']} | {row['ratio'] * 100:.1f}% |"
            )
        if len(single_disc) > _MULTI_DISC_LIMIT:
            write(f"\n…and {len(single_disc) - _MULTI_DISC_LIMIT} more single-disc-touch releases.")
        write("")


def _render_failed_releases(write: WriteLine, errors: list[dict]) -> None:
    if not errors:
        return

    write("## Failed releases\n")
    write(f"{len(errors)} release(s) failed to fetch from MusicBrainz. They will be retried automatically on the next `tagwell complete` run.\n")
    write("| release_id | Error | When |")
    write("|------------|-------|------|")
    for err in errors:
        e = err.get("error", {})
        kind = e.get("kind", "?")
        message = e.get("message", "?")
        attempted_at = e.get("attempted_at", "?")
        rid = err.get("release_id", "?")
        write(f"| `{rid}` | {_md(f'{kind}: {message}')} | {attempted_at} |")
    write("")


def _render_orphan_files(write: WriteLine, orphan: dict | None) -> None:
    if not orphan:
        return
    files = orphan.get("files") or []
    count = orphan.get("count", len(files))

    write("## Orphan files\n")
    write(f"{count} local file(s) lack a `release_id` and could not be enriched. They are missing the MusicBrainz Album Id tag — `tagwell patch` cannot help here; the files would need to be tagged from a Picard-style workflow first.\n")

    shown = files[:_ORPHAN_LIST_LIMIT]
    for entry in shown:
        write(f"- `{entry.get('relative_path', '?')}`")
    if count > len(shown):
        write(f"- … and {count - len(shown)} more")
    write("")


# ---------- Helpers ----------

def _completeness(snap: Snapshot) -> dict[str, Any]:
    return snap.get("completeness") or {}


def _artist_track_counts(snapshots: list[Snapshot]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for snap in snapshots:
        artist = _artist_label(snap)
        if artist == "—":
            continue
        for medium in snap.get("media", []):
            for track in medium.get("tracks", []):
                counter[artist] += len(track.get("local") or [])
    return counter


def _render_other_primary_type_note(write: WriteLine, snapshots: list[Snapshot]) -> None:
    titles = _other_primary_type_titles(snapshots)
    if not titles:
        return
    preview = ", ".join(titles[:3])
    if len(titles) > 3:
        preview += f" (+{len(titles) - 3} more)"
    write(f"- **MB `primary_type=Other`**: {_md(preview)}\n")


def _other_primary_type_titles(snapshots: list[Snapshot]) -> list[str]:
    titles = [
        snap.get("title") or snap.get("release_id") or "?"
        for snap in snapshots
        if (snap.get("release_group") or {}).get("primary_type") == "Other"
    ]
    return sorted(titles, key=_sort_text)


def _multi_disc_rows(snapshots: list[Snapshot]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snap in snapshots:
        media = snap.get("media") or []
        if len(media) <= 1:
            continue
        discs_touched = sum(
            1
            for medium in media
            if any(track.get("local") for track in medium.get("tracks", []))
        )
        c = _completeness(snap)
        ratio = c.get("ratio_vocal")
        rows.append({
            "album": snap.get("title") or snap.get("release_id"),
            "artist": _artist_label(snap),
            "discs_total": len(media),
            "discs_touched": discs_touched,
            "present": c.get("tracks_vocal_present", 0),
            "total": c.get("tracks_vocal_total", 0),
            "ratio": ratio or 0.0,
        })
    return rows


def _release_year(snap: Snapshot) -> int | None:
    date = snap.get("date") or ""
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _artist_label(snap: Snapshot) -> str:
    artists = snap.get("artist_credit") or []
    names = [a.get("name") for a in artists if a.get("name")]
    return ", ".join(names) if names else "—"


def _release_group_label(snap: Snapshot) -> str:
    rg = snap.get("release_group") or {}
    primary = rg.get("primary_type") or "Unknown"
    secondary = rg.get("secondary_types") or []
    if not secondary:
        return primary
    return f"{primary} + {', '.join(secondary)}"


def _collect_missing_vocal_titles(snap: Snapshot) -> list[str]:
    missing: list[str] = []
    for medium in snap.get("media", []):
        for track in medium.get("tracks", []):
            if track.get("is_instrumental"):
                continue
            if track.get("local"):
                continue
            title = track.get("title") or f"#{track.get('position', '?')}"
            missing.append(title)
    return missing
