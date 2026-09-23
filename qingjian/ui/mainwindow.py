"""The main window.

Deliberately thin: it draws, it listens for keys, and it asks
:class:`qingjian.core.engine.Engine` to do things. Anything that decides
*what* should happen lives in the core so it can be tested without a display.
"""
from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent, QObject,
                            QPropertyAnimation, QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog,
                               QPushButton, QSizePolicy, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import __display_name__
from ..core import config, metadata, naming, ops, platform_, scanner
from ..core.imaging import decodes_slowly
from ..core.engine import Engine, human_size
from ..core.i18n import LANGUAGE_CODES, get_language, set_language, tr
from ..core.logsetup import get_logger
from ..core.naming import NameError_
from ..core.safestore import Cancelled, TransactionError
from ..core.sidecar import PROMPT_ALWAYS, PROMPT_EACH, PROMPT_ONCE
from . import icons, theme
from .browsers import Filmstrip, ThumbnailGrid
from .dialogs import BackupDialog, ConflictDialog, SidecarDialog, TableDialog
from .duplicates import DuplicatesDialog
from .editors import BindingsDialog, SettingsDialog
from .preview import MediaPreview, PreviewPrefetcher
from .thumbs import ThumbnailCache
from .widgets import (BindingCard, CountLabel, ElidedLabel, FlyingPrint, FolderSlip,
                      LabelSwatches, Segmented, StarRating, elide, icon_button, separator,
                      text_button)

log = get_logger("window")

#: Thumbnail size shown while an arrow key is held: big enough to recognise a
#: picture, small enough to decode between two key repeats.
HOLD_EDGE = 480
#: A held key normally ends with its release. This catches a release that went
#: to another window, so the preview never stays on a soft stand-in.
SETTLE_MS = 500
#: How long a filed print takes to drop into its envelope. The next print is
#: already on the counter when it starts, so this never slows sorting down.
BAG_MS = 180


class _ArrowKeys(QObject):
    """Tells the window whether the arrow press it is handling is a key repeat.

    QShortcut does not say. The key event that fired it does, and that event
    passes the application's event filters on its way to the shortcut.
    """

    released = Signal()

    ARROWS = (Qt.Key.Key_Left, Qt.Key.Key_Right)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.repeating = False

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        kind = event.type()
        if kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            if event.key() in self.ARROWS:
                self.repeating = event.isAutoRepeat()
        elif kind == QEvent.Type.KeyRelease:
            if event.key() in self.ARROWS and not event.isAutoRepeat():
                self.repeating = False
                self.released.emit()
        return False


