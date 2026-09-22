"""One view of a media file's metadata, whatever the format.

Ordinary photographs go through Pillow, camera raw through the small TIFF
reader, video through PyAV when it is installed and the ISO base-media header
otherwise. Every path degrades to "we could not tell" rather than raising.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import exifread, isobmff, mediatypes
from .logsetup import get_logger

log = get_logger("metadata")

_CACHE: "OrderedDict[tuple, MediaInfo]" = OrderedDict()
#: The duplicate scan reads headers on a pool of worker threads while the
#: interface reads them too, and undo drops entries from under both. A bare
#: dict raised "mutated during iteration" there.
_CACHE_LOCK = threading.Lock()
#: Big enough that a full pass over a large folder does not evict its own
#: earlier entries. Each record is small; twenty thousand cost a few megabytes.
CACHE_LIMIT = 24576

_DATE_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S")


def _pillow():
    try:
        from PIL import Image, ExifTags  # type: ignore
        return Image, ExifTags
    except Exception:  # pragma: no cover - Pillow is a hard requirement in practice
        return None, None


def _av():
    try:
        import av  # type: ignore
        return av
    except Exception:
        return None


def _rational(value) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    # Pillow hands back an IFDRational, which is a Fraction.
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    if numerator is not None and denominator:
        return int(numerator), int(denominator)
    try:
        return int(value), 1
    except (TypeError, ValueError):
        return None


def format_aperture(value) -> str:
    pair = _rational(value)
    if not pair or not pair[1]:
        return ""
    return f"f/{pair[0] / pair[1]:g}"


def format_shutter(value) -> str:
    pair = _rational(value)
    if not pair or not pair[1]:
        return ""
    num, den = pair
    if num == 0:
        return ""
    seconds = num / den
    if seconds >= 1:
        return f"{seconds:g}s"
    return f"1/{round(den / max(1, num))}"


def format_focal(value) -> str:
    pair = _rational(value)
    if not pair or not pair[1]:
        return ""
    return f"{pair[0] / pair[1]:g}mm"


def format_duration(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return ""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _parse_datetime(text: str, offset: str = "") -> datetime | None:
    value = str(text or "").strip()
    if not value or value.startswith("0000"):
        return None
    value = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
        return parsed
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


@dataclass
class MediaInfo:
    path: Path
    kind: str = mediatypes.KIND_OTHER
    size: int = 0
    mtime: float = 0.0
    width: int = 0
    height: int = 0
    fmt: str = ""
    duration: float | None = None
    codec: str = ""
    framerate: str = ""
    frames: int = 1
    camera: str = ""
    lens: str = ""
    iso: str = ""
    aperture: str = ""
    shutter: str = ""
    focal: str = ""
    orientation: int = 1
    rotation: int = 0
    captured: datetime | None = None
    captured_is_fallback: bool = True
    error: str = ""
    raw_tags: dict = field(default_factory=dict)

    @property
    def is_landscape(self) -> bool:
        return bool(self.display_width and self.display_height) and self.display_width > self.display_height

    @property
    def is_portrait(self) -> bool:
        return bool(self.display_width and self.display_height) and self.display_height > self.display_width

    @property
    def is_square(self) -> bool:
        return bool(self.display_width) and self.display_width == self.display_height

    @property
    def display_width(self) -> int:
        return self.height if self.orientation in (5, 6, 7, 8) or self.rotation % 180 else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.orientation in (5, 6, 7, 8) or self.rotation % 180 else self.height

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000 if self.width and self.height else 0.0

    def when(self) -> datetime:
        if self.captured is not None:
            return self.captured
        try:
            return datetime.fromtimestamp(self.mtime or 0)
        except (OSError, OverflowError, ValueError):
            return datetime(1970, 1, 1) + timedelta(seconds=self.mtime or 0)

    def timestamp(self) -> float:
        when = self.when()
        try:
            return when.timestamp()
        except (OSError, OverflowError, ValueError):
            return (when - datetime(1970, 1, 1)).total_seconds()


def _apply_exif(info: MediaInfo, tags: dict) -> None:
    make = str(tags.get("Make") or "").strip()
    model = str(tags.get("Model") or "").strip()
    if model.lower().startswith(make.lower()) and make:
        info.camera = model
    else:
        info.camera = (make + " " + model).strip()
    info.lens = str(tags.get("LensModel") or "").strip()
    iso = tags.get("ISOSpeedRatings") or tags.get("PhotographicSensitivity")
    if isinstance(iso, (list, tuple)):
        iso = iso[0] if iso else None
    info.iso = str(int(iso)) if isinstance(iso, (int, float)) else ""
    info.aperture = format_aperture(tags.get("FNumber"))
    info.shutter = format_shutter(tags.get("ExposureTime"))
    info.focal = format_focal(tags.get("FocalLength"))
    try:
        info.orientation = int(tags.get("Orientation") or 1)
    except (TypeError, ValueError):
        info.orientation = 1
    width = tags.get("PixelXDimension") or tags.get("ImageWidth")
    height = tags.get("PixelYDimension") or tags.get("ImageLength")
    if not info.width and isinstance(width, int):
        info.width = width
    if not info.height and isinstance(height, int):
        info.height = height
    offset = str(tags.get("OffsetTimeOriginal") or tags.get("OffsetTime") or "")
    for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        parsed = _parse_datetime(tags.get(key, ""), offset)
        if parsed:
            info.captured = parsed
            info.captured_is_fallback = False
            break
    info.raw_tags = {k: v for k, v in tags.items() if not k.startswith("_")}


def _read_image(info: MediaInfo) -> None:
    Image, ExifTags = _pillow()
    if Image is None:
        return
    try:
        with Image.open(info.path) as image:
            info.width, info.height = image.size
            info.fmt = image.format or ""
            info.frames = getattr(image, "n_frames", 1)
            try:
                exif = image.getexif() if image.format != "PNG" or "exif" in image.info else {}
                tags = {ExifTags.TAGS.get(t, str(t)): v for t, v in dict(exif).items()}
                try:
                    inner = exif.get_ifd(0x8769)
                    tags.update({ExifTags.TAGS.get(t, str(t)): v for t, v in inner.items()})
                except (KeyError, ValueError, TypeError, AttributeError):
                    pass
            except Exception:
                tags = {}
        if tags:
            _apply_exif(info, tags)
    except Exception as error:
        info.error = str(error)


def _read_raw(info: MediaInfo) -> None:
    tags = exifread.read_tiff_exif(info.path)
    if tags:
        _apply_exif(info, tags)
        info.fmt = info.path.suffix.lstrip(".").upper()
        return
    # CR3 and HEIF-based raws are ISO base media, not TIFF.
    header = isobmff.read_header(info.path)
    if header:
        info.fmt = str(header.get("brand") or info.path.suffix.lstrip(".").upper())
        info.width = int(header.get("width") or 0)
        info.height = int(header.get("height") or 0)
        created = header.get("created")
        if isinstance(created, datetime):
            info.captured = created.astimezone().replace(tzinfo=None)
            info.captured_is_fallback = False
        return
    # Last resort: Pillow may still know the format (DNG previews, for example).
    _read_image(info)


def _read_video(info: MediaInfo) -> None:
    av = _av()
    if av is not None:
        try:
            with av.open(str(info.path)) as container:
                stream = container.streams.video[0]
                if stream.duration and stream.time_base:
                    info.duration = float(stream.duration * stream.time_base)
                elif container.duration:
                    info.duration = float(container.duration / av.time_base)
                info.width = int(stream.width or 0)
                info.height = int(stream.height or 0)
                info.codec = str(stream.codec_context.name or "")
                info.framerate = str(stream.average_rate or "")
                header = isobmff.read_header(info.path)
                info.rotation = int(header.get("rotation") or 0)
                created = container.metadata.get("creation_time") or \
                    stream.metadata.get("creation_time") or ""
                parsed = _parse_datetime(created)
                if parsed:
                    if parsed.tzinfo is not None:
                        try:
                            parsed = parsed.astimezone().replace(tzinfo=None)
                        except (OSError, OverflowError, ValueError):
                            parsed = parsed.replace(tzinfo=None)
                    info.captured = parsed
                    info.captured_is_fallback = False
                return
        except Exception as error:
            info.error = str(error)
    header = isobmff.read_header(info.path)
    if header:
        info.fmt = str(header.get("brand") or "")
        duration = header.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            info.duration = float(duration)
        info.width = int(header.get("width") or info.width)
        info.height = int(header.get("height") or info.height)
        info.rotation = int(header.get("rotation") or 0)
        created = header.get("created")
        if isinstance(created, datetime):
            info.captured = created.astimezone().replace(tzinfo=None)
            info.captured_is_fallback = False
            info.error = ""


def read(path: str | Path, use_cache: bool = True) -> MediaInfo:
    """Everything known about *path*. Cached by (path, size, mtime)."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError as error:
        return MediaInfo(path=path, error=str(error))
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                return cached

    info = MediaInfo(path=path, kind=mediatypes.kind(path), size=stat.st_size, mtime=stat.st_mtime)
    try:
        if info.kind == mediatypes.KIND_VIDEO:
            _read_video(info)
        elif info.kind == mediatypes.KIND_RAW:
            _read_raw(info)
        elif info.kind == mediatypes.KIND_IMAGE:
            _read_image(info)
    except Exception as error:  # pragma: no cover - the readers already guard
        log.debug("metadata failed for %s: %s", path, error)
        info.error = str(error)

    if info.captured is None:
        try:
            info.captured = datetime.fromtimestamp(stat.st_mtime)
        except (OSError, OverflowError, ValueError):
            info.captured = datetime(1970, 1, 1) + timedelta(seconds=stat.st_mtime)
        info.captured_is_fallback = True

    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = info
            while len(_CACHE) > CACHE_LIMIT:
                _CACHE.popitem(last=False)
    return info


