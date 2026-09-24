"""The duplicates window: exact copies, near matches and bursts.

Scanning happens on a worker thread. It used to run inside ``__init__``, on the
interface thread, with no progress and no way out: opening this on a folder of
twenty thousand photographs hashed every one of them behind a window that had
stopped painting, which reads as a hang and invites the user to kill the app
mid-scan. Here the window opens immediately, reports what it is doing, and can
be stopped; results are kept per mode so switching tabs does not rescan.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QProgressBar, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import dedupe
from ..core.engine import human_size
from ..core.i18n import tr
from ..core.logsetup import get_logger
from ..core.safestore import Cancelled, TransactionError
from . import theme
from .preview import MediaPreview
from .widgets import Segmented, caption, separator, set_primary_button

log = get_logger("duplicates")

#: Rows added per timer tick while filling the table. Inserting tens of
#: thousands in one go blocks the interface for as long as the scan did.
_CHUNK = 300

_MARK_COLOURS = {
    "keep": theme.BALLPOINT_LIGHT,
    "extra": theme.GREASE,
    "lower": theme.AMBER,
}

_COLUMN_WIDTHS = (260, 380, 120, 100, 80, 120)

_DETACHED_POOLS: dict[QThreadPool, QObject] = {}
_DETACHED_TIMERS: set[QTimer] = set()


def _retain_running_pool(pool: QThreadPool, signals: QObject) -> None:
    """Keep a timed-out pool alive without tying it to the closed dialog."""
    _DETACHED_POOLS[pool] = signals
    timer = QTimer()
    timer.setInterval(100)

    def release_when_idle() -> None:
        if pool.activeThreadCount():
            return
        timer.stop()
        _DETACHED_POOLS.pop(pool, None)
        _DETACHED_TIMERS.discard(timer)
        timer.deleteLater()

    timer.timeout.connect(release_when_idle)
    _DETACHED_TIMERS.add(timer)
    timer.start()


class _ScanSignals(QObject):
    progress = Signal(str, int)
    done = Signal(str, object)
    failed = Signal(str, str)
    cancelled = Signal(str)


class _ScanTask(QRunnable):
    """One duplicate scan, off the interface thread.

    The engine layer it calls holds no Qt objects and takes its own lock, so
    running it here is safe; only the signals cross back.
    """

    def __init__(self, engine, mode: str, stop: threading.Event,
                 signals: _ScanSignals) -> None:
        super().__init__()
        self.engine = engine
        self.mode = mode
        self.stop = stop
        self.signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:                        # noqa: D401 - Qt entry point
        try:
            groups = self.engine.find_duplicates(
                self.mode,
                lambda message, percent: self.signals.progress.emit(message, percent),
                self.stop.is_set)
        except Cancelled:
            self.signals.cancelled.emit(self.mode)
        except Exception as error:                # noqa: BLE001 - a worker must not die
            log.exception("duplicate scan failed")
            self.signals.failed.emit(self.mode, str(error))
        else:
            self.signals.done.emit(self.mode, groups)


class DuplicatesDialog(QDialog):
    """Grouped review with an A/B comparison and non-destructive ignoring."""

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.root = engine.source_root
        self.setWindowTitle(tr("dup.title"))
        self.resize(1420, 880)
        self.mode = dedupe.MODE_EXACT
        self.groups: list[dedupe.Group] = []
        self.rows: list[tuple[int, dedupe.Candidate]] = []
        #: Results already computed, so switching tabs does not rescan.
        self._results: dict[str, list[dedupe.Group]] = {}
        #: True once something here moved files into the review queue, so the
        #: window that owns us knows whether its queue needs rebuilding.
        self.changed_the_queue = False
        self._stop = threading.Event()
        self._scanning = ""
        self._closing = False
        #: The mode whose scan the user stopped, so it is not restarted.
        self._abandoned = ""
        self._pending: list[tuple[int, int, dedupe.Candidate]] = []
        self._fill_timer = QTimer(self)
        self._fill_timer.setInterval(0)
        self._fill_timer.timeout.connect(self._fill_chunk)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(1)
        self._signals = _ScanSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._signals.cancelled.connect(self._on_cancelled)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(11)

        heading = QLabel(tr("dup.title"))
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        layout.addWidget(caption(tr("dup.subtitle")))

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.tabs = Segmented([
            (dedupe.MODE_EXACT, tr("dup.tab.exact")),
            (dedupe.MODE_SIMILAR, tr("dup.tab.similar")),
            (dedupe.MODE_BURST, tr("dup.tab.burst")),
        ])
        self.tabs.changed.connect(self._switch_mode)
        controls.addWidget(self.tabs)
        controls.addStretch(1)
        self.summary = QLabel("")
        self.summary.setObjectName("caption")
        controls.addWidget(self.summary)
        layout.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            tr("dup.col.file"), tr("dup.col.folder"), tr("dup.col.resolution"),
            tr("dup.col.size"), tr("dup.col.match"), ""])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Interactive, never resize-to-contents: measuring every cell of a
        # table this long costs as much as building it.
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate(_COLUMN_WIDTHS):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.currentCellChanged.connect(lambda row, *_: self._show(row))
        self.table.cellDoubleClicked.connect(lambda row, *_: self._enlarge(row))
        splitter.addWidget(self.table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(9)
        self.preview = MediaPreview()
        self.preview.video_error.connect(lambda text: self.summary.setText(text))
        right_layout.addWidget(self.preview, 1)
        self.detail = QLabel("")
        self.detail.setObjectName("caption")
        self.detail.setWordWrap(True)
        right_layout.addWidget(self.detail)
        note = QLabel(tr("dup.ignore_note"))
        note.setObjectName("note")
        note.setWordWrap(True)
        right_layout.addWidget(note)
        splitter.addWidget(right)
        splitter.setSizes([720, 640])
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.auto_keep = QPushButton(tr("dup.auto_keep_best"))
        self.auto_keep.setObjectName("primaryButton")
        self.auto_keep.clicked.connect(self._auto_keep)
        self.to_review = QPushButton(tr("dup.extras_to_review"))
        self.to_review.clicked.connect(self._extras_to_review)
        self.ignore_one = QPushButton(tr("dup.ignore_selected"))
        self.ignore_one.clicked.connect(lambda: self._ignore(False))
        self.ignore_group = QPushButton(tr("dup.ignore_group"))
        self.ignore_group.clicked.connect(lambda: self._ignore(True))
        self.restore = QPushButton(tr("dup.restore_ignored", count=0))
        self.restore.clicked.connect(self._restore)
        self.rescan = QPushButton(tr("dup.rescan"))
        self.rescan.clicked.connect(self._rescan_clicked)
        close = QPushButton(tr("close"))
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        for widget in (self.auto_keep, self.to_review, separator(), self.ignore_one,
                       self.ignore_group, self.restore, self.rescan):
            actions.addWidget(widget)
        actions.addStretch(1)
        actions.addWidget(close)
        layout.addLayout(actions)
        set_primary_button(self, close)

        self.reload()

    # -- scanning ------------------------------------------------------
    def _switch_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self._stop_scan()
        self.mode = mode
        # A scan that is still winding down clears `_scanning` from its own
        # signal, so `reload` may not be able to start one yet; the completion
        # handlers pick the pending mode up.
        self.reload()

    def reload(self, force: bool = False) -> None:
        """Show this mode's results, scanning for them only when needed."""
        self._fill_timer.stop()
        self._pending = []
        if force:
            self._results.clear()
        cached = self._results.get(self.mode)
        if cached is not None:
            self.groups = cached
            self._set_busy(True)
            self._fill()
            return
        self.groups = []
        self._clear_table()
        self._start_scan()

    def _start_scan(self) -> None:
        if self._scanning or self._closing:
            return
        self._abandoned = ""
        self._scanning = self.mode
        self._stop = threading.Event()
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.summary.setText(tr("dup.searching"))
        self.detail.setText(tr("dup.first_scan_note"))
        self._set_busy(True)
        self._pool.start(_ScanTask(self.engine, self.mode, self._stop, self._signals))

    def _stop_scan(self) -> None:
        if self._scanning:
            self._abandoned = self._scanning
            self._stop.set()

    def _rescan_clicked(self) -> None:
        if self._scanning:
            self._stop_scan()
            return
        self.reload(force=True)

    def _set_busy(self, busy: bool) -> None:
        self.rescan.setText(tr("dup.stop") if busy else tr("dup.rescan"))
        self.tabs.setEnabled(not busy)
        for widget in (self.auto_keep, self.to_review, self.ignore_one,
                       self.ignore_group):
            widget.setEnabled(not busy and bool(self.rows))
        self._update_restore_button()

    def _update_restore_button(self, ignored: int | None = None) -> None:
        if ignored is None:
            ignored = len(self.engine.state.ignored_keys(str(self.engine.source_root or "")))
        self.restore.setEnabled(bool(ignored) and not self._scanning
                                and not self._fill_timer.isActive())

    def _on_progress(self, message: str, percent: int) -> None:
        if self._closing or not self._scanning:
            return
        self.progress.setValue(max(0, min(100, int(percent))))
        if message:
            self.summary.setText(tr("dup.hashing", name=message))

    def _on_done(self, mode: str, groups) -> None:
        if self._closing:
            return
        self._scanning = ""
        self.progress.setVisible(False)
        self._results[mode] = list(groups)
        if mode == self.mode:
            self.groups = self._results[mode]
            self._fill()
            return
        self._set_busy(False)
        self._catch_up()

    def _on_failed(self, mode: str, error: str) -> None:
        if self._closing:
            return
        self._scanning = ""
        self.progress.setVisible(False)
        self._set_busy(False)
        self.summary.setText(tr("dup.scan_failed", error=error))
        if mode != self.mode:
            self._catch_up()

    def _on_cancelled(self, mode: str) -> None:
        if self._closing:
            return
        self._scanning = ""
        self.progress.setVisible(False)
        self._set_busy(False)
        self.summary.setText(tr("dup.cancelled"))
        if mode != self.mode:
            # The user changed tab rather than stopping; the mode on screen
            # still has nothing to show.
            self._abandoned = ""
            self._catch_up()

    def _catch_up(self) -> None:
        """Scan the mode now on screen, if the last scan was for another one.

        Stopping a scan is asynchronous: the worker notices between files, so
        the tab can already have changed by the time the signal lands.
        """
        if self._closing or self._scanning:
            return
        if self.mode in self._results or self.mode == self._abandoned:
            return
        self._start_scan()

    # -- table ---------------------------------------------------------
    def _clear_table(self) -> None:
        self.preview.show_empty()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self.rows = []

    def _fill(self) -> None:
        """Queue every member for insertion; the timer does the work."""
        self._clear_table()
        self._pending = [
            (group_index, member_index, member)
            for group_index, group in enumerate(self.groups)
            for member_index, member in enumerate(group.members)
        ]
        self._update_summary()
        if self._pending:
            self._fill_timer.start()
        else:
            self._set_busy(False)
            self.detail.setText("")

    def _fill_chunk(self) -> None:
        done = len(self.rows)
        batch = self._pending[done:done + _CHUNK]
        if not batch:
            self._fill_timer.stop()
            self._finish_fill()
            return
        self.table.blockSignals(True)
        # Rows appear only once they have content: pre-allocating the whole
        # table showed hundreds of blank rows that answered no clicks.
        self.table.setRowCount(len(self.rows) + len(batch))
        for group_index, member_index, member in batch:
            self._write_row(len(self.rows), group_index, member_index, member)
            self.rows.append((group_index, member))
        self.table.blockSignals(False)
        if len(self._pending) > _CHUNK * 4:
            self.summary.setText(tr("dup.loading_rows", done=len(self.rows),
                                    total=len(self._pending)))

    def _write_row(self, row: int, group_index: int, member_index: int,
                   member: dedupe.Candidate) -> None:
        group = self.groups[group_index]
        tint = QColor("#1F1B18" if group_index % 2 == 0 else "#29241F")
        mark = "keep" if member_index == group.keeper else "extra"
        if member_index != group.keeper and member.pixels and \
                member.pixels < group.keep().pixels:
            mark = "lower"
        values = [
            member.path.name,
            str(member.path.parent),
            f"{member.width} × {member.height}" if member.width else "—",
            human_size(member.size),
            f"{member.similarity * 100:.0f}%" if member_index != group.keeper else "—",
            self._mark_text(mark, group.mode),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(member.path))
            item.setBackground(tint)
            item.setForeground(QColor(theme.PAPER))
            if column == 5:
                item.setForeground(QColor(_MARK_COLOURS[mark]))
            self.table.setItem(row, column, item)

    def _finish_fill(self) -> None:
        self._pending = []
        self._update_summary()
        self._set_busy(False)
        if self.rows and self.table.currentRow() <= 0:
            # currentCellChanged is connected, so selecting row 0 already calls
            # _show. Blocking it here keeps the media from being torn down and
            # reloaded twice on every reload.
            self.table.blockSignals(True)
            self.table.setCurrentCell(0, 0)
            self.table.blockSignals(False)
            self._show(0)

    def _update_summary(self) -> None:
        stats = dedupe.summarise(self.groups)
        ignored = len(self.engine.state.ignored_keys(str(self.engine.source_root or "")))
        self.summary.setText(
            f"{tr('dup.summary', groups=stats['groups'], files=stats['files'])}   ·   "
            f"{tr('dup.reclaimable', size=human_size(stats['reclaimable']))}   ·   "
            f"{tr('dup.ignored_count', count=ignored)}")
        self.restore.setText(tr("dup.restore_ignored", count=ignored))
        self._update_restore_button(ignored)

    @staticmethod
    def _mark_text(mark: str, mode: str) -> str:
        if mark == "keep":
            return tr("dup.mark.keep_sharp") if mode == dedupe.MODE_BURST else tr("dup.mark.keep")
        if mark == "lower":
            return tr("dup.mark.lower_res")
        return tr("dup.mark.extra")

    # -- preview -------------------------------------------------------
    def _show(self, row: int) -> None:
        if not (0 <= row < len(self.rows)):
            return
        _group_index, member = self.rows[row]
        self.preview.release()
        ok, error = self.preview.show_path(member.path)
        if not ok:
            self.summary.setText(tr("status.preview_failed", name=member.path.name,
                                    error=error))
        bits = [f"{member.width} × {member.height}" if member.width else "",
                human_size(member.size)]
        if member.sharpness:
            bits.append(f"{tr('dup.col.sharpness')} {member.sharpness:.0f}")
        if member.defect:
            bits.append(tr(f"dup.defect.{member.defect}"))
        self.detail.setText("   ·   ".join(part for part in bits if part))

    def _enlarge(self, row: int) -> None:
        if not (0 <= row < len(self.rows)):
            return
        _group_index, member = self.rows[row]
        self.preview.player.pause()
        # A nested event loop would otherwise keep the fill timer running and
        # move the table about behind the modal window.
        filling = self._fill_timer.isActive()
        if filling:
            self._fill_timer.stop()
        dialog = QDialog(self)
        dialog.setWindowTitle(member.path.name)
        dialog.resize(1180, 800)
        layout = QVBoxLayout(dialog)
        title = QLabel(str(member.path))
        title.setWordWrap(True)
        layout.addWidget(title)
        preview = MediaPreview()
        preview.video_error.connect(title.setText)
        layout.addWidget(preview, 1)
        close = QPushButton(tr("close"))
        close.setObjectName("primaryButton")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        try:
            ok, error = preview.show_path(member.path)
            if not ok:
                title.setText(f"{member.path}\n{error}")
            dialog.exec()
        finally:
            preview.release()
            dialog.setParent(None)
            dialog.deleteLater()
            if filling and self._pending:
                self._fill_timer.start()

    # -- actions -------------------------------------------------------
    def _selected(self, whole_group: bool) -> list[dedupe.Candidate]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        chosen: list[dedupe.Candidate] = []
        if whole_group:
            groups = {self.rows[row][0] for row in rows if 0 <= row < len(self.rows)}
            for group_index in groups:
                chosen.extend(self.groups[group_index].members)
        else:
            chosen = [self.rows[row][1] for row in rows if 0 <= row < len(self.rows)]
        return chosen

    def _ignore(self, whole_group: bool) -> None:
        chosen = self._selected(whole_group)
        if not chosen:
            return
        try:
            self.engine.ignore_duplicates(chosen, self.root)
        except TransactionError as error:
            self.summary.setText(str(error))
            return
        # Ignoring changes what every mode would report, and the hashes behind
        # them are cached, so a fresh scan here is cheap.
        self.reload(force=True)

    def _restore(self) -> None:
        try:
            self.engine.restore_ignored()
        except TransactionError as error:
            self.summary.setText(str(error))
            return
        self.reload(force=True)

    def _extras_to_review(self) -> None:
        extras = [member.path for group in self.groups for member in group.extras]
        if extras:
            self.engine.send_to_review(extras, self.root)
            self.changed_the_queue = True
            self.accept()

    def _auto_keep(self) -> None:
        """Mark every non-keeper for review; nothing is moved or deleted here."""
        self._extras_to_review()

    def done(self, result: int) -> None:          # noqa: D401 - Qt entry point
        self._closing = True
        self._fill_timer.stop()
        self._stop_scan()
        # Cut the signals before waiting. A `cancelled` emitted from the worker
        # is queued, so it would otherwise arrive after this returns and start a
        # fresh scan on a dialog already on its way out.
        for signal, slot in (
            (self._signals.progress, self._on_progress),
            (self._signals.done, self._on_done),
            (self._signals.failed, self._on_failed),
            (self._signals.cancelled, self._on_cancelled),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        # A worker stuck in a decoder must not make closing the dialog block.
        # The unparented pool is retained until it becomes idle, rather than
        # being synchronously destroyed along with this dialog.
        if not self._pool.waitForDone(1000):
            log.warning("duplicate scan did not stop in time")
            _retain_running_pool(self._pool, self._signals)
        self.preview.release()
        super().done(result)
