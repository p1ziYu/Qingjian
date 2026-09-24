"""The filmstrip and the thumbnail grid.

Both are ``QListWidget`` in icon mode, with three things that keep a folder of
tens of thousands of files usable:

* **Icons are requested for what the viewport shows, not for the whole list.**
  Asking for every thumbnail on open was the single reason a large folder hung.
* **Rows are laid out in batches** (``LayoutMode.Batched``) and are all the same
  size, so Qt never has to measure the entire model at once.
* **The filmstrip holds a window around the cursor**, not the whole queue; it is
  a way to glance at neighbours, and nobody scrubs through 20 000 of them.

Badges (raw, duration, rating, colour label) are painted into the thumbnail, so
the same composited image serves both views. The delegate only lays each
thumbnail out as a small print: a paper border that hugs the picture whatever
its shape, and a grease-pencil box around the frames that are chosen.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (QItemSelection, QItemSelectionModel, QPointF, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QListWidget, QListWidgetItem, QStyle,
                               QStyledItemDelegate)

from ..core import mediatypes, metadata, viewport
from . import theme
from .thumbs import ThumbnailCache

_ROLE_PATH = int(Qt.ItemDataRole.UserRole) + 1

#: Rows fetched beyond the visible range, so a slow scroll is never empty.
_OVERSCAN = 12
#: Upper bound on the walk that finds the last visible row.
_MAX_PROBE = 400
#: How many neighbours the filmstrip keeps loaded around the current item.
FILMSTRIP_WINDOW = 80


def _star(painter: QPainter, centre: QPointF, radius: float) -> None:
    points = [(12, 2.4), (14.6, 8.6), (21.2, 9.2), (16.2, 13.6), (17.6, 20.2),
              (12, 16.8), (6.4, 20.2), (7.8, 13.6), (2.8, 9.2), (9.4, 8.6)]
    scale = radius / 10.0
    path = QPainterPath(QPointF(centre.x() + (points[0][0] - 12) * scale,
                                centre.y() + (points[0][1] - 12) * scale))
    for x, y in points[1:]:
        path.lineTo(QPointF(centre.x() + (x - 12) * scale, centre.y() + (y - 12) * scale))
    path.closeSubpath()
    painter.drawPath(path)


def decorate(pixmap: QPixmap, raw: bool = False, duration: str = "", rating: int = 0,
             label: str = "", burst: str = "", keeper: bool = False) -> QPixmap:
    """Paint the small status marks onto a copy of *pixmap*."""
    if pixmap.isNull():
        return pixmap
    if not (raw or duration or rating or label or burst or keeper):
        return pixmap
    canvas = QPixmap(pixmap)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    width, height = canvas.width(), canvas.height()
    painter.setFont(theme.numeral_font(max(8, int(min(width, height) * 0.1))))
    metrics = painter.fontMetrics()
    pad = max(2, int(min(width, height) * 0.04))
    chip_height = metrics.height() + 2

    def chip(text: str, x: float, y: float, fill: QColor, ink: str, right: bool = False,
             extra: float = 0.0) -> float:
        box_width = metrics.horizontalAdvance(text) + 8 + extra
        left = x - box_width if right else x
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(left, y, box_width, chip_height), 2, 2)
        painter.setPen(QColor(ink))
        painter.drawText(QRectF(left + extra, y, box_width - extra, chip_height),
                         int(Qt.AlignmentFlag.AlignCenter), text)
        return left + box_width

    night = QColor(20, 18, 16, 190)
    left = float(pad)
    if label:
        colour = theme.LABEL_COLOURS.get(label)
        if colour:
            dot = max(7.0, min(width, height) * 0.09)
            painter.setPen(QPen(QColor(20, 18, 16, 200), 1.2))
            painter.setBrush(QColor(colour))
            painter.drawEllipse(QPointF(left + dot / 2 + 1, pad + dot / 2 + 1), dot / 2, dot / 2)
            left += dot + pad + 2
    if keeper:
        end = chip("", left, pad, QColor(theme.PAPER), theme.INK, extra=chip_height * 0.6)
        tick = QPainterPath(QPointF(left + 4, pad + chip_height * 0.52))
        tick.lineTo(QPointF(left + 4 + chip_height * 0.22, pad + chip_height * 0.74))
        tick.lineTo(QPointF(left + 4 + chip_height * 0.6, pad + chip_height * 0.28))
        pen = QPen(QColor(theme.INK), max(1.4, chip_height * 0.12))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(tick)
        left = end + pad
    if burst:
        chip(burst, left, pad, QColor(theme.KRAFT), theme.INK)
    if raw:
        chip("RAW", width - pad, pad, QColor(theme.PAPER), theme.INK, right=True)
    if duration:
        chip(duration, width - pad, height - pad - chip_height, night, theme.PAPER, right=True)
    if rating:
        count = min(5, rating)
        radius = chip_height * 0.36
        box_width = count * radius * 2.3 + 6
        top = height - pad - chip_height
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(night)
        painter.drawRoundedRect(QRectF(pad, top, box_width, chip_height), 2, 2)
        painter.setBrush(QColor(theme.PAPER))
        for step in range(count):
            _star(painter, QPointF(pad + 3 + radius * 1.15 + step * radius * 2.3,
                                   top + chip_height / 2), radius)
    painter.end()
    return canvas


_PAPER = QColor(theme.PAPER)
_TILE = QColor(theme.RAISED)
_DROP = QColor(0, 0, 0, 110)
_GREASE_PEN = QPen(QColor(theme.GREASE), 2.0)
_GREASE_PEN.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
_HOVER_PEN = QPen(QColor(theme.LINE_CONTROL), 1.0)
_NAME = QColor(theme.FAINT)
_NAME_CHOSEN = QColor(theme.PAPER)
#: Height under a print for its name, including the clearance below a grease box.
_NAME_ROOM = 27
#: The name font and its metrics, made on first use: a font needs the application.
_NAME_TYPE: list = []


def _name_type() -> tuple:
    if not _NAME_TYPE:
        font = theme.ui_font(11)
        _NAME_TYPE.extend((font, QFontMetrics(font)))
    return _NAME_TYPE[0], _NAME_TYPE[1]


#: How far a print's shadow falls below it, in pixels.
_DROP_OFFSET = 2
#: Tiles kept ready to draw. A few screens' worth; the thumbnails behind them
#: stay in the thumbnail cache either way.
_TILE_LIMIT = 600


def print_tile(pixmap: QPixmap, fit: QSize, border: int) -> QPixmap:
    """A thumbnail made into a small print: fitted, paper border, shadow underneath.

    Composed once when the thumbnail lands, so a repaint of the strip or the
    sheet is one pixmap per frame instead of a scale and three fills.
    """
    size = pixmap.size()
    if size.width() > fit.width() or size.height() > fit.height():
        pixmap = pixmap.scaled(fit, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        size = pixmap.size()
    tile = QPixmap(size.width() + 2 * border, size.height() + 2 * border + _DROP_OFFSET)
    tile.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tile)
    paper = QRectF(0, 0, size.width() + 2 * border, size.height() + 2 * border)
    painter.fillRect(paper.translated(0, _DROP_OFFSET), _DROP)
    painter.fillRect(paper, _PAPER)
    painter.drawPixmap(border, border, pixmap)
    painter.end()
    return tile


class PrintDelegate(QStyledItemDelegate):
    """Lays a small print out in its cell and marks it when it is chosen."""

    def __init__(self, view: "_Browser", border: int = 3) -> None:
        super().__init__(view)
        self.view = view
        self.border = border

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt naming
        grid = self.view.gridSize()
        return grid if grid.isValid() else QSize(96, 96)

    def paint(self, painter: QPainter, option, index) -> None:
        cell = option.rect
        text = index.data(Qt.ItemDataRole.DisplayRole)
        tile = self.view.tile(index.data(_ROLE_PATH))
        gap = self.border + 4
        if tile is None:
            width, height = int(cell.width() * 0.6), int(cell.height() * 0.45)
            frame = QRectF(cell.x() + (cell.width() - width) / 2,
                           cell.y() + (cell.height() - (30 if text else 0) - height) / 2,
                           width, height)
            painter.fillRect(frame, _TILE)
        else:
            x = cell.x() + (cell.width() - tile.width()) // 2
            if text:
                # The print stands on a common floor, so every name in a row
                # sits on one line right under its picture.
                y = cell.bottom() - gap - _NAME_ROOM - tile.height() + _DROP_OFFSET
            else:
                y = cell.y() + (cell.height() - tile.height() + _DROP_OFFSET) // 2
            painter.drawPixmap(x, y, tile)
            frame = QRectF(x, y, tile.width(), tile.height() - _DROP_OFFSET)

        state = option.state
        selected = bool(state & QStyle.StateFlag.State_Selected)
        if selected or state & QStyle.StateFlag.State_MouseOver:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(_GREASE_PEN if selected else _HOVER_PEN)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(frame.adjusted(-3.5, -3.5, 3.5, 3.5), 3, 3)
            painter.restore()

        if text:
            font, metrics = _name_type()
            painter.save()
            painter.setFont(font)
            painter.setPen(_NAME_CHOSEN if selected else _NAME)
            label = metrics.elidedText(str(text), Qt.TextElideMode.ElideMiddle, cell.width() - 10)
            painter.drawText(QRectF(cell.x() + 5, cell.bottom() - gap - _NAME_ROOM + 9,
                                    cell.width() - 10, _NAME_ROOM - 9),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), label)
            painter.restore()


class _Browser(QListWidget):
    """Shared plumbing: paths in, thumbnails filled in as they become visible."""

    path_activated = Signal(str)
    path_selected = Signal(str)

    def __init__(self, cache: ThumbnailCache, edge: int, parent=None) -> None:
        super().__init__(parent)
        self.cache = cache
        self.edge = edge
        self._rows: dict[str, QListWidgetItem] = {}
        self._decorations: dict[str, dict] = {}
        #: path -> ((thumbnail key, decoration), tile), most recently drawn last.
        self._tiles: "OrderedDict[str, tuple]" = OrderedDict()
        #: How many tiles are worth keeping: one screenful, never fewer than
        #: `_TILE_LIMIT`. A fixed cap evicted tiles that were still on screen.
        self._tile_room = _TILE_LIMIT
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setMovement(QListWidget.Movement.Static)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.setLayoutMode(QListWidget.LayoutMode.Batched)
        self.setBatchSize(200)
        self.setIconSize(QSize(edge, edge))
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setItemDelegate(PrintDelegate(self, 2 if edge < 120 else 3))
        self.cache.ready.connect(self._thumbnail_ready)
        self.cache.dropped.connect(self._thumbnail_dropped)
        self.itemActivated.connect(self._activated)
        self.itemSelectionChanged.connect(self._selection_changed)

        # Scrolling only marks the view dirty; the actual fetch happens once
        # the scroll settles, so a flick does not queue every row it passed.
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setSingleShot(True)
        self._fetch_timer.setInterval(60)
        self._fetch_timer.timeout.connect(self.request_visible)
        self.verticalScrollBar().valueChanged.connect(self._schedule_fetch)
        self.horizontalScrollBar().valueChanged.connect(self._schedule_fetch)

    # -- population ----------------------------------------------------
    def set_paths(self, paths, decorations: dict | None = None) -> None:
        self.blockSignals(True)
        self.setUpdatesEnabled(False)
        try:
            self.clear()
            self._rows.clear()
            self._decorations = dict(decorations or {})
            for path in paths:
                item = QListWidgetItem()
                item.setData(_ROLE_PATH, str(path))
                item.setToolTip(str(path))
                self._label(item, Path(path))
                self.addItem(item)
                self._rows[str(path)] = item
        finally:
            self.setUpdatesEnabled(True)
            self.blockSignals(False)
        self._schedule_fetch()

    def set_paths_chunked(self, paths, decorations: dict | None = None,
                          progress=None, cancel=None, chunk: int = 500) -> bool:
        """Fill a very long list without the window ever looking stuck.

        Returns False if the caller cancelled part-way, in which case the view
        holds whatever was built so far.
        """
        paths = list(paths)
        total = max(1, len(paths))
        self.blockSignals(True)
        self.clear()
        self._rows.clear()
        self._decorations = dict(decorations or {})
        finished = True
        try:
            for start in range(0, len(paths), chunk):
                if cancel is not None and cancel():
                    finished = False
                    break
                self.setUpdatesEnabled(False)
                for path in paths[start:start + chunk]:
                    item = QListWidgetItem()
                    item.setData(_ROLE_PATH, str(path))
                    item.setToolTip(str(path))
                    self._label(item, Path(path))
                    self.addItem(item)
                    self._rows[str(path)] = item
                self.setUpdatesEnabled(True)
                if progress is not None:
                    progress("", int(min(100, (start + chunk) * 100 / total)))
        finally:
            self.setUpdatesEnabled(True)
            self.blockSignals(False)
        self._schedule_fetch()
        return finished

    def _label(self, item: QListWidgetItem, path: Path) -> None:
        """Subclass hook: what the caption under the tile says."""

    def remove_path(self, path: str | Path) -> int:
        """Drop one row. Returns the position it held, or -1.

        Rebuilding the whole list after every classification was turning each
        keystroke into O(number of files).
        """
        item = self._rows.pop(str(path), None)
        if item is None:
            return -1
        row = self.row(item)
        self.blockSignals(True)
        self.takeItem(row)
        self.blockSignals(False)
        return row

    def insert_path(self, path: str | Path, row: int, decoration: dict | None = None) -> None:
        key = str(path)
        if key in self._rows:
            return
        if decoration:
            self._decorations[key] = decoration
        item = QListWidgetItem()
        item.setData(_ROLE_PATH, key)
        item.setToolTip(key)
        self._label(item, Path(key))
        self.blockSignals(True)
        self.insertItem(max(0, min(row, self.count())), item)
        self.blockSignals(False)
        self._rows[key] = item
        self._schedule_fetch()

    def paths(self) -> list[str]:
        return [str(self.item(row).data(_ROLE_PATH)) for row in range(self.count())]

    # -- lazy loading --------------------------------------------------
    def _schedule_fetch(self) -> None:
        self._fetch_timer.start()

    def showEvent(self, event) -> None:              # noqa: N802 - Qt naming
        super().showEvent(event)
        self._schedule_fetch()

    def resizeEvent(self, event) -> None:            # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._schedule_fetch()

    def visible_rows(self) -> tuple[int, int]:
        """First and last row the viewport currently shows.

        Walks forward from the first visible index rather than scanning the
        whole model, so the cost is proportional to what is on screen.
        """
        total = self.count()
        if not total:
            return (0, -1)
        viewport = self.viewport().rect()
        first_index = self.indexAt(viewport.topLeft())
        start = first_index.row() if first_index.isValid() else 0
        if start < 0:
            start = 0
        end = start
        bottom = viewport.bottom()
        right = viewport.right()
        # 400 rows is short of a 4K window full of the smallest thumbnails,
        # and everything past the cap stayed blank for good.
        limit = _MAX_PROBE
        cell = self.gridSize()
        if cell.isValid() and cell.width() > 0 and cell.height() > 0:
            columns = max(1, viewport.width() // cell.width())
            limit = max(_MAX_PROBE, columns * (viewport.height() // cell.height() + 2))
        for row in range(start, min(total, start + limit)):
            rect = self.visualItemRect(self.item(row))
            if rect.isNull():
                break
            if rect.top() > bottom or (self.flow() == QListWidget.Flow.LeftToRight
                                       and not self.isWrapping() and rect.left() > right):
                break
            end = row
        return (start, end)

    def request_visible(self) -> None:
        total = self.count()
        if not total or not self.isVisible():
            return
        start, end = self.visible_rows()
        low, high = viewport.band(start, end, total, _OVERSCAN)
        wanted = [str(self.item(row).data(_ROLE_PATH)) for row in range(low, high + 1)]
        self._tile_room = max(_TILE_LIMIT, len(wanted))
        # Anything outside this band stops being worth a worker's time.
        self.cache.set_wanted(wanted, self.edge)
        for key in wanted:
            pixmap = self.cache.request(key, self.edge)
            if pixmap is not None:
                self._apply_icon(key, pixmap)

    def _apply_icon(self, key: str, pixmap: QPixmap) -> None:
        item = self._rows.get(key)
        if item is None:
            return
        decoration = self._decorations.get(key)
        known = self._tiles.get(key)
        signature = (pixmap.cacheKey(), repr(decoration))
        if known is None or known[0] != signature:
            tile = print_tile(self._decorated(key, pixmap), self.iconSize(),
                              self.itemDelegate().border)
            self._tiles[key] = (signature, tile)
            while len(self._tiles) > self._tile_room:
                self._tiles.popitem(last=False)
        else:
            self._tiles.move_to_end(key)
        # The delegate draws from the tile, so this row has to be drawn again.
        self.update(self.indexFromItem(item))

    def tile(self, key) -> QPixmap | None:
        entry = self._tiles.get(str(key)) if key is not None else None
        return entry[1] if entry is not None else None

    def _decorated(self, key: str, pixmap: QPixmap) -> QPixmap:
        extra = self._decorations.get(key)
        path = Path(key)
        duration = ""
        if mediatypes.is_video(path):
            info = metadata.read(path)
            duration = metadata.format_duration(info.duration)
        options = {"raw": mediatypes.is_raw(path), "duration": duration}
        if extra:
            options.update(extra)
        return decorate(pixmap, **options)

    def _thumbnail_ready(self, path: str, edge: int, pixmap: QPixmap) -> None:
        if edge != self.edge:
            return
        self._apply_icon(path, pixmap)

    def _thumbnail_dropped(self, edge: int) -> None:
        """Something was abandoned mid-flight; ask again for what is on screen."""
        if edge == self.edge:
            self._schedule_fetch()

    def set_decoration(self, path: str, **options) -> None:
        self._decorations[str(path)] = options
        cached = self.cache.peek(path, self.edge)
        if cached is not None:
            self._apply_icon(str(path), cached)

    # -- selection -----------------------------------------------------
    def _activated(self, item: QListWidgetItem) -> None:
        self.path_activated.emit(str(item.data(_ROLE_PATH)))

    def _selection_changed(self) -> None:
        paths = self.selected_paths()
        if paths:
            self.path_selected.emit(paths[0])

    def selected_paths(self) -> list[str]:
        return [str(item.data(_ROLE_PATH)) for item in self.selectedItems()]

    def current_path(self) -> str | None:
        """The item the keyboard cursor sits on, whether or not it is selected."""
        item = self.currentItem()
        return str(item.data(_ROLE_PATH)) if item is not None else None

    def show_path(self, path: str | Path) -> None:
        item = self._rows.get(str(path))
        if item is None:
            return
        self.blockSignals(True)
        self.setCurrentItem(item)
        self.blockSignals(False)
        self.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
        self._schedule_fetch()

    def set_edge(self, edge: int) -> None:
        if edge == self.edge:
            return
        self.edge = edge
        self._tiles.clear()
        self.setIconSize(QSize(edge, edge))
        keys = self.paths()
        selected = self.selected_paths()
        self.set_paths(keys, self._decorations)
        # setSelected fires itemSelectionChanged synchronously, so restoring a
        # selection one item at a time would emit a burst of half-built states
        # on every tick of the size slider.
        self.blockSignals(True)
        for path in selected:
            item = self._rows.get(path)
            if item is not None:
                item.setSelected(True)
        self.blockSignals(False)
        if selected:
            self._selection_changed()


class Filmstrip(_Browser):
    """A window of neighbours under the preview, for jumping about by eye."""

    def __init__(self, cache: ThumbnailCache, parent=None) -> None:
        super().__init__(cache, 72, parent)
        self.setObjectName("filmstrip")
        self.setFlow(QListWidget.Flow.LeftToRight)
        self.setWrapping(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        # The icon box fits inside the delegate's room exactly, so a thumbnail
        # is drawn at its own size and never rescaled on a repaint.
        self.setFixedHeight(76)
        self.setSpacing(0)
        self.setGridSize(QSize(84, 66))
        self.setIconSize(QSize(72, 54))
        self._all: list[str] = []
        self._window = (0, 0)

    def set_queue(self, paths, current_index: int, decorations: dict | None = None) -> None:
        """Show a window of the queue centred on *current_index*."""
        self._all = [str(p) for p in paths]
        self._show_window(current_index, decorations, force=True)

    def follow(self, current_index: int, decorations: dict | None = None) -> None:
        self._show_window(current_index, decorations, force=False)

    def _show_window(self, index: int, decorations: dict | None, force: bool) -> None:
        total = len(self._all)
        if not total:
            if force:
                self.set_paths([], decorations)
                self._window = (0, 0)
            return
        index = viewport.clamp_index(index, total)
        low, high = viewport.window(index, total, FILMSTRIP_WINDOW)
        # Rebuild only once the cursor nears the edge of the loaded window.
        if not force and self.count() and not viewport.needs_rebuild(
                index, self._window, total=total):
            self.show_path(self._all[index])
            return
        self._window = (low, high)
        # `follow` passes no decorations; dropping the ones we hold would wipe
        # the stars and colour labels off the strip on every rebuild. Hand the
        # same dict back rather than pass it in: `set_paths` would copy it, and
        # this runs on every flip that crosses the window edge.
        held = self._decorations
        self.set_paths(self._all[low:high], decorations)
        if decorations is None:
            self._decorations = held
        self.show_path(self._all[index])

    def drop(self, path: str | Path, row: int | None = None) -> None:
        """Remove one item, keeping the loaded window aligned with the queue.

        A bulk classification in the grid removes items from anywhere, not just
        from inside the window, so the adjustment depends on where the item was.
        Letting the window collapse would silently turn every later step back
        into a full rebuild.
        """
        key = str(path)
        if row is None:
            try:
                position = self._all.index(key)
            except ValueError:
                self.remove_path(key)
                return
        else:
            position = int(row)
            if not 0 <= position < len(self._all) or self._all[position] != key:
                self.remove_path(key)
                return
        self._all.pop(position)
        self.remove_path(key)
        low, high = self._window
        if position < low:
            low, high = max(0, low - 1), max(0, high - 1)
        elif position < high:
            high = max(low, high - 1)
        self._window = (low, high)

    def insert(self, path: str | Path, row: int, decoration: dict | None = None) -> None:
        """Put one item back, keeping the loaded window aligned with the queue.

        The counterpart of `drop`. Rebuilding the strip instead turned an undo
        into a full repopulate, which is what `drop` exists to avoid.
        """
        key = str(path)
        if key in self._all:
            return
        position = max(0, min(int(row), len(self._all)))
        self._all.insert(position, key)
        low, high = self._window
        if position < low:
            self._window = (low + 1, high + 1)
            return
        if position <= high:
            self.insert_path(key, position - low, decoration)
            self._window = (low, high + 1)


class ThumbnailGrid(_Browser):
    """Many at once, with multi-select so a whole burst files in one keypress."""

    selection_changed = Signal(int)

    def __init__(self, cache: ThumbnailCache, parent=None) -> None:
        super().__init__(cache, 168, parent)
        self.setObjectName("grid")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setSpacing(4)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.show_names = True
        self.itemSelectionChanged.connect(
            lambda: self.selection_changed.emit(len(self.selectedItems())))
        # A batch move walks `remove_path` once per file. Coalescing the recount
        # keeps one operation to a single O(n) sweep of `selectedItems()`.
        self._recount = QTimer(self)
        self._recount.setSingleShot(True)
        self._recount.setInterval(0)
        self._recount.timeout.connect(
            lambda: self.selection_changed.emit(len(self.selectedItems())))
        self._sync_grid()

    def _announce_selection(self) -> None:
        """Say the selection changed after a change Qt made under blockSignals."""
        self._recount.start()

    def set_paths(self, paths, decorations: dict | None = None) -> None:
        super().set_paths(paths, decorations)
        self._announce_selection()

    def set_paths_chunked(self, paths, decorations: dict | None = None,
                          progress=None, cancel=None, chunk: int = 500) -> bool:
        done = super().set_paths_chunked(paths, decorations, progress, cancel, chunk)
        self._announce_selection()
        return done

    def remove_path(self, path: str | Path) -> int:
        row = super().remove_path(path)
        if row >= 0:
            self._announce_selection()
        return row

    def show_path(self, path: str | Path) -> None:
        item = self._rows.get(str(path))
        if item is None:
            return
        if self.selectionModel().hasSelection():
            # Moving the cursor must not throw away what the user picked.
            self.selectionModel().setCurrentIndex(
                self.indexFromItem(item), QItemSelectionModel.SelectionFlag.NoUpdate)
            self.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
            self._schedule_fetch()
            return
        super().show_path(path)
        self._announce_selection()

    def _sync_grid(self) -> None:
        """Share the width out between the columns, so no empty column is left over."""
        label = 30 if self.show_names else 0
        base = self.edge + 18
        style = self.style()
        # Measuring the live viewport made the width depend on whether the
        # scrollbar happened to be up, and each new grid size changed that
        # answer again: a few hundred item counts re-laid out forever.
        width = self.maximumViewportSize().width()
        if not style.pixelMetric(QStyle.PixelMetric.PM_ScrollView_ScrollBarOverlap, None, self):
            width -= style.pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent,
                                       None, self.verticalScrollBar())
        available = max(base, width - 1)
        columns = max(1, available // base)
        room = 2 * self.spacing()        # Qt adds it around every cell
        grid = QSize(max(base - room, available // columns - room), base + label)
        if grid != self.gridSize():
            self.setGridSize(grid)

    def resizeEvent(self, event) -> None:            # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._sync_grid()

    def _label(self, item: QListWidgetItem, path: Path) -> None:
        item.setText(path.name if self.show_names else "")
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)

    def set_edge(self, edge: int) -> None:
        super().set_edge(edge)
        self._sync_grid()

    def set_show_names(self, enabled: bool) -> None:
        self.show_names = bool(enabled)
        for row in range(self.count()):
            item = self.item(row)
            self._label(item, Path(str(item.data(_ROLE_PATH))))
        self._sync_grid()

    def select_all_paths(self, paths) -> None:
        # One QItemSelection instead of N setSelected calls: four thousand
        # items took two seconds a click, and an invert after it took minutes.
        rows = sorted(self.row(self._rows[str(p)]) for p in paths if str(p) in self._rows)
        model = self.model()
        selection = QItemSelection()
        start = previous = None
        for row in rows:
            if start is None:
                start = previous = row
            elif row == previous + 1:
                previous = row
            else:
                selection.select(model.index(start, 0), model.index(previous, 0))
                start = previous = row
        if start is not None:
            selection.select(model.index(start, 0), model.index(previous, 0))
        self.selectionModel().select(selection,
                                     QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self._announce_selection()

    def invert_selection(self) -> None:
        if not self.count():
            return
        model = self.model()
        whole = QItemSelection(model.index(0, 0), model.index(self.count() - 1, 0))
        self.selectionModel().select(whole, QItemSelectionModel.SelectionFlag.Toggle)
        self._announce_selection()
