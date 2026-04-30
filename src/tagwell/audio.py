"""Audio container/codec property extraction via mutagen."""

from __future__ import annotations

from typing import Any

import mutagen
from mutagen.flac import FLAC
from mutagen.mp3 import MP3, BitrateMode
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.aiff import AIFF
from mutagen.wave import WAVE


_BITRATE_MODE_NAMES = {
    BitrateMode.CBR: "cbr",
    BitrateMode.VBR: "vbr",
    BitrateMode.ABR: "abr",
}


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
        "bitrate_mode": _bitrate_mode(mf),
        "encoder_info": _encoder_info(mf),
        "encoder_settings": _encoder_settings(mf),
        "sketchy": _sketchy(mf),
    }


def _bitrate_mode(mf: mutagen.FileType) -> str | None:
    """MP3 only: 'cbr' / 'vbr' / 'abr'. None for UNKNOWN or other formats."""
    if not isinstance(mf, MP3):
        return None
    mode = getattr(mf.info, "bitrate_mode", None)
    return _BITRATE_MODE_NAMES.get(mode)


def _encoder_info(mf: mutagen.FileType) -> str | None:
    """Encoder identity string. MP3: LAME tag info. FLAC: Vorbis comment vendor."""
    if isinstance(mf, MP3):
        return getattr(mf.info, "encoder_info", "") or None
    if isinstance(mf, FLAC):
        vendor = getattr(getattr(mf, "tags", None), "vendor", None)
        return vendor or None
    return None


def _encoder_settings(mf: mutagen.FileType) -> str | None:
    """MP3 only: LAME settings string (e.g. '-b 320')."""
    if isinstance(mf, MP3):
        return getattr(mf.info, "encoder_settings", "") or None
    return None


def _sketchy(mf: mutagen.FileType) -> bool | None:
    """MP3 only: True if mutagen had to fall back to lenient frame parsing."""
    if not isinstance(mf, MP3):
        return None
    return bool(getattr(mf.info, "sketchy", False))


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
        "bitrate_mode": None,
        "encoder_info": None,
        "encoder_settings": None,
        "sketchy": None,
    }
