"""Tag reading, raw preservation, and convenience parsing for tagwell.

Design notes on raw tag key casing:
- Vorbis comments (FLAC, Ogg): keys are case-insensitive per spec.
  Mutagen normalises them to lowercase. We preserve that verbatim.
- ID3 (MP3, AIFF): frame IDs like "TIT2" are always uppercase.
  TXXX descriptions preserve original casing (e.g. "TXXX:originalyear").
  We store them as-is.
- MP4 (M4A/AAC): atom keys like "©nam" are kept verbatim from mutagen.
- WAV with ID3: treated the same as ID3.
Raw tag values are always coerced to list[str] for uniformity.

Downstream consumers should use ``parsed`` for normalised access and ``raw``
only for provenance / tag-cleaning workflows.
"""

from __future__ import annotations

import re
from typing import Any

import mutagen
from mutagen.flac import FLAC, Picture as FLACPicture
from mutagen.id3 import ID3, APIC, PictureType
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.aiff import AIFF
from mutagen.wave import WAVE

from tagwell.imaging import image_dimensions


# ---------- raw tag extraction ----------

def extract_raw_tags(mf: mutagen.FileType) -> tuple[str | None, dict[str, list[str]]]:
    """Return (tag_format_name, raw_tags_dict).

    All values are coerced to list[str].
    """
    if mf is None or mf.tags is None:
        return None, {}

    if isinstance(mf, (FLAC, OggVorbis, OggOpus)):
        return "vorbis_comment", _raw_vorbis(mf.tags)
    if isinstance(mf, (MP3, AIFF, WAVE)):
        return "id3", _raw_id3(mf.tags)
    if isinstance(mf, MP4):
        return "mp4", _raw_mp4(mf.tags)
    # generic fallback
    return "unknown", _raw_generic(mf.tags)


def _raw_vorbis(tags: Any) -> dict[str, list[str]]:
    # VorbisComment inherits from list, so `for x in tags` yields (key, value)
    # tuples. Use .keys() to get string keys, then index to get value lists.
    out: dict[str, list[str]] = {}
    for key in tags.keys():
        vals = tags[key]
        out[key] = _coerce_list(vals)
    return out


def _raw_id3(tags: ID3) -> dict[str, list[str]]:
    """Extract ID3 frames into a string-keyed dict.

    Binary frames (APIC, etc.) are skipped; pictures are handled separately.
    Uses id(frame) to deduplicate frames exposed under multiple keys
    (e.g. COMM and COMM:XXX pointing to the same frame object).
    """
    out: dict[str, list[str]] = {}
    seen_ids: set[int] = set()
    for key, frame in tags.items():
        # Deduplicate: skip if we already serialised this exact frame object
        fid = id(frame)
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        # Skip picture frames — handled in extract_pictures
        if key.startswith("APIC"):
            continue
        # TXXX user-defined text frames: use description as sub-key
        if hasattr(frame, "desc") and hasattr(frame, "text"):
            sub_key = f"{frame.FrameID}:{frame.desc}" if frame.desc else frame.FrameID
            out[sub_key] = _coerce_list(frame.text)
        elif hasattr(frame, "text"):
            out[key] = _coerce_list(frame.text)
        elif hasattr(frame, "url"):
            out[key] = [frame.url]
        # Skip binary-only frames we don't understand
    return out


_MP4_FREEFORM_PREFIX = "----:"

# Map common MP4 atom short-keys to human-readable names for raw output.
# We keep the original atom key as the dict key, verbatim.

