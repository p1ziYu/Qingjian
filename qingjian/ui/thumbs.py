"""Background thumbnail production with a bounded cache and a bounded queue.

Two rules keep a large folder from bringing the window down:

* only what the viewport can actually show is ever asked for, and
* a request that scrolls out of view before a worker reaches it is dropped.

Without the second rule a fast scroll through ten thousand photographs leaves
ten thousand decodes queued behind the ones the user is looking at now.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

from ..core import imaging, mediatypes, metadata, video
from ..core.logsetup import get_logger
from . import icons, theme

log = get_logger("thumbs")

#: Decoding is memory-hungry, so a handful of workers beats one per core.
MAX_WORKERS = max(2, min(4, (os.cpu_count() or 4) // 2))


def pil_to_qimage(image) -> QImage:
    """Convert a Pillow image without going through a temporary file."""
    converted = image.convert("RGBA")
    data = converted.tobytes("raw", "RGBA")
    result = QImage(data, converted.width, converted.height,
                    converted.width * 4, QImage.Format.Format_RGBA8888)
    # tobytes gives a temporary buffer; copy so the QImage owns its pixels.
    return result.copy()


def placeholder(size: QSize, kind: str, caption: str = "") -> QPixmap:
    """A drawn stand-in for anything that cannot be rasterised here."""
    pixmap = QPixmap(size)
    pixmap.fill(QColor(theme.MAT))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.fillRect(pixmap.rect(), QColor(theme.RAISED))
    name = {mediatypes.KIND_VIDEO: "film", mediatypes.KIND_RAW: "camera"}.get(kind, "info")
    glyph = icons.pixmap(name, min(40, max(12, size.height() // 2)), theme.LINE_CONTROL)
    painter.drawPixmap((size.width() - glyph.width()) // 2,
                       (size.height() - glyph.height()) // 2 - (8 if caption else 0), glyph)
    if caption:
        painter.setPen(QColor(theme.FAINT))
        font = painter.font()
        font.setPointSizeF(max(6.0, size.height() * 0.09))
        painter.setFont(font)
        painter.drawText(pixmap.rect().adjusted(4, 0, -4, -8),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, caption)
    painter.end()
    return pixmap


class _Signals(QObject):
    ready = Signal(object, int, object)


class _Task(QRunnable):
    """Decodes one thumbnail, unless the view has moved on by the time it runs."""

    def __init__(self, path: Path, edge: int, key: tuple, signals: _Signals,
                 still_wanted) -> None:
        super().__init__()
        self.path = path
        self.edge = edge
        self.key = key
        self.signals = signals
        self.still_wanted = still_wanted

    def run(self) -> None:                      # noqa: D401 - Qt entry point
        if not self.still_wanted(str(self.path), self.edge):
            self.signals.ready.emit(self.key, -self.edge, None)
            return
        image = None
        try:
            if mediatypes.is_video(self.path):
                if video.available():
                    frame = video.cover(self.path, self.edge * 2)
                    if frame is not None:
                        frame.thumbnail((self.edge, self.edge))
                        image = pil_to_qimage(frame)
            else:
                thumb = imaging.thumbnail(self.path, (self.edge, self.edge))
                image = pil_to_qimage(thumb)
        except Exception as error:
            log.debug("thumbnail failed for %s: %s", self.path, error)
        self.signals.ready.emit(self.key, self.edge, image)


class ThumbnailCache(QObject):
    """Serves from memory when it can, decodes off the interface thread when it
    must, and forgets anything nobody is looking at any more."""

    ready = Signal(str, int, QPixmap)
    #: A queued decode was abandoned because the view had scrolled past it.
    #: Views listen so they can ask again for anything still on screen; without
    #: this a tile scrolled away from and back to could stay blank for good.
    dropped = Signal(int)

    def __init__(self, budget_mb: int = 512, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._pending: set[tuple] = set()
        self._wanted: set[tuple[str, int]] = set()
        self._lock = threading.RLock()
        self._budget = max(32, budget_mb) * 1024 * 1024
        self._bytes = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_WORKERS)
        self._signals = _Signals()
        self._signals.ready.connect(self._store)

    # -- keys ----------------------------------------------------------
    @staticmethod
    def _key(path: Path, edge: int) -> tuple:
        try:
            stat = Path(path).stat()
            return (str(path), edge, stat.st_size, stat.st_mtime_ns)
        except OSError:
            return (str(path), edge, 0, 0)

    def _is_wanted(self, path: str, edge: int) -> bool:
        with self._lock:
            return (path, edge) in self._wanted

    # -- interest ------------------------------------------------------
    def set_wanted(self, paths, edge: int) -> None:
        """Declare the only thumbnails worth decoding at *edge* right now.

        Scoped to one size because the filmstrip and the grid share a cache at
        different sizes; replacing the whole set would let whichever view ran
        last cancel the other's work.
        """
        with self._lock:
            self._wanted = {entry for entry in self._wanted if entry[1] != edge}
            self._wanted.update((str(p), edge) for p in paths)

    # -- access --------------------------------------------------------
    def peek(self, path: str | Path, edge: int) -> QPixmap | None:
        key = self._key(Path(path), edge)
        pixmap = self._cache.get(key)
        if pixmap is not None:
            self._cache.move_to_end(key)
        return pixmap

    def request(self, path: str | Path, edge: int) -> QPixmap | None:
        """Return the thumbnail now, or start producing it and return None."""
        target = Path(path)
        key = self._key(target, edge)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if key in self._pending:
            return None
        self._pending.add(key)
        with self._lock:
            self._wanted.add((str(target), edge))
        self._pool.start(_Task(target, edge, key, self._signals, self._is_wanted))
        return None

    def _store(self, key: tuple, edge: int, image) -> None:
        self._pending.discard(key)
        if edge < 0:
            # The task was dropped because the view had scrolled past it.
            self.dropped.emit(-edge)
            return
        target = Path(key[0])
        if image is None or (hasattr(image, "isNull") and image.isNull()):
            info = metadata.read(target)
            caption = metadata.format_duration(info.duration) if info.duration else ""
            pixmap = placeholder(QSize(edge, edge), mediatypes.kind(target), caption)
        else:
            pixmap = QPixmap.fromImage(image)
        self._cache[key] = pixmap
        self._bytes += pixmap.width() * pixmap.height() * 4
        while self._bytes > self._budget and len(self._cache) > 1:
            _old_key, old = self._cache.popitem(last=False)
            self._bytes -= old.width() * old.height() * 4
        self.ready.emit(str(target), edge, pixmap)

    # -- housekeeping --------------------------------------------------
    def pending(self) -> int:
        return len(self._pending)

    def clear(self) -> None:
        self._cache.clear()
        self._pending.clear()
        self._bytes = 0
        with self._lock:
            self._wanted.clear()

    def shutdown(self) -> None:
        with self._lock:
            self._wanted.clear()
        self._pool.clear()
        self._pool.waitForDone(3000)
