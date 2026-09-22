"""Thumbnails, perceptual hashes and a sharpness score.

Byte-identical duplicates are the easy case and the previous version already
found them. What actually clutters a photo library is the *near* duplicate: the
same frame exported twice, a web-sized copy, twenty frames of one burst. That
needs a perceptual hash, and choosing which of them to keep needs a focus
measure.
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import mediatypes
from .logsetup import get_logger

log = get_logger("imaging")

HASH_BITS = 64
_DCT_SIZE = 32
_HASH_SIDE = 8

#: Bumped whenever the decode path changes in a way that moves hash values.
#: Cached hashes from an older algorithm are discarded rather than compared
#: with fresh ones, which would silently break near-match grouping.
ALGORITHM_VERSION = 4

# JPEG start/end markers, used to lift the preview out of a raw container.
_SOI = b"\xff\xd8\xff"
_EOI = b"\xff\xd9"


def _pillow():
    from PIL import Image, ImageOps  # noqa: PLC0415 - optional at import time
    return Image, ImageOps


def extract_embedded_jpeg(path: str | Path, search_bytes: int = 16 * 1024 * 1024) -> bytes | None:
    """The largest JPEG embedded in a raw container, or None.

    Every camera writes a full-size preview into its raw file; using it means a
    raw library previews at ordinary speed with no raw decoder installed.
    """
    try:
        with open(path, "rb") as stream:
            blob = stream.read(search_bytes)
    except OSError:
        return None
    best: bytes | None = None
    start = blob.find(_SOI)
    while start != -1:
        end = _jpeg_end(blob, start)
        if end is None:
            end = blob.find(_EOI, start + 3)
            if end < 0:
                start = blob.find(_SOI, start + 3)
                continue
        candidate = blob[start:end + 2]
        if best is None or len(candidate) > len(best):
            best = candidate
        start = blob.find(_SOI, end + 2)
    # The largest embedded JPEG wins. Some bodies store only a small preview; a
    # soft picture still beats refusing to show the file, and the perceptual
    # hash is computed at 32 pixels either way.
    return best


def _jpeg_end(blob: bytes, start: int) -> int | None:
    """Find EOI after parsing header segments, which may contain JPEG bytes."""
    cursor = start + 2
    while cursor + 1 < len(blob):
        if blob[cursor] != 0xFF:
            return None
        while cursor < len(blob) and blob[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(blob):
            return None
        marker = blob[cursor]
        cursor += 1
        if marker == 0xD9:
            return cursor - 2
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if cursor + 2 > len(blob):
            return None
        length = int.from_bytes(blob[cursor:cursor + 2], "big")
        if length < 2 or cursor + length > len(blob):
            return None
        cursor += length
        if marker == 0xDA:
            while cursor + 1 < len(blob):
                if blob[cursor] == 0xFF and blob[cursor + 1] == 0xD9:
                    return cursor
                if blob[cursor] == 0xFF and blob[cursor + 1] not in (0x00, *range(0xD0, 0xD8)):
                    return None
                cursor += 1
    return None


def _open_drafted(source, target: tuple[int, int] | None):
    """Open an image, asking the decoder for the smallest size that will do.

    ``Image.draft()`` hands the request down to libjpeg, which then decodes at
    1/2, 1/4 or 1/8 scale straight out of the DCT coefficients. For a 24 MP
    photograph reduced to a thumbnail that is roughly fourteen times less work
    and sixty times less memory than decoding in full and shrinking afterwards
    — the difference between a folder that opens and one that hangs.
    """
    Image, _ = _pillow()
    image = Image.open(source)
    if target:
        try:
            image.draft("RGB", (max(1, target[0]), max(1, target[1])))
        except (AttributeError, ValueError, OSError):
            pass                      # not a format that can scale while decoding
    image.load()
    return image


#: Above this, a format that cannot be scaled while it is decoded is read on a
#: worker rather than on the thread that paints. A twelve-thousand-pixel PNG is
#: five seconds and three hundred megabytes of work. JPEG and camera raw stay
#: where they are: libjpeg scales during the decode, so their cost follows the
#: compressed size rather than the pixel count.
HEAVY_BYTES = 12 * 1024 * 1024

#: No format is worth decoding on the interface thread above this.
ALWAYS_HEAVY_BYTES = 64 * 1024 * 1024

#: Suffixes libjpeg can scale on the way out. `Image.draft` is a no-op for
#: everything else, so those decode at full size whatever target is asked for.
SCALES_WHILE_DECODING = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})


def decodes_slowly(path: str | Path) -> bool:
    """Would decoding *path* stall the window long enough to notice?

    False for video: that never reaches a decoder here at all, it goes to the
    media player, which is asynchronous already. Saying True for it left the
    preview showing a placeholder that nothing ever replaced.

    True for PNG and WebP even though those *can* hold an animation, because
    almost none do and a large still one is exactly the case that stalls for
    seconds. The preview tries the animation path first regardless, so an
    animated file still animates; it only costs one wasted decode.
    """
    target = Path(path)
    if mediatypes.is_video(target):
        return False
    if mediatypes.suffix(target) in {".heic", ".heif", ".hif"}:
        return True
    try:
        size = target.stat().st_size
    except OSError:
        return False
    if target.suffix.lower() in SCALES_WHILE_DECODING or mediatypes.is_raw(target):
        # libjpeg scales on the way out, so the cost follows the compressed
        # size rather than the pixel count.
        return size >= ALWAYS_HEAVY_BYTES
    return size >= HEAVY_BYTES


def open_image(path: str | Path, target: tuple[int, int] | None = None):
    """Open *path* right-way-up, no larger than *target* if one is given.

    A raw file is served from its embedded preview, so a raw library browses at
    ordinary speed with no raw decoder installed.
    """
    _Image, ImageOps = _pillow()
    path = Path(path)
    if mediatypes.is_raw(path):
        blob = extract_embedded_jpeg(path)
        if blob:
            import io
            try:
                return _orient_raw_preview(path, _open_drafted(io.BytesIO(blob), target), ImageOps)
            except Exception:
                pass
    return ImageOps.exif_transpose(_open_drafted(path, target))


def _orient_raw_preview(path: Path, image, image_ops):
    if image.getexif().get(0x112):
        return image_ops.exif_transpose(image)
    from . import metadata  # noqa: PLC0415 - avoids a metadata/imaging import cycle

    orientation = metadata.read(path).orientation
    from PIL import Image  # noqa: PLC0415

    transforms = {
        2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    return image.transpose(transforms[orientation]) if orientation in transforms else image


def thumbnail(path: str | Path, size: tuple[int, int] = (320, 320)):
    """A right-way-up thumbnail no larger than *size*."""
    image = open_image(path, size)
    image.thumbnail(size)
    return image


def _gray_array(path: str | Path, side: int) -> np.ndarray | None:
    """A small square of luminance, decoded as cheaply as the format allows.

    ``draft("L", ...)`` asks libjpeg for grayscale output at a reduced scale,
    which skips both most of the IDCT work and the YCbCr-to-RGB conversion.
    """
    Image, ImageOps = _pillow()
    source = Path(path)
    try:
        if mediatypes.is_raw(source):
            blob = extract_embedded_jpeg(source)
            if not blob:
                return None
            import io as _io
            handle = Image.open(_io.BytesIO(blob))
        else:
            handle = Image.open(source)
        try:
            handle.draft("L", (side, side))
        except (AttributeError, ValueError, OSError):
            pass
        handle.load()
        image = (_orient_raw_preview(source, handle, ImageOps)
                 if mediatypes.is_raw(source) else ImageOps.exif_transpose(handle))
        if image.mode.startswith("I;16") or image.mode in ("I", "F"):
            values = np.asarray(image, dtype=np.float64)
            finite = np.isfinite(values)
            if values.size and finite.any():
                low, high = float(values[finite].min()), float(values[finite].max())
                scaled = np.zeros_like(values)
                if high > low:
                    scaled[finite] = (values[finite] - low) * (255.0 / (high - low))
                values = scaled
            else:
                values = np.zeros_like(values)
            image = Image.fromarray(np.clip(np.rint(values), 0, 255).astype(np.uint8), "L")
        if image.mode != "L":
            image = image.convert("L")
        image = image.resize((side, side), Image.Resampling.LANCZOS)
    except Exception as error:
        log.debug("cannot rasterise %s: %s", path, error)
        return None
    return np.asarray(image, dtype=np.float64)


def _dct_matrix(size: int) -> np.ndarray:
    grid = np.arange(size)
    matrix = np.cos(math.pi / size * (grid[:, None] + 0.5) * grid[None, :])
    matrix[:, 0] *= 1 / math.sqrt(2)
    return matrix * math.sqrt(2 / size)


@lru_cache(maxsize=4)
def _dct(size: int) -> np.ndarray:
    return _dct_matrix(size)


def phash(path: str | Path) -> int | None:
    """64-bit perceptual hash: DCT of a 32x32 grey image, low frequencies only."""
    pixels = _gray_array(path, _DCT_SIZE)
    if pixels is None:
        return None
    basis = _dct(_DCT_SIZE)
    transformed = basis.T @ pixels @ basis
    block = transformed[:_HASH_SIDE, :_HASH_SIDE].flatten()
    # Drop the DC term: it only reports overall brightness.
    median = np.median(block[1:])
    bits = block > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def dhash(path: str | Path) -> int | None:
    """64-bit difference hash: cheaper than pHash, good at catching crops."""
    pixels = _gray_array(path, _HASH_SIDE + 1)
    if pixels is None:
        return None
    diff = pixels[:_HASH_SIDE, 1:] > pixels[:_HASH_SIDE, :-1]
    value = 0
    for bit in diff.flatten():
        value = (value << 1) | int(bit)
    return value


def hamming(left: int, right: int) -> int:
    return int(left ^ right).bit_count()


def similarity(left: int | None, right: int | None) -> float:
    """1.0 for identical hashes, 0.0 for maximally different."""
    if left is None or right is None:
        return 0.0
    return 1.0 - hamming(left, right) / HASH_BITS


def sharpness(path: str | Path, side: int = 256) -> float:
    """Variance of the Laplacian: higher is crisper.

    Comparable only between frames of the same scene, which is exactly how it
    is used — picking the best frame of one burst.
    """
    pixels = _gray_array(path, side)
    if pixels is None:
        return 0.0
    laplace = (
        -4.0 * pixels[1:-1, 1:-1]
        + pixels[:-2, 1:-1] + pixels[2:, 1:-1]
        + pixels[1:-1, :-2] + pixels[1:-1, 2:]
    )
    return float(np.var(laplace))


def average_brightness(path: str | Path, side: int = 64) -> float:
    pixels = _gray_array(path, side)
    return float(np.mean(pixels)) if pixels is not None else 0.0


def quality_score(path: str | Path) -> dict:
    """Focus and exposure in one pass, for the 'which frame to keep' decision."""
    pixels = _gray_array(path, 256)
    if pixels is None:
        return {"sharpness": 0.0, "brightness": 0.0, "blown": 0.0, "crushed": 0.0}
    laplace = (
        -4.0 * pixels[1:-1, 1:-1]
        + pixels[:-2, 1:-1] + pixels[2:, 1:-1]
        + pixels[1:-1, :-2] + pixels[1:-1, 2:]
    )
    total = pixels.size
    return {
        "sharpness": float(np.var(laplace)),
        "brightness": float(np.mean(pixels)),
        "blown": float(np.count_nonzero(pixels >= 253) / total),
        "crushed": float(np.count_nonzero(pixels <= 2) / total),
    }


def looks_unusable(score: dict, sharp_floor: float = 12.0) -> str:
    """Name the obvious defect in *score*, or an empty string."""
    if score.get("blown", 0) > 0.55:
        return "overexposed"
    if score.get("crushed", 0) > 0.75:
        return "black"
    if score.get("sharpness", 0) < sharp_floor:
        return "blurry"
    return ""
