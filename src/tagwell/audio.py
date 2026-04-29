"""Audio container/codec property extraction via mutagen."""

from __future__ import annotations

from typing import Any

import mutagen
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.aiff import AIFF
from mutagen.wave import WAVE


def extract_audio_info(mf: mutagen.FileType) -> dict[str, Any]:
    """Return the `audio` block for the JSONL record."""
    info = mf.info if mf else None
    if info is None:
        return _empty_audio()

    duration = getattr(info, "length", None)
    sample_rate = getattr(info, "sample_rate", None)
    channels = getattr(info, "channels", None)
    bitrate = getattr(info, "bitrate", None)  # bps for most formats
    bit_depth = getattr(info, "bits_per_sample", None)

    container, codec, lossless = _classify(mf)

    estimated_bitrate_kbps: float | None = None
    if bitrate is not None:
        estimated_bitrate_kbps = round(bitrate / 1000, 1)
    elif duration and duration > 0:
        # mutagen sometimes doesn't expose bitrate; estimate from file
        pass  # leave None rather than guess wrong

    return {
        "container": container,
        "codec": codec,
        "duration_seconds": round(duration, 2) if duration else None,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "bit_depth": bit_depth,
        "lossless": lossless,
        "estimated_bitrate_kbps": estimated_bitrate_kbps,
    }


def _classify(mf: mutagen.FileType) -> tuple[str | None, str | None, bool | None]:
    """Return (container, codec, lossless) based on mutagen type."""
    if isinstance(mf, FLAC):
        return "flac", "flac", True
    if isinstance(mf, MP3):
        return "mp3", "mp3", False
    if isinstance(mf, MP4):
        # MP4 / M4A — codec depends on content
        codec_str = getattr(mf.info, "codec", None)
        if codec_str:
            codec_str = codec_str.lower()
        else:
            codec_str = "aac"
        lossless = "alac" in (codec_str or "")
        return "m4a", codec_str, lossless
    if isinstance(mf, OggOpus):
        return "ogg", "opus", False
    if isinstance(mf, OggVorbis):
        return "ogg", "vorbis", False
    if isinstance(mf, AIFF):
        return "aiff", "pcm", True
    if isinstance(mf, WAVE):
        return "wav", "pcm", True
    # Fallback: try the mutagen mime list
    mime = getattr(mf, "mime", [])
    if mime:
        m = mime[0].lower()
        if "ogg" in m:
            return "ogg", None, None
        if "aac" in m:
            return "aac", "aac", False
    return None, None, None


def _empty_audio() -> dict[str, Any]:
    return {
        "container": None,
        "codec": None,
        "duration_seconds": None,
        "sample_rate_hz": None,
        "channels": None,
        "bit_depth": None,
        "lossless": None,
        "estimated_bitrate_kbps": None,
    }
