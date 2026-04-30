"""Shared helpers for tagwell Markdown report generators."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

Snapshot = dict[str, Any]
WriteLine = Callable[[str], None]


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


def _tags(snapshot: Snapshot) -> Snapshot:
    return snapshot.get("tags", {})


def _audio(snapshot: Snapshot) -> Snapshot:
    return snapshot.get("audio", {})


def _file(snapshot: Snapshot) -> Snapshot:
    return snapshot.get("file", {})


def _relative_path(snapshot: Snapshot) -> str:
    return _file(snapshot).get("relative_path", "(unknown path)")


def _sort_text(value: Any) -> str:
    return str(value).casefold()


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _pct(count: int, total: int) -> str:
    if total == 0:
        return "–"
    return f"{count / total * 100:.1f}%"


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"
