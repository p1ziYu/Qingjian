"""Dialogs: name clashes, sidecar confirmation, tables, backups."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import ops
from ..core.engine import human_size
from ..core.i18n import tr
from ..core.sidecar import KIND_LIVE, KIND_MASTER, KIND_METADATA, KIND_RAW, SidecarGroup
from . import icons, theme
from .widgets import caption, separator, set_primary_button


def _title_row(title: str, subtitle: str = "", icon_name: str = "") -> QWidget:
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)
    if icon_name:
        badge = QLabel()
        badge.setPixmap(icons.pixmap(icon_name, 22, theme.PAPER_DIM))
        badge.setFixedSize(26, 30)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
    texts = QVBoxLayout()
    texts.setSpacing(4)
    heading = QLabel(title)
    heading.setObjectName("dialogTitle")
    heading.setWordWrap(True)
    texts.addWidget(heading)
    if subtitle:
        note = QLabel(subtitle)
        note.setObjectName("dialogSubtitle")
        note.setWordWrap(True)
        texts.addWidget(note)
    row.addLayout(texts, 1)
    return holder


#: Rows written per timer tick while a long table fills.
_CHUNK_ROWS = 250

#: Columns are sized from this many rows, not from all of them.
_MEASURE_ROWS = 60


class _NoArrowButtonDialog(QDialog):
    """Keep arrow keys from moving focus onto a destructive button."""

    def _block_button_arrows(self) -> None:
        for control in self.findChildren(QAbstractButton):
            control.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        if (isinstance(watched, QAbstractButton)
                and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                                    Qt.Key.Key_Up, Qt.Key.Key_Down)):
            return True
        return super().eventFilter(watched, event)


class ConflictDialog(_NoArrowButtonDialog):
    """What to do when the destination already holds a file of that name."""

    def __init__(self, source: Path, target: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("conflict.title"))
        self.setMinimumWidth(560)
        self.choice = ops.CONFLICT_CANCEL

        try:
            existing = human_size(target.stat().st_size)
        except OSError:
            existing = tr("unknown")
        try:
            incoming = human_size(source.stat().st_size)
        except OSError:
            incoming = tr("unknown")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(_title_row(tr("conflict.text", name=source.name), "", "warning"))
        layout.addWidget(caption(tr("conflict.detail", folder=str(target.parent),
                                    existing=existing, incoming=incoming)))
        self.remember = QCheckBox(tr("conflict.remember"))
        layout.addWidget(self.remember)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        replace = QPushButton(tr("conflict.replace"))
        replace.setObjectName("dangerButton")
        replace.clicked.connect(lambda: self._choose(ops.CONFLICT_REPLACE))
        skip = QPushButton(tr("conflict.skip"))
        skip.clicked.connect(lambda: self._choose(ops.CONFLICT_SKIP))
        cancel = QPushButton(tr("cancel"))
        cancel.clicked.connect(self.reject)
        sequence = QPushButton(tr("conflict.sequence"))
        sequence.setObjectName("primaryButton")
        sequence.setDefault(True)
        sequence.clicked.connect(lambda: self._choose(ops.CONFLICT_SEQUENCE))
        buttons.addWidget(replace)
        buttons.addWidget(skip)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(sequence)
        layout.addLayout(buttons)
        set_primary_button(self, sequence)
        self._block_button_arrows()

    def _choose(self, value: str) -> None:
        self.choice = value
        self.accept()

    def remembered(self) -> bool:
        return self.remember.isChecked()

    @classmethod
    def ask(cls, parent, source: Path, target: Path) -> tuple[str, bool]:
        dialog = cls(source, target, parent)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return ops.CONFLICT_CANCEL, False
            return dialog.choice, dialog.remembered()
        finally:
            dialog.setParent(None)
            dialog.deleteLater()


_KIND_TEXT = {
    KIND_MASTER: ("sidecar.kind.master", theme.PAPER, theme.PAPER, theme.INK),
    KIND_RAW: ("sidecar.kind.raw", "transparent", theme.LINE_CONTROL, theme.PAPER_DIM),
    KIND_METADATA: ("sidecar.kind.metadata", "transparent", theme.LINE_CONTROL,
                    theme.PAPER_DIM),
    KIND_LIVE: ("sidecar.kind.live", "transparent", theme.LINE_CONTROL, theme.PAPER_DIM),
}


class SidecarDialog(_NoArrowButtonDialog):
    """Confirm that the raw and the metadata travel with the photograph."""

    def __init__(self, group: SidecarGroup, target_name: str, target_folder: str,
                 parent=None, remember: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("sidecar.title"))
        self.setMinimumWidth(680)
        self._checks: list[tuple[QCheckBox, Path]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(_title_row(
            tr("sidecar.title"),
            tr("sidecar.body", name=group.master.name, count=len(group.sidecars)),
            "sidecar"))

        target = QLabel(f"{tr('sidecar.target')}   {target_name}    {target_folder}")
        target.setObjectName("pathBox")
        target.setWordWrap(True)
        layout.addWidget(target)

        for member in group.members:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(11, 8, 11, 8)
            line.setSpacing(11)
            check = QCheckBox()
            check.setChecked(True)
            check.setEnabled(member.kind != KIND_MASTER)
            line.addWidget(check)
            name = QLabel(member.path.name)
            name.setObjectName("mono")
            line.addWidget(name, 1)
            size = QLabel(human_size(member.size))
            size.setObjectName("caption")
            line.addWidget(size)
            key, fill, border, ink = _KIND_TEXT.get(
                member.kind, ("sidecar.kind.other", "transparent", theme.LINE_CONTROL,
                              theme.PAPER_DIM))
            badge = QLabel(tr(key))
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setMinimumWidth(104)
            badge.setStyleSheet(f"QLabel {{ background: {fill}; border: 1px solid {border};"
                                f" border-radius: 3px; padding: 2px 9px; color: {ink};"
                                f" font-weight: 700; }}")
            line.addWidget(badge)
            highlight = member.kind == KIND_MASTER
            row.setStyleSheet(
                f"QWidget {{ background: {theme.RAISED if highlight else '#211D1A'};"
                f" border: 1px solid {theme.LINE_CONTROL if highlight else theme.LINE};"
                f" border-radius: 4px; }}")
            layout.addWidget(row)
            self._checks.append((check, member.path))

        note = QLabel(tr("sidecar.atomic", count=group.count))
        note.setObjectName("note")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.remember = QCheckBox(tr("sidecar.remember"))
        # Ticked for "ask once"; unticked for "ask each time", where a ticked box
        # turned asking off with the first answer.
        self.remember.setChecked(remember)
        layout.addWidget(self.remember)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        buttons.addStretch(1)
        cancel = QPushButton(tr("cancel"))
        cancel.clicked.connect(self.reject)
        master_only = QPushButton(tr("sidecar.master_only"))
        master_only.clicked.connect(self._master_only)
        accept = QPushButton(tr("sidecar.move_all", count=group.count))
        accept.setObjectName("primaryButton")
        accept.setDefault(True)
        accept.clicked.connect(self._move_all)
        buttons.addWidget(cancel)
        buttons.addWidget(master_only)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        self._remember_decision: bool | None = None
        set_primary_button(self, accept)
        self._block_button_arrows()

    def _move_all(self) -> None:
        self._remember_decision = self.remember.isChecked() and all(
            check.isChecked() for check, _path in self._checks if check.isEnabled())
        self.accept()

    def _master_only(self) -> None:
        self._remember_decision = False
        for check, _path in self._checks:
            check.setChecked(check.isEnabled() is False)
        self.accept()

    def chosen(self) -> list[Path]:
        return [path for check, path in self._checks if check.isChecked()]

    def remembered(self) -> bool:
        if self._remember_decision is None:
            return self.remember.isChecked()
        return self._remember_decision


class TableDialog(QDialog):
    """History, statistics, media info — anything that is a list of rows."""

    def __init__(self, title: str, columns: list[str], rows: list[list],
                 actions: list[tuple[str, object]] | None = None,
                 subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1020, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(_title_row(title, subtitle))

        table = QTableWidget(0, len(columns))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setHorizontalHeaderLabels(columns)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(26)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # Measuring a column means visiting every cell in it. A history of two
        # thousand operations is twelve thousand cells, and doing that on open
        # stalled the window for seconds.
        table.horizontalHeader().setResizeContentsPrecision(_MEASURE_ROWS)
        layout.addWidget(table, 1)
        self.table = table
        self._rows = rows
        self._written = 0
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._write_chunk)
        if len(rows) <= _CHUNK_ROWS:
            self._write_chunk()
        else:
            # Long lists arrive a few hundred rows at a time so the window is
            # up and scrollable straight away.
            self._timer.start()

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        for label, handler in actions or []:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, fn=handler: (self.accept(), fn()))
            buttons.addWidget(button)
        buttons.addStretch(1)
        close = QPushButton(tr("close"))
        close.setObjectName("primaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        set_primary_button(self, close)

    def _write_chunk(self) -> None:
        batch = self._rows[self._written:self._written + _CHUNK_ROWS]
        if not batch:
            self._timer.stop()
            self.table.resizeColumnsToContents()
            return
        self.table.setRowCount(self._written + len(batch))
        for offset, row in enumerate(batch):
            for column, value in enumerate(row):
                text = str(value)
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(self._written + offset, column, item)
        self._written += len(batch)
        if self._written >= len(self._rows):
            self._timer.stop()
            self.table.resizeColumnsToContents()

    def done(self, result: int) -> None:          # noqa: D401 - Qt entry point
        self._timer.stop()
        super().done(result)


class BackupDialog(QDialog):
    """Shows what the restore copies cost and offers the two ways to shrink them."""

    def __init__(self, used: int, policy, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("backup.title"))
        self.setMinimumWidth(620)
        self.result_action = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(_title_row(tr("backup.title"), tr("backup.policy_text"), "shield"))

        bar = QProgressBar()
        bar.setObjectName("quotaBar")
        cap = max(1, policy.max_bytes)
        bar.setRange(0, 100)
        percent = 0 if policy.max_bytes == 0 else min(100, int(used * 100 / cap))
        bar.setValue(percent)
        bar.setProperty("full", "true" if percent > 85 else "false")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(bar, 1)
        row.addWidget(QLabel(tr("unlimited") if policy.max_bytes == 0
                             else f"{human_size(used)} / {human_size(policy.max_bytes)}"))
        layout.addLayout(row)

        for label, value in (
            (tr("backup.keep_last"), tr("backup.operations", count=policy.max_operations)),
            (tr("backup.disk_cap"), tr("unlimited") if policy.max_bytes == 0
             else human_size(policy.max_bytes)),
            (tr("backup.keep_days"), tr("backup.days", count=policy.max_days)),
        ):
            line = QHBoxLayout()
            line.addWidget(caption(label), 1)
            value_label = QLabel(value)
            value_label.setStyleSheet("font-weight: 700;")
            line.addWidget(value_label)
            layout.addLayout(line)

        layout.addWidget(separator(vertical=False))
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        clear = QPushButton(tr("backup.clear_all"))
        clear.setObjectName("dangerButton")
        clear.clicked.connect(lambda: self._finish("clear"))
        reclaim = QPushButton(tr("backup.reclaim_now"))
        reclaim.clicked.connect(lambda: self._finish("reclaim"))
        close = QPushButton(tr("close"))
        close.setObjectName("primaryButton")
        close.clicked.connect(self.reject)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        buttons.addWidget(reclaim)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        set_primary_button(self, close)

    def _finish(self, action: str) -> None:
        self.result_action = action
        self.accept()
