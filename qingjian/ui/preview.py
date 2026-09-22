"""The preview pane: stills, animations and video in one stack."""
from __future__ import annotations

from pathlib import Path
import threading

from collections import OrderedDict

from PySide6.QtCore import (QObject, QRect, QRectF, QRunnable, QSize, Qt, QThreadPool, QUrl,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QImage, QImageReader, QMovie, QPainter, QPixmap,
                           QTransform)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QComboBox, QFrame, QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QSlider, QStackedWidget, QToolButton, QVBoxLayout,
                               QWidget)

from ..core import imaging, mediatypes, video
from ..core.i18n import tr
from ..core.logsetup import get_logger
from . import icons, theme
from .thumbs import pil_to_qimage

log = get_logger("preview")

#: Formats Qt itself can decode. Anything else (raw, and often HEIC or JXL)
#: goes through Pillow, which can also lift a raw file's embedded preview.
_QT_READABLE = {bytes(fmt).decode("ascii", "ignore").lower()
                for fmt in QImageReader.supportedImageFormats()}


def decode_qimage(path: str | Path, target: QSize) -> tuple[QImage, str]:
    """Decode *path* at no more than *target*, as cheaply as the format allows.

    Safe to call from a worker thread: it produces a ``QImage``, never a
    ``QPixmap``. ``setScaledSize`` lets Qt's JPEG reader scale straight out of
    the DCT coefficients, which is the reason a full-size photograph appears
    the instant it is asked for rather than a third of a second later.
    """
    target_path = Path(path)
    suffix = target_path.suffix.lstrip(".").lower()
    error = ""
    if suffix in _QT_READABLE and not mediatypes.is_raw(target_path):
        reader = QImageReader(str(target_path))
        reader.setAutoTransform(True)
        original = reader.size()
        if original.isValid() and (original.width() > target.width()
                                   or original.height() > target.height()):
            reader.setScaledSize(original.scaled(target, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if not image.isNull():
            return image, ""
        error = reader.errorString()
    try:
        wanted = (max(target.width(), 640), max(target.height(), 640))
        # A raw preview or a 24 MP HEIC does not need decoding in full to fill
        # a 1400 px pane, so the request is passed down to the decoder.
        pil = imaging.open_image(target_path, wanted)
        pil.thumbnail(wanted)
        return pil_to_qimage(pil), ""
    except Exception as pillow_error:
        return QImage(), error or str(pillow_error)


def decode_pixmap(path: str | Path, target: QSize) -> tuple[QPixmap, str]:
    """Decode *path* no larger than needed. Interface thread only."""
    image, error = decode_qimage(path, target)
    return (QPixmap(), error) if image.isNull() else (QPixmap.fromImage(image), "")


class _PreloadSignals(QObject):
    ready = Signal(object, int, object, bool)


class _PreloadTask(QRunnable):
    def __init__(self, key: tuple, path: Path, target: QSize,
                 signals: _PreloadSignals, generation: int, still_wanted) -> None:
        super().__init__()
        self.key = key
        self.path = path
        self.target = target
        self.signals = signals
        self.generation = generation
        self.still_wanted = still_wanted

    def run(self) -> None:                       # noqa: D401 - Qt entry point
        # The key travels with the task: the pane can be resized while a decode
        # is in flight, and two sizes of one file must not be filed under each
        # other's key.
        if not self.still_wanted(self.key, self.generation):
            self.signals.ready.emit(self.key, self.generation, None, False)
            return
        # Stat on the worker, never in set_wanted on the interface thread.
        # A queued task may now point to a newer file at the same path.
        if self.key != PreviewPrefetcher._key(self.path, self.target):
            self.signals.ready.emit(self.key, self.generation, None, False)
            return
        image, _error = decode_qimage(self.path, self.target)
        if self.key != PreviewPrefetcher._key(self.path, self.target):
            self.signals.ready.emit(self.key, self.generation, None, False)
            return
        self.signals.ready.emit(self.key, self.generation,
                                None if image.isNull() else image, True)


class PreviewPrefetcher(QObject):
    """Decodes the neighbouring items so that the next key press is instant.

    The previous version did this and it is most of what made the program feel
    light; the rewrite lost it. Only a couple of frames are ever held, so the
    memory cost is small and bounded.
    """

    #: Emitted with a path string once its image is in the cache.
    arrived = Signal(str)

    def __init__(self, depth: int = 4, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: "OrderedDict[tuple, QImage]" = OrderedDict()
        self._pending: set[tuple] = set()
        # Interest tracks names and viewport size. Full cache keys below still
        # include file identity, but recomputing four stats on every view refresh
        # duplicates the stats done by take/request on the same paths.
        self._wanted: set[tuple[str, int, int]] = set()
        self._generation = 0
        self._lock = threading.RLock()
        self._depth = max(2, depth)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = _PreloadSignals()
        self._signals.ready.connect(self._store)

    @staticmethod
    def _key(path: Path, target: QSize) -> tuple:
        try:
            stat = Path(path).stat()
        except OSError:
            return (str(path), target.width(), target.height(), 0, 0)
        return (str(path), target.width(), target.height(), stat.st_size,
                stat.st_mtime_ns)

    def take(self, path: str | Path, target: QSize) -> QPixmap | None:
        """A decoded neighbour, if one was ready. Interface thread only."""
        image = self._cache.get(self._key(Path(path), target))
        if image is None or image.isNull():
            return None
        return QPixmap.fromImage(image)

    def prefetch(self, paths, target: QSize) -> None:
        for path in list(paths)[:self._depth]:
            self.request(path, target, priority=0)

    def set_wanted(self, paths, target: QSize) -> None:
        keys = {(str(path), target.width(), target.height()) for path in paths}
        with self._lock:
            self._wanted = keys
        # Old queued tasks are cheap to discard in their run method; clearing
        # the pool here would also discard current requests queued just before it.

    def _still_wanted(self, key: tuple, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and key[:3] in self._wanted

    def request(self, path: str | Path, target: QSize, priority: int = 1) -> bool:
        """Decode *path* on a worker. True when it was queued or already in hand.

        A priority above zero jumps the neighbours already queued, which is what
        the file actually on screen needs.
        """
        candidate = Path(path)
        if mediatypes.is_video(candidate):
            return False
        key = self._key(candidate, target)
        if key in self._cache:
            return True
        if key in self._pending:
            return True
        self._pending.add(key)
        with self._lock:
            self._wanted.add(key[:3])
            generation = self._generation
        self._pool.start(_PreloadTask(key, candidate, target, self._signals,
                                      generation, self._still_wanted), priority)
        return True

    def _store(self, key, generation, image, current) -> None:
        if generation != self._generation:
            return
        self._pending.discard(key)
        if not current or not self._still_wanted(key, generation):
            return
        if image is not None:
            self._cache[key] = image
        while len(self._cache) > self._depth:
            self._cache.popitem(last=False)
        self.arrived.emit(key[0])

    def clear(self) -> None:
        """Forget everything, including work still in flight for the old folder."""
        # Dropping the bookkeeping alone left the previous folder's decodes
        # queued: they still ran, still landed in the cache and still evicted
        # the file now on screen.
        with self._lock:
            self._generation += 1
            self._wanted.clear()
        self._pool.clear()
        self._cache.clear()
        self._pending.clear()

    def shutdown(self) -> None:
        self._pool.clear()
        self._pool.waitForDone(2000)


#: Paper border as a share of the photograph's shorter side.
_BORDER_SHARE = 0.018
#: The print's shadow: (spread, drop, alpha) per layer, in border widths. Kept
#: tight: every border width of shadow that has to fit is taken off the photo.
_SHADOW = ((0.3, 0.5, 72), (0.7, 0.9, 40), (1.1, 1.3, 18))


class ImageSurface(QGraphicsView):
    """A print on the counter. Zoom with the wheel, pan by dragging, double-click to fit.

    The paper border and the shadow are scene items sized from the picture, so
    they scale with it as a real print would when it is brought closer.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        # Five items that move together on every flip: a spatial index is overhead.
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
        self.setScene(self._scene)
        # The paper border and the shadow are thin rectangles round the
        # picture, never under it: whole rectangles under the photo got filled
        # and blended on every flip only to be covered by the picture again.
        colours = [QColor(0, 0, 0, alpha) for _spread, _drop, alpha in _SHADOW for _side in "lrb"]
        colours += [QColor(theme.PAPER)] * 4
        self._bands: list[QGraphicsRectItem] = []
        for colour in colours:
            band = QGraphicsRectItem()
            band.setPen(Qt.PenStyle.NoPen)
            band.setBrush(QBrush(colour))
            band.setVisible(False)
            self._scene.addItem(band)
            self._bands.append(band)
        #: The picture size the bands were last laid out for; most flips reuse them.
        self._band_size = QSize()
        self._band_border = 0.0
        self._paper_rect = QRectF()
        #: Set when the user zooms or drags, so the next picture is fitted again.
        self._touched = False
        self._fitted_view = QSize()
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)
        self._frame = QRectF()
        self._original = QPixmap()
        self._rotation = 0
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(f"background: {theme.MAT}; border: none;")

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._original = pixmap
        self._rotation = 0
        self._apply()

    def clear_media(self) -> None:
        self._original = QPixmap()
        self._item.setPixmap(QPixmap())
        for band in self._bands:
            band.setVisible(False)
        self._band_size = QSize()
        self._band_border = 0.0
        self._frame = QRectF()
        self._scene.setSceneRect(0, 0, 1, 1)

    def _apply(self) -> None:
        pixmap = self._original
        if self._rotation:
            pixmap = pixmap.transformed(QTransform().rotate(self._rotation),
                                        Qt.TransformationMode.SmoothTransformation)
        self._item.setPixmap(pixmap)
        picture = self._item.boundingRect()
        natural_border = max(2.0, round(min(picture.width(), picture.height()) * _BORDER_SHARE))
        viewport = self.viewport().size()
        if picture.width() and picture.height() and viewport.isValid():
            scale = min(viewport.width() / picture.width(),
                        viewport.height() / picture.height())
            border = min(natural_border, 12.0 / max(scale, 0.001))
        else:
            border = natural_border
        paper = picture.adjusted(-border, -border, border, border)
        shown = not pixmap.isNull()
        if pixmap.size() != self._band_size or border != self._band_border:
            self._band_size = pixmap.size()
            self._band_border = border
            self._lay_bands(picture, paper, border, shown)
        self._paper_rect = paper
        # Fitted to the print and its shadow, no more: the photo is what the
        # window is for, and a margin here is the photo made smaller.
        frame = paper.adjusted(-1.2 * border, -0.3 * border, 1.2 * border, 2.5 * border)
        # A picture the same size as the last one lies exactly where it did.
        # Fitting again would redraw the whole counter on every held-key flip
        # instead of just the print.
        refit = (frame != self._frame or self._touched
                 or self._fitted_view != self.viewport().size())
        if refit:
            self._frame = frame
            self._scene.setSceneRect(frame)
            self.fit()

    def _lay_bands(self, picture: QRectF, paper: QRectF, border: float, shown: bool) -> None:
        rects = []
        for spread, drop, _alpha in _SHADOW:
            outer = paper.adjusted(-spread * border, (drop - spread) * border,
                                   spread * border, (drop + spread) * border)
            rects += [
                QRectF(outer.left(), outer.top(), paper.left() - outer.left(), outer.height()),
                QRectF(paper.right(), outer.top(), outer.right() - paper.right(), outer.height()),
                QRectF(paper.left(), paper.bottom(), paper.width(),
                       outer.bottom() - paper.bottom()),
            ]
        # The paper reaches a unit under the picture, so no hairline of counter
        # can show between them once the print is scaled to the screen.
        inside = picture.adjusted(1, 1, -1, -1)
        rects += [
            QRectF(paper.left(), paper.top(), paper.width(), inside.top() - paper.top()),
            QRectF(paper.left(), inside.bottom(), paper.width(), paper.bottom() - inside.bottom()),
            QRectF(paper.left(), paper.top(), inside.left() - paper.left(), paper.height()),
            QRectF(inside.right(), paper.top(), paper.right() - inside.right(), paper.height()),
        ]
        for band, rect in zip(self._bands, rects):
            band.setRect(rect)
            band.setVisible(shown)

    def fit(self) -> None:
        if self._item.pixmap().isNull():
            return
        self.resetTransform()
        self.fitInView(self._frame, Qt.AspectRatioMode.KeepAspectRatio)
        self._fitted_view = self.viewport().size()
        self._touched = False

    def current_pixmap(self) -> QPixmap:
        """The picture as it is shown, turned if it was turned."""
        return self._item.pixmap()

    def print_rect(self) -> QRect:
        """Where the print, paper border included, lies in the viewport."""
        if self._item.pixmap().isNull() or not self._bands[-1].isVisible():
            return QRect()
        return self.mapFromScene(self._paper_rect).boundingRect()

    def zoom_percent(self) -> int:
        if self._item.pixmap().isNull():
            return 100
        return max(1, int(round(self.transform().m11() * 100)))

    def rotate_by(self, degrees: int) -> None:
        """Rotate the preview only. The file on disk is never modified."""
        if self._original.isNull():
            return
        self._rotation = (self._rotation + degrees) % 360
        self._apply()

    def wheelEvent(self, event) -> None:            # noqa: N802 - Qt naming
        if self._item.pixmap().isNull():
            return
        step = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._touched = True
        self.scale(step, step)

    def mousePressEvent(self, event) -> None:       # noqa: N802 - Qt naming
        self._touched = True                        # a drag may follow
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.fit()

    def resizeEvent(self, event) -> None:            # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if not self._original.isNull():
            self._apply()


class _Slip(QFrame):
    """A paper slip lying on the counter, with the counter's soft shadow."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        paper = QRectF(self.rect()).adjusted(14, 10, -14, -18)
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, drop, alpha in ((2, 5, 60), (6, 8, 30), (11, 11, 12)):
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(paper.adjusted(-spread, drop - spread, spread, drop + spread),
                                    3 + spread, 3 + spread)
        painter.setBrush(QColor(theme.PAPER))
        painter.drawRoundedRect(paper, 2, 2)
        painter.end()


class EmptySurface(QWidget):
    """Nothing to show: a slip on the counter saying why, and what to do about it.

    While a large file is still being read it stands in for that file instead,
    without the folder button.
    """

    choose_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.slip = _Slip()
        self.slip.setMinimumWidth(460)
        self.slip.setMaximumWidth(560)
        layout = QVBoxLayout(self.slip)
        layout.setContentsMargins(46, 36, 46, 44)
        layout.setSpacing(10)
        glyph = QLabel()
        glyph.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.glyph = glyph
        self.title = QLabel(tr("header.no_folder"))
        self.title.setObjectName("slipTitle")
        self.title.setWordWrap(True)
        self.hint = QLabel(tr("scan.hint_adjust"))
        self.hint.setObjectName("slipText")
        self.hint.setWordWrap(True)
        self.button = QPushButton(tr("header.choose_folder"))
        self.button.setObjectName("inkButton")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setIcon(icons.icon("folder", 15, theme.PAPER))
        self.button.clicked.connect(self.choose_requested)
        self.keys = QLabel(tr("side.keyhint"))
        self.keys.setObjectName("slipHint")
        self.keys.setWordWrap(True)
        layout.addWidget(glyph)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        layout.addSpacing(8)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(6)
        layout.addWidget(self.keys)
        outer.addWidget(self.slip)
        self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        """Hide the folder button while this stands in for a file that is opening."""
        self.button.setVisible(not busy)
        self.keys.setVisible(not busy)
        self.glyph.setPixmap(icons.pixmap("camera" if busy else "folder", 30, theme.INK_SOFT, 1.6))

    def retranslate(self, title: str = "", hint: str = "") -> None:
        self.title.setText(title or tr("header.no_folder"))
        self.hint.setText(hint or tr("scan.hint_adjust"))
        self.button.setText(tr("header.choose_folder"))
        self.keys.setText(tr("side.keyhint"))


class MediaPreview(QWidget):
    """Shows whatever the current item is, and reports what it could not show."""

    choose_requested = Signal()
    video_error = Signal(str)
    frame_stepped = Signal(float)

    EMPTY, IMAGE, ANIMATED, VIDEO = 0, 1, 2, 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_path: Path | None = None
        self._movie: QMovie | None = None
        self._movie_size = QSize()
        self._frame_time: float | None = None
        self._frame_path: Path | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.stack = QStackedWidget()
        self.stack.setObjectName("previewStack")
        self.empty = EmptySurface()
        self.empty.choose_requested.connect(self.choose_requested)
        self.image = ImageSurface()
        self.animated = QLabel()
        self.animated.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.animated.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.animated.setStyleSheet(f"background: {theme.MAT};")

        self.video_page = QWidget()
        video_layout = QVBoxLayout(self.video_page)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(0)
        self.video = QVideoWidget()
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        video_layout.addWidget(self.video, 1)
        video_layout.addWidget(self._build_controls())

        for widget in (self.empty, self.image, self.animated, self.video_page):
            self.stack.addWidget(widget)
        outer.addWidget(self.stack)

        #: Opened with the first video, not here: on some machines asking for the
        #: audio device takes a second and a half, and most sessions are photos.
        self.audio: QAudioOutput | None = None
        self._muted = False
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._state_changed)
        self.player.errorOccurred.connect(self._error)
        self.volume.valueChanged.connect(self._volume_changed)

    def _open_audio(self) -> None:
        if self.audio is not None:
            return
        self.audio = QAudioOutput(self)
        self.audio.setVolume(self.volume.value() / 100)
        self.audio.setMuted(self._muted)
        self.player.setAudioOutput(self.audio)

    def _volume_changed(self, value: int) -> None:
        if self.audio is not None:
            self.audio.setVolume(value / 100)

    # -- controls ------------------------------------------------------
    def _build_controls(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("videoControls")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(8)

        def round_button(name: str, tip: str) -> QToolButton:
            button = QToolButton()
            button.setObjectName("roundButton")
            button.setIcon(icons.icon(name, 14, theme.PAPER_DIM))
            button.setIconSize(QSize(14, 14))
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            return button

        self.back_button = round_button("chevron-left", "J  −5s")
        self.back_button.clicked.connect(lambda: self.seek_relative(-5000))
        self.play_button = round_button("play", "K / Space")
        self.play_button.clicked.connect(self.toggle_play)
        self.forward_button = round_button("chevron-right", "L  +5s")
        self.forward_button.clicked.connect(lambda: self.seek_relative(5000))
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 0)
        self.position.sliderMoved.connect(self.player_seek)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("timeLabel")
        self.speed = QComboBox()
        for text, value in (("0.5×", 0.5), ("1×", 1.0), ("1.5×", 1.5), ("2×", 2.0)):
            self.speed.addItem(text, value)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(
            lambda: self.player.setPlaybackRate(float(self.speed.currentData())))
        self.mute_button = round_button("volume", "M")
        self.mute_button.clicked.connect(self.toggle_mute)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(60)
        self.volume.setFixedWidth(72)

        for widget in (self.back_button, self.play_button, self.forward_button):
            row.addWidget(widget)
        row.addWidget(self.position, 1)
        row.addWidget(self.time_label)
        row.addWidget(self.speed)
        row.addWidget(self.mute_button)
        row.addWidget(self.volume)
        return bar

    # -- showing -------------------------------------------------------
    def _show_empty(self, title: str = "", hint: str = "") -> None:
        self.current_path = None
        self.release()
        self.image.clear_media()
        self.empty.retranslate(title, hint)
        self.stack.setCurrentIndex(self.EMPTY)

    def show_loading(self, path: str | Path, title: str = "", hint: str = "") -> None:
        """Stand in for a file whose decode is happening on a worker.

        The empty surface carries a "choose folder" button, which has no
        business being offered while a file is opening, so it is hidden for the
        duration.
        """
        self.current_path = Path(path)
        self._frame_time = None
        self._frame_path = None
        self.release()
        self.image.clear_media()
        self.empty.set_busy(True)
        self.empty.retranslate(title or Path(path).name, hint)
        self.stack.setCurrentIndex(self.EMPTY)

    def show_empty(self, title: str = "", hint: str = "") -> None:
        self.empty.set_busy(False)
        self._show_empty(title, hint)

    def show_path(self, path: str | Path, cached: QPixmap | None = None) -> tuple[bool, str]:
        target = Path(path)
        self.current_path = target
        self._frame_time = None
        self._frame_path = None
        self.release()

        if mediatypes.is_video(target):
            self.image.clear_media()
            self.stack.setCurrentIndex(self.VIDEO)
            self._open_audio()
            self.player.setSource(QUrl.fromLocalFile(str(target)))
            self.player.play()
            return True, ""

        if mediatypes.may_animate(target):
            movie = QMovie(str(target))
            if movie.isValid() and movie.frameCount() > 1:
                self._movie = movie
                self._movie_size = movie.currentImage().size()
                if not self._movie_size.isValid():
                    self._movie_size = movie.frameRect().size()
                self.animated.setMovie(movie)
                self._scale_movie()
                movie.start()
                self.stack.setCurrentIndex(self.ANIMATED)
                return True, ""

        pixmap = cached
        error = ""
        if pixmap is None or pixmap.isNull():
            pixmap, error = decode_pixmap(
                target, QSize(max(1400, self.image.width() * 2),
                              max(1000, self.image.height() * 2)))
        if pixmap.isNull():
            self.image.clear_media()
            self.stack.setCurrentIndex(self.IMAGE)
            return False, error or tr("error.decode_image")
        self.image.set_pixmap(pixmap)
        self.stack.setCurrentIndex(self.IMAGE)
        return True, ""

    def show_still(self, path: str | Path, pixmap: QPixmap) -> None:
        """Put an already-decoded picture up at once, standing in for *path*.

        While an arrow key is held this is all the preview does per item: no
        decode, no player, no animation check. A playing video stops, so its
        sound does not carry on under the pictures flipping past.
        """
        self.current_path = Path(path)
        self._frame_time = None
        self._frame_path = None
        if self.stack.currentIndex() in (self.VIDEO, self.ANIMATED):
            self.release()
        self.image.set_pixmap(pixmap)
        self.stack.setCurrentIndex(self.IMAGE)

    def release(self) -> None:
        """Let go of the file so a move or delete cannot be blocked by us."""
        self.player.stop()
        self.player.setSource(QUrl())
        if self._movie is not None:
            self._movie.stop()
            device = self._movie.device()
            if device is not None:
                device.close()
            self.animated.setMovie(None)
            self._movie = None
            self._movie_size = QSize()

    def _scale_movie(self) -> None:
        if self._movie is None:
            return
        size = self._movie_size
        if size.isValid():
            self._movie.setScaledSize(size.scaled(self.animated.size(),
                                                  Qt.AspectRatioMode.KeepAspectRatio))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._scale_movie()

    # -- playback ------------------------------------------------------
    def is_video_page(self) -> bool:
        return self.stack.currentIndex() == self.VIDEO

    def toggle_play(self) -> None:
        if self._frame_path == self.current_path and self._frame_time is not None:
            self.stack.setCurrentIndex(self.VIDEO)
            self.player.setPosition(int(round(self._frame_time * 1000)))
            self._frame_time = None
            self.player.play()
            return
        if not self.is_video_page():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def seek_relative(self, milliseconds: int) -> None:
        if self.is_video_page():
            self.player.setPosition(max(0, min(self.player.duration(),
                                               self.player.position() + milliseconds)))

    def player_seek(self, value: int) -> None:
        self.player.setPosition(value)

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        if self.audio is not None:
            self.audio.setMuted(self._muted)
        self.mute_button.setIcon(icons.icon("mute" if self._muted else "volume", 14,
                                            theme.PAPER_DIM))

    def step_frame(self, direction: int) -> str:
        """Show the neighbouring frame by its real timestamp. Returns a status line."""
        path = self.current_path
        if path is None or not mediatypes.is_video(path):
            return ""
        if not video.available():
            self.seek_relative(40 * direction)
            return ""
        player_position = self.player.position() / 1000
        self.player.pause()
        base = (self._frame_time if self._frame_path == path and self._frame_time is not None
                else player_position)
        result = video.frame_at(path, base, direction)
        if result is None:
            return tr("error.no_frame")
        image, timestamp = result
        self._frame_time, self._frame_path = timestamp, path
        self.image.set_pixmap(QPixmap.fromImage(pil_to_qimage(image)))
        self.stack.setCurrentIndex(self.IMAGE)
        self.frame_stepped.emit(timestamp)
        return f"{timestamp:.6f}"

    # -- player signals ------------------------------------------------
    def _position_changed(self, position: int) -> None:
        if not self.position.isSliderDown():
            self.position.setValue(position)
        self.time_label.setText(f"{_clock(position)} / {_clock(self.player.duration())}")

    def _duration_changed(self, duration: int) -> None:
        self.position.setRange(0, max(0, duration))

    def _state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(icons.icon("pause" if playing else "play", 14,
                                            theme.PAPER_DIM))
        if playing:
            self._frame_time = None

    def _error(self, _code, message: str) -> None:
        if self.current_path is not None and message:
            self.video_error.emit(message)


def _clock(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
