"""Core scanning logic — walk directory, read each audio file, produce records."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import mutagen

from tagwell.schema import (
    SUPPORTED_EXTENSIONS,
    audio_file_record,
    error_record,
    make_scan_id,
    scan_header_record,
    to_jsonl_line,
)
from tagwell.audio import extract_audio_info
from tagwell.tags import (
    extract_external_ids,
    extract_pictures,
    extract_raw_tags,
    parse_tags,
)


def _ns_to_iso(ns: int | None) -> str | None:
    """Convert nanosecond timestamp to ISO 8601 string (second precision)."""
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def scan_library(
    root: Path,
    out_file: TextIO,
    *,
    pretty: bool = False,
    follow_symlinks: bool = False,
    on_error: str = "continue",
) -> dict[str, Any]:
    """Scan `root` recursively and write JSONL to `out_file`.

    Returns a summary dict with counts and timing.
    """
    root = root.resolve()
    scan_id, started_at = make_scan_id()

    # Write scan header as the first JSONL line
    header = scan_header_record(
        scan_id=scan_id,
        started_at=started_at,
        root=str(root),
        reader={"name": "mutagen", "version": mutagen.version_string},
    )
    out_file.write(to_jsonl_line(header, pretty=pretty) + "\n")

    count_audio = 0
    count_skipped = 0
    count_errors = 0
    t0 = time.monotonic()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        # Sort for deterministic output order
        dirnames.sort()
        filenames.sort()
        for fname in filenames:
            full_path = Path(dirpath) / fname
            ext = full_path.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                count_skipped += 1
                continue

            try:
                record = _process_file(
                    full_path,
                    root=root,
                )
                line = to_jsonl_line(record, pretty=pretty)
                out_file.write(line + "\n")
                count_audio += 1
            except Exception as exc:
                count_errors += 1
                rel = str(full_path.relative_to(root))
                err = error_record(
                    relative_path=rel,
                    stage="read_tags",
                    kind=type(exc).__name__,
                    exception_type=type(exc).__name__,
                    message=str(exc),
                    recoverable=True,
                )
                line = to_jsonl_line(err, pretty=pretty)
                out_file.write(line + "\n")
                print(f"[warn] {rel}: {exc}", file=sys.stderr)
                if on_error == "fail":
                    raise

    elapsed = time.monotonic() - t0

    return {
        "root": str(root),
        "scan_id": scan_id,
        "audio_files": count_audio,
        "skipped_files": count_skipped,
        "errors": count_errors,
        "elapsed_seconds": round(elapsed, 2),
    }


def _process_file(
    path: Path,
    *,
    root: Path,
) -> dict[str, Any]:
    """Read one audio file and return a complete record dict."""
    stat = path.stat()
    rel = str(path.relative_to(root))

    file_info: dict[str, Any] = {
        "relative_path": rel,
        "name": path.name,
        "stem": path.stem,
        "ext": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "mtime": _ns_to_iso(stat.st_mtime_ns),
        "ctime": _ns_to_iso(getattr(stat, "st_ctime_ns", None)),
    }

    # Open with mutagen
    mf = mutagen.File(str(path))

    # Audio properties
    audio_info = extract_audio_info(mf)

    # Tags
    tag_format, raw = extract_raw_tags(mf)
    parsed = parse_tags(tag_format, raw, mf)
    external_ids = extract_external_ids(tag_format, raw)
    pictures = extract_pictures(mf)

    tags_info: dict[str, Any] = {
        "format": tag_format,
        "raw": raw,
        "parsed": parsed,
        "external_ids": external_ids,
        "pictures": pictures,
    }

    warnings: list[str] = []
    if mf is None:
        warnings.append("mutagen could not identify file type")

    return audio_file_record(
        file_info=file_info,
        audio_info=audio_info,
        tags_info=tags_info,
        warnings=warnings,
    )
