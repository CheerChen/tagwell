"""Metadata quality report generator for tagwell JSONL output."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

Snapshot = dict[str, Any]
WriteLine = Callable[[str], None]

_SCRIPT_BUCKETS: list[tuple[str, set[str]]] = [
    ("Japanese", {"Jpan", "Kana"}),
    ("Latin", {"Latn"}),
    ("Chinese", {"Hans", "Hant"}),
    ("Korean", {"Hang", "Kore"}),
]
_COUNTRY_NAMES = {
    "AF": "Afghanistan",
    "CN": "China",
    "JP": "Japan",
    "KR": "South Korea",
    "TW": "Taiwan",
    "US": "United States",
    "XW": "Worldwide",
}
_CATALOG_BUCKET_ORDER = ["1 track", "2-3 tracks", "4-9 tracks", "10+ tracks"]
_DURATION_BUCKET_ORDER = ["<30s", "30-59s", "1-1:59", "2-2:59", "3-4:59", "5-9:59", "10-14:59", "15m+"]
_COMPLETENESS_BUCKET_ORDER = ["100%", "75-99%", "50-74%", "<50%", ">100%"]


@dataclass
class AlbumGroup:
    key: tuple[Any, ...]
    album: str
    album_artists: tuple[str, ...] = ()
    release_id: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)


def load_snapshots(jsonl_path: Path) -> tuple[Snapshot | None, list[Snapshot]]:
    """Load a tagwell JSONL file. Returns (header_or_None, list_of_snapshots)."""
    header = None
    snapshots: list[Snapshot] = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            record_type = rec.get("record_type")
            if record_type == "scan_header":
                header = rec
            elif record_type == "audio_file_snapshot":
                snapshots.append(rec)
    return header, snapshots


def generate_report(jsonl_path: Path) -> str:
    """Generate a Markdown quality report from a tagwell JSONL file."""
    header, snapshots = load_snapshots(jsonl_path)
    if not snapshots:
        return "# Tagwell Metadata Report\n\nNo audio files found in JSONL.\n"

    album_groups, group_lookup = _build_album_groups(snapshots)

    lines: list[str] = []
    write = lines.append

    write("# Tagwell Metadata Report\n")
    _render_source_info(write, jsonl_path, header)
    _render_overview(write, snapshots, album_groups)

    write("## Data Quality\n")
    _render_musicbrainz_coverage(write, snapshots, album_groups, group_lookup)
    _render_parsed_field_completeness(write, snapshots)
    _render_cover_art(write, snapshots)

    write("## Library Shape\n")
    _render_top_lists(write, snapshots, album_groups)
    _render_catalog_depth(write, snapshots)
    _render_album_completeness(write, album_groups)
    _render_multi_disc(write, album_groups)
    _render_va_segment(write, snapshots, album_groups)
    _render_release_timeline(write, snapshots)
    _render_peak_years(write, snapshots)
    _render_label_profile(write, snapshots)
    _render_script_profile(write, snapshots)
    _render_release_country_profile(write, snapshots)
    _render_duration_profile(write, snapshots)
    _render_collaboration_profile(write, snapshots)
    _render_encoding_profile(write, snapshots)

    return "\n".join(lines) + "\n"


def _render_source_info(write: WriteLine, jsonl_path: Path, header: Snapshot | None) -> None:
    if not header:
        return

    scan = header.get("scan", {})
    scanner = header.get("scanner", {})
    write(f"- **Source**: `{jsonl_path.name}`")
    write(f"- **Scan root**: `{scan.get('root', '?')}`")
    write(f"- **Scanned at**: {scan.get('started_at', '?')}")
    write(f"- **Scanner**: {scanner.get('name', '?')} {scanner.get('version', '?')}")
    reader = scanner.get("reader", {})
    if reader:
        write(f"- **Reader**: {reader.get('name', '?')} {reader.get('version', '?')}")
    write("")


def _render_overview(write: WriteLine, snapshots: list[Snapshot], album_groups: list[AlbumGroup]) -> None:
    all_artists: set[str] = set()
    all_album_artists: set[str] = set()

    total_duration = 0.0
    for snapshot in snapshots:
        parsed = _parsed(snapshot)
        all_artists.update(parsed.get("artists", []))
        all_album_artists.update(parsed.get("album_artists", []))
        duration = _audio(snapshot).get("duration_seconds")
        if duration:
            total_duration += duration

    write("## Overview\n")
    write("| Metric | Count |")
    write("|--------|-------|")
    write(f"| Audio files | {len(snapshots)} |")
    write(f"| Unique albums | {len(album_groups)} |")
    write(f"| Unique artists | {len(all_artists)} |")
    write(f"| Unique album artists | {len(all_album_artists)} |")
    write("")
    write(f"**Total duration**: {total_duration / 3600:.1f} hours ({total_duration:.0f} seconds)\n")


def _render_musicbrainz_coverage(
    write: WriteLine,
    snapshots: list[Snapshot],
    album_groups: list[AlbumGroup],
    group_lookup: dict[int, AlbumGroup],
) -> None:
    total = len(snapshots)
    has_recording = 0
    has_release = 0
    has_release_group = 0
    missing_recording: list[Snapshot] = []

    for snapshot in snapshots:
        musicbrainz = _musicbrainz(snapshot)
        if musicbrainz.get("recording_id"):
            has_recording += 1
        else:
            missing_recording.append(snapshot)
        if musicbrainz.get("release_id"):
            has_release += 1
        if musicbrainz.get("release_group_id"):
            has_release_group += 1

    write("### MusicBrainz coverage\n")
    write("| ID type | Count | % |")
    write("|---------|-------|---|")
    write(f"| recording_id | {has_recording} | {_pct(has_recording, total)} |")
    write(f"| release_id | {has_release} | {_pct(has_release, total)} |")
    write(f"| release_group_id | {has_release_group} | {_pct(has_release_group, total)} |")
    write("")

    if not missing_recording:
        write("All files have a MusicBrainz recording_id.\n")
        return

    groups_by_key = {group.key: group for group in album_groups}
    missing_by_album: Counter[tuple[Any, ...]] = Counter()
    for snapshot in missing_recording:
        missing_by_album[group_lookup[id(snapshot)].key] += 1

    write("### Missing recording_id backlog\n")
    write(f"{len(missing_recording)} files are missing a MusicBrainz `recording_id`.\n")
    write("| Album | Album artist | Missing tracks |")
    write("|-------|--------------|----------------|")
    for key, count in sorted(
        missing_by_album.items(),
        key=lambda item: (-item[1], _sort_text(groups_by_key[item[0]].album), _sort_text(_join_names(groups_by_key[item[0]].album_artists))),
    ):
        group = groups_by_key[key]
        write(
            f"| {_md(group.album)} | {_md(_join_names(group.album_artists) or '—')} | {count} |"
        )
    write("")


def _render_parsed_field_completeness(write: WriteLine, snapshots: list[Snapshot]) -> None:
    total = len(snapshots)
    missing: Counter[str] = Counter()
    single_fields = ["title", "album"]
    multi_fields = ["artists", "album_artists", "genres", "labels"]
    special_fields = {
        "release_date": lambda parsed: parsed.get("release_date") is None or parsed.get("release_date", {}).get("year") is None,
        "track_number": lambda parsed: parsed.get("track_number") is None,
        "track_total": lambda parsed: parsed.get("track_total") is None,
        "disc_number": lambda parsed: parsed.get("disc_number") is None,
        "disc_total": lambda parsed: parsed.get("disc_total") is None,
    }

    for snapshot in snapshots:
        parsed = _parsed(snapshot)
        for field in single_fields:
            if not parsed.get(field):
                missing[field] += 1
        for field in multi_fields:
            if not parsed.get(field):
                missing[field] += 1
        for field, is_missing in special_fields.items():
            if is_missing(parsed):
                missing[field] += 1

    write("### Parsed field completeness\n")
    write("| Field | Missing | % | Present |")
    write("|-------|---------|---|---------|")
    for field in single_fields + multi_fields + list(special_fields.keys()):
        count = missing.get(field, 0)
        write(f"| {field} | {count} | {_pct(count, total)} | {total - count} |")
    write("")


def _render_cover_art(write: WriteLine, snapshots: list[Snapshot]) -> None:
    total = len(snapshots)
    has_cover = sum(1 for snapshot in snapshots if _tags(snapshot).get("pictures"))

    write("### Cover art\n")
    if has_cover == total:
        write(f"All {total} files have embedded cover art.\n")
        return

    write("| Metric | Count | % |")
    write("|--------|-------|---|")
    write(f"| Has cover art | {has_cover} | {_pct(has_cover, total)} |")
    write(f"| Missing cover art | {total - has_cover} | {_pct(total - has_cover, total)} |")
    write("")

    missing_cover = [
        _relative_path(snapshot)
        for snapshot in snapshots
        if not _tags(snapshot).get("pictures")
    ]
    if missing_cover:
        write("#### Files missing cover art\n")
        for path in sorted(missing_cover):
            write(f"- `{path}`")
        write("")


def _render_top_lists(write: WriteLine, snapshots: list[Snapshot], album_groups: list[AlbumGroup]) -> None:
    artist_counter: Counter[str] = Counter()
    for snapshot in snapshots:
        for artist in _parsed(snapshot).get("artists", []):
            artist_counter[artist] += 1

    write("### Top 10 artists by track count\n")
    write("| Artist | Tracks |")
    write("|--------|--------|")
    for artist, count in _top_counter_rows(artist_counter, limit=10):
        write(f"| {_md(artist)} | {count} |")
    write("")
    top10_sum = sum(c for _, c in artist_counter.most_common(10))
    write(f"Top 10 artists account for {_pct(top10_sum, len(snapshots))} of the library ({top10_sum}/{len(snapshots)}).\n")

    write("### Top 10 albums by track count\n")
    write("| Album | Album artist | Tracks |")
    write("|-------|--------------|--------|")
    top_groups = sorted(
        album_groups,
        key=lambda group: (-len(group.snapshots), _sort_text(group.album), _sort_text(_join_names(group.album_artists))),
    )[:10]
    for group in top_groups:
        write(
            f"| {_md(group.album)} | {_md(_join_names(group.album_artists) or '—')} | {len(group.snapshots)} |"
        )
    write("")
    top10_album_sum = sum(len(g.snapshots) for g in top_groups)
    write(f"Top 10 albums account for {_pct(top10_album_sum, len(snapshots))} of the library ({top10_album_sum}/{len(snapshots)}).\n")


def _render_catalog_depth(write: WriteLine, snapshots: list[Snapshot]) -> None:
    artist_counter: Counter[str] = Counter()
    for snapshot in snapshots:
        for artist in _parsed(snapshot).get("artists", []):
            artist_counter[artist] += 1

    total_artists = len(artist_counter)
    depth_counter: Counter[str] = Counter()
    for count in artist_counter.values():
        depth_counter[_catalog_bucket(count)] += 1

    write("### Catalog depth\n")
    if total_artists == 0:
        write("No artist data available.\n")
        return

    write("| Bucket | Artists | % of artists |")
    write("|--------|---------|--------------|")
    for bucket in _CATALOG_BUCKET_ORDER:
        count = depth_counter.get(bucket, 0)
        write(f"| {bucket} | {count} | {_pct(count, total_artists)} |")
    write("")


def _render_album_completeness(write: WriteLine, album_groups: list[AlbumGroup]) -> None:
    comparable_rows: list[dict[str, Any]] = []
    bucket_counter: Counter[str] = Counter()
    excluded_groups = 0
    conflicting_groups = 0
    overfilled_groups = 0

    for group in album_groups:
        disc_track_totals: dict[int, int] = {}
        has_track_total = False
        conflicting = False

        for snapshot in group.snapshots:
            parsed = _parsed(snapshot)
            disc_number = parsed.get("disc_number") or 1
            track_total = parsed.get("track_total")
            if isinstance(track_total, int) and track_total > 0:
                has_track_total = True
                existing_total = disc_track_totals.get(disc_number)
                if existing_total is not None and existing_total != track_total:
                    conflicting = True
                disc_track_totals[disc_number] = max(existing_total or 0, track_total)

        if not has_track_total:
            excluded_groups += 1
            continue

        expected_tracks = sum(disc_track_totals.values())
        if expected_tracks == 0:
            excluded_groups += 1
            continue

        present_tracks = len(group.snapshots)
        ratio = present_tracks / expected_tracks
        if conflicting:
            conflicting_groups += 1

        if ratio > 1:
            bucket = ">100%"
            overfilled_groups += 1
        elif ratio == 1:
            bucket = "100%"
        elif ratio >= 0.75:
            bucket = "75-99%"
        elif ratio >= 0.5:
            bucket = "50-74%"
        else:
            bucket = "<50%"

        bucket_counter[bucket] += 1
        comparable_rows.append({
            "album": group.album,
            "album_artist": _join_names(group.album_artists) or "—",
            "present": present_tracks,
            "expected": expected_tracks,
            "ratio": ratio,
        })

    write("### Album completeness (disc-aware)\n")
    write("Uses release/disc units because `track_total` is disc-scoped on multi-disc releases; completely missing discs cannot be inferred.\n")

    if not comparable_rows:
        write("No album groups have usable `track_total` tags.\n")
        return

    comparable_total = len(comparable_rows)
    write("| Bucket | Album groups | % of comparable groups |")
    write("|--------|--------------|------------------------|")
    for bucket in _COMPLETENESS_BUCKET_ORDER:
        count = bucket_counter.get(bucket, 0)
        if count == 0:
            continue
        write(f"| {bucket} | {count} | {_pct(count, comparable_total)} |")
    write("")

    notes = [f"{excluded_groups} excluded for missing `track_total`"]
    if conflicting_groups:
        notes.append(f"{conflicting_groups} with conflicting per-disc totals")
    if overfilled_groups:
        notes.append(f"{overfilled_groups} above 100%, likely duplicates or inconsistent tags")
    write(f"- {'; '.join(notes)}.")
    write("")

    write("#### Lowest completeness album groups\n")
    write("| Album | Album artist | Present | Expected | Completeness |")
    write("|-------|--------------|---------|----------|--------------|")
    for row in sorted(
        comparable_rows,
        key=lambda item: (item["ratio"], _sort_text(item["album"]), _sort_text(item["album_artist"])),
    )[:10]:
        write(
            f"| {_md(row['album'])} | {_md(row['album_artist'])} | {row['present']} | {row['expected']} | {row['ratio'] * 100:.1f}% |"
        )
    write("")


def _render_release_timeline(write: WriteLine, snapshots: list[Snapshot]) -> None:
    year_counter: Counter[int] = Counter()
    no_year = 0

    for snapshot in snapshots:
        release_date = _parsed(snapshot).get("release_date")
        year = release_date.get("year") if isinstance(release_date, dict) else None
        if year:
            year_counter[year] += 1
        else:
            no_year += 1

    write("### Release timeline\n")
    if not year_counter:
        write("No release year data available.\n")
        return

    decade_counter: Counter[str] = Counter()
    for year, count in year_counter.items():
        decade_counter[f"{(year // 10) * 10}s"] += count

    # Date precision
    prec: Counter[str] = Counter()
    for snapshot in snapshots:
        rd = _parsed(snapshot).get("release_date") or {}
        if rd.get("day"):
            prec["Full date"] += 1
        elif rd.get("month"):
            prec["Year+month"] += 1
        elif rd.get("year"):
            prec["Year only"] += 1

    write(f"- **Range**: {min(year_counter)} – {max(year_counter)}")
    write(f"- **Missing year**: {no_year} files\n")

    write("| Precision | Count | % |")
    write("|-----------|-------|---|")
    for label in ["Full date", "Year+month", "Year only"]:
        count = prec.get(label, 0)
        if count:
            write(f"| {label} | {count} | {_pct(count, len(snapshots))} |")
    write("")

    write("| Decade | Count | % of library |")
    write("|--------|-------|--------------|")
    for decade in sorted(decade_counter, key=lambda value: int(value[:-1])):
        count = decade_counter[decade]
        write(f"| {decade} | {count} | {_pct(count, len(snapshots))} |")
    write("")

    write("#### Top 10 years\n")
    write("| Year | Tracks |")
    write("|------|--------|")
    for year, count in sorted(year_counter.items(), key=lambda item: (-item[1], item[0]))[:10]:
        write(f"| {year} | {count} |")
    write("")


def _render_script_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    script_codes: Counter[str] = Counter()
    script_buckets: Counter[str] = Counter()

    for snapshot in snapshots:
        script_code = _first_raw_value(snapshot, "script")
        if script_code is None:
            script_buckets["Missing"] += 1
            continue
        script_codes[script_code] += 1
        script_buckets[_script_bucket(script_code)] += 1

    write("### Script profile\n")
    if not script_codes:
        write("No `script` tags were found in raw metadata.\n")
        return

    tagged_files = sum(script_codes.values())
    write(f"- **Coverage**: {tagged_files}/{len(snapshots)} files have a raw `script` tag.\n")
    write("| Bucket | Count | % of library |")
    write("|--------|-------|--------------|")
    for bucket in ["Japanese", "Latin", "Chinese", "Korean", "Other", "Missing"]:
        count = script_buckets.get(bucket, 0)
        if count == 0:
            continue
        write(f"| {bucket} | {count} | {_pct(count, len(snapshots))} |")
    write("")

    write("#### Raw script codes\n")
    write("| Code | Count | % of tagged files |")
    write("|------|-------|-------------------|")
    for code, count in sorted(script_codes.items(), key=lambda item: (-item[1], item[0])):
        write(f"| {code} | {count} | {_pct(count, tagged_files)} |")
    write("")


def _render_release_country_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    country_counter: Counter[str] = Counter()
    missing = 0

    for snapshot in snapshots:
        country_code = _first_raw_value(snapshot, "releasecountry")
        if country_code is None:
            missing += 1
            continue
        country_counter[country_code] += 1

    write("### Release country profile\n")
    if not country_counter:
        write("No `releasecountry` tags were found in raw metadata.\n")
        return

    tagged_files = sum(country_counter.values())
    write(f"- **Coverage**: {tagged_files}/{len(snapshots)} files have a raw `releasecountry` tag.")
    if missing:
        write(f"- **Missing releasecountry**: {missing} files\n")
    else:
        write("")
    write("| Country | Count | % of library |")
    write("|---------|-------|--------------|")
    for code, count in sorted(country_counter.items(), key=lambda item: (-item[1], item[0])):
        write(f"| {_md(_country_label(code))} | {count} | {_pct(count, len(snapshots))} |")
    write("")


def _render_duration_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    durations: list[tuple[float, Snapshot]] = []
    bucket_counter: Counter[str] = Counter()

    for snapshot in snapshots:
        duration = _audio(snapshot).get("duration_seconds")
        if duration is None:
            continue
        durations.append((duration, snapshot))
        bucket_counter[_duration_bucket(duration)] += 1

    write("### Duration profile\n")
    if not durations:
        write("No duration data available.\n")
        return

    durations.sort(key=lambda item: item[0])
    short_tracks = sum(1 for duration, _ in durations if duration < 30)
    long_tracks = sum(1 for duration, _ in durations if duration >= 900)

    write(f"- **Range**: {_format_duration(durations[0][0])} – {_format_duration(durations[-1][0])}")
    write(f"- **Under 30s**: {short_tracks}")
    write(f"- **15m+**: {long_tracks}\n")

    write("| Bucket | Tracks | % of library |")
    write("|--------|--------|--------------|")
    for bucket in _DURATION_BUCKET_ORDER:
        count = bucket_counter.get(bucket, 0)
        if count == 0:
            continue
        write(f"| {bucket} | {count} | {_pct(count, len(snapshots))} |")
    write("")

    write("#### Shortest tracks\n")
    write("| Path | Duration |")
    write("|------|----------|")
    for duration, snapshot in durations[:5]:
        write(f"| {_md(_relative_path(snapshot))} | {_format_duration(duration)} |")
    write("")

    write("#### Longest tracks\n")
    write("| Path | Duration |")
    write("|------|----------|")
    for duration, snapshot in reversed(durations[-5:]):
        write(f"| {_md(_relative_path(snapshot))} | {_format_duration(duration)} |")
    write("")


def _render_collaboration_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    collaboration_rows = []
    for snapshot in snapshots:
        artists = _parsed(snapshot).get("artists", [])
        if len(artists) > 1:
            collaboration_rows.append((_relative_path(snapshot), artists))

    write("### Collaboration tracks\n")
    write(f"- **Tracks with multiple parsed artists**: {len(collaboration_rows)} ({_pct(len(collaboration_rows), len(snapshots))})\n")

    if not collaboration_rows:
        return

    write("| Path | Parsed artists |")
    write("|------|----------------|")
    for path, artists in sorted(collaboration_rows, key=lambda item: (-len(item[1]), _sort_text(item[0])))[:10]:
        write(f"| {_md(path)} | {_md(', '.join(artists))} |")
    write("")


def _render_multi_disc(write: WriteLine, album_groups: list[AlbumGroup]) -> None:
    multi: list[tuple[str, str, int, int]] = []  # (album, artist, discs_present, disc_total)
    for group in album_groups:
        disc_numbers: set[int] = set()
        disc_total: int | None = None
        for snapshot in group.snapshots:
            p = _parsed(snapshot)
            dn = p.get("disc_number")
            dt = p.get("disc_total")
            if dn:
                disc_numbers.add(dn)
            if isinstance(dt, int) and dt > 1:
                disc_total = max(disc_total or 0, dt)
        if disc_total and disc_total > 1:
            multi.append((
                group.album,
                _join_names(group.album_artists) or "—",
                len(disc_numbers),
                disc_total,
            ))

    write("### Multi-disc releases\n")
    if not multi:
        write("No multi-disc releases found.\n")
        return

    write(f"**{len(multi)}** album groups span multiple discs.\n")
    write("| Album | Album artist | Discs present | Disc total |")
    write("|-------|--------------|---------------|------------|")
    for album, artist, present, total in sorted(multi, key=lambda r: (-r[3], _sort_text(r[0]))):
        write(f"| {_md(album)} | {_md(artist)} | {present} | {total} |")
    write("")


def _render_va_segment(write: WriteLine, snapshots: list[Snapshot], album_groups: list[AlbumGroup]) -> None:
    va_snapshots = [
        s for s in snapshots
        if "Various Artists" in (_parsed(s).get("album_artists") or [])
    ]
    if not va_snapshots:
        return

    va_album_counter: Counter[str] = Counter()
    for s in va_snapshots:
        va_album_counter[_parsed(s).get("album", "")] += 1

    # Decade distribution
    decade_counter: Counter[str] = Counter()
    for s in va_snapshots:
        rd = _parsed(s).get("release_date") or {}
        y = rd.get("year")
        if y:
            decade_counter[f"{(y // 10) * 10}s"] += 1

    write("### Various Artists segment\n")
    write(f"- **VA tracks**: {len(va_snapshots)}/{len(snapshots)} ({_pct(len(va_snapshots), len(snapshots))})")
    write(f"- **VA albums**: {len(va_album_counter)}\n")

    write("| Album | Tracks |")
    write("|-------|--------|")
    for album, count in sorted(va_album_counter.items(), key=lambda item: (-item[1], _sort_text(item[0])))[:10]:
        write(f"| {_md(album)} | {count} |")
    write("")

    if decade_counter:
        write("| Decade | VA tracks |")
        write("|--------|-----------|")
        for decade in sorted(decade_counter, key=lambda v: int(v[:-1])):
            write(f"| {decade} | {decade_counter[decade]} |")
        write("")


def _render_peak_years(write: WriteLine, snapshots: list[Snapshot]) -> None:
    year_counter: Counter[int] = Counter()
    year_snapshots: dict[int, list[Snapshot]] = defaultdict(list)
    for s in snapshots:
        rd = _parsed(s).get("release_date") or {}
        y = rd.get("year")
        if y:
            year_counter[y] += 1
            year_snapshots[y].append(s)

    if not year_counter:
        return

    # Pick top 2 years
    top_years = [y for y, _ in year_counter.most_common(2)]

    write("### Peak year breakdown\n")
    write(f"Expanding the top {len(top_years)} years by track count.\n")

    for year in sorted(top_years):
        snaps = year_snapshots[year]
        artist_counter: Counter[str] = Counter()
        album_counter: Counter[str] = Counter()
        for s in snaps:
            for a in _parsed(s).get("artists", []):
                artist_counter[a] += 1
            album_counter[_parsed(s).get("album", "")] += 1

        write(f"#### {year} ({len(snaps)} tracks)\n")
        write("| Artist | Tracks |")
        write("|--------|--------|")
        for artist, count in artist_counter.most_common(10):
            write(f"| {_md(artist)} | {count} |")
        write("")
        write("| Album | Tracks |")
        write("|-------|--------|")
        for album, count in album_counter.most_common(10):
            write(f"| {_md(album)} | {count} |")
        write("")


def _render_label_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    label_counter: Counter[str] = Counter()
    has_label = 0
    for s in snapshots:
        labels = _parsed(s).get("labels") or []
        if labels:
            has_label += 1
        for l in labels:
            label_counter[l] += 1

    write("### Label profile\n")
    if not label_counter:
        write("No label data available.\n")
        return

    write(f"- **Coverage**: {has_label}/{len(snapshots)} files have label tags")
    write(f"- **Unique labels**: {len(label_counter)}\n")

    write("| Label | Tracks | % of library |")
    write("|-------|--------|--------------|")
    for label, count in _top_counter_rows(label_counter, limit=10):
        write(f"| {_md(label)} | {count} | {_pct(count, len(snapshots))} |")
    write("")

    top5_sum = sum(c for _, c in label_counter.most_common(5))
    write(f"Top 5 labels account for {_pct(top5_sum, has_label)} of labeled files ({top5_sum}/{has_label}).\n")


def _render_encoding_profile(write: WriteLine, snapshots: list[Snapshot]) -> None:
    format_counter: Counter[str] = Counter()
    formats_by_class: dict[str, set[str]] = defaultdict(set)
    lossy_bitrates: list[float] = []
    lossless_sample_rates: Counter[str] = Counter()
    lossless_bit_depths: Counter[str] = Counter()

    lossless_count = 0
    lossy_count = 0
    unknown_count = 0

    for snapshot in snapshots:
        audio = _audio(snapshot)
        container = audio.get("container") or "unknown"
        codec = audio.get("codec") or "unknown"
        format_label = f"{container}/{codec}" if container != codec else codec
        format_counter[format_label] += 1

        lossless = audio.get("lossless")
        if lossless is True:
            lossless_count += 1
            formats_by_class["lossless"].add(format_label)
            sample_rate = audio.get("sample_rate_hz")
            bit_depth = audio.get("bit_depth")
            if sample_rate:
                lossless_sample_rates[f"{sample_rate} Hz"] += 1
            if bit_depth:
                lossless_bit_depths[f"{bit_depth}-bit"] += 1
        elif lossless is False:
            lossy_count += 1
            formats_by_class["lossy"].add(format_label)
            bitrate = audio.get("estimated_bitrate_kbps")
            if bitrate is not None:
                lossy_bitrates.append(bitrate)
        else:
            unknown_count += 1
            formats_by_class["unknown"].add(format_label)

    write("### Encoding profile\n")
    if lossless_count:
        write(f"- **Lossless**: {lossless_count} files ({', '.join(sorted(formats_by_class['lossless'], key=_sort_text))})")
    if lossy_count:
        write(f"- **Lossy**: {lossy_count} files ({', '.join(sorted(formats_by_class['lossy'], key=_sort_text))})")
    if unknown_count:
        write(f"- **Unknown**: {unknown_count} files ({', '.join(sorted(formats_by_class['unknown'], key=_sort_text))})")
    write("")

    nonzero_classes = sum(1 for count in (lossless_count, lossy_count, unknown_count) if count)
    show_codec_table = any(len(values) > 1 for values in formats_by_class.values()) or len(format_counter) > nonzero_classes
    if show_codec_table:
        write("| Format | Count | % of library |")
        write("|--------|-------|--------------|")
        for format_label, count in sorted(format_counter.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
            write(f"| {_md(format_label)} | {count} | {_pct(count, len(snapshots))} |")
        write("")

    if lossy_bitrates:
        rounded_bitrates: Counter[str] = Counter()
        for bitrate in lossy_bitrates:
            rounded_bitrates[_format_number(bitrate)] += 1

        if len(rounded_bitrates) == 1:
            bitrate_label = next(iter(rounded_bitrates))
            write(f"All lossy files are {bitrate_label} kbps.\n")
        else:
            write("#### Lossy bitrate distribution\n")
            write("| Bitrate | Files | % of lossy files |")
            write("|---------|-------|------------------|")
            for bitrate, count in sorted(rounded_bitrates.items(), key=lambda item: (float(item[0]), item[0])):
                write(f"| {bitrate} kbps | {count} | {_pct(count, len(lossy_bitrates))} |")
            write("")

    if lossless_sample_rates:
        write("#### Lossless quality\n")
        write("| Sample rate | Count |")
        write("|-------------|-------|")
        for sample_rate, count in sorted(lossless_sample_rates.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
            write(f"| {sample_rate} | {count} |")
        write("")

        if lossless_bit_depths:
            write("| Bit depth | Count |")
            write("|-----------|-------|")
            for bit_depth, count in sorted(lossless_bit_depths.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
                write(f"| {bit_depth} | {count} |")
            write("")


def _build_album_groups(snapshots: list[Snapshot]) -> tuple[list[AlbumGroup], dict[int, AlbumGroup]]:
    release_ids_by_fallback: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    fallback_keys: dict[int, tuple[str, tuple[str, ...]] | None] = {}

    for snapshot in snapshots:
        fallback_key = _album_fallback_key(snapshot)
        fallback_keys[id(snapshot)] = fallback_key
        release_id = _musicbrainz(snapshot).get("release_id")
        if fallback_key and release_id:
            release_ids_by_fallback[fallback_key].add(release_id)

    groups_by_key: dict[tuple[Any, ...], AlbumGroup] = {}
    group_lookup: dict[int, AlbumGroup] = {}

    for snapshot in snapshots:
        parsed = _parsed(snapshot)
        fallback_key = fallback_keys[id(snapshot)]
        release_id = _musicbrainz(snapshot).get("release_id")
        resolved_release_id = release_id

        if release_id:
            key = ("release", release_id)
        elif fallback_key and len(release_ids_by_fallback[fallback_key]) == 1:
            resolved_release_id = next(iter(release_ids_by_fallback[fallback_key]))
            key = ("release", resolved_release_id)
        elif fallback_key:
            key = ("album", fallback_key[0], fallback_key[1])
        else:
            key = ("file", _relative_path(snapshot))

        album = parsed.get("album") or (fallback_key[0] if fallback_key else _relative_path(snapshot))
        album_artists = fallback_key[1] if fallback_key else ()

        group = groups_by_key.get(key)
        if group is None:
            group = AlbumGroup(key=key, album=album, album_artists=album_artists, release_id=resolved_release_id)
            groups_by_key[key] = group
        else:
            if not group.album and album:
                group.album = album
            if not group.album_artists and album_artists:
                group.album_artists = album_artists
            if group.release_id is None and resolved_release_id is not None:
                group.release_id = resolved_release_id

        group.snapshots.append(snapshot)
        group_lookup[id(snapshot)] = group

    return list(groups_by_key.values()), group_lookup


def _album_fallback_key(snapshot: Snapshot) -> tuple[str, tuple[str, ...]] | None:
    parsed = _parsed(snapshot)
    album = parsed.get("album")
    if not album:
        return None
    return album, tuple(_coalesced_album_artists(parsed))


def _coalesced_album_artists(parsed: Snapshot) -> list[str]:
    album_artists = parsed.get("album_artists") or []
    if album_artists:
        return album_artists
    return parsed.get("artists") or []


def _parsed(snapshot: Snapshot) -> Snapshot:
    return _tags(snapshot).get("parsed", {})


def _tags(snapshot: Snapshot) -> Snapshot:
    return snapshot.get("tags", {})


def _audio(snapshot: Snapshot) -> Snapshot:
    return snapshot.get("audio", {})


def _file(snapshot: Snapshot) -> Snapshot:
    return snapshot.get("file", {})


def _musicbrainz(snapshot: Snapshot) -> Snapshot:
    return _tags(snapshot).get("external_ids", {}).get("musicbrainz", {})


def _relative_path(snapshot: Snapshot) -> str:
    return _file(snapshot).get("relative_path", "(unknown path)")


def _first_raw_value(snapshot: Snapshot, field_name: str) -> str | None:
    raw = _tags(snapshot).get("raw", {})
    aliases = _RAW_KEY_ALIASES.get(field_name, [field_name])
    for alias in aliases:
        for key, values in raw.items():
            if key.lower() != alias.lower():
                continue
            for value in values:
                if value:
                    return str(value)
    return None


_RAW_KEY_ALIASES: dict[str, list[str]] = {
    "script": ["script", "TXXX:SCRIPT", "----:com.apple.iTunes:SCRIPT"],
    "releasecountry": [
        "releasecountry",
        "TXXX:MusicBrainz Album Release Country",
        "----:com.apple.iTunes:MusicBrainz Album Release Country",
    ],
}


def _catalog_bucket(track_count: int) -> str:
    if track_count == 1:
        return "1 track"
    if track_count <= 3:
        return "2-3 tracks"
    if track_count <= 9:
        return "4-9 tracks"
    return "10+ tracks"


def _duration_bucket(duration_seconds: float) -> str:
    if duration_seconds < 30:
        return "<30s"
    if duration_seconds < 60:
        return "30-59s"
    if duration_seconds < 120:
        return "1-1:59"
    if duration_seconds < 180:
        return "2-2:59"
    if duration_seconds < 300:
        return "3-4:59"
    if duration_seconds < 600:
        return "5-9:59"
    if duration_seconds < 900:
        return "10-14:59"
    return "15m+"


def _script_bucket(script_code: str) -> str:
    for bucket, codes in _SCRIPT_BUCKETS:
        if script_code in codes:
            return bucket
    return "Other"


def _country_label(country_code: str) -> str:
    country_name = _COUNTRY_NAMES.get(country_code)
    if country_name is None:
        return country_code
    return f"{country_code} ({country_name})"


def _top_counter_rows(counter: Counter[str], limit: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda item: (-item[1], _sort_text(item[0])))[:limit]


def _join_names(names: tuple[str, ...] | list[str]) -> str:
    return ", ".join(names)


def _format_duration(seconds: float) -> str:
    total_seconds = int(round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _sort_text(value: Any) -> str:
    return str(value).casefold()


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _pct(count: int, total: int) -> str:
    if total == 0:
        return "–"
    return f"{count / total * 100:.1f}%"
