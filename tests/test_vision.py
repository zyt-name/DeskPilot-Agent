import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from xm_agent import vision


def _png(width: int, height: int) -> bytes:
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\x20\x40\x60" * width

    def chunk(tag: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(tag + data) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", checksum)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class VisionTests(unittest.TestCase):
    def test_digest_is_stable_and_path_digest_matches_bytes(self):
        data = _png(4, 4)
        self.assertEqual(
            vision.digest_bytes(data), vision.digest_bytes(data)
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            path.write_bytes(data)
            self.assertEqual(
                vision.digest_path(str(path)), vision.digest_bytes(data)
            )
            self.assertEqual(vision.digest_path(str(Path(tmp) / "no.png")), "")
        self.assertEqual(vision.digest_path(None), "")

    def test_png_size_reads_header(self):
        self.assertEqual(vision.png_size(_png(7, 3)), (7, 3))
        self.assertEqual(vision.png_size(b"not a png"), (0, 0))

    def test_prepare_image_falls_back_without_pillow(self):
        data = _png(4, 4)
        with mock.patch.object(vision, "Image", None):
            prepared = vision.prepare_image(data)
        self.assertEqual(prepared["data"], data)
        self.assertEqual(prepared["extension"], "png")
        self.assertEqual(prepared["scale"], 1.0)

    def test_prepare_image_downscales_wide_screens(self):
        if vision.Image is None:
            self.skipTest("Pillow is not installed")
        prepared = vision.prepare_image(_png(2000, 1000))
        self.assertLessEqual(prepared["width"], vision.MAX_WIDTH)
        self.assertGreater(prepared["scale"], 1.0)
        self.assertIn(prepared["extension"], {"jpg", "png"})
        self.assertLess(len(prepared["data"]), 2000 * 1000 * 3)

    def test_save_image_writes_optimized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _png(16, 8)
            saved = vision.save_image(data, tmp, prefix="test")
            path = Path(saved["path"])
            self.assertTrue(path.exists())
            self.assertTrue(path.name.startswith("test-"))
            self.assertEqual(saved["hash"], vision.digest_bytes(data))
            self.assertEqual(saved["bytes"], path.stat().st_size)
            self.assertEqual(
                (saved["width"], saved["height"]), (16, 8)
            )

    def test_save_image_falls_back_without_pillow(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _png(8, 8)
            with mock.patch.object(vision, "Image", None):
                saved = vision.save_image(data, tmp, prefix="fallback")
            self.assertEqual(Path(saved["path"]).suffix, ".png")
            self.assertEqual(
                Path(saved["path"]).read_bytes(), data
            )

    def test_scale_point_maps_and_clamps(self):
        self.assertEqual(
            vision.scale_point(
                100,
                50,
                image_width=1280,
                image_height=720,
                source_width=2560,
                source_height=1440,
            ),
            (200, 100),
        )
        self.assertEqual(
            vision.scale_point(
                5000,
                -20,
                image_width=1000,
                image_height=800,
                source_width=2000,
                source_height=1600,
            ),
            (1999, 0),
        )
        self.assertEqual(
            vision.scale_point(
                30,
                40,
                image_width=0,
                image_height=0,
                source_width=100,
                source_height=100,
            ),
            (30, 40),
        )


if __name__ == "__main__":
    unittest.main()
