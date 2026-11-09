"""Screenshot capture, compression and hashing.

Vision pricing and latency scale with image pixels, so every screenshot the
agent sends is downscaled and re-encoded. Pillow is optional: when it is not
installed the original bytes are kept and callers still work.
"""

from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised only when Pillow is installed
    from PIL import Image, ImageGrab
except Exception:  # pragma: no cover - optional dependency
    Image = None
    ImageGrab = None

MAX_WIDTH = 1280
JPEG_QUALITY = 80


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_path(path: str | None) -> str:
    if not path:
        return ""
    try:
        return digest_bytes(Path(path).read_bytes())
    except OSError:
        return ""


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    return 0, 0


def prepare_image(
    data: bytes,
    *,
    max_width: int = MAX_WIDTH,
    quality: int = JPEG_QUALITY,
) -> dict[str, Any]:
    """Return optimized bytes plus the geometry needed to map coordinates.

    ``scale`` is native pixels per sent pixel. It is 1.0 when the image was
    not downscaled, so coordinate mapping stays a no-op for small screens.
    """
    source_width, source_height = png_size(data)
    raw = {
        "data": data,
        "extension": "png",
        "mime": "image/png",
        "width": source_width,
        "height": source_height,
        "source_width": source_width,
        "source_height": source_height,
        "scale": 1.0,
    }
    if Image is None or not data:
        return raw
    try:  # pragma: no cover - depends on optional Pillow
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            source_width, source_height = image.size
            working = image.convert("RGB")
            if source_width > max_width:
                height = max(1, round(source_height * max_width / source_width))
                working = working.resize((max_width, height), Image.LANCZOS)
            jpeg = io.BytesIO()
            working.save(jpeg, format="JPEG", quality=quality, optimize=True)
            optimized = jpeg.getvalue()
            png = io.BytesIO()
            working.save(png, format="PNG", optimize=True)
            png_data = png.getvalue()
            if len(png_data) < len(optimized):
                payload, extension, mime = png_data, "png", "image/png"
            else:
                payload, extension, mime = optimized, "jpg", "image/jpeg"
            width, height = working.size
            return {
                "data": payload,
                "extension": extension,
                "mime": mime,
                "width": width,
                "height": height,
                "source_width": source_width,
                "source_height": source_height,
                "scale": (
                    source_width / width if width > 0 else 1.0
                ),
            }
    except Exception:
        return raw


def save_image(
    data: bytes,
    directory: str | Path,
    *,
    prefix: str = "shot",
    max_width: int = MAX_WIDTH,
) -> dict[str, Any]:
    prepared = prepare_image(data, max_width=max_width)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / (
        f"{prefix}-{time.time_ns()}-{prepared['width']}x"
        f"{prepared['height']}.{prepared['extension']}"
    )
    path.write_bytes(prepared["data"])
    return {
        **{key: value for key, value in prepared.items() if key != "data"},
        "path": str(path.resolve()),
        "hash": digest_bytes(data),
        "bytes": len(prepared["data"]),
        "source_bytes": len(data),
    }


def capture_primary_screen(
    directory: str | Path,
    *,
    prefix: str = "screen",
    max_width: int = MAX_WIDTH,
) -> dict[str, Any] | None:
    """Grab the desktop for audit purposes; None when Pillow is unavailable."""
    if ImageGrab is None:
        return None
    try:  # pragma: no cover - needs a desktop session
        image = ImageGrab.grab(all_screens=True)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return save_image(
            buffer.getvalue(), directory, prefix=prefix, max_width=max_width
        )
    except Exception:
        return None


def scale_point(
    x: int,
    y: int,
    *,
    image_width: int,
    image_height: int,
    source_width: int,
    source_height: int,
) -> tuple[int, int]:
    if image_width <= 0 or image_height <= 0:
        return int(x), int(y)
    native_x = round(int(x) * source_width / image_width)
    native_y = round(int(y) * source_height / image_height)
    if source_width > 0:
        native_x = min(max(native_x, 0), source_width - 1)
    if source_height > 0:
        native_y = min(max(native_y, 0), source_height - 1)
    return int(native_x), int(native_y)


__all__ = [
    "JPEG_QUALITY",
    "MAX_WIDTH",
    "capture_primary_screen",
    "digest_bytes",
    "digest_path",
    "png_size",
    "prepare_image",
    "save_image",
    "scale_point",
]
