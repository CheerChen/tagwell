"""Audio source quality report generator for tagwell JSONL output."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from tagwell._report_helpers import (
    Snapshot,
    WriteLine,
    _audio,
    _format_number,
    _md,
    _pct,
    _relative_path,
    _render_source_info,
    _sort_text,
    load_snapshots,
)

_LIBFLAC_PREFIX = "reference libFLAC"
_LAVF_PREFIX = "Lavf"

# 16-bit stereo at ≤48 kHz from a real lossless source rarely compresses below
# this threshold; lower bitrates suggest the input was already lossy.
_SUSPECT_LOSSLESS_BITRATE_KBPS = 700


def generate_quality_report(jsonl_path: Path) -> str:
    header, snapshots = load_snapshots(jsonl_path)
    if not snapshots:
        return "# Tagwell Audio Quality Report\n\nNo audio files found in JSONL.\n"

    lines: list[str] = []
    write = lines.append

    write("# Tagwell Audio Quality Report\n")
    _render_source_info(write, jsonl_path, header)

    _render_format_overview(write, snapshots)
    _render_codec_breakdown(write, snapshots)
    _render_lossless_integrity(write, snapshots)
    _render_mp3_encoder_breakdown(write, snapshots)
    _render_lossy_bitrate_distribution(write, snapshots)
    _render_lossless_quality(write, snapshots)
    _render_red_flags(write, snapshots)

    return "\n".join(lines) + "\n"


def _render_format_overview(write: WriteLine, snapshots: list[Snapshot]) -> None:
    formats_by_class: dict[str, set[str]] = defaultdict(set)
    lossless_count = lossy_count = unknown_count = 0

    for snapshot in snapshots:
        audio = _audio(snapshot)
        label = _format_label(audio)
        lossless = audio.get("lossless")
        if lossless is True:
            lossless_count += 1
            formats_by_class["lossless"].add(label)
        elif lossless is False:
            lossy_count += 1
            formats_by_class["lossy"].add(label)
        else:
            unknown_count += 1
            formats_by_class["unknown"].add(label)

    total = len(snapshots)
    write("## Format overview\n")
    write(f"- **Total**: {total} audio files")
    if lossless_count:
        formats = ", ".join(sorted(formats_by_class["lossless"], key=_sort_text))
        write(f"- **Lossless**: {lossless_count} ({_pct(lossless_count, total)}) — {formats}")
    if lossy_count:
        formats = ", ".join(sorted(formats_by_class["lossy"], key=_sort_text))
        write(f"- **Lossy**: {lossy_count} ({_pct(lossy_count, total)}) — {formats}")
    if unknown_count:
        formats = ", ".join(sorted(formats_by_class["unknown"], key=_sort_text))
        write(f"- **Unknown**: {unknown_count} ({_pct(unknown_count, total)}) — {formats}")
    write("")


def _render_codec_breakdown(write: WriteLine, snapshots: list[Snapshot]) -> None:
    counter: Counter[str] = Counter()
    for snapshot in snapshots:
        counter[_format_label(_audio(snapshot))] += 1

    if len(counter) <= 1:
        return

    write("## Codec breakdown\n")
    write("| Format | Files | % of library |")
    write("|--------|-------|--------------|")
    for label, count in sorted(counter.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
        write(f"| {_md(label)} | {count} | {_pct(count, len(snapshots))} |")
    write("")


def _render_lossless_integrity(write: WriteLine, snapshots: list[Snapshot]) -> None:
    flacs = [s for s in snapshots if _audio(s).get("container") == "flac"]
    if not flacs:
        return

    bucket_counter: Counter[str] = Counter()
    distinct: Counter[str] = Counter()
    for snapshot in flacs:
        encoder = _audio(snapshot).get("encoder_info") or ""
        bucket_counter[_flac_encoder_bucket(encoder)] += 1
        distinct[encoder or "(missing)"] += 1

    write("## Lossless integrity (FLAC)\n")
    write(
        "Bucketed by `encoder_info` (the Vorbis comment vendor string). "
        "`Lavf*` indicates FFmpeg/libavformat muxing — commonly produced when "
        "remuxing or transcoding from another source, including lossy ones.\n"
    )
    write("| Encoder | Files | % of FLAC |")
    write("|---------|-------|-----------|")
    for bucket in ["reference libFLAC", "Lavf (FFmpeg)", "Other", "Missing"]:
        count = bucket_counter.get(bucket, 0)
        if count == 0:
            continue
        write(f"| {bucket} | {count} | {_pct(count, len(flacs))} |")
    write("")

    if len(distinct) > 1:
        write("### FLAC vendor strings observed\n")
        write("| Vendor | Files |")
        write("|--------|-------|")
        for vendor, count in sorted(distinct.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
            write(f"| {_md(vendor)} | {count} |")
        write("")


def _render_mp3_encoder_breakdown(write: WriteLine, snapshots: list[Snapshot]) -> None:
    mp3s = [s for s in snapshots if _audio(s).get("container") == "mp3"]
    if not mp3s:
        return

    encoder_counter: Counter[str] = Counter()
    mode_counter: Counter[str] = Counter()
    for snapshot in mp3s:
        audio = _audio(snapshot)
        encoder_counter[audio.get("encoder_info") or "(missing — no LAME/Xing header)"] += 1
        mode_counter[audio.get("bitrate_mode") or "unknown"] += 1

    write("## MP3 encoder breakdown\n")
    write(f"- **MP3 files**: {len(mp3s)}\n")

    write("### Bitrate mode\n")
    write("| Mode | Files | % of MP3 |")
    write("|------|-------|----------|")
    for mode in ["cbr", "vbr", "abr", "unknown"]:
        count = mode_counter.get(mode, 0)
        if count == 0:
            continue
        write(f"| {mode.upper()} | {count} | {_pct(count, len(mp3s))} |")
    write("")

    write("### Encoder identity\n")
    write("| Encoder | Files | % of MP3 |")
    write("|---------|-------|----------|")
    for encoder, count in sorted(encoder_counter.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
        write(f"| {_md(encoder)} | {count} | {_pct(count, len(mp3s))} |")
    write("")


def _render_lossy_bitrate_distribution(write: WriteLine, snapshots: list[Snapshot]) -> None:
    by_bitrate: dict[str, Counter[str]] = defaultdict(Counter)
    total_lossy = 0

    for snapshot in snapshots:
        audio = _audio(snapshot)
        if audio.get("lossless") is not False:
            continue
        bitrate = audio.get("estimated_bitrate_kbps")
        if bitrate is None:
            continue
        mode = audio.get("bitrate_mode") or "unknown"
        by_bitrate[_format_number(bitrate)][mode] += 1
        total_lossy += 1

    if not by_bitrate:
        return

    write("## Lossy bitrate distribution\n")

    if len(by_bitrate) == 1:
        bitrate_str, mode_counter = next(iter(by_bitrate.items()))
        write(f"All {total_lossy} lossy files are {bitrate_str} kbps ({_mode_summary(mode_counter)}).\n")
        return

    write("| Bitrate | Files | % of lossy | Mode |")
    write("|---------|-------|------------|------|")
    for bitrate_str, mode_counter in sorted(by_bitrate.items(), key=lambda item: float(item[0])):
        files = sum(mode_counter.values())
        write(
            f"| {bitrate_str} kbps | {files} | {_pct(files, total_lossy)} | "
            f"{_mode_summary(mode_counter)} |"
        )
    write("")


def _render_lossless_quality(write: WriteLine, snapshots: list[Snapshot]) -> None:
    sample_rates: Counter[str] = Counter()
    bit_depths: Counter[str] = Counter()
    lossless_count = 0

    for snapshot in snapshots:
        audio = _audio(snapshot)
        if audio.get("lossless") is not True:
            continue
        lossless_count += 1
        sample_rate = audio.get("sample_rate_hz")
        bit_depth = audio.get("bit_depth")
        if sample_rate:
            sample_rates[f"{sample_rate} Hz"] += 1
        if bit_depth:
            bit_depths[f"{bit_depth}-bit"] += 1

    if not lossless_count:
        return

    write("## Lossless quality\n")
    if sample_rates:
        write("### Sample rate\n")
        write("| Sample rate | Files |")
        write("|-------------|-------|")
        for sample_rate, count in sorted(sample_rates.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
            write(f"| {sample_rate} | {count} |")
        write("")
    if bit_depths:
        write("### Bit depth\n")
        write("| Bit depth | Files |")
        write("|-----------|-------|")
        for bit_depth, count in sorted(bit_depths.items(), key=lambda item: (-item[1], _sort_text(item[0]))):
            write(f"| {bit_depth} | {count} |")
        write("")


def _render_red_flags(write: WriteLine, snapshots: list[Snapshot]) -> None:
    sketchy_mp3s: list[str] = []
    lavf_flacs: list[tuple[str, str]] = []
    suspect_lossless: list[tuple[float, str]] = []

    for snapshot in snapshots:
        audio = _audio(snapshot)
        container = audio.get("container")
        if container == "mp3" and audio.get("sketchy") is True:
            sketchy_mp3s.append(_relative_path(snapshot))
        if container == "flac":
            encoder = audio.get("encoder_info") or ""
            if encoder.startswith(_LAVF_PREFIX):
                lavf_flacs.append((encoder, _relative_path(snapshot)))
        if _is_suspect_lossless(audio):
            suspect_lossless.append((audio["estimated_bitrate_kbps"], _relative_path(snapshot)))

    write("## Red flags\n")

    if not (sketchy_mp3s or lavf_flacs or suspect_lossless):
        write("No quality red flags detected.\n")
        return

    if lavf_flacs:
        write(f"### FLAC files muxed by FFmpeg ({len(lavf_flacs)})\n")
        write(
            "These FLAC files have a `Lavf*` vendor string instead of `reference libFLAC*`. "
            "They were produced by FFmpeg/libavformat — commonly used to remux or transcode, "
            "including from lossy sources.\n"
        )
        write("| Path | Vendor |")
        write("|------|--------|")
        for vendor, path in sorted(lavf_flacs, key=lambda item: _sort_text(item[1])):
            write(f"| {_md(path)} | {_md(vendor)} |")
        write("")

    if sketchy_mp3s:
        write(f"### MP3 files with sketchy frame parsing ({len(sketchy_mp3s)})\n")
        write(
            "Mutagen flagged these MP3 files as borderline — frame headers required lenient "
            "parsing. May indicate truncation, corruption, or unusual encoders.\n"
        )
        for path in sorted(sketchy_mp3s):
            write(f"- `{path}`")
        write("")

    if suspect_lossless:
        write(f"### Lossless files with suspiciously low bitrate ({len(suspect_lossless)})\n")
        write(
            f"These files declare lossless but compress below "
            f"{_SUSPECT_LOSSLESS_BITRATE_KBPS} kbps at 16-bit / ≤48 kHz / mono-or-stereo — "
            "a configuration where genuine lossless compression rarely drops that low. "
            "Possible lossy origin.\n"
        )
        write("| Path | Bitrate (kbps) |")
        write("|------|----------------|")
        for bitrate, path in sorted(suspect_lossless, key=lambda item: item[0]):
            write(f"| {_md(path)} | {_format_number(bitrate)} |")
        write("")


def _format_label(audio: Snapshot) -> str:
    container = audio.get("container") or "unknown"
    codec = audio.get("codec") or "unknown"
    return f"{container}/{codec}" if container != codec else codec


def _flac_encoder_bucket(encoder: str) -> str:
    if not encoder:
        return "Missing"
    if encoder.startswith(_LIBFLAC_PREFIX):
        return "reference libFLAC"
    if encoder.startswith(_LAVF_PREFIX):
        return "Lavf (FFmpeg)"
    return "Other"


def _mode_summary(mode_counter: Counter[str]) -> str:
    if not mode_counter:
        return "—"
    items = mode_counter.most_common()
    top_mode, top_count = items[0]
    total = sum(count for _, count in items)
    if top_count == total:
        return top_mode.upper()
    return " / ".join(f"{mode.upper()} {count}" for mode, count in items)


def _is_suspect_lossless(audio: Snapshot) -> bool:
    if audio.get("lossless") is not True:
        return False
    bit_depth = audio.get("bit_depth")
    sample_rate = audio.get("sample_rate_hz")
    bitrate = audio.get("estimated_bitrate_kbps")
    channels = audio.get("channels")
    if not (bit_depth and sample_rate and bitrate and channels):
        return False
    if bit_depth > 16 or sample_rate > 48000 or channels not in (1, 2):
        return False
    return bitrate < _SUSPECT_LOSSLESS_BITRATE_KBPS
