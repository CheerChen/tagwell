"""Tests for image dimension parsing."""

import struct

from tagwell.imaging import image_dimensions


class TestPngDimensions:
    def _make_png(self, width: int, height: int) -> bytes:
        header = b"\x89PNG\r\n\x1a\n"
        # IHDR chunk: length(4) + "IHDR"(4) + width(4) + height(4) + ...
        ihdr_data = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
        ihdr_len = struct.pack(">I", len(ihdr_data))
        return header + ihdr_len + b"IHDR" + ihdr_data

    def test_basic_png(self):
        data = self._make_png(640, 480)
        assert image_dimensions(data) == (640, 480)

    def test_large_png(self):
        data = self._make_png(3000, 3000)
        assert image_dimensions(data) == (3000, 3000)

    def test_truncated_png(self):
        assert image_dimensions(b"\x89PNG\r\n\x1a\n\x00") == (None, None)


class TestJpegDimensions:
    def _make_jpeg_sof(self, width: int, height: int) -> bytes:
        # Minimal JPEG: SOI + APP0 (short) + SOF0 with dimensions
        soi = b"\xff\xd8"
        # APP0 marker with minimal length
        app0 = b"\xff\xe0" + struct.pack(">H", 2)  # length=2 (just the length field)
        # SOF0 marker
        sof_len = struct.pack(">H", 11)  # length field
        sof_data = b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 3
        sof0 = b"\xff\xc0" + sof_len + sof_data
        return soi + app0 + sof0

    def test_basic_jpeg(self):
        data = self._make_jpeg_sof(800, 600)
        assert image_dimensions(data) == (800, 600)

    def test_small_jpeg(self):
        data = self._make_jpeg_sof(1, 1)
        assert image_dimensions(data) == (1, 1)


class TestUnknownFormat:
    def test_empty(self):
        assert image_dimensions(b"") == (None, None)

    def test_short(self):
        assert image_dimensions(b"\x00\x01") == (None, None)

    def test_random_bytes(self):
        assert image_dimensions(b"not an image at all") == (None, None)