#: EXIF tag numbers, in the order `_apply_exif` prefers them.
_DATE_TAGS = (36867, 36868, 306)          # Original, Digitized, DateTime
_EXIF_IFD = 0x8769


def capture_only(path: str | Path) -> float | None:
    """The capture time alone, without reading the rest of the metadata.

    Sorting a folder by date needs one timestamp per file and nothing else.
    :func:`read` builds a named dictionary of every EXIF tag and then copies it
    again into ``raw_tags``, which is three quarters of its cost -- five and a
    half seconds for twenty thousand photographs. This reads the three date
    tags by number and stops.

    Returns None when this file needs the full reader (raw and video go through
    their own parsers), so the caller falls back to :func:`capture_time`.
    """
    source = Path(path)
    if mediatypes.kind(source) != mediatypes.KIND_IMAGE:
        return None
    Image, _ = _pillow()
    if Image is None:
        return None
    try:
        with Image.open(source) as image:
            if image.format == "PNG" and "exif" not in image.info:
                return source.stat().st_mtime
            exif = image.getexif()
            inner = {}
            try:
                inner = exif.get_ifd(_EXIF_IFD)
            except (KeyError, ValueError, TypeError, AttributeError):
                pass
            for tag in _DATE_TAGS:
                # The Exif IFD wins, the same way the merged dict does.
                for source_tags in (inner, exif):
                    parsed = _parse_datetime(str(source_tags.get(tag) or ""))
                    if parsed:
                        return parsed.timestamp()
    except Exception:                     # noqa: BLE001 - any unreadable file
        return None
    try:
        return source.stat().st_mtime
    except OSError:
        return None


def capture_time(path: str | Path) -> float:
    """Epoch seconds used for date sorting; the mtime when nothing better exists."""
    info = read(path)
    when = info.captured or datetime.fromtimestamp(info.mtime)
    try:
        return when.timestamp()
    except (OverflowError, OSError, ValueError):
        return info.mtime


def duration_seconds(path: str | Path) -> float | None:
    return read(path).duration


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def forget(paths) -> None:
    """Drop just these files from the cache.

    Undo and redo used to clear the whole cache, which meant the next sort
    re-parsed every file in the folder to learn a capture time it already knew.
    """
    wanted = {str(p) for p in paths}
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if k and str(k[0]) in wanted]:
            _CACHE.pop(key, None)


def utc_now() -> datetime:  # pragma: no cover - trivial helper used by callers
    return datetime.now(timezone.utc)
