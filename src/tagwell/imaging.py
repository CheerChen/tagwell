"""Lightweight image dimension parsing for JPEG and PNG.

Avoids Pillow dependency by reading raw headers:
- JPEG: scan markers to find SOF0 (0xFFC0) or SOF2 (0xFFC2)
- PNG: read IHDR chunk at fixed offset (bytes 16-24)
"""

from __future__ import annotations

import struct


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Return (width, height) from raw image bytes, or (None, None)."""
    if len(data) < 8:
        return None, None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _png_dimensions(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_dimensions(data)
    return None, None


def _png_dimensions(data: bytes) -> tuple[int | None, int | None]:
    # IHDR is always the first chunk: offset 8 = chunk length (4) + "IHDR" (4) + width (4) + height (4)
    if len(data) < 24:
        return None, None
    width = struct.unpack(">I", data[16:20])[0]
    height = struct.unpack(">I", data[20:24])[0]
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    # Walk JPEG markers looking for SOF0 (0xC0) or SOF2 (0xC2)
    i = 2  # skip SOI marker (0xFFD8)
    length = len(data)
    while i < length - 1:
        if data[i] != 0xFF:
            return None, None
        marker = data[i + 1]
        # Skip padding 0xFF bytes
        if marker == 0xFF:
            i += 1
            continue
        # SOF markers: C0-C3, C5-C7, C9-CB, CD-CF
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 < length:
                height = struct.unpack(">H", data[i + 5:i + 7])[0]
                width = struct.unpack(">H", data[i + 7:i + 9])[0]
                return width, height
            return None, None
        # SOS marker (0xDA) — start of scan data, stop searching
        if marker == 0xDA:
            return None, None
        # Skip other markers using their length field
        if i + 3 < length:
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg_len
        else:
            return None, None
    return None, None
