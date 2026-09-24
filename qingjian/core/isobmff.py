"""Just enough ISO base media (MP4 / MOV / HEIC / CR3) to read duration and size.

The previous version pulled in a whole parser framework to answer "how long is
this clip", which is one 32-byte structure inside ``moov/mvhd``.
"""
from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ISO base media counts seconds from 1904-01-01, not the Unix epoch.
_EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)
_CONTAINERS = {b"moov", b"trak", b"mdia", b"edts", b"udta", b"meta"}
MAX_DEPTH = 6


def _iter_boxes(stream, end: int, depth: int = 0):
    while stream.tell() + 8 <= end:
        start = stream.tell()
        header = stream.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            extra = stream.read(8)
            if len(extra) < 8:
                return
            (size,) = struct.unpack(">Q", extra)
            header_size = 16
        elif size == 0:
            size = end - start
        if size < header_size or start + size > end:
            return
        yield kind, start + header_size, start + size, depth
        if kind in _CONTAINERS and depth < MAX_DEPTH:
            stream.seek(start + header_size)
            yield from _iter_boxes(stream, start + size, depth + 1)
        stream.seek(start + size)


def read_header(path: str | Path) -> dict:
    """Return duration, dimensions and creation time when the file carries them."""
    result: dict[str, object] = {}
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as stream:
            for kind, body, stop, _depth in _iter_boxes(stream, size):
                if kind == b"ftyp":
                    stream.seek(body)
                    brand = stream.read(4)
                    result["brand"] = brand.decode("ascii", "replace").strip()
                elif kind == b"mvhd":
                    stream.seek(body)
                    version = stream.read(1)
                    stream.read(3)
                    if version == b"\x01":
                        created, _modified, timescale, duration = struct.unpack(
                            ">QQIQ", stream.read(28))
                    else:
                        created, _modified, timescale, duration = struct.unpack(
                            ">IIII", stream.read(16))
                    if timescale:
                        result["duration"] = duration / timescale
                    if created:
                        try:
                            result["created"] = _EPOCH_1904 + timedelta(seconds=created)
                        except (OverflowError, OSError, ValueError):
                            pass
                elif kind == b"tkhd" and "width" not in result:
                    stream.seek(body)
                    version = stream.read(1)
                    stream.read(3)
                    stream.read(16 if version == b"\x01" else 8)   # times
                    stream.read(4 + 4)                              # track id + reserved
                    stream.read(8 if version == b"\x01" else 4)     # duration
                    stream.read(8 + 2 + 2 + 2 + 2)                  # reserved + layer/volume
                    matrix = stream.read(36)
                    if len(matrix) == 36:
                        a, b, _, c, d, *_ = struct.unpack(">9i", matrix)
                        if a == d == 0 and b == 65536 and c == -65536:
                            result["rotation"] = 90
                        elif a == d == 0 and b == -65536 and c == 65536:
                            result["rotation"] = -90
                    raw = stream.read(8)
                    if len(raw) == 8:
                        width, height = struct.unpack(">II", raw)
                        # 16.16 fixed point.
                        if width and height:
                            result["width"] = int(width / 65536)
                            result["height"] = int(height / 65536)
    except (OSError, struct.error, ValueError):
        return result
    return result
