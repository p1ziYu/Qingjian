"""Frame-accurate video decoding, when PyAV is installed.

Every entry point returns None rather than raising when PyAV is absent, so the
rest of the program degrades to "no video thumbnail, no frame stepping" instead
of failing to start.
"""
from __future__ import annotations

from pathlib import Path
import threading

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


def _to_pil(frame, max_width: int = 2560):
    width = min(max_width, frame.width)
    height = max(1, round(frame.height * width / frame.width))
    return frame.reformat(width=width, height=height, format="rgb24").to_image()


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
