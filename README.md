# tagwell

Read-only local music metadata scanner. Recursively scans a music library and exports one JSONL record per audio file, preserving raw metadata and basic file/audio/container facts.

## What tagwell does

- Recursively walks a directory of audio files
- Reads metadata tags via [mutagen](https://mutagen.readthedocs.io/) — never writes back
- Exports one JSON object per audio file (JSONL format)
- Preserves raw tag keys and values faithfully
- Extracts parsed convenience fields (title, artists, album, dates, track numbers, etc.)
- Records embedded picture metadata (type, mime, size, dimensions) without embedding image bytes
- Extracts MusicBrainz IDs when present
- Reports file properties: size, mtime, container, codec, duration, sample rate, channels

## What tagwell intentionally does NOT do (yet)

- Write or modify audio file tags
- Call external APIs (MusicBrainz, Discogs, etc.)
- Infer canonical artist/release/recording identity
- Deduplicate or match across releases
- Generate a wiki, database, or UI
- Recommend music or analyze audio content
- Correct or normalize tags

## Safety

**tagwell is strictly read-only.** It never writes to source music files. It only reads metadata and writes output to the specified JSONL file.

## Supported formats

`.mp3` `.flac` `.m4a` `.aac` `.ogg` `.opus` `.wav` `.aiff` `.aif`

## Installation

```bash
# From the repo root
uv sync
```

## Usage

```bash
# Basic scan
tagwell scan ./my-music --out ./output/library.jsonl

# Or via python -m
python -m tagwell scan ./my-music --out ./output/library.jsonl

# With options
tagwell scan ./my-music --out library.jsonl --pretty --follow-symlinks
```

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--out` / `-o` | (required) | Output JSONL file path |
| `--pretty` | off | Pretty-print JSON (multi-line) |
| `--follow-symlinks` | off | Follow symbolic links |
| `--on-error` | `continue` | `continue` or `fail` |

## JSONL output structure

The output file contains:

1. **Line 1: `scan_header`** — declares scanner info and scan context once
2. **Lines 2+: `audio_file_snapshot`** — one per audio file (or `file_error` on failure)

### scan_header

```json
{
  "schema_version": 1,
  "record_type": "scan_header",
  "scanner": { "name": "tagwell", "version": "0.1.0" },
  "scan": {
    "scan_id": "a1b2c3d4-...",
    "started_at": "2026-04-29T14:23:11+09:00",
    "root": "/abs/path/to/music",
    "root_id": "main"
  }
}
```

### audio_file_snapshot

```json
{
  "schema_version": 1,
  "record_type": "audio_file_snapshot",
  "file": {
    "relative_path": "Artist/Album/01 - Track.flac",
    "name": "01 - Track.flac",
    "ext": ".flac",
    "size_bytes": 41293822
  },
  "audio": {
    "container": "flac",
    "codec": "flac",
    "duration_seconds": 245.32,
    "sample_rate_hz": 44100,
    "channels": 2,
    "bit_depth": 16,
    "lossless": true
  },
  "tags": {
    "format": "vorbis_comment",
    "raw": { "title": ["Track Name"], "artist": ["Artist Name"] },
    "parsed": { "title": "Track Name", "artists": ["Artist Name"] }
  }
}
```

## Raw vs Parsed tags

The `tags` block contains two complementary views of the same tag data:

### `raw` — verbatim tag serialization

Raw tag keys preserve the exact casing from mutagen:

- **Vorbis Comment** (FLAC, Ogg): mutagen normalises keys to **lowercase** (e.g. `title`, `artist`, `musicbrainz_trackid`). This is per Vorbis spec (keys are case-insensitive).
- **ID3** (MP3, AIFF): frame IDs are always uppercase (`TIT2`, `TPE1`). TXXX user-defined frames use the format `TXXX:<description>` where the description preserves original casing.
- **MP4** (M4A/AAC): atom keys are kept verbatim from mutagen (e.g. `©nam`, `©ART`).

All raw values are arrays of strings (`list[str]`), even for single-value fields.

### `parsed` — normalised convenience view

Parsed fields use consistent key names across all formats:

- Single-value fields are `string | null` (e.g. `title`, `album`)
- Multi-value fields are `list[str]` (e.g. `artists`, `genres`), empty `[]` when absent
- Nested objects like `release_date` are `null` when absent; internal fields may be partially filled (e.g. year-only → `month: null`)

**Recommendation:** use `parsed` for downstream analysis; use `raw` for provenance auditing and tag-cleaning workflows.

## Running tests

```bash
uv run pytest
```

## License

MIT