def _raw_mp4(tags: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, val in tags.items():
        # Skip cover art atom
        if key == "covr":
            continue
        out[key] = _coerce_list(val)
    return out


def _raw_generic(tags: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if hasattr(tags, "items"):
        for k, v in tags.items():
            out[str(k)] = _coerce_list(v)
    elif hasattr(tags, "keys"):
        for k in tags.keys():
            out[str(k)] = _coerce_list(tags[k])
    return out


def _coerce_list(val: Any) -> list[str]:
    """Coerce a tag value (single or multiple) to list[str]."""
    if isinstance(val, list):
        return [_coerce_str(v) for v in val]
    if isinstance(val, tuple):
        return [_coerce_str(v) for v in val]
    return [_coerce_str(val)]


def _coerce_str(val: Any) -> str:
    """Convert a single tag value to str, decoding bytes as UTF-8."""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


# ---------- parsed convenience fields ----------

def parse_tags(tag_format: str | None, raw: dict[str, list[str]], mf: mutagen.FileType) -> dict[str, Any]:
    """Build the `parsed` convenience dict from raw tags."""
    if tag_format == "vorbis_comment":
        return _parse_vorbis(raw)
    if tag_format == "id3":
        return _parse_id3(raw, mf)
    if tag_format == "mp4":
        return _parse_mp4(raw, mf)
    return _empty_parsed()


def _parse_vorbis(raw: dict[str, list[str]]) -> dict[str, Any]:
    def _first(key: str) -> str | None:
        vals = raw.get(key) or raw.get(key.upper()) or raw.get(key.lower())
        return vals[0] if vals else None

    def _all(key: str) -> list[str]:
        vals = raw.get(key) or raw.get(key.upper()) or raw.get(key.lower())
        return vals if vals else []

    track_num, track_total = _parse_track_field(_first("TRACKNUMBER"), _first("TRACKTOTAL") or _first("TOTALTRACKS"))
    disc_num, disc_total = _parse_track_field(_first("DISCNUMBER"), _first("DISCTOTAL") or _first("TOTALDISCS"))

    return {
        "title": _first("TITLE"),
        "artists": _all("ARTISTS") or _all("ARTIST"),
        "album": _first("ALBUM"),
        "album_artists": _all("ALBUMARTISTS") or _all("ALBUMARTIST"),
        "release_date": parse_date_tag(_first("DATE")),
        "track_number": track_num,
        "track_total": track_total,
        "disc_number": disc_num,
        "disc_total": disc_total,
        "genres": _all("GENRE"),
        "labels": _all("LABEL") or _all("ORGANIZATION"),
    }


def _parse_id3(raw: dict[str, list[str]], mf: mutagen.FileType) -> dict[str, Any]:
    def _first(key: str) -> str | None:
        vals = raw.get(key)
        return vals[0] if vals else None

    def _all(key: str) -> list[str]:
        return raw.get(key, [])

    track_raw = _first("TRCK")
    disc_raw = _first("TPOS")
    track_num, track_total = _parse_track_field(track_raw, None)
    disc_num, disc_total = _parse_track_field(disc_raw, None)

    return {
        "title": _first("TIT2"),
        "artists": _all("TXXX:Artists") or _all("TPE1"),
        "album": _first("TALB"),
        "album_artists": _all("TXXX:ALBUMARTISTS") or _all("TPE2"),
        "release_date": parse_date_tag(_first("TDRC") or _first("TYER")),
        "track_number": track_num,
        "track_total": track_total,
        "disc_number": disc_num,
        "disc_total": disc_total,
        "genres": _all("TCON"),
        "labels": _all("TPUB"),
    }


def _parse_mp4(raw: dict[str, list[str]], mf: mutagen.FileType) -> dict[str, Any]:
    def _first(key: str) -> str | None:
        vals = raw.get(key)
        return vals[0] if vals else None

    def _all(key: str) -> list[str]:
        return raw.get(key, [])

    # MP4 trkn / disk are stored by mutagen as list of (num, total) tuples,
    # but we already coerced to str. Parse from the original tags.
    track_num, track_total = None, None
    disc_num, disc_total = None, None
    if mf.tags and "trkn" in mf.tags:
        pairs = mf.tags["trkn"]
        if pairs and isinstance(pairs[0], tuple):
            track_num = pairs[0][0] or None
            track_total = pairs[0][1] or None
    if mf.tags and "disk" in mf.tags:
        pairs = mf.tags["disk"]
        if pairs and isinstance(pairs[0], tuple):
            disc_num = pairs[0][0] or None
            disc_total = pairs[0][1] or None

    return {
        "title": _first("\xa9nam"),
        "artists": _all("\xa9ART"),
        "album": _first("\xa9alb"),
        "album_artists": _all("aART"),
        "release_date": parse_date_tag(_first("\xa9day")),
        "track_number": track_num,
        "track_total": track_total,
        "disc_number": disc_num,
        "disc_total": disc_total,
        "genres": _all("\xa9gen"),
        "labels": [],
    }


def _empty_parsed() -> dict[str, Any]:
    return {
        "title": None,
        "artists": [],
        "album": None,
        "album_artists": [],
        "release_date": None,
        "track_number": None,
        "track_total": None,
        "disc_number": None,
        "disc_total": None,
        "genres": [],
        "labels": [],
    }


# ---------- date parsing ----------

_DATE_FULL = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DATE_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_DATE_YEAR = re.compile(r"^(\d{4})$")


def parse_date_tag(raw: str | None) -> dict[str, Any] | None:
    """Parse a date string into {raw, year, month, day, precision}."""
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None

    m = _DATE_FULL.match(raw)
    if m:
        return {
            "raw": raw,
            "year": int(m.group(1)),
            "month": int(m.group(2)),
            "day": int(m.group(3)),
            "precision": "day",
        }
    m = _DATE_YEAR_MONTH.match(raw)
    if m:
        return {
            "raw": raw,
            "year": int(m.group(1)),
            "month": int(m.group(2)),
            "day": None,
            "precision": "month",
        }
    m = _DATE_YEAR.match(raw)
    if m:
        return {
            "raw": raw,
            "year": int(m.group(1)),
            "month": None,
            "day": None,
            "precision": "year",
        }
    # Unparseable — still store the raw value
    return {
        "raw": raw,
        "year": None,
        "month": None,
        "day": None,
        "precision": "unknown",
    }


# ---------- track / disc number parsing ----------

def _parse_track_field(value: str | None, total_field: str | None) -> tuple[int | None, int | None]:
    """Parse track/disc number fields.

    Handles: "1", "01", "1/12", "01/12".
    `total_field` is an optional separate total value (e.g. TRACKTOTAL).
    """
    num: int | None = None
    total: int | None = None

    if value is not None:
        value = str(value).strip()
        if "/" in value:
            parts = value.split("/", 1)
            num = _safe_int(parts[0])
            total = _safe_int(parts[1])
        else:
            num = _safe_int(value)

    if total_field is not None and total is None:
        total = _safe_int(str(total_field).strip())

    return num, total


def _safe_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


# ---------- external IDs ----------

def _ci_get(raw: dict[str, list[str]], key: str) -> list[str] | None:
    """Case-insensitive dict get — tries exact, lower, upper."""
    return raw.get(key) or raw.get(key.lower()) or raw.get(key.upper())


def _ci_first(raw: dict[str, list[str]], key: str) -> str | None:
    vals = _ci_get(raw, key)
    return vals[0] if vals else None


def extract_external_ids(tag_format: str | None, raw: dict[str, list[str]]) -> dict[str, Any]:
    """Pull MusicBrainz and other external IDs from raw tags."""
    mb: dict[str, Any] = {
        "recording_id": None,
        "release_id": None,
        "release_group_id": None,
        "release_track_id": None,
        "artist_ids": [],
        "album_artist_ids": [],
    }

    if tag_format == "vorbis_comment":
        mb["recording_id"] = _ci_first(raw, "MUSICBRAINZ_TRACKID")
        mb["release_id"] = _ci_first(raw, "MUSICBRAINZ_ALBUMID")
        mb["release_group_id"] = _ci_first(raw, "MUSICBRAINZ_RELEASEGROUPID")
        mb["release_track_id"] = _ci_first(raw, "MUSICBRAINZ_RELEASETRACKID")
        mb["artist_ids"] = _ci_get(raw, "MUSICBRAINZ_ARTISTID") or []
        mb["album_artist_ids"] = _ci_get(raw, "MUSICBRAINZ_ALBUMARTISTID") or []
    elif tag_format == "id3":
        # MusicBrainz Picard stores these as TXXX frames
        mb["recording_id"] = _first_or_none(raw, "TXXX:MusicBrainz Recording Id") or _first_or_none(raw, "TXXX:MusicBrainz Track Id")
        mb["release_id"] = _first_or_none(raw, "TXXX:MusicBrainz Album Id")
        mb["release_group_id"] = _first_or_none(raw, "TXXX:MusicBrainz Release Group Id")
        mb["release_track_id"] = _first_or_none(raw, "TXXX:MusicBrainz Release Track Id")
        mb["artist_ids"] = raw.get("TXXX:MusicBrainz Artist Id", [])
        mb["album_artist_ids"] = raw.get("TXXX:MusicBrainz Album Artist Id", [])
    elif tag_format == "mp4":
        # Freeform atoms used by Picard
        _mb4 = "----:com.apple.iTunes:MusicBrainz "
        mb["recording_id"] = _first_or_none(raw, f"{_mb4}Track Id")
        mb["release_id"] = _first_or_none(raw, f"{_mb4}Album Id")
        mb["release_group_id"] = _first_or_none(raw, f"{_mb4}Release Group Id")
        mb["release_track_id"] = _first_or_none(raw, f"{_mb4}Release Track Id")
        mb["artist_ids"] = raw.get(f"{_mb4}Artist Id", [])
        mb["album_artist_ids"] = raw.get(f"{_mb4}Album Artist Id", [])

    return {"musicbrainz": mb}


def _first_or_none(raw: dict[str, list[str]], key: str) -> str | None:
    vals = raw.get(key)
    return vals[0] if vals else None


# ---------- embedded pictures ----------

# Full ID3v2 APIC picture type mapping (0-20)
_PICTURE_TYPE_MAP = {
    0: "other",
    1: "file_icon",
    2: "other_file_icon",
    3: "front",
    4: "back",
    5: "leaflet",
    6: "media",
    7: "lead_artist",
    8: "artist",
    9: "conductor",
    10: "band",
    11: "composer",
    12: "lyricist",
    13: "recording_location",
    14: "during_recording",
    15: "during_performance",
    16: "screen_capture",
    17: "bright_fish",
    18: "illustration",
    19: "band_logotype",
    20: "publisher_logotype",
}


def extract_pictures(mf: mutagen.FileType) -> list[dict[str, Any]]:
    """Extract metadata about embedded pictures (no raw bytes in output)."""
    pics: list[dict[str, Any]] = []

    if isinstance(mf, FLAC):
        for p in mf.pictures:
            w, h = p.width or None, p.height or None
            if not w or not h:
                w, h = image_dimensions(p.data) if p.data else (None, None)
            pics.append(_picture_record(
                type_id=p.type,
                mime=p.mime,
                data=p.data,
                width=w,
                height=h,
            ))
    elif isinstance(mf, (MP3, AIFF, WAVE)):
        if mf.tags:
            for key, frame in mf.tags.items():
                if isinstance(frame, APIC):
                    w, h = image_dimensions(frame.data) if frame.data else (None, None)
                    pics.append(_picture_record(
                        type_id=frame.type,
                        mime=frame.mime,
                        data=frame.data,
                        width=w,
                        height=h,
                    ))
    elif isinstance(mf, (OggVorbis, OggOpus)):
        # Vorbis comments can embed pictures via METADATA_BLOCK_PICTURE
        if mf.tags:
            for b64 in mf.tags.get("METADATA_BLOCK_PICTURE", []):
                try:
                    import base64
                    pic = FLACPicture(base64.b64decode(b64))
                    w, h = pic.width or None, pic.height or None
                    if not w or not h:
                        w, h = image_dimensions(pic.data) if pic.data else (None, None)
                    pics.append(_picture_record(
                        type_id=pic.type,
                        mime=pic.mime,
                        data=pic.data,
                        width=w,
                        height=h,
                    ))
                except Exception:
                    pass
    elif isinstance(mf, MP4):
        if mf.tags and "covr" in mf.tags:
            for cover in mf.tags["covr"]:
                mime = "image/jpeg"
                if isinstance(cover, MP4Cover):
                    if cover.imageformat == MP4Cover.FORMAT_PNG:
                        mime = "image/png"
                cover_bytes = bytes(cover)
                w, h = image_dimensions(cover_bytes)
                pics.append(_picture_record(
                    type_id=3,  # assume front
                    mime=mime,
                    data=cover_bytes,
                    width=w,
                    height=h,
                ))

    return pics


def _picture_record(
    *,
    type_id: int,
    mime: str,
    data: bytes,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    return {
        "type": _PICTURE_TYPE_MAP.get(type_id, f"unknown({type_id})"),
        "mime": mime,
        "size_bytes": len(data) if data else 0,
        "width": width,
        "height": height,
    }
