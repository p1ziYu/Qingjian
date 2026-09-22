"""A small TIFF/EXIF reader.

Pillow opens ordinary photographs, but most camera raw files are TIFF
containers it will not decode — and reading their capture date is exactly what
a sorter needs. Parsing the handful of tags we care about directly is a few
dozen lines and removes the need for a raw-decoding dependency just to answer
"when was this taken".
"""
from __future__ import annotations

import struct
from pathlib import Path

# Only the tags that drive sorting, templates and the info panel.
IFD0_TAGS = {
    0x00FE: "_NewSubfileType",
    0x014A: "_SubIFDs",
    0xC620: "_DefaultCropSize",
    0x010F: "Make",
    0x0110: "Model",
    0x0112: "Orientation",
    0x0132: "DateTime",
    0x0100: "ImageWidth",
    0x0101: "ImageLength",
    0x8769: "_ExifIFD",
    0xC612: "_DNGVersion",
}
EXIF_TAGS = {
    0x9003: "DateTimeOriginal",
    0x9004: "DateTimeDigitized",
    0x9010: "OffsetTime",
    0x9011: "OffsetTimeOriginal",
    0x829A: "ExposureTime",
    0x829D: "FNumber",
    0x8827: "ISOSpeedRatings",
    0x8832: "PhotographicSensitivity",
    0x920A: "FocalLength",
    0xA002: "PixelXDimension",
    0xA003: "PixelYDimension",
    0xA434: "LensModel",
}

_FORMATS = {
    1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8),
    6: ("b", 1), 7: ("s", 1), 8: ("h", 2), 9: ("i", 4), 10: ("ii", 8),
    11: ("f", 4), 12: ("d", 8),
}

MAX_ENTRIES = 512


def _read_value(blob: bytes, endian: str, kind: int, count: int, offset: int):
    spec = _FORMATS.get(kind)
    if spec is None:
        return None
    code, size = spec
    total = size * count
    if total > 4:
        try:
            (pointer,) = struct.unpack_from(endian + "I", blob, offset)
        except struct.error:
            return None
        start = pointer
    else:
        start = offset
    if start < 0 or start + total > len(blob):
        return None
    if kind in (2, 7):
        raw = blob[start:start + total]
        text = raw.split(b"\x00", 1)[0]
        try:
            return text.decode("utf-8", "replace").strip()
        except Exception:  # pragma: no cover - decode never raises with replace
            return None
    if kind in (5, 10):
        values = []
        for index in range(count):
            try:
                num, den = struct.unpack_from(endian + ("II" if kind == 5 else "ii"),
                                              blob, start + index * 8)
            except struct.error:
                return None
            values.append((num, den))
        return values[0] if count == 1 else values
    values = []
    for index in range(count):
        try:
            (value,) = struct.unpack_from(endian + code, blob, start + index * size)
        except struct.error:
            return None
        values.append(value)
    return values[0] if count == 1 else values


def _read_ifd(blob: bytes, endian: str, offset: int, names: dict[int, str]) -> dict:
    out: dict[str, object] = {}
    if offset <= 0 or offset + 2 > len(blob):
        return out
    try:
        (count,) = struct.unpack_from(endian + "H", blob, offset)
    except struct.error:
        return out
    count = min(count, MAX_ENTRIES)
    for index in range(count):
        entry = offset + 2 + index * 12
        if entry + 12 > len(blob):
            break
        try:
            tag, kind, length = struct.unpack_from(endian + "HHI", blob, entry)
        except struct.error:
            break
        name = names.get(tag)
        if not name or name in out:
            continue
        if (kind in (2, 7) and length > 4096) or (kind not in (2, 7) and length > 16):
            continue
        value = _read_value(blob, endian, kind, length, entry + 8)
        if value is not None:
            out[name] = value
    return out


def read_tiff_exif(path: str | Path, max_bytes: int = 2 * 1024 * 1024) -> dict:
    """Parse the TIFF header of *path*. Returns {} when it is not TIFF-like.

    Only the first few megabytes are read: IFD0 and the EXIF IFD live near the
    start of every raw format that uses this layout.
    """
    try:
        with open(path, "rb") as stream:
            blob = stream.read(max_bytes)
    except OSError:
        return {}
    if len(blob) < 8:
        return {}
    marker = blob[:2]
    if marker == b"II":
        endian = "<"
    elif marker == b"MM":
        endian = ">"
    else:
        return {}
    try:
        magic, first = struct.unpack_from(endian + "HI", blob, 2)
    except struct.error:
        return {}
    if magic not in (42, 0x4F52, 0x5352, 0x0055):
        return {}
    data = _read_ifd(blob, endian, first, IFD0_TAGS)
    sub_offset = data.pop("_SubIFDs", None)
    thumbnail = bool(int(data.pop("_NewSubfileType", 0) or 0) & 1)
    crop = data.pop("_DefaultCropSize", None)
    if isinstance(sub_offset, list):
        sub_offset = sub_offset[0] if sub_offset else None
    main = _read_ifd(blob, endian, sub_offset, IFD0_TAGS) if isinstance(sub_offset, int) else {}
    if main:
        main.pop("_NewSubfileType", None)
        main.pop("_SubIFDs", None)
        crop = main.pop("_DefaultCropSize", crop)
        if main.get("ImageWidth") and main.get("ImageLength"):
            data.update({key: main[key] for key in ("ImageWidth", "ImageLength")})
    elif thumbnail:
        data.pop("ImageWidth", None)
        data.pop("ImageLength", None)
    if isinstance(crop, list) and len(crop) >= 2:
        data["ImageWidth"], data["ImageLength"] = int(crop[0]), int(crop[1])
    exif_offset = data.pop("_ExifIFD", None)
    data.pop("_DNGVersion", None)
    if isinstance(exif_offset, int):
        data.update(_read_ifd(blob, endian, exif_offset, EXIF_TAGS))
    return data
