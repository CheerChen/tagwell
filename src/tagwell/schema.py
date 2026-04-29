"""Data structures and serialization helpers for tagwell JSONL output."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1
SCANNER_NAME = "tagwell"
SCANNER_VERSION = "0.1.0"

SUPPORTED_EXTENSIONS: set[str] = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif",
}


def scanner_block(*, reader: dict[str, str] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"name": SCANNER_NAME, "version": SCANNER_VERSION}
    if reader:
        block["reader"] = reader
    return block


def make_scan_id() -> tuple[str, str]:
    """Return (scan_id_uuid4, started_at_iso) based on current local time."""
    scan_id = str(uuid.uuid4())
    started_at = datetime.now().astimezone().isoformat()
    return scan_id, started_at


def scan_header_record(
    *,
    scan_id: str,
    started_at: str,
    root: str,
    root_id: str = "main",
    reader: dict[str, str] | None = None,
) -> dict[str, Any]:
    """First JSONL record — declares scan context once."""
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "scan_header",
        "scanner": scanner_block(reader=reader),
        "scan": {
            "scan_id": scan_id,
            "started_at": started_at,
            "root": root,
            "root_id": root_id,
        },
    }


def audio_file_record(
    *,
    file_info: dict,
    audio_info: dict,
    tags_info: dict,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "audio_file_snapshot",
        "file": file_info,
        "audio": audio_info,
        "tags": tags_info,
        "warnings": warnings or [],
    }


def error_record(
    *,
    relative_path: str,
    stage: str,
    kind: str,
    exception_type: str,
    message: str,
    recoverable: bool = True,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "file_error",
        "file": {
            "relative_path": relative_path,
        },
        "error": {
            "stage": stage,
            "kind": kind,
            "exception_type": exception_type,
            "message": message,
            "recoverable": recoverable,
        },
    }


def to_jsonl_line(record: dict[str, Any], pretty: bool = False) -> str:
    if pretty:
        return json.dumps(record, ensure_ascii=False, indent=2)
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))
