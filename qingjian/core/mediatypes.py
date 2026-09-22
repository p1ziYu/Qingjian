"""What counts as media, and what kind of media it is.

Extension driven on purpose: sniffing every file's magic bytes during a scan of
tens of thousands of items is far more expensive than the mistake it prevents,
and a file whose extension lies will still fail loudly at decode time.
"""
from __future__ import annotations

from pathlib import Path

#: Ordinary raster images the preview can decode directly.
IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".bmp", ".dib", ".gif",
    ".webp", ".tif", ".tiff", ".heic", ".heif", ".hif", ".avif", ".ico",
    ".jxl", ".jp2", ".j2k", ".ppm", ".pgm", ".tga", ".qoi",
})

#: Camera raw formats. Absent from the previous version, which meant anyone
#: shooting raw could not sort their own library.
RAW_EXTENSIONS: frozenset[str] = frozenset({
    ".cr2", ".cr3", ".crw",          # Canon
    ".nef", ".nrw",                  # Nikon
    ".arw", ".srf", ".sr2",          # Sony
    ".raf",                          # Fujifilm
    ".orf",                          # Olympus / OM
    ".rw2",                          # Panasonic
    ".pef", ".ptx",                  # Pentax
    ".srw",                          # Samsung
    ".erf",                          # Epson
    ".kdc", ".dcr",                  # Kodak
    ".mrw",                          # Minolta
    ".3fr", ".fff",                  # Hasselblad
    ".iiq",                          # Phase One
    ".mos",                          # Leaf
    ".x3f",                          # Sigma
    ".rwl",                          # Leica
    ".dng",                          # Adobe / universal
    ".ari",                          # Arri
    ".braw",                         # Blackmagic (raw stills workflow)
})

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".m4v", ".mov", ".qt", ".avi", ".mkv", ".webm", ".wmv", ".asf",
    ".mpg", ".mpeg", ".mpe", ".mp2", ".m2v", ".vob", ".3gp", ".3g2",
    ".ts", ".m2ts", ".mts", ".mxf", ".ogv", ".flv", ".f4v", ".divx",
    ".rm", ".rmvb", ".insv",
})

#: Formats that may carry more than one frame. Used to decide whether the
#: preview should animate rather than show frame zero.
ANIMATABLE_EXTENSIONS: frozenset[str] = frozenset({".gif", ".webp", ".apng", ".png", ".avif"})

MEDIA_EXTENSIONS: frozenset[str] = IMAGE_EXTENSIONS | RAW_EXTENSIONS | VIDEO_EXTENSIONS

#: Raw files whose embedded preview the viewer can usually show without a raw
#: decoder, because the container is a TIFF/JPEG the imaging stack can open.
RAW_WITH_TIFF_CONTAINER: frozenset[str] = frozenset({".dng", ".nef", ".cr2", ".arw", ".pef", ".srw"})

KIND_IMAGE = "image"
KIND_RAW = "raw"
KIND_VIDEO = "video"
KIND_OTHER = "other"


def suffix(path: str | Path) -> str:
    """Lower-cased extension of *path*, including the leading dot."""
    return Path(path).suffix.lower()


def kind(path: str | Path) -> str:
    ext = suffix(path)
    if ext in VIDEO_EXTENSIONS:
        return KIND_VIDEO
    if ext in RAW_EXTENSIONS:
        return KIND_RAW
    if ext in IMAGE_EXTENSIONS:
        return KIND_IMAGE
    return KIND_OTHER


def is_video(path: str | Path) -> bool:
    return suffix(path) in VIDEO_EXTENSIONS


def is_raw(path: str | Path) -> bool:
    return suffix(path) in RAW_EXTENSIONS


def is_image(path: str | Path) -> bool:
    """True for anything that shows as a still, raw included."""
    ext = suffix(path)
    return ext in IMAGE_EXTENSIONS or ext in RAW_EXTENSIONS


def is_media(path: str | Path) -> bool:
    return suffix(path) in MEDIA_EXTENSIONS


def is_decodable(path: str | Path) -> bool:
    """Whether an installed still-image reader supports this extension."""
    ext = suffix(path)
    if ext not in IMAGE_EXTENSIONS:
        return ext in RAW_EXTENSIONS or ext in VIDEO_EXTENSIONS
    if ext not in {".heic", ".heif", ".hif", ".jxl"}:
        return True
    from PIL import Image  # noqa: PLC0415 - optional codec registration

    Image.init()
    if ext == ".jxl":
        return "JXL" in Image.OPEN
    return any(name in Image.OPEN for name in ("HEIF", "HEIC"))


def may_animate(path: str | Path) -> bool:
    return suffix(path) in ANIMATABLE_EXTENSIONS