class MainWindow(QMainWindow):
    queue_event = Signal(str, object)

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.engine = engine
        self.settings = engine.settings
        self.view_mode = self.settings.default_view
        self._conflict_defaults: dict[str, str] = {}
        self._remembered_replace_paths: set[Path] = set()
        self._clash_notice = ""
        self._handled_count = 0
        self._busy = False
        self._pending_open: Path | None = None
        self._pending_open_retry_queued = False
        self._close_requested = False
        self._closed = False
        self._reporting = False
        self._failed_seen = 0
        self._failure_report_queued = False
        #: True while the grid is being filled in chunks. Rows must not be
        #: inserted into a list that is still being built from a snapshot.
        self._grid_building = False
        self._started = False
        self.startup_folder: Path | None = None
        self._binding_cards: list[BindingCard] = []
        self._binding_shortcuts: list[QShortcut] = []
        self._fixed_shortcuts: list[QShortcut] = []
        self._arrow_shortcuts: list[QShortcut] = []
        self._retranslators: list = []
        self._inflight: dict[str, tuple[Path, int]] = {}
        #: The copy that drops into an envelope, its animation and where it lands.
        #: Made once: a widget created per key press is polished against the
        #: whole stylesheet every time.
        self._flyer: FlyingPrint | None = None
        self._fall: QPropertyAnimation | None = None
        self._landing: BindingCard | None = None
        self._motion = platform_.animations_enabled()
        # The grid holds one row per file, so it is filled the first time it is
        # actually shown rather than every time the queue changes.
        self._grid_dirty = True

        self.thumbs = ThumbnailCache(self.settings.thumb_cache_mb)
        self.preloader = PreviewPrefetcher()
        self.preloader.arrived.connect(self._preview_arrived)
        #: Heavy decodes re-asked for after a resize, so one cannot loop.
        self._preview_retries: dict[str, int] = {}
        set_language(self.settings.language)

        self.setWindowTitle(f"{__display_name__} · {tr('app.tagline')}")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumSize(1180, 740)
        self.resize(1540, 940)
        self.setAcceptDrops(True)

        # Holding an arrow key flips through what is already decoded; the item
        # it is released on gets the full render. See `_arrow`.
        self._arrows = _ArrowKeys(self)
        QApplication.instance().installEventFilter(self._arrows)
        self._arrows.released.connect(self._settle)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(SETTLE_MS)
        self._settle_timer.timeout.connect(self._settle)
        self._settle_pending = False
        self._ledger_fit_timer = QTimer(self)
        self._ledger_fit_timer.setSingleShot(True)
        self._ledger_fit_timer.timeout.connect(self._fit_ledger)
        self._ledger_fit_sizes: tuple[int, int, int, bool] | None = None
        self._flipped: deque[str] = deque(maxlen=4)
        self.thumbs.ready.connect(self._flip_arrived)

        self._build()
        self._build_shortcuts()
        self.queue_event.connect(self._on_queue_event)
        self._queue_listener = lambda event, job: self.queue_event.emit(event, job)
        engine.queue_listeners.append(self._queue_listener)
        self._restore_geometry()
        QTimer.singleShot(0, self._first_run)

    # ================================================================ build
    def _build(self) -> None:
        """The counter, top to bottom.

        Its edge (folder, view and tools); the stage with the print on it, the
        backprint line and the index strip; the ten envelopes; the status line.
        Every row outside the stage is kept to what it needs, because the print
        is what the window is for.
        """
        root = QWidget()
        root.setObjectName("root")
        # The counter itself takes focus at start, so no control opens with a
        # focus ring on it and every key goes to the shortcuts.
        root.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 8, 16, 8)
        outer.setSpacing(0)
        outer.addLayout(self._build_header())
        outer.addSpacing(6)
        self._build_stamps()
        outer.addWidget(self._build_viewer(), 1)
        outer.addSpacing(6)
        outer.addLayout(self._build_envelopes())
        outer.addSpacing(6)
        outer.addLayout(self._build_status())
        # The stack opens on the single view; a saved grid preference has to
        # be applied, or the view switch and the page shown disagree.
        self._set_view(self.view_mode)
        # Initialise undo/redo before the first event-loop turn. Qt creates
        # buttons enabled by default, so a fresh window must not flash a
        # clickable recovery action while startup is still settling.
        self._update_actions()

    def _run_dialog(self, dialog) -> int:
        """Show a modal dialog and let go of it afterwards.

        Every dialog here is parented to the window, so without this the C++
        object and all of its children would live until the program exits.
        """
        try:
            return dialog.exec()
        finally:
            dialog.setParent(None)
            dialog.deleteLater()
            QTimer.singleShot(0, self._open_pending)

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._header_row = row
        brand = QLabel(__display_name__)
        brand.setObjectName("brand")
        subtitle = QLabel(tr("app.subtitle"))
        subtitle.setObjectName("brandSubtitle")
        spaced = subtitle.font()
        spaced.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.0)
        subtitle.setFont(spaced)
        row.addWidget(brand, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(subtitle, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(10)

        # The folder being sorted, as a pickup slip. Clicking it picks another.
        self.source_slip = FolderSlip()
        self.source_slip.setMaximumWidth(560)
        self.source_slip.clicked.connect(self.choose_folder)
        # Spare width goes to the slip first, up to its cap, then to the gap.
        row.addWidget(self.source_slip, 6)
        self.choose_button = text_button(tr("header.choose_folder"), "compactButton", "folder")
        self.choose_button.clicked.connect(self.choose_folder)
        row.addWidget(self.choose_button)
        self.rescan_button = icon_button("refresh", tr("header.rescan_tip"), 16)
        self.rescan_button.clicked.connect(self.rescan)
        row.addWidget(self.rescan_button)
        row.addStretch(1)
        row.addSpacing(8)

        self.view_switch = Segmented()
        self.view_switch.changed.connect(self._change_view)
        row.addWidget(self.view_switch)
        self.filter_combo = QComboBox()
        self.filter_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        row.addWidget(self.filter_combo)
        self.sort_combo = QComboBox()
        self.sort_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        row.addWidget(self.sort_combo)
        self.reverse_button = icon_button("reverse", tr("sort.reverse"), 16)
        self.reverse_button.setCheckable(True)
        self.reverse_button.setChecked(self.settings.sort_reverse)
        self.reverse_button.toggled.connect(self._reverse_changed)
        row.addWidget(self.reverse_button)
        self.recursive_check = QCheckBox(tr("scan.recursive"))
        self.recursive_check.setChecked(self.settings.recursive)
        self.recursive_check.toggled.connect(self._recursive_changed)
        row.addWidget(self.recursive_check)
        row.addSpacing(8)

        self.recover_button = text_button(tr("tool.recover"), "warningButton", "warning",
                                          theme.AMBER)
        self.recover_button.clicked.connect(self.recover_pending)
        self.recover_button.setVisible(False)
        row.addWidget(self.recover_button)
        self.duplicates_button = text_button(tr("tool.duplicates"), "quietButton", "duplicate")
        self.duplicates_button.clicked.connect(self.open_duplicates)
        row.addWidget(self.duplicates_button)
        self.more_button = QToolButton()
        self.more_button.setObjectName("menuButton")
        self.more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.more_button.setIcon(icons.icon("more", 15, theme.PAPER_DIM))
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self.more_button)
        self.history_action = more.addAction(icons.icon("history", 15, theme.PAPER_DIM),
                                             tr("tool.history"), self.show_history)
        self.stats_action = more.addAction(icons.icon("stats", 15, theme.PAPER_DIM),
                                           tr("tool.stats"), self.show_statistics)
        self.backup_action = more.addAction(icons.icon("shield", 15, theme.PAPER_DIM),
                                            tr("tool.backups"), self.open_backups)
        self.more_button.setMenu(more)
        row.addWidget(self.more_button)

        self.language_button = text_button("", "quietButton")
        self.language_button.clicked.connect(self._toggle_language)
        row.addWidget(self.language_button)
        self.settings_button = icon_button("gear", tr("settings.title"), 17)
        self.settings_button.clicked.connect(self.open_settings)
        row.addWidget(self.settings_button)
        return row

    def _build_stamps(self) -> None:
        """Stars, label stickers and the review queue: what gets marked on a print.

        One set, carried to whichever view is showing: under the print in the
        single view, above the sheet in the grid, where it marks the selection.
        """
        self.stamps = QWidget()
        row = QHBoxLayout(self.stamps)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.rating = StarRating()
        self.rating.setToolTip(f"{tr('side.rating')} · Shift + 1–5")
        self.rating.rated.connect(self._rate_current)
        row.addWidget(self.rating)
        self.labels = LabelSwatches()
        self.labels.setToolTip(f"{tr('side.colour_label')} · Alt + 1–5")
        self.labels.labelled.connect(self._label_current)
        row.addWidget(self.labels)
        self.review_button = QPushButton(tr("side.review_queue", count=0))
        self.review_button.setObjectName("reviewButton")
        self.review_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.review_button.setToolTip("Ctrl+R")
        self.review_button.clicked.connect(self.toggle_review)
        row.addWidget(self.review_button)
        self._stamps_row: QHBoxLayout | None = None

    def _place_stamps(self, mode: str) -> None:
        row, index = ((self._grid_row, self._grid_stamps_at) if mode == config.VIEW_GRID
                      else (self._backprint_row, self._backprint_stamps_at))
        if self._stamps_row is row:
            return
        if self._stamps_row is not None:
            self._stamps_row.removeWidget(self.stamps)
        row.insertWidget(index, self.stamps)
        self._stamps_row = row

    def _build_viewer(self) -> QWidget:
        self.viewer_stack = QStackedWidget()

        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        stage = QFrame()
        stage.setObjectName("stage")
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = MediaPreview()
        self.preview.choose_requested.connect(self.choose_folder)
        self.preview.video_error.connect(
            lambda text: self.status(tr("status.video_failed", error=text), "warning"))
        stage_layout.addWidget(self.preview)
        column.addWidget(stage, 1)
        self.metadata_bar = self._build_metadata_bar()
        column.addWidget(self.metadata_bar)

        strip_holder = QFrame()
        strip_layout = QHBoxLayout(strip_holder)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(0)
        self.filmstrip = Filmstrip(self.thumbs)
        self.filmstrip.path_selected.connect(self._filmstrip_selected)
        strip_layout.addWidget(self.filmstrip, 1)
        column.addWidget(strip_holder)
        self.filmstrip_holder = strip_holder
        self.viewer_stack.addWidget(page)

        grid_page = QWidget()
        grid_layout = QVBoxLayout(grid_page)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(6)
        # The toolbar wires signals to the grid, so the grid must already
        # exist while the toolbar is being built.  Creating it afterwards
        # made every real MainWindow construction fail with AttributeError.
        self.grid = ThumbnailGrid(self.thumbs)
        grid_layout.addLayout(self._build_grid_toolbar())
        self.grid.path_activated.connect(self._grid_activated)
        self.grid.selection_changed.connect(self._grid_selection_changed)
        grid_layout.addWidget(self.grid, 1)
        self.viewer_stack.addWidget(grid_page)
        return self.viewer_stack

    def _build_grid_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        from PySide6.QtWidgets import QSlider
        self.grid_size = QSlider(Qt.Orientation.Horizontal)
        self.grid_size.setRange(96, 320)
        self.grid_size.setValue(168)
        self.grid_size.setFixedWidth(120)
        # Resizing rebuilds every row of the grid. Doing that on each tick of a
        # drag cost 69 ms a tick with five thousand items, so wait for a pause.
        self._grid_size_timer = QTimer(self)
        self._grid_size_timer.setSingleShot(True)
        self._grid_size_timer.setInterval(150)
        self._grid_size_timer.timeout.connect(self._apply_grid_size)
        self.grid_size.valueChanged.connect(lambda _value: self._grid_size_timer.start())
        self.grid_size.sliderReleased.connect(self._apply_grid_size)
        size_icon = QLabel()
        size_icon.setPixmap(icons.pixmap("grid", 15, theme.FAINT))
        row.addWidget(size_icon)
        row.addWidget(self.grid_size)
        row.addSpacing(6)
        self.show_names = QCheckBox(tr("grid.show_filename"))
        self.show_names.setChecked(True)
        self.show_names.toggled.connect(self.grid.set_show_names)
        row.addWidget(self.show_names)
        row.addStretch(1)
        self.grid_selected = QLabel("")
        self.grid_selected.setObjectName("markTag")
        self.grid_selected.setVisible(False)
        row.addWidget(self.grid_selected)
        row.addSpacing(6)
        self._grid_row = row
        self._grid_stamps_at = row.count()
        row.addSpacing(6)
        self.select_all_button = text_button(tr("select_all"), "quietButton")
        self.select_all_button.clicked.connect(
            lambda: self.grid.select_all_paths(self.engine.queue_paths))
        self.invert_button = text_button(tr("invert_selection"), "quietButton")
        self.invert_button.clicked.connect(self.grid.invert_selection)
        self.clear_selection_button = text_button(tr("clear_selection"), "quietButton")
        self.clear_selection_button.clicked.connect(self.grid.clearSelection)
        for widget in (self.select_all_button, self.invert_button, self.clear_selection_button):
            row.addWidget(widget)
        return row

    def _apply_grid_size(self) -> None:
        self._grid_size_timer.stop()
        self.grid.set_edge(self.grid_size.value())

    def _build_metadata_bar(self) -> QFrame:
        """The backprint line: what the print on the counter is, how it is marked, where it sits."""
        bar = QFrame()
        row = QHBoxLayout(bar)
        row.setContentsMargins(2, 5, 0, 3)
        row.setSpacing(6)
        self.filename_label = QLabel("")
        self.filename_label.setObjectName("filename")
        self.filename_label.setSizePolicy(QSizePolicy.Policy.Maximum,
                                          QSizePolicy.Policy.Preferred)
        row.addWidget(self.filename_label)
        row.addSpacing(6)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("fileDetail")
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                        QSizePolicy.Policy.Preferred)
        row.addWidget(self.detail_label, 1)

        self.sidecar_badge = QLabel("")
        self.sidecar_badge.setObjectName("tag")
        self.sidecar_badge.setVisible(False)
        row.addWidget(self.sidecar_badge)
        row.addSpacing(6)
        self._backprint_row = row
        self._backprint_stamps_at = row.count()
        row.addSpacing(10)

        self.info_button = text_button(tr("tool.info"), "quietButton", "info")
        self.info_button.clicked.connect(self.show_info)
        row.addWidget(self.info_button)
        self.rotate_left = icon_button("undo", tr("tool.rotate_left"), 15, "navButton")
        self.rotate_left.clicked.connect(lambda: self.preview.image.rotate_by(-90))
        self.rotate_right = icon_button("redo", tr("tool.rotate_right"), 15, "navButton")
        self.rotate_right.clicked.connect(lambda: self.preview.image.rotate_by(90))
        row.addWidget(self.rotate_left)
        row.addWidget(self.rotate_right)
        row.addSpacing(4)
        self.counter_label = CountLabel("0 / 0", 13, theme.PAPER)
        self.counter_label.setMinimumWidth(88)
        row.addWidget(self.counter_label)
        self.previous_button = icon_button("chevron-left", tr("tool.previous"), 16, "navButton")
        self.previous_button.clicked.connect(lambda: self.step(-1))
        self.next_button = icon_button("chevron-right", tr("tool.next"), 16, "navButton")
        self.next_button.clicked.connect(lambda: self.step(1))
        row.addWidget(self.previous_button)
        row.addWidget(self.next_button)
        return bar

    def _build_envelopes(self) -> QHBoxLayout:
        """The ten keys, as envelopes along the front edge of the counter."""
        row = QHBoxLayout()
        row.setSpacing(6)
        height = self._envelope_height()
        for index in range(config.BINDING_COUNT):
            card = BindingCard(index, motion=self._motion)
            card.set_envelope_height(height)
            card.activated.connect(self.classify_index)
            card.folder_requested.connect(self.choose_binding_folder)
            row.addWidget(card, 1)
            self._binding_cards.append(card)
        return row

    def _envelope_height(self) -> int:
        return int(theme.metrics(self.settings.density)["button"] * 2.1)

    def _build_status(self) -> QHBoxLayout:
        """The counter's front ledger: which set of envelopes, then how the work stands."""
        row = QHBoxLayout()
        self._ledger_row = row
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        self.preset_label = QLabel(tr("side.preset"))
        self.preset_label.setObjectName("caption")
        row.addWidget(self.preset_label)
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(110)
        self.preset_combo.currentTextChanged.connect(self._switch_preset)
        row.addWidget(self.preset_combo)
        self.preset_menu_button = icon_button("plus", tr("side.preset_new"), 15)
        self.preset_menu_button.clicked.connect(self._preset_menu)
        row.addWidget(self.preset_menu_button)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("search")
        self.search_edit.setPlaceholderText(tr("side.search_targets"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.addAction(icons.icon("search", 14, theme.FAINT),
                                   QLineEdit.ActionPosition.LeadingPosition)
        self.search_edit.textChanged.connect(self._filter_bindings)
        self.search_edit.returnPressed.connect(self._activate_first_binding)
        row.addWidget(self.search_edit)
        self.bindings_button = icon_button("edit", tr("bind.title"), 15)
        self.bindings_button.clicked.connect(self.open_bindings)
        row.addWidget(self.bindings_button)
        self.keys_button = icon_button("keyboard", tr("side.keyhint"), 16)
        row.addWidget(self.keys_button)
        row.addWidget(separator(length=14))

        self.status_label = ElidedLabel(tr("status.ready"), "statusBar")
        row.addWidget(self.status_label, 1)
        # Copying or favouriting marks a file handled and hides it from every
        # later queue; this is the way back.
        self.handled_button = text_button("", "quietButton")
        self.handled_button.setVisible(False)
        self.handled_button.clicked.connect(self.reveal_handled)
        row.addWidget(self.handled_button)
        self.undo_button = text_button(tr("side.undo"), "quietButton", "undo")
        self.undo_button.setToolTip("Ctrl+Z")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = text_button(tr("side.redo"), "quietButton", "redo")
        self.redo_button.setToolTip("Ctrl+Y")
        self.redo_button.setEnabled(False)
        self.redo_button.clicked.connect(self.redo)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        row.addWidget(separator(length=14))
        self.queue_label = QLabel("")
        self.queue_label.setObjectName("statusBar")
        row.addWidget(self.queue_label)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("statusBar")
        row.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(96)
        self.progress_bar.setTextVisible(False)
        row.addWidget(self.progress_bar)
        self._quota_separator = separator(length=14)
        row.addWidget(self._quota_separator)
        self.quota_label = QLabel("")
        self.quota_label.setObjectName("statusBar")
        row.addWidget(self.quota_label)
        self.quota_bar = QProgressBar()
        self.quota_bar.setObjectName("quotaBar")
        self.quota_bar.setFixedWidth(56)
        self.quota_bar.setTextVisible(False)
        row.addWidget(self.quota_bar)
        return row

    # ============================================================ shortcuts
    def _shortcut(self, sequence: str, handler, repeat: bool = False) -> QShortcut:
        item = QShortcut(QKeySequence(sequence), self)
        item.setContext(Qt.ShortcutContext.WindowShortcut)
        item.setAutoRepeat(repeat)
        item.activated.connect(handler)
        return item

    def _build_shortcuts(self) -> None:
        pairs = [
            ("Ctrl+Z", self.undo), ("Ctrl+Y", self.redo), ("Ctrl+Shift+Z", self.redo),
            ("F5", self.rescan), ("F11", self.toggle_fullscreen),
            ("Space", self.preview.toggle_play), ("K", self.preview.toggle_play),
            ("J", lambda: self.preview.seek_relative(-5000)),
            ("L", lambda: self.preview.seek_relative(5000)),
            (",", lambda: self._step_frame(-1)), (".", lambda: self._step_frame(1)),
            ("S", self.skip_current), ("G", self._toggle_view),
            ("M", self.preview.toggle_mute),
            ("F2", self.rename_current), ("Delete", self.trash_current),
            ("Ctrl+F", self.search_edit.setFocus), ("/", self.search_edit.setFocus),
            ("Ctrl+D", self.open_duplicates), ("Ctrl+,", self.open_settings),
            ("Ctrl+I", self.show_info), ("Ctrl+R", self.toggle_review),
        ]
        self._fixed_shortcuts = [self._shortcut(sequence, handler)
                                 for sequence, handler in pairs]
        # The only keys that repeat while held: see `_arrow`. `_set_view` turns
        # them off in the grid, where the arrows belong to the grid itself.
        self._arrow_shortcuts = [self._shortcut("Left", lambda: self._arrow(-1), repeat=True),
                                 self._shortcut("Right", lambda: self._arrow(1), repeat=True)]
        self._fixed_shortcuts.extend(self._arrow_shortcuts)
        for value in range(1, 6):
            self._fixed_shortcuts.append(
                self._shortcut(f"Shift+{value}", lambda v=value: self._rate_current(v)))
            self._fixed_shortcuts.append(
                self._shortcut(f"Alt+{value}", lambda v=value: self._label_by_index(v)))
        self._fixed_shortcuts.append(self._shortcut("Shift+0", lambda: self._rate_current(0)))
        self._fixed_shortcuts.append(self._shortcut("Alt+0", lambda: self._label_current("")))
        escape = self._shortcut("Escape", self._escape)
        self._fixed_shortcuts.append(escape)
        self._install_binding_shortcuts()

    def reserved_keys(self) -> list[str]:
        """Keys the window itself answers to, which no binding may take."""
        return [item.key().toString() for item in self._fixed_shortcuts]

    def _install_binding_shortcuts(self) -> None:
        for item in self._binding_shortcuts:
            item.setEnabled(False)
            item.deleteLater()
        self._binding_shortcuts.clear()
        # Two shortcuts on one key fire neither. A clash saved before the key
        # editor refused them keeps the built-in key and says so.
        clashes = config.reserved_conflicts(self.settings.bindings, self.reserved_keys())
        for index, binding in enumerate(self.settings.bindings):
            if not binding.key or binding.key in clashes:
                continue
            self._binding_shortcuts.append(
                self._shortcut(binding.key, lambda i=index: self.classify_index(i)))
        self._clash_notice = tr("status.reserved_key", keys=", ".join(clashes)) if clashes else ""
        if self._clash_notice:
            self.status(self._clash_notice, "warning")

    def _escape(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        elif self.search_edit.hasFocus():
            self.search_edit.clear()
            self.setFocus()

    # ================================================================ state
    def _first_run(self) -> None:
        # A queued zero-delay timer can fire during any processEvents(), so
        # this must be idempotent and must never run inside another scan.
        if self._started or self._busy:
            return
        self._started = True
        self.centralWidget().setFocus()
        self._retranslate()
        self._refresh_presets()
        self._refresh_bindings()
        self._update_quota()
        if self.engine.has_pending():
            self.recover_button.setVisible(True)
            self.status(tr("error.pending_first"), "warning")
            QTimer.singleShot(200, self.recover_pending)
            return
        self._open_initial_folder()
        if self._clash_notice:
            self.status(self._clash_notice, "warning")

    def _open_initial_folder(self) -> None:
        if self.startup_folder is not None and self.startup_folder.is_dir():
            self.open_folder(self.startup_folder)
            return
        folder = self.settings.source_folder
        if self.settings.restore_position and folder and Path(folder).is_dir():
            self.open_folder(Path(folder), restore=True)
        else:
            self._refresh_view()

    def status(self, message: str, tone: str = "normal") -> None:
        self.status_label.setText(message)
        self.status_label.setProperty("tone", tone)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _report(self, error: BaseException) -> None:
        if isinstance(error, Cancelled):
            self.status(tr("error.cancelled"), "normal")
            return
        key = getattr(error, "key", "")
        fields = getattr(error, "fields", {}) or {}
        message = tr(key, **fields) if key else str(error)
        log.warning("operation failed: %s", error)
        self.status(message, "error")
        QMessageBox.warning(self, tr("error.title"), message)

    # ============================================================ language
    def _change_language(self, code: str) -> None:
        if code not in LANGUAGE_CODES:
            return
        set_language(code)
        self.settings.language = code
        self.engine.save_settings()
        self._retranslate()

    def _toggle_language(self) -> None:
        self._change_language("en" if get_language() == "zh" else "zh")

    def _retranslate(self) -> None:
        self.setWindowTitle(f"{__display_name__} · {tr('app.tagline')}")
        self._update_slip()
        self.choose_button.setText(tr("header.choose_folder"))
        self.rescan_button.setToolTip(tr("header.rescan_tip"))
        self.settings_button.setToolTip(tr("settings.title"))
        # The button names the language it switches to.
        self.language_button.setText("EN" if get_language() == "zh" else "中")
        self.language_button.setToolTip(tr("header.language_tip"))
        self.view_switch.set_options(
            [(config.VIEW_SINGLE, tr("view.single")), (config.VIEW_GRID, tr("view.grid"))],
            {config.VIEW_SINGLE: "single", config.VIEW_GRID: "grid"})
        self.view_switch.set_value(self.view_mode, quiet=True)

        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        for mode in scanner.FILTERS:
            key = f"filter.{mode}" if mode != "short" else "filter.short_video"
            label = (tr(key, seconds=self.settings.short_video_seconds)
                     if mode == "short" else tr(key))
            self.filter_combo.addItem(label, mode)
        index = self.filter_combo.findData(self.settings.filter_mode)
        self.filter_combo.setCurrentIndex(max(0, index))
        self.filter_combo.blockSignals(False)

        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        for mode in scanner.SORTS:
            self.sort_combo.addItem(tr(f"sort.{mode}"), mode)
        index = self.sort_combo.findData(self.settings.sort_mode)
        self.sort_combo.setCurrentIndex(max(0, index))
        self.sort_combo.blockSignals(False)

        self.reverse_button.setToolTip(tr("sort.reverse"))
        self.recursive_check.setText(tr("scan.recursive"))
        self.duplicates_button.setText(tr("tool.duplicates"))
        self.more_button.setText(tr("tool.more"))
        self.history_action.setText(tr("tool.history"))
        self.stats_action.setText(tr("tool.stats"))
        self.backup_action.setText(tr("tool.backups"))
        self.recover_button.setText(tr("tool.recover"))
        self.info_button.setText(tr("tool.info"))
        self.rotate_left.setToolTip(tr("tool.rotate_left"))
        self.rotate_right.setToolTip(tr("tool.rotate_right"))
        self.previous_button.setToolTip(tr("tool.previous"))
        self.next_button.setToolTip(tr("tool.next"))
        self.show_names.setText(tr("grid.show_filename"))
        self.select_all_button.setText(tr("select_all"))
        self.invert_button.setText(tr("invert_selection"))
        self.clear_selection_button.setText(tr("clear_selection"))
        self.rating.setToolTip(f"{tr('side.rating')} · Shift + 1–5")
        self.labels.setToolTip(f"{tr('side.colour_label')} · Alt + 1–5")
        self.bindings_button.setToolTip(tr("bind.title"))
        self.keys_button.setToolTip(tr("side.keyhint"))
        self.preset_label.setText(tr("side.preset"))
        self.preset_menu_button.setToolTip(tr("side.preset_new"))
        self.undo_button.setText(tr("side.undo"))
        self.redo_button.setText(tr("side.redo"))
        self._compact_header()
        self._refresh_bindings()
        self._refresh_view()
        self._update_quota()
        self._update_handled()

    def _compact_header(self) -> None:
        """The header's text buttons keep their words only while the header fits with them.

        Asked of the row itself, in whichever language is showing. The folder slip
        is what gives way otherwise, and a folder name cut to three letters tells
        nobody which folder is on the counter.
        """
        self._header_words(True)
        if self._overflows(self._header_row):
            self._header_words(False)
        self._fit_ledger()

    def _overflows(self, row: QHBoxLayout) -> bool:
        """True when *row* cannot hold its widgets even at their smallest."""
        # New words on a button mark only the window's layout stale; the row
        # would otherwise answer with the minimum it worked out last time.
        row.invalidate()
        # The row's own geometry still has the old width while the window is
        # being resized, so the room comes from the counter and its margins.
        central = self.centralWidget()
        margins = central.layout().contentsMargins()
        room = central.width() - margins.left() - margins.right()
        return row.minimumSize().width() > room

    def _header_words(self, words: bool) -> None:
        for button, key in ((self.choose_button, "header.choose_folder"),
                            (self.duplicates_button, "tool.duplicates")):
            button.setText(tr(key) if words else "")
            button.setToolTip("" if words else tr(key))
        self.more_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon if words
            else Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.more_button.setToolTip("" if words else tr("tool.more"))
        self.recursive_check.setText(tr("scan.recursive") if words else "")
        self.recursive_check.setIcon(QIcon() if words
                                     else icons.icon("subfolders", 15, theme.PAPER_DIM))
        self.recursive_check.setToolTip("" if words else tr("scan.recursive"))
        policy = (QComboBox.SizeAdjustPolicy.AdjustToContents if words
                  else QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for combo in (self.filter_combo, self.sort_combo):
            combo.setMinimumContentsLength(6)
            combo.setSizeAdjustPolicy(policy)

    def _fit_ledger(self) -> None:
        """Where the last file went outranks everything else on the status line.

        The message is kept wide enough for a whole filing report. The preset
        caption, the long search hint and the snapshot figures give way only when
        the row cannot hold them as well, which is asked of the row itself.
        """
        metrics = self.status_label.fontMetrics()
        report = tr("status.done_action", action=tr("action.move"), name="IMG_0000.JPG")
        self.status_label.setMinimumWidth(metrics.horizontalAdvance(report))
        self._ledger_buttons(True)
        self._ledger_extras(True)
        if self._overflows(self._ledger_row):
            self._ledger_extras(False)
        if self._overflows(self._ledger_row):
            self._ledger_buttons(False)
        self._ledger_fit_sizes = self._ledger_sizes()

    def _ledger_sizes(self) -> tuple[int, int, int, bool]:
        return (self.progress_label.sizeHint().width(), self.queue_label.sizeHint().width(),
                len(str(self._handled_count)), self.handled_button.isVisibleTo(self))

    def _ledger_buttons(self, words: bool) -> None:
        count = self._handled_count
        full = tr("status.hidden_handled", count=count)
        self.handled_button.setText(full if words else str(count))
        self.handled_button.setToolTip("" if words else full)
        self.handled_button.setIcon(QIcon() if words else icons.icon("history", 15, theme.PAPER_DIM))
        for button, key, shortcut in ((self.undo_button, "side.undo", "Ctrl+Z"),
                                      (self.redo_button, "side.redo", "Ctrl+Y")):
            button.setText(tr(key) if words else "")
            button.setToolTip(shortcut if words else f"{tr(key)} · {shortcut}")

    def _schedule_ledger_fit(self) -> None:
        if not self._started or self._ledger_fit_timer.isActive():
            return
        sizes = self._ledger_sizes()
        previous = self._ledger_fit_sizes
        if sizes != previous:
            self._ledger_fit_timer.start(0)

    def _ledger_extras(self, shown: bool) -> None:
        self.preset_label.setVisible(shown)
        hint = tr("side.search_targets") if shown else tr("side.search_short")
        self.search_edit.setPlaceholderText(hint)
        # Room for the words, the search icon and the clear button, each of those
        # about as wide as the field is tall.
        self.search_edit.setFixedWidth(self.search_edit.fontMetrics().horizontalAdvance(hint)
                                       + 2 * self.search_edit.sizeHint().height())
        # A bar with no figure beside it says nothing, so the pair goes together.
        for widget in (self.quota_label, self.quota_bar, self._quota_separator):
            widget.setVisible(shown)

    def resizeEvent(self, event) -> None:          # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self._started:
            self._compact_header()

    def _update_slip(self) -> None:
        """The pickup slip: which folder is on the counter, and how many items it holds."""
        root = self.engine.source_root
        count = len(self.engine.all_files) if root else 0
        counted = tr("header.item_count", count=count)
        self.source_slip.set_folder(str(root or ""), counted if root else "",
                                    tr("header.no_folder"))
        self.source_slip.setToolTip(f"{root}\n{counted}" if root else tr("header.choose_folder"))

    # ============================================================= folders
    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, tr("header.choose_folder"),
            self.settings.source_folder or str(Path.home()))
        if folder:
            self.open_folder(Path(folder))

    def open_folder(self, folder: Path, restore: bool = False) -> None:
        if self._busy:
            return
        if not self._drain_queue():
            return
        try:
            self._with_progress(tr("scan.scanning"),
                                lambda progress, cancel: self.engine.open_folder(
                                    folder, progress, cancel))
        except Cancelled:
            self._after_queue_change("")
            return
        except Exception as error:      # noqa: BLE001 - report every scan failure
            self._report(error)
            self._after_queue_change("")
            return
        self.preloader.clear()
        self._conflict_defaults.clear()
        self._update_slip()
        self.engine.save_settings()
        if restore and self.settings.last_path:
            self.engine.go_to(self.settings.last_path)
        self._after_queue_change(tr("scan.complete", count=len(self.engine.all_files)))

    def rescan(self) -> None:
        if self._busy or not self.engine.source_root:
            return
        if not self._drain_queue():
            return
        try:
            self._with_progress(tr("scan.scanning"),
                                lambda progress, cancel: self.engine.rescan(progress, cancel))
        except (TransactionError, OSError) as error:
            self._report(error)
            return
        except Cancelled:
            return
        self._conflict_defaults.clear()
        self._after_queue_change(tr("scan.complete", count=len(self.engine.all_files)))

    def _rebuild_queue(self) -> None:
        if self._busy:
            return
        try:
            if len(self.engine.all_files) > 2000:
                # Filtering and date-sorting a big library takes long enough
                # that silence would read as a freeze.
                self._with_progress(tr("scan.filtering"),
                                    lambda progress, cancel:
                                    self.engine.rebuild_queue(progress, cancel))
            else:
                self.engine.rebuild_queue()
        except Cancelled:
            return
        except OSError as error:
            self._report(error)
            return
        self._after_queue_change("")

    def _after_queue_change(self, message: str) -> None:
        self._update_slip()
        self._refresh_browsers()
        self._refresh_view()
        self._update_quota()
        self._update_handled()
        if message:
            self.status(message, "success")

    def _update_handled(self) -> None:
        count = self.engine.hidden_handled()
        self._handled_count = count
        if self.handled_button.isVisibleTo(self) != (count > 0):
            self.handled_button.setVisible(count > 0)
        self._schedule_ledger_fit()

    def reveal_handled(self) -> None:
        if self._blocked() or not self.engine.source_root:
            return
        # A copy still in the queue would mark its file again behind this.
        if not self._drain_queue():
            return
        try:
            count = self._with_progress(
                tr("scan.filtering"),
                lambda progress, cancel: self.engine.reveal_handled(progress, cancel))
        except Cancelled:
            return
        self._after_queue_change(tr("status.handled_revealed", count=count))

    def _refresh_browsers(self) -> None:
        paths = self.engine.queue_paths
        decorations = self._decorations(paths)
        self.filmstrip.set_queue(paths, self.engine.index, decorations)
        self._grid_dirty = True
        if self.view_mode == config.VIEW_GRID:
            self._fill_grid()

    def _fill_grid(self) -> None:
        """Populate the grid, but only when it is on screen and out of date."""
        if not self._grid_dirty or self._grid_building:
            return
        paths = list(self.engine.queue_paths)
        decorations = self._decorations(paths)
        if len(paths) <= 1500:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.grid.set_paths(paths, decorations)
            finally:
                QApplication.restoreOverrideCursor()
            self._grid_dirty = False
            return
        # A very long queue is built in chunks so the window keeps painting and
        # the user can back out instead of watching it appear to hang. The dirty
        # flag stays up for the whole build: the chunked pass pumps events, and
        # anything that inserts a row into a half-built grid puts it out of step
        # with the queue for good.
        self._grid_building = True
        self._grid_dirty = False
        try:
            completed = self._with_progress(
                tr("scan.filtering"),
                lambda progress, cancel: self.grid.set_paths_chunked(
                    paths, decorations, progress, cancel))
        finally:
            self._grid_building = False
        # Backed out part-way leaves the grid incomplete, so it stays dirty --
        # and so does a change that arrived through the events the build pumped.
        if not completed:
            self._grid_dirty = True

    def _decorations(self, paths) -> dict:
        """Only files that actually carry a rating or a label need an entry."""
        tags = self.engine.state.tags_for([str(p) for p in paths])
        return {path: {"rating": rating, "label": label}
                for path, (rating, label) in tags.items() if rating or label}

    # ============================================================== cursor
    def step(self, offset: int) -> None:
        if self.engine.step(offset) is not None:
            self._refresh_view()

    def _arrow(self, offset: int) -> None:
        """Left and Right. A tap renders in full; a held key flips.

        Key repeats arrive thirty times a second and a full render decodes a
        photograph, so holding the key used to be switched off altogether.
        """
        if not self._arrows.repeating:
            self.step(offset)
            return
        if self.view_mode != config.VIEW_SINGLE:
            return                  # the grid shows no cursor for a held key to move
        if self.engine.step(offset) is not None:
            self._flip()

    def _flip(self) -> None:
        """One repeat of a held arrow key: move on and show only what is in hand.

        The counter, the name, the filmstrip cursor and a picture that is
        already decoded. Anything that reads metadata, queries the database or
        decodes on this thread waits for `_settle`.
        """
        path = self.engine.current_path()
        if path is None:
            self.preloader.set_wanted([], self._preview_target())
            return
        self.counter_label.setText(f"{self.engine.index + 1} / {len(self.engine.queue_paths)}")
        elide(self.filename_label, path.name, max(160, self.filename_label.width()))
        self.detail_label.setText("")
        self.sidecar_badge.setVisible(False)
        self.filmstrip.follow(self.engine.index)
        self._flipped.append(str(path))
        pixmap = self.preloader.take(path, self._preview_target())
        if pixmap is None:
            pixmap = self.thumbs.peek(path, HOLD_EDGE)
        if pixmap is not None:
            self.preview.show_still(path, pixmap)
        else:
            # Decode on a worker. Only the last few items stay wanted, so a long
            # hold never leaves a backlog behind it.
            self.thumbs.set_wanted(self._flipped, HOLD_EDGE)
            self.thumbs.request(path, HOLD_EDGE)
        self._settle_pending = True
        self._settle_timer.start()

    def _flip_arrived(self, path: str, edge: int, pixmap) -> None:
        """A held-key thumbnail landed. Show it if the flip has not gone far past it."""
        if edge != HOLD_EDGE or not self._settle_pending or path not in self._flipped:
            return
        self.preview.show_still(Path(path), pixmap)

    def _settle(self) -> None:
        """The arrow key was let go: render the item it stopped on, in full."""
        if not self._settle_pending:
            return
        if QApplication.activeModalWidget() is not None:
            self._settle_timer.start()      # after the dialog, not decoding behind it
            return
        self._flipped.clear()
        if self.view_mode == config.VIEW_SINGLE:
            self._refresh_view()
        self._settle_pending = False

    def _guard_unseen_key_action(self) -> bool:
        """Show the selected photo before accepting an action on it."""
        if self._settle_pending and self.preview.current_path != self.engine.current_path():
            self._settle()
            return True
        return False

    def _filmstrip_selected(self, path: str) -> None:
        if self.engine.go_to(path):
            self._refresh_view(scroll_strip=False)

    def _grid_activated(self, path: str) -> None:
        if self.engine.go_to(path):
            self._set_view(config.VIEW_SINGLE)

    def _grid_selection_changed(self, count: int) -> None:
        # More than one chosen means the next key files all of them.
        self.grid_selected.setText(tr("side.bulk_subtitle", count=count) if count > 1
                                   else tr("grid.selected", count=count))
        self.grid_selected.setVisible(bool(count))

    def _refresh_view(self, scroll_strip: bool = True) -> None:
        # A full render supersedes whatever a held arrow key left pending.
        self._settle_pending = False
        self._settle_timer.stop()
        path = self.engine.current_path()
        total = len(self.engine.queue_paths)
        self.counter_label.setText(f"{self.engine.index + 1 if total else 0} / {total}")
        for widget in (self.previous_button, self.next_button, self.info_button,
                       self.rotate_left, self.rotate_right):
            widget.setEnabled(path is not None)

        self.metadata_bar.setVisible(path is not None)
        self.filmstrip_holder.setVisible(total > 0)
        if path is None:
            self.preloader.set_wanted([], self._preview_target())
            if not self.engine.source_root:
                self.preview.show_empty(tr("header.no_folder"), tr("scan.hint_start"))
            else:
                self.preview.show_empty(
                    tr("scan.review_empty") if self.engine.review_mode
                    else tr("scan.no_media"), tr("scan.hint_adjust"))
            self.filename_label.setText("")
            self.detail_label.setText("")
            self.sidecar_badge.setVisible(False)
            self.rating.set_value(0)
            self.labels.set_value("")
            self._update_actions()
            return

        # Nothing of the single view is on screen in the grid, so decoding and
        # prefetching here would only spend time on a page nobody can see.
        if self.view_mode == config.VIEW_SINGLE:
            target = self._preview_target()
            queue = self.engine.queue_paths
            index = self.engine.index
            wanted = [path] + ([queue[(index + step) % total]
                                 for step in (1, 2, -1)] if total > 1 else [])
            self.preloader.set_wanted(wanted, target)
            ready = self.preloader.take(path, target)
            if ready is None and decodes_slowly(path):
                # A large PNG or TIFF cannot be scaled while it is decoded, so it
                # would hold the window for seconds. Show its name, decode on a
                # worker, and swap the picture in when it lands.
                self.preview.show_loading(path, path.name, tr("scan.reading"))
                self.preloader.request(path, target)
            else:
                ok, error = self.preview.show_path(path, ready)
                if not ok:
                    self.status(tr("status.preview_failed", name=path.name, error=error),
                                "warning")
            self._prefetch_neighbours()
        else:
            self.preloader.set_wanted([], self._preview_target())
        elide(self.filename_label, path.name, max(160, self.filename_label.width()))
        info = metadata.read(path)
        bits = []
        if info.width:
            bits.append(f"{info.width} × {info.height}")
        bits.append(human_size(info.size))
        when = info.when().strftime("%Y-%m-%d %H:%M")
        bits.append(when + (" *" if info.captured_is_fallback else ""))
        for value in (info.camera, info.lens, info.iso and f"ISO {info.iso}",
                      info.aperture, info.shutter, metadata.format_duration(info.duration)):
            if value:
                bits.append(str(value))
        elide(self.detail_label, "  ·  ".join(bits), max(200, self.detail_label.width()))

        group = self.engine.group_for(path)
        if group.sidecars:
            self.sidecar_badge.setText(tr("sidecar.badge", count=len(group.sidecars)))
            self.sidecar_badge.setVisible(True)
        else:
            self.sidecar_badge.setVisible(False)

        rating, label = self.engine.state.tag(str(path))
        self.rating.set_value(rating)
        self.labels.set_value(label)
        if scroll_strip and self.view_mode == config.VIEW_SINGLE:
            self.filmstrip.follow(self.engine.index)
        self._update_actions()

    def _preview_arrived(self, path: str) -> None:
        """A worker finished a decode; show it if it is still the current file."""
        current = self.engine.current_path()
        if current is None or str(current) != path:
            return
        if self.preview.stack.currentIndex() != self.preview.EMPTY:
            return                      # something else is already on screen
        target = self._preview_target()
        ready = self.preloader.take(current, target)
        if ready is None:
            error = self.preloader.error(current, target)
            if error:
                self._preview_retries.pop(path, None)
                message = tr("status.preview_failed", name=Path(path).name, error=error)
                self.preview.show_empty(message)
                self.status(message, "warning")
                return
            # The target size is part of the cache key, so a window resized
            # while this was decoding misses. Ask again at the size we want now
            # rather than declaring the file unreadable.
            if self._preview_retries.get(path, 0) < 2 and self.preloader.request(
                    current, target):
                self._preview_retries[path] = self._preview_retries.get(path, 0) + 1
                return
            self._preview_retries.pop(path, None)
            self.preview.show_empty(tr("status.preview_failed", name=Path(path).name,
                                       error=""))
            return
        self._preview_retries.pop(path, None)
        ok, error = self.preview.show_path(current, ready)
        if not ok:
            self.status(tr("status.preview_failed", name=Path(path).name, error=error),
                        "warning")

    def _preview_target(self) -> QSize:
        surface = self.preview.image
        return QSize(max(1400, surface.width() * 2), max(1000, surface.height() * 2))

    def _prefetch_neighbours(self) -> None:
        """Decode the next and previous frames while the user looks at this one."""
        queue = self.engine.queue_paths
        total = len(queue)
        if total < 2:
            return
        index = self.engine.index
        ahead = [queue[(index + step) % total] for step in (1, 2, -1)]
        self.preloader.prefetch(ahead, self._preview_target())

    def _update_actions(self) -> None:
        root = str(self.engine.source_root or "")
        # A journal can survive a previous session, but without a selected
        # source folder it is not an action the user can safely perform here.
        self.undo_button.setEnabled(bool(root) and self.engine.can_undo())
        self.redo_button.setEnabled(bool(root) and self.engine.can_redo())
        count = self.engine.state.review_count(root) if root else 0
        self.review_button.setText(
            tr("side.review_exit", count=count) if self.engine.review_mode
            else tr("side.review_queue", count=count))
        self.review_button.setProperty("active", "true" if self.engine.review_mode else "false")
        self.review_button.style().unpolish(self.review_button)
        self.review_button.style().polish(self.review_button)
        self.recover_button.setVisible(self.engine.has_pending() and
                                       not self.engine.queue.pending)
        handled = self.engine.stats.total()
        total = handled + len(self.engine.queue_paths)
        self.progress_label.setText(tr("status.session_progress", done=handled, total=total))
        self._schedule_ledger_fit()
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(handled)

    def _update_quota(self) -> None:
        used = self.engine.backup_usage()
        cap = max(1, self.settings.quota.max_bytes)
        usage = tr("status.snapshot_usage", used=human_size(used), cap=human_size(cap))
        self.quota_label.setText(usage)
        self.quota_bar.setToolTip(usage)
        percent = min(100, int(used * 100 / cap))
        self.quota_bar.setRange(0, 100)
        self.quota_bar.setValue(percent)
        self.quota_bar.setProperty("full", "true" if percent > 85 else "false")
        self.quota_bar.style().unpolish(self.quota_bar)
        self.quota_bar.style().polish(self.quota_bar)

    # ============================================================== views
    def _set_view(self, mode: str) -> None:
        self.view_mode = mode
        self.view_switch.set_value(mode, quiet=True)
        self._place_stamps(mode)
        self.viewer_stack.setCurrentIndex(0 if mode == config.VIEW_SINGLE else 1)
        # Left/Right belong to whichever view is on screen: the grid moves its
        # own cursor with them, and a hidden page must not decode anything.
        for shortcut in self._arrow_shortcuts:
            shortcut.setEnabled(mode == config.VIEW_SINGLE)
        if mode == config.VIEW_SINGLE:
            # The grid cursor is the only one the user could see; come back to it.
            chosen = self.grid.current_path()
            if chosen is not None:
                self.engine.go_to(chosen)
            self._refresh_view()
        else:
            self.preview.release()
            self._fill_grid()
            current = self.engine.current_path()
            if current is not None:
                self.grid.show_path(current)
            self.grid.setFocus()

    def _change_view(self, mode: str) -> None:
        self._set_view(mode)

    def _toggle_view(self) -> None:
        if self._blocked():
            return
        self._set_view(config.VIEW_GRID if self.view_mode == config.VIEW_SINGLE
                       else config.VIEW_SINGLE)

    def toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # =========================================================== filtering
    def _filter_changed(self) -> None:
        if not self._drain_queue():
            return
        self.settings.filter_mode = str(self.filter_combo.currentData() or "all")
        self.engine.save_settings()
        self._rebuild_queue()

    def _sort_changed(self) -> None:
        if not self._drain_queue():
            return
        self.settings.sort_mode = str(self.sort_combo.currentData() or "name")
        self.engine.save_settings()
        self._rebuild_queue()

    def _reverse_changed(self, checked: bool) -> None:
        if not self._drain_queue():
            return
        self.settings.sort_reverse = bool(checked)
        self.engine.save_settings()
        self._rebuild_queue()

    def _recursive_changed(self, checked: bool) -> None:
        if self._blocked():
            # Put the box back without coming straight back in here.
            self.recursive_check.blockSignals(True)
            self.recursive_check.setChecked(self.settings.recursive)
            self.recursive_check.blockSignals(False)
            return
        if not self._drain_queue():
            return
        self.settings.recursive = bool(checked)
        self.engine.save_settings()
        if checked:
            self.rescan()            # subfolders have to be read to be listed
            return
        # Turning it off only ever removes files, and they are already in hand.
        change = self.engine.drop_subfolders()
        if change is None:
            self._rebuild_queue()
            return
        self._apply_change(change)
        self._update_slip()

    def toggle_review(self) -> None:
        if self._blocked() or not self.engine.source_root:
            return
        self.engine.set_review_mode(not self.engine.review_mode)
        self._after_queue_change(tr("status.enter_review") if self.engine.review_mode
                                 else tr("status.leave_review"))

    # ============================================================ bindings
    def _refresh_presets(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.settings.ensure_profile()
        self.preset_combo.addItems(list(self.settings.profiles.keys()))
        self.preset_combo.setCurrentText(self.settings.current_profile)
        self.preset_combo.blockSignals(False)

    def _switch_preset(self, name: str) -> None:
        if name and name in self.settings.profiles:
            self._conflict_defaults.clear()
            self.settings.current_profile = name
            self.engine.save_settings()
            self._install_binding_shortcuts()
            self._refresh_bindings()
            self._rebuild_queue()

    def _preset_menu(self) -> None:
        menu = QMenu(self)
        duplicate = menu.addAction(tr("side.preset_new"))
        rename = menu.addAction(tr("side.preset_rename"))
        delete = menu.addAction(tr("side.preset_delete"))
        try:
            chosen = menu.exec(self.cursor().pos())
        finally:
            menu.deleteLater()
        if chosen is duplicate:
            name, ok = QInputDialog.getText(self, tr("side.preset_new"), tr("side.preset_name"),
                                            text=f"{self.settings.current_profile} 2")
            name = (name or "").strip()
            if ok and name:
                if name in self.settings.profiles:
                    QMessageBox.warning(self, tr("error.title"), tr("side.preset_exists"))
                    return
                self.settings.profiles[name] = [
                    config.Binding(**b.to_dict()) for b in self.settings.bindings]
                self.settings.current_profile = name
                self.engine.save_settings()
                self._refresh_presets()
                self._refresh_bindings()
        elif chosen is rename:
            name, ok = QInputDialog.getText(self, tr("side.preset_rename"),
                                            tr("side.preset_name"),
                                            text=self.settings.current_profile)
            name = (name or "").strip()
            if ok and name and name != self.settings.current_profile:
                if name in self.settings.profiles:
                    QMessageBox.warning(self, tr("error.title"), tr("side.preset_exists"))
                    return
                self.settings.profiles[name] = self.settings.profiles.pop(
                    self.settings.current_profile)
                self.settings.current_profile = name
                self.engine.save_settings()
                self._refresh_presets()
        elif chosen is delete:
            if len(self.settings.profiles) == 1:
                QMessageBox.information(self, tr("error.title"), tr("side.preset_last"))
                return
            if QMessageBox.question(self, tr("side.preset_delete"),
                                    self.settings.current_profile) \
                    == QMessageBox.StandardButton.Yes:
                self.settings.profiles.pop(self.settings.current_profile, None)
                self.settings.current_profile = next(iter(self.settings.profiles))
                self.engine.save_settings()
                self._refresh_presets()
                self._install_binding_shortcuts()
                self._refresh_bindings()

    def _refresh_bindings(self) -> None:
        counts = self.engine.folder_counts()
        for card, binding in zip(self._binding_cards, self.settings.bindings):
            label_key, _desc, needs = config.ACTIONS.get(binding.action,
                                                         ("action.move", "", True))
            # Moving is what nearly every key does; a box saying so on all ten
            # envelopes hid the ones that copy or recycle.
            badge_key = {"copy": "action.badge.copy",
                         "favorite": "action.badge.favorite",
                         "trash": "action.badge.undoable"}.get(binding.action, "")
            folder = str(Path(binding.folder).resolve()) if binding.folder else ""
            card.update_binding(binding, counts.get(folder, 0), tr(label_key),
                                tr(badge_key) if badge_key else "")
            card.setVisible(True)
        self._filter_bindings(self.search_edit.text())

    def _filter_bindings(self, text: str) -> None:
        needle = (text or "").strip().casefold()
        for card, binding in zip(self._binding_cards, self.settings.bindings):
            if not needle:
                card.setVisible(True)
                continue
            haystack = f"{binding.key} {binding.folder} {binding.action}".casefold()
            card.setVisible(needle in haystack)

    def _activate_first_binding(self) -> None:
        if not self.search_edit.text().strip():
            self.setFocus()
            return
        for index, card in enumerate(self._binding_cards):
            if card.isVisible():
                self.search_edit.clear()
                self.setFocus()
                self.classify_index(index)
                return

    def choose_binding_folder(self, index: int) -> None:
        if self._blocked():
            return
        if not self._drain_queue():
            return
        binding = self.settings.bindings[index]
        if not binding.needs_folder():
            self.open_bindings()
            return
        folder = QFileDialog.getExistingDirectory(
            self, tr("bind.choose_for", key=binding.key),
            binding.folder or str(self.engine.source_root or Path.home()))
        if folder:
            self._conflict_defaults.clear()
            binding.folder = folder
            self.engine.save_settings()
            self._refresh_bindings()
            self._drop_excluded()

    def _drop_excluded(self) -> None:
        """Take a newly chosen destination folder out of the queue.

        Only matters when that folder sits under the source root, which is the
        uncommon case; assigning a key used to re-walk the whole source tree
        either way.
        """
        change = self.engine.exclude_targets()
        if change is None:
            return
        self._apply_change(change)
        self._update_slip()

    def open_bindings(self) -> None:
        if self._blocked():
            return
        if not self._drain_queue():
            return
        dialog = BindingsDialog(self.settings.bindings, self._template_samples(), self,
                                reserved=self.reserved_keys(), engine=self.engine)
        if self._run_dialog(dialog) == QDialog.DialogCode.Accepted:
            self._conflict_defaults.clear()
            self.settings.set_bindings(dialog.result_bindings())
            self.engine.save_settings()
            self._install_binding_shortcuts()
            self._refresh_bindings()
            self._drop_excluded()

    def _template_samples(self) -> list:
        samples = []
        for path in self.engine.queue_paths[:6]:
            samples.append(self.engine.planner.context_for(
                path, self.settings.bindings[0], self.engine.source_root, 1))
        return samples

    # ========================================================= classifying
    def classify_index(self, index: int) -> None:
        if self._blocked():
            return
        if self._guard_unseen_key_action():
            return
        if not (0 <= index < len(self.settings.bindings)):
            return
        binding = self.settings.bindings[index]
        targets = self._targets()
        if not targets:
            if self.view_mode == config.VIEW_GRID:
                self.status(tr("status.nothing_chosen"), "warning")
            return
        if binding.needs_folder() and not binding.folder:
            self.choose_binding_folder(index)
            return
        if binding.action == "reveal":
            platform_.reveal(targets[0])
            self.status(tr("status.revealed"), "success")
            return
        if binding.action == "rename":
            self.rename_current()
            return
        if binding.action == "tag":
            return
        if binding.action == "skip":
            self.skip_current()
            return
        flight = self._take_off() if len(targets) == 1 else None
        filed = False
        for path in targets:
            filed = self._classify_one(binding, Path(path)) or filed
        if filed:
            self._bag(index, flight)

    def _take_off(self) -> tuple | None:
        """The print on the counter and where it lies, before the next one replaces it."""
        if self.view_mode != config.VIEW_SINGLE or not self._motion:
            return None
        if self.preview.stack.currentIndex() != MediaPreview.IMAGE:
            return None
        pixmap = self.preview.image.current_pixmap()
        if pixmap.isNull():
            return None
        area = self.preview.image.print_rect()
        corner = self.preview.image.viewport().mapTo(self.centralWidget(), area.topLeft())
        return pixmap, QRect(corner, area.size())

    def _bag(self, index: int, flight: tuple | None) -> None:
        """装袋: a copy of the filed print drops into its envelope, which stamps it.

        Purely a picture of what happened. It starts on the next turn of the
        event loop, once the key press has put the next print up, so scaling
        the copy never holds that print back.
        """
        card = self._binding_cards[index]
        if flight is None or not card.isVisible():
            card.receive()
            return
        QTimer.singleShot(0, lambda: self._drop(card, flight))

    def _drop(self, card: BindingCard, flight: tuple) -> None:
        pixmap, area = flight
        if area.isEmpty() or not card.isVisible():
            card.receive()
            return
        if self._flyer is None:
            self._flyer = FlyingPrint(self.centralWidget())
            self._fall = QPropertyAnimation(self._flyer, b"geometry", self)
            self._fall.setDuration(BAG_MS)
            self._fall.setEasingCurve(QEasingCurve.Type.OutExpo)
            self._fall.valueChanged.connect(self._falling)
            self._fall.finished.connect(self._landed)
        if self._fall.state() == QAbstractAnimation.State.Running:
            # A quick run of keys: the last drop lands now, so its stamp still shows.
            self._fall.setCurrentTime(self._fall.duration())
        # Cut down once, cheaply. The copy is first drawn at the animation's
        # first tick, when OutExpo has it at about half size, and every later
        # frame draws it smaller again with smoothing.
        cut = area.size() * 0.6
        if pixmap.width() > cut.width():
            pixmap = pixmap.scaled(cut, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.FastTransformation)
        self._flyer.set_pixmap(pixmap)
        finish = QRect(0, 0, 22, max(10, int(22 * area.height() / max(1, area.width()))))
        finish.moveCenter(card.mapTo(self.centralWidget(), card.mouth().center()))
        self._landing = card
        self._flyer.hide()
        self._flyer.setGeometry(area)
        self._fall.setStartValue(area)
        self._fall.setEndValue(finish)
        self._fall.start()

    def _falling(self, _value) -> None:
        """Show the copy only once it is under half size and on its way.

        Its first frames would otherwise cover the next print. The animation
        timer can already be running for an envelope's stamp, so the first tick
        may come a millisecond after the start: time alone is not enough.
        """
        if self._flyer is None or self._fall is None or self._flyer.isVisible():
            return
        # Setting the next drop's end points on the stopped animation reports
        # the last drop's final value; only a running drop may show the copy.
        if self._fall.state() == QAbstractAnimation.State.Stopped:
            return
        progress = self._fall.easingCurve().valueForProgress(
            self._fall.currentTime() / max(1, self._fall.duration()))
        if progress >= 0.45:
            self._flyer.show()
            self._flyer.raise_()

    def _landed(self) -> None:
        if self._flyer is not None:
            self._flyer.hide()
        if self._landing is not None:
            self._landing.receive()
            self._landing = None

    def _targets(self) -> list[Path]:
        if self.view_mode == config.VIEW_GRID:
            # The engine cursor is not on screen here, so a key that fell back
            # to it would file a file nobody chose.
            return [Path(p) for p in self.grid.selected_paths()]
        current = self.engine.current_path()
        return [current] if current is not None else []

    def _classify_one(self, binding: config.Binding, path: Path) -> bool:
        """File one item. True when an operation was started for it."""
        group = self.engine._operation_group(self.engine.group_for(path))
        rules = self.settings.sidecar
        if group.sidecars and rules.enabled and binding.action in ("move", "copy", "favorite"):
            if rules.prompt in (PROMPT_EACH, PROMPT_ONCE):
                target = self.engine.preview_target(binding, path, group)
                dialog = SidecarDialog(group, target.name if target else path.name,
                                       str(target.parent) if target else binding.folder, self,
                                       remember=rules.prompt == PROMPT_ONCE)
                accepted = self._run_dialog(dialog) == QDialog.DialogCode.Accepted
                if not accepted:
                    return False
                chosen = set(dialog.chosen())
                group.members = [m for m in group.members if m.path in chosen]
                if dialog.remembered():
                    self.settings.sidecar = rules.with_prompt(PROMPT_ALWAYS)
                    self.engine.planner.settings = self.settings
                    self.engine.save_settings()

        decision = ""
        if binding.action in ("move", "copy", "favorite"):
            target = self.engine.preview_target(binding, path, group)
            if target is not None:
                if len(group.members) == 1:
                    exists = target.exists()
                    if exists and self.engine.planner._same_path(path, target, True):
                        self.status(tr("error.target_is_source"), "warning")
                        return False
                    conflicts = [(path, target)] if exists else []
                else:
                    pairs = self.engine.planner._group_pairs(group, target.parent, target.name)
                    target_exists = [destination.exists() for _, destination in pairs]
                    sources = [source for source, _ in pairs]
                    if any(exists and any(self.engine.planner._same_path(source, destination, True)
                                          for source in sources)
                           for (_, destination), exists in zip(pairs, target_exists, strict=True)):
                        self.status(tr("error.target_is_source"), "warning")
                        return False
                    conflicts = [pair for pair, exists in zip(pairs, target_exists, strict=True)
                                 if exists]
            else:
                conflicts = []
            if conflicts:
                conflict_source, conflict_target = conflicts[0]
                folder_key = str(Path(binding.folder).resolve())
                remembered = self._conflict_defaults.get(folder_key, "")
                if remembered:
                    decision = remembered
                    if decision == ops.CONFLICT_REPLACE:
                        self._remembered_replace_paths.add(path)
                        self.status(tr("status.remembered_replace", name=conflict_target.name),
                                    "warning")
                else:
                    decision, remember = ConflictDialog.ask(self, conflict_source, conflict_target)
                    if decision == ops.CONFLICT_CANCEL:
                        return False
                    if remember:
                        self._conflict_defaults[folder_key] = decision
                if decision == ops.CONFLICT_SKIP:
                    self.status(tr("skip"), "normal")
                    return False
        # Recycling asks nothing: Ctrl+Z brings the file straight back, and a
        # question per file turned a grid selection into a row of dialogs.
        resolver = (lambda a, b, value=decision: value) if decision else ops.always_sequence
        root = self.engine.source_root
        recycle_mode = self.settings.recycle_mode
        from_review = self.engine.review_mode
        self._run_operation(
            path,
            lambda progress, cancel: self.engine.classify(
                binding, path, resolver, progress, cancel, group=group,
                allow_system=self.settings.background_queue, root=root,
                recycle_mode=recycle_mode, from_review=from_review),
            f"{tr(config.ACTIONS[binding.action][0])} · {path.name}")
        return True

    def _blocked(self) -> bool:
        """True while an operation owns the lists; the caller must not proceed.

        Long passes call ``QApplication.processEvents`` to keep the window
        painting, and that dispatches whatever the user has already typed. A
        buffered key press arriving there used to reach straight into the queue
        and the two views while a transaction was mid-flight.
        """
        return self._busy

    def _run_operation(self, path: Path, work, label: str) -> None:
        """Advance the view now; let the transaction finish behind it."""
        if self._blocked():
            self._remembered_replace_paths.discard(path)
            return
        if not self.settings.background_queue:
            released = self.preview.current_path == path
            if released:
                self.preview.release()
            try:
                outcome = work(lambda message, percent: None, lambda: False)
            except (TransactionError, NameError_, OSError) as error:
                self._remembered_replace_paths.discard(path)
                if released:
                    self._refresh_view()
                self._report(error)
                return
            if released and (outcome is None or outcome.cancelled or outcome.skipped):
                self._refresh_view()
            self._finish_operation(path, self.engine.index, outcome)
            return
        # _detach reports the row the item really occupied. Using the
        # navigation cursor instead would put a failed item back in the wrong
        # place whenever a grid selection spans more than the current item.
        position = self._detach(path)
        job = self.engine.enqueue(label, work, {"path": str(path), "index": position})
        self._inflight[job.id] = (path, position)

    def _detach(self, path: Path) -> int:
        """Take the item out of the queue at once; return the row it held.

        One row is removed from each view. Rebuilding both lists here is what
        used to make every keystroke cost as much as opening the folder.
        """
        position = self.engine.drop_from_queue(path)
        if position >= 0:
            self.filmstrip.drop(path)
            if not self._grid_dirty and not self._grid_building:
                self.grid.remove_path(path)
        self._refresh_view()
        return position

    def _finish_operation(self, path: Path, index: int, outcome) -> None:
        remembered_replace = path in self._remembered_replace_paths
        self._remembered_replace_paths.discard(path)
        if outcome is None or outcome.cancelled:
            return
        if outcome.skipped and outcome.message_key:
            self.status(tr(outcome.message_key), "normal")
        if outcome.record is not None:
            action_label = tr(config.ACTIONS.get(outcome.record.action, ("action.move",))[0])
            message = tr("status.done_action", action=action_label, name=path.name)
            if remembered_replace:
                message += " · " + tr("status.remembered_replace", name=path.name)
            self.status(message, "success")
        if outcome.message_key and not outcome.skipped:
            self.status(tr(outcome.message_key), "warning")
        if outcome.record:
            # Synchronous mode did a full rebuild per key press: on a folder of
            # twenty thousand files that is a sixth of a second of filtering and
            # sorting for one photograph. `absorb` reports the rows that went,
            # and `_apply_change` is what takes them out of both views -- going
            # through `_detach` would not, because absorb has already removed
            # the path from the queue it looks in.
            change = self.engine.absorb(outcome.record)
            if change is None:
                if not self.settings.background_queue:
                    self._rebuild_queue()
                else:
                    # Random and review queues have no stable insertion point.
                    removed = []
                    for touched in self.engine.touched_paths(outcome.record):
                        if not touched.exists():
                            if touched in self.engine.all_files:
                                self.engine.all_files.remove(touched)
                            if self.engine.drop_from_queue(touched) >= 0:
                                removed.append(touched)
                    if removed:
                        self._apply_change({"removed": removed, "added": []})
                self._update_slip()
            else:
                self._apply_change(change)
                self._update_slip()
        self._refresh_bindings()
        self._update_actions()
        self._update_quota()
        self._update_handled()

    def _on_queue_event(self, event: str, job) -> None:
        if self._closed:
            return
        pending = self.engine.queue.pending
        self.queue_label.setText(tr("status.queue_pending", count=pending) if pending else "")
        self._schedule_ledger_fit()
        if event not in ("finished", "failed"):
            return
        # _inflight is the record this thread owns; job.context is written by
        # the worker and is only a fallback for a job we did not enqueue.
        entry = self._inflight.pop(job.id, None)
        if entry is not None:
            path, position = entry
        elif job.context.get("path"):
            path = Path(job.context["path"])
            position = int(job.context.get("index", -1))
        else:
            return

        if event == "finished":
            self._finish_operation(path, position, job.result)
            self._schedule_open_pending(50)
            return
        self._remembered_replace_paths.discard(path)
        if path.exists() and path not in self.engine.queue_paths:
            # Put the item back exactly where it was so nothing is lost.
            where = (len(self.engine.queue_paths) if position < 0
                     else min(position, len(self.engine.queue_paths)))
            self.engine.insert_at(path, where)
            # The whole decoration map, not just this file's: set_paths replaces
            # what the strip holds, so a one-entry map wipes every other badge.
            self.filmstrip.set_queue(self.engine.queue_paths, self.engine.index,
                                     self._decorations(self.engine.queue_paths))
            if not self._grid_dirty and not self._grid_building:
                self.grid.insert_path(path, where)
            self._refresh_view()
        if job.error is not None:
            failures = self.engine.queue.failures()
            self._failed_seen += 1
            self.status(tr("status.queue_failed", count=max(self._failed_seen,
                                                            len(failures))), "error")
            if failures and not self._failure_report_queued:
                self._failure_report_queued = True
                QTimer.singleShot(0, self._report_queue_failures)
        self._schedule_open_pending(50)

    def _report_queue_failures(self) -> None:
        self._failure_report_queued = False
        if self._reporting or not self._failed_seen:
            return
        if self.engine.queue.pending:
            self._failure_report_queued = True
            QTimer.singleShot(50, self._report_queue_failures)
            return
        count = self._failed_seen
        self._reporting = True
        self.engine.queue.clear_failures()
        try:
            QMessageBox.warning(self, tr("error.title"),
                                tr("status.queue_failed", count=count))
        finally:
            self._reporting = False
            self._failed_seen = max(0, self._failed_seen - count)
            if self._failed_seen:
                self._failure_report_queued = True
                QTimer.singleShot(0, self._report_queue_failures)
            self._schedule_open_pending()

    # =============================================================== other
    def skip_current(self) -> None:
        if self._blocked():
            return
        if self._guard_unseen_key_action():
            return
        if not self._drain_queue():
            return
        targets = self._targets()
        if not targets or self.engine.review_mode:
            return
        for path in targets:
            self._detach(Path(path))
            self.engine.skip(path)
        self._update_actions()

    def rename_current(self) -> None:
        if self._blocked():
            return
        if self._guard_unseen_key_action():
            return
        if not self._drain_queue():
            return
        if self.view_mode == config.VIEW_GRID:
            targets = self._targets()
            if len(targets) != 1:
                self.status(tr("status.rename_one_only") if targets
                            else tr("status.nothing_chosen"), "warning")
                return
            path = targets[0]
        else:
            path = self.engine.current_path()
        if path is None:
            return
        name, ok = QInputDialog.getText(self, tr("rename"), tr("info.filename"), text=path.name)
        if not ok or not name.strip():
            return
        try:
            new_name = naming.ensure_suffix(name.strip(), path.suffix)
            naming.validate_filename(new_name)
            target = path.with_name(new_name)
            decision = ""
            if target.exists():
                decision, _remember = ConflictDialog.ask(self, path, target)
                if decision in (ops.CONFLICT_CANCEL, ops.CONFLICT_SKIP):
                    return
            resolver = (lambda a, b, value=decision: value) if decision else ops.always_sequence
            released = self.preview.current_path == path
            if released:
                self.preview.release()
            outcome = self.engine.rename(new_name, path, resolver)
        except (NameError_, TransactionError, OSError, ValueError) as error:
            if self.preview.current_path == path:
                self._refresh_view()
            self._report(error)
            return
        if released and (outcome is None or outcome.cancelled or outcome.skipped):
            self._refresh_view()
        # `_finish_operation` owns the absorb in synchronous mode; doing it here
        # as well patched the views twice for one rename.
        self._finish_operation(path, self.engine.index, outcome)

    def trash_current(self) -> None:
        if self._blocked():
            return
        if self._guard_unseen_key_action():
            return
        targets = self._targets()
        if not targets:
            self.status(tr("status.nothing_chosen"), "warning")
            return
        recycle_mode = self.settings.recycle_mode
        allow_system = self.settings.background_queue
        from_review = self.engine.review_mode
        for path in targets:
            # `target=path` binds the loop variable now; a bare closure would
            # recycle the last item once per selected file.
            self._run_operation(
                path,
                lambda progress, cancel, target=path: self.engine.trash(
                    target, progress, cancel, allow_system=allow_system,
                    recycle_mode=recycle_mode, from_review=from_review),
                f"{tr('action.trash')} · {path.name}")

    def undo(self) -> None:
        self._transition(self.engine.undo, tr("status.undo_done"))

    def redo(self) -> None:
        self._transition(self.engine.redo, tr("status.redo_done"))

    def _drain_queue(self) -> bool:
        """Let queued sorting finish before touching history.

        Sorting runs on the queue thread. Undoing while a move is still in
        flight meant two threads planning against the same files, and a plan is
        only valid for the filesystem it was built against.
        """
        if not self.engine.queue.pending:
            return True
        was_busy = self._busy
        self._busy = True
        dialog = None
        started = time.monotonic()
        deadline = started + 60.0
        try:
            while self.engine.queue.pending and time.monotonic() < deadline:
                if self.engine.queue.wait_idle(0.05):
                    break
                # Most queues drain in well under a second; only say so when
                # the wait is long enough for the user to notice it.
                if dialog is None and time.monotonic() - started > 0.3:
                    dialog = QProgressDialog(tr("queue.drain"), "", 0, 0, self)
                    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
                    dialog.setCancelButton(None)
                    dialog.show()
                QApplication.processEvents()
            # The queue marks itself idle after it emits the last job's result,
            # so that event is still waiting here. Undo has to run after it: the
            # failure handler puts a row back, and doing that afterwards would
            # leave the views disagreeing with the queue.
            QApplication.processEvents()
        finally:
            if dialog is not None:
                dialog.close()
                dialog.deleteLater()
            self._busy = was_busy
        return not self.engine.queue.pending

    def _transition(self, action, message: str) -> None:
        if self._busy:
            return
        if not self._drain_queue():
            self.status(tr("status.queue_pending", count=self.engine.queue.pending), "warn")
            return
        self.preview.release()
        try:
            outcome = self._with_progress(
                message, lambda progress, cancel: action(progress, cancel))
        except (TransactionError, OSError) as error:
            if getattr(error, "key", "") == "status.undo_stuck":
                self.status(tr("status.undo_stuck"), "warning")
                return
            self._report(error)
            return
        except Cancelled:
            return
        record = getattr(outcome, "record", None)
        # The record names the files that moved, so there is no reason to walk
        # the folder again; a full rescan here is what made undo on a large
        # folder feel like reopening it.
        try:
            change = self.engine.absorb(record)
        except OSError as error:
            self._report(error)
            change = None
        if change is None:
            self._rebuild_queue()
            self._refresh_bindings()
            self.status(message, "success")
            return
        self._apply_change(change)
        self._update_slip()
        self._update_quota()
        self._update_handled()
        self.status(message, "success")
        self._refresh_bindings()

    def _apply_change(self, change: dict) -> None:
        """Patch both views with the rows a transition added or removed.

        One row in, one row out. Calling `set_queue` here instead rebuilt the
        whole visible strip and asked the tag table for every file in the
        library, which is the cost the incremental path exists to avoid.
        """
        added = change.get("added") or []
        removed = change.get("removed") or []
        touchable = not self._grid_dirty and not self._grid_building
        for path in removed:
            self.filmstrip.drop(path)
            if touchable:
                self.grid.remove_path(path)
        if added:
            decorations = self._decorations([path for _where, path in added])
            for where, path in added:
                self.filmstrip.insert(path, where, decorations.get(str(path)))
                if touchable:
                    self.grid.insert_path(path, where, decorations.get(str(path)))
        self.filmstrip.follow(self.engine.index)
        if self._grid_dirty and self.view_mode == config.VIEW_GRID:
            # A grid the user cancelled part-way is still out of date, and
            # nothing else would come back to finish it.
            self._fill_grid()
        self._refresh_view()

    def recover_pending(self) -> None:
        if self._busy:
            return
        if not self._drain_queue():
            return
        try:
            recovered = self._with_progress(
                tr("tool.recover"),
                lambda progress, cancel: self.engine.recover(progress))
        except (TransactionError, OSError) as error:
            self._offer_recover_exit(error, can_rollback=True)
            return
        except ValueError as error:              # a damaged journal; JSONDecodeError is one
            log.warning("the journal could not be read: %s", error)
            self._offer_recover_exit(TransactionError("error.journal_damaged"),
                                     can_rollback=False)
            return
        except Cancelled:
            return
        if not recovered:
            self.status(tr("status.recover_none"), "normal")
            return
        self.status(tr("status.recover_done"), "success")
        self.recover_button.setVisible(self.engine.has_pending() and
                                       not self.engine.queue.pending)
        if self.engine.source_root:
            self.rescan()
        else:
            self._open_initial_folder()

    def _offer_recover_exit(self, error: BaseException, can_rollback: bool) -> None:
        """Recovery failed: let the user reverse what ran, or keep the files as they are.

        This is the only way out of a journal that can never be replayed. It is
        not a delete confirmation: it appears after a failure, never before one.
        """
        key = getattr(error, "key", "")
        fields = getattr(error, "fields", {}) or {}
        message = tr(key, **fields) if key else str(error)
        log.warning("recovery could not finish: %s", error)
        self.status(message, "error")
        choice = self._ask_recover_exit(message, can_rollback)
        if choice:
            rollback = choice == "rollback"
            try:
                self._with_progress(
                    tr("tool.recover"),
                    lambda progress, cancel: self.engine.abandon_pending(rollback, progress))
            except Cancelled:
                # Cancelling the way out is not a failure: the journal is still
                # there, so the same exit can be taken again.
                self.status(tr("error.cancelled"), "normal")
            except (TransactionError, OSError, ValueError) as failure:
                self._report(failure)
            else:
                self.status(tr("status.recover_rolled_back") if rollback
                            else tr("status.recover_kept"), "success")
        self.recover_button.setVisible(self.engine.has_pending())
        if choice and self.engine.source_root:
            self.rescan()

    def _ask_recover_exit(self, message: str, can_rollback: bool) -> str:
        box = QMessageBox(QMessageBox.Icon.Warning, tr("error.title"),
                          tr("recover.exit_text", error=message),
                          QMessageBox.StandardButton.NoButton, self)
        roles = QMessageBox.ButtonRole
        rollback = box.addButton(tr("recover.rollback"), roles.AcceptRole) if can_rollback else None
        keep = box.addButton(tr("recover.keep"), roles.DestructiveRole)
        later = box.addButton(tr("recover.later"), roles.RejectRole)
        box.setDefaultButton(later)
        box.setEscapeButton(later)
        box.exec()
        clicked = box.clickedButton()
        if rollback is not None and clicked is rollback:
            return "rollback"
        return "keep" if clicked is keep else ""

    def _step_frame(self, direction: int) -> None:
        message = self.preview.step_frame(direction)
        if message:
            self.detail_label.setText(f"{message} s")

    def _rate_current(self, value: int) -> None:
        if self._blocked():
            return
        if self._guard_unseen_key_action():
            return
        targets = self._targets()
        if not targets:
            return
        self.engine.tag(targets, rating=value)
        self.rating.set_value(value)
        for path in targets:
            rating, label = self.engine.state.tag(str(path))
            self.grid.set_decoration(str(path), rating=rating, label=label)
            self.filmstrip.set_decoration(str(path), rating=rating, label=label)

    def _label_current(self, name: str) -> None:
        if self._blocked():
            return
        if self._guard_unseen_key_action():
            return
        targets = self._targets()
        if not targets:
            return
        self.engine.tag(targets, label=name)
        self.labels.set_value(name)
        for path in targets:
            rating, label = self.engine.state.tag(str(path))
            self.grid.set_decoration(str(path), rating=rating, label=label)
            self.filmstrip.set_decoration(str(path), rating=rating, label=label)

    def _label_by_index(self, index: int) -> None:
        names = list(theme.LABEL_COLOURS)
        if 1 <= index <= len(names):
            self._label_current(names[index - 1])

    # ============================================================= dialogs
    def show_info(self) -> None:
        if self._blocked():
            return
        path = self.engine.current_path()
        if path is None:
            return
        rows = [[label, value] for label, value in self.engine.info_rows(path)]
        self._run_dialog(TableDialog(tr("info.title"),
                                     [tr("info.property"), tr("info.value")], rows,
                                     subtitle=path.name, parent=self))

    def show_history(self) -> None:
        if self._blocked():
            return
        from ..core.state import STACK_HISTORY
        rows = []
        for record in self.engine.state.records(limit=2000):
            label_key = config.ACTIONS.get(record.action, ("action.move",))[0]
            rows.append([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.time_epoch)),
                tr(label_key), record.original, record.destination,
                tr(f"conflict.mode.{record.conflict}") if record.conflict
                else tr("conflict.mode.none"),
                tr("history.state.done") if record.stack == STACK_HISTORY
                else tr("history.state.undone"),
            ])
        self._run_dialog(TableDialog(
            tr("history.title"),
            [tr("history.time"), tr("history.action"), tr("history.original"),
             tr("history.destination"), tr("history.conflict"), tr("history.state")],
            rows, [(tr("history.undo_last"), self.undo), (tr("side.redo"), self.redo)],
            parent=self))

    def show_statistics(self) -> None:
        rows = [[label, value] for label, value in self.engine.statistics()]
        self._run_dialog(TableDialog(tr("stats.title"),
                                     [tr("stats.item"), tr("stats.count")], rows,
                                     [(tr("stats.export"), self.export_csv)], parent=self))

    def export_csv(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, tr("stats.export_dialog"),
                                                  tr("stats.export_name"), "CSV (*.csv)")
        if not filename:
            return
        try:
            self.engine.export_csv(filename)
            self.status(tr("status.exported"), "success")
        except OSError as error:
            self._report(error)

    def open_duplicates(self) -> None:
        if self._blocked():
            return
        if not self._drain_queue():
            return
        if not self.engine.source_root:
            return
        self.preview.release()
        dialog = DuplicatesDialog(self.engine, self)
        self._run_dialog(dialog)
        # Looking and closing changed nothing; only sending extras to the review
        # list moves the queue, and rebuilding it regardless cost a sixth of a
        # second every time this window was closed.
        if dialog.changed_the_queue:
            self._rebuild_queue()
        else:
            self._refresh_view()
            self._update_actions()

    def open_backups(self) -> None:
        if self._blocked():
            return
        dialog = BackupDialog(self.engine.backup_usage(), self.settings.quota, self)
        if self._run_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        if dialog.result_action == "reclaim":
            freed, retired = self.engine.reclaim(force=True)
            self.status(tr("backup.reclaimed", size=human_size(freed), count=retired),
                        "success")
        elif dialog.result_action == "clear":
            if QMessageBox.warning(self, tr("backup.clear_all"), tr("backup.clear_confirm"),
                                   QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.Cancel,
                                   QMessageBox.StandardButton.Cancel) \
                    != QMessageBox.StandardButton.Yes:
                return
            freed = self.engine.clear_backups()
            self.status(tr("backup.reclaimed", size=human_size(freed), count=0), "success")
        self._update_quota()
        self._update_actions()

    def open_settings(self) -> None:
        if self._blocked():
            return
        if not self._drain_queue():
            return
        dialog = SettingsDialog(self.settings, self.engine, self)
        if self._run_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        updated = dialog.result_settings()
        updated.profiles = self.settings.profiles
        updated.current_profile = self.settings.current_profile
        updated.source_folder = self.settings.source_folder
        self.settings = updated
        self.engine.apply_settings(updated)
        set_language(updated.language)
        theme.apply(QApplication.instance(), updated.density)
        height = self._envelope_height()
        for card in self._binding_cards:
            card.set_envelope_height(height)
        self._retranslate()
        self._rebuild_queue()

    # ============================================================ plumbing
    def _with_progress(self, title: str, work):
        """Run *work* with a cancellable modal progress dialog."""
        dialog = QProgressDialog(title, tr("cancel"), 0, 100, self)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(300)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        cancelled = {"value": False}
        dialog.canceled.connect(lambda: cancelled.update(value=True))

        def progress(message: str, percent: int) -> None:
            dialog.setLabelText(f"{title}\n{message}" if message else title)
            dialog.setValue(max(0, min(100, int(percent))))
            # Keeps the dialog painting and the cancel button live. Every
            # caller sets _busy first, so a re-entrant scan cannot start here.
            QApplication.processEvents()

        was_busy = self._busy
        self._busy = True
        try:
            return work(progress, lambda: cancelled["value"])
        finally:
            self._busy = was_busy
            dialog.close()
            dialog.deleteLater()
            if not was_busy:
                QTimer.singleShot(0, self._open_pending)
                if self._close_requested:
                    self._close_requested = False
                    QTimer.singleShot(0, self.close)

    def open_requested(self, folder: str) -> None:
        """A later launch -- Explorer's "Open with Qingjian" -- handed over a folder."""
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        if folder and Path(folder).is_dir():
            self._pending_open = Path(folder)
            self._open_pending()

    def _open_pending(self) -> None:
        if (self._pending_open is None or self._busy or
                QApplication.activeModalWidget() is not None):
            return
        if self.engine.queue.pending:
            self._schedule_open_pending(50)
            return
        target = self._pending_open
        self.open_folder(target)
        if self._pending_open == target and self.engine.source_root == target:
            self._pending_open = None

    def _schedule_open_pending(self, delay: int = 0) -> None:
        if self._pending_open is None or self._pending_open_retry_queued:
            return
        self._pending_open_retry_queued = True

        def open_later() -> None:
            self._pending_open_retry_queued = False
            self._open_pending()

        QTimer.singleShot(delay, open_later)

    # -- drag and drop --------------------------------------------------
    def dragEnterEvent(self, event) -> None:      # noqa: N802 - Qt naming
        if any(url.isLocalFile() and url.toLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:           # noqa: N802 - Qt naming
        for url in event.mimeData().urls():
            if not url.isLocalFile() or not url.toLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_dir():
                self.open_folder(path)
                break
            if path.is_file() and path.parent.is_dir():
                self.open_folder(path.parent)
                break

    # -- geometry and shutdown -------------------------------------------
    def _restore_geometry(self) -> None:
        blob = self.settings.window_geometry
        if not blob:
            return
        try:
            from base64 import b64decode
            self.restoreGeometry(b64decode(blob.encode("ascii")))
        except Exception:
            pass

    def closeEvent(self, event) -> None:          # noqa: N802 - Qt naming
        if self._busy:
            self._close_requested = True
            event.ignore()
            return
        if self.engine.queue.pending:
            answer = QMessageBox.question(self, tr("queue.drain"),
                                          tr("status.queue_pending",
                                             count=self.engine.queue.pending))
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # `wait_idle` alone froze the window for up to half a minute with
            # nothing on screen. Drain the same way undo does, so the last file
            # being moved is visible and the window keeps painting.
            if not self._drain_queue():
                # Still working after the wait: closing here would abandon a
                # transaction part-way, so let the user decide rather than
                # deciding for them.
                answer = QMessageBox.question(
                    self, tr("queue.drain"),
                    tr("status.queue_pending", count=self.engine.queue.pending))
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        QApplication.processEvents()
        self._closed = True
        if self._queue_listener in self.engine.queue_listeners:
            self.engine.queue_listeners.remove(self._queue_listener)
        QApplication.instance().removeEventFilter(self._arrows)
        self.preview.release()
        self.preloader.shutdown()
        self.thumbs.shutdown()
        from base64 import b64encode
        self.settings.window_geometry = b64encode(bytes(self.saveGeometry())).decode("ascii")
        current = self.engine.current_path()
        self.settings.last_path = str(current) if current else ""
        self.engine.save_settings()
        self.engine.close()
        event.accept()
