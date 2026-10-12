"""Frame-accurate video decoding, when PyAV is installed.

Every entry point returns None rather than raising when PyAV is absent, so the
rest of the program degrades to "no video thumbnail, no frame stepping" instead
of failing to start.
"""
from __future__ import annotations

import math
import struct
import threading
from pathlib import Path

from .logsetup import get_logger

log = get_logger("video")
_CHECKED = False
_AV = None
_AV_LOCK = threading.Lock()


def _av():
    global _CHECKED, _AV
    if not _CHECKED:
        with _AV_LOCK:
            if not _CHECKED:
                try:
                    import av  # type: ignore
                    _AV = av
                except Exception:
                    _AV = None
                _CHECKED = True
    return _AV


def available() -> bool:
    return _av() is not None


def _upright(frame) -> list:
    """The turns and mirrors that show *frame* the way it was recorded.

    A phone held upright stores its frames on their side, with a display matrix
    that turns them. PyAV's ``rotation`` reads only the turn, and a mirrored
    matrix -- some front cameras and editing apps write one -- comes out of it
    as half a turn, so a mirrored clip stood upside down. The matrix is read
    the way ffmpeg's own autorotate reads it: the angle first, then the signs
    that say whether it also mirrors.
    """
    from PIL import Image  # noqa: PLC0415 - PyAV already needs Pillow here
    turn = Image.Transpose
    matrix = None
    try:
        for data in frame.side_data:
            if "DISPLAYMATRIX" in str(data.type).upper():
                matrix = struct.unpack("<9i", bytes(data)[:36])
                break
    except (AttributeError, TypeError, ValueError, struct.error):
        matrix = None
    try:
        if matrix is None:
            theta = round(-float(getattr(frame, "rotation", 0) or 0)) % 360
            return {90: [turn.ROTATE_270], 180: [turn.ROTATE_180],
                    270: [turn.ROTATE_90]}.get(theta, [])
        theta = round(math.degrees(math.atan2(matrix[1], matrix[0]))) % 360
    except (TypeError, ValueError, OverflowError):
        return []
    a, c, d = matrix[0], matrix[3], matrix[4]
    if theta == 90:
        return [turn.TRANSPOSE if c > 0 else turn.ROTATE_270]
    if theta == 180:
        return ([turn.FLIP_LEFT_RIGHT] if a < 0 else []) + ([turn.FLIP_TOP_BOTTOM] if d < 0 else [])
    if theta == 270:
        return [turn.TRANSVERSE if c < 0 else turn.ROTATE_90]
    if theta == 0 and d < 0:
        return [turn.FLIP_TOP_BOTTOM]
    return []


def _to_pil(frame, max_width: int = 2560):
    width = min(max_width, frame.width)
    height = max(1, round(frame.height * width / frame.width))
    image = frame.reformat(width=width, height=height, format="rgb24").to_image()
    for step in _upright(frame):
        image = image.transpose(step)
    return image


def frame_at(path: str | Path, position_seconds: float, direction: int = 1,
             max_width: int = 2560):
    """The frame next to *position_seconds*, with its presentation timestamp.

    Decoding by presentation timestamp rather than stepping a fixed 40 ms is
    what makes this correct for variable frame rate footage and for B frames.
    Returns ``(image, timestamp)`` or None.
    """
    av = _av()
    if av is None:
        return None
    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            base = float((stream.start_time or 0) * stream.time_base)
            target = max(0.0, position_seconds) + base
            for window in (2.0, 10.0, target + 1.0):
                seek_time = max(base, target - window)
                container.seek(int(seek_time / float(stream.time_base)), stream=stream,
                               backward=True, any_frame=False)
                previous = None
                first = None
                last = None
                for frame in container.decode(stream):
                    if frame.pts is None:
                        continue
                    timestamp = float(frame.pts * stream.time_base)
                    first = first or (frame, timestamp)
                    last = (frame, timestamp)
                    if direction > 0:
                        if timestamp > target + 1e-5:
                            return _to_pil(frame, max_width), timestamp - base
                    else:
                        if timestamp >= target - 1e-5:
                            if previous:
                                return _to_pil(previous[0], max_width), previous[1] - base
                            break
                        previous = (frame, timestamp)
                if direction > 0 and last:
                    return _to_pil(last[0], max_width), last[1] - base
                if direction < 0 and previous:
                    return _to_pil(previous[0], max_width), previous[1] - base
                if seek_time <= base:
                    selected = previous or first
                    if selected:
                        return _to_pil(selected[0], max_width), selected[1] - base
                    break
    except Exception as error:
        log.debug("frame decode failed for %s: %s", path, error)
    return None


def cover(path: str | Path, max_width: int = 1280):
    """A poster frame for the thumbnail grid, or None."""
    result = frame_at(path, 0.0, -1, max_width)
    return result[0] if result else None
