"""Editors: key bindings, path templates and application settings."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
                               QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from ..core import config, platform_, template
from ..core.engine import human_size
from ..core.i18n import LANGUAGES, tr
from ..core.logsetup import get_logger
from ..core.naming import NameError_
from ..core.safestore import QuotaPolicy, VERIFY_FAST, VERIFY_FULL
from ..core.sidecar import (PROMPT_ALWAYS, PROMPT_EACH, PROMPT_NEVER, PROMPT_ONCE,
                            SidecarRules)
from . import icons, theme
from .prompts import ask, information, warning
from .widgets import (SectionCard, SettingRow, Segmented, Switch, caption, separator,
                      set_primary_button)

log = get_logger("editors")


def shortcut_identity(text: str) -> int | None:
    """Return the Qt key identity used by QShortcut, independent of case."""
    sequence = QKeySequence(text.strip())
    return sequence[0].toCombined() if not sequence.isEmpty() else None


class ShortcutEdit(QLineEdit):
    """Captures one key press as the binding's shortcut."""

    def __init__(self, sequence: str = "", parent=None) -> None:
        super().__init__(sequence, parent)
        self.setReadOnly(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setPlaceholderText("—")

    def keyPressEvent(self, event) -> None:      # noqa: N802 - Qt naming
        key = event.key()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            super().keyPressEvent(event)
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self.setText("")
            return
        if event.modifiers() & (Qt.KeyboardModifier.ShiftModifier
                                | Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.AltModifier
                                | Qt.KeyboardModifier.MetaModifier):
            return
        text = event.text()
        if text and text.isprintable() and not text.isspace():
            self.setText(text.upper())
            return
        named = {Qt.Key.Key_Space: "Space", Qt.Key.Key_Return: "Return",
                 Qt.Key.Key_Enter: "Enter", Qt.Key.Key_Insert: "Insert",
                 Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End"}
        if key in named:
            self.setText(named[key])

    def sequence(self) -> str:
        return self.text().strip()


class TemplateEditor(QDialog):
    """Edit one binding's path and filename templates, with a live preview."""

    def __init__(self, binding: config.Binding, samples: list, engine=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("tpl.title"))
        self.resize(940, 700)
        self.binding = binding
        self.samples = samples
        self.engine = engine

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)
        heading = QLabel(tr("tpl.title"))
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        layout.addWidget(caption(tr("tpl.applies_to", preset="", key=binding.key,
                                    target=Path(binding.folder).name or binding.folder)))

        card = QFrame()
        card.setObjectName("settingsCard")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(16, 14, 16, 14)
        inner.setSpacing(10)

        inner.addWidget(self._label(tr("tpl.path")))
        self.path_edit = QLineEdit(binding.path_template)
        self.path_edit.setPlaceholderText("{YYYY}/{YYYY-MM}")
        inner.addWidget(self.path_edit)
        inner.addWidget(self._chips(template.SUGGESTED_PATH_TOKENS, self.path_edit))

        inner.addWidget(separator(vertical=False))
        inner.addWidget(self._label(tr("tpl.name")))
        self.name_edit = QLineEdit(binding.name_template)
        self.name_edit.setPlaceholderText("{YYYY-MM-DD}_{seq:4}_{name}")
        inner.addWidget(self.name_edit)
        inner.addWidget(self._chips(template.SUGGESTED_NAME_TOKENS, self.name_edit))

        numbers = QHBoxLayout()
        numbers.setSpacing(14)
        numbers.addWidget(caption(tr("tpl.seq_start")))
        self.seq_start = QSpinBox()
        self.seq_start.setRange(1, 10 ** 8)
        self.seq_start.setValue(max(1, binding.sequence_start))
        numbers.addWidget(self.seq_start)
        numbers.addStretch(1)
        inner.addLayout(numbers)
        layout.addWidget(card)

        preview_card = QFrame()
        preview_card.setObjectName("settingsCard")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(16, 14, 16, 14)
        preview_layout.setSpacing(9)
        header = QHBoxLayout()
        header.addWidget(self._label(tr("tpl.preview")))
        header.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("caption")
        header.addWidget(self.status)
        preview_layout.addLayout(header)
        self.preview = QListWidget()
        self.preview.setObjectName("previewList")
        preview_layout.addWidget(self.preview, 1)
        layout.addWidget(preview_card, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        reset = QPushButton(tr("reset"))
        reset.clicked.connect(self._reset)
        cancel = QPushButton(tr("cancel"))
        cancel.clicked.connect(self.reject)
        apply_button = QPushButton(tr("tpl.apply_to_preset"))
        apply_button.setObjectName("primaryButton")
        apply_button.clicked.connect(self._apply)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)
        set_primary_button(self, apply_button)

        self.path_edit.textChanged.connect(self._refresh)
        self.name_edit.textChanged.connect(self._refresh)
        self.seq_start.valueChanged.connect(self._refresh)
        self._refresh()

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("rowTitle")
        return label

    def _chips(self, tokens, target: QLineEdit) -> QWidget:
        holder = QWidget()
        flow = QHBoxLayout(holder)
        flow.setContentsMargins(0, 0, 0, 0)
        flow.setSpacing(6)
        for token in tokens:
            chip = QPushButton("{" + token + "}")
            chip.setAutoDefault(False)
            chip.setObjectName("compactButton")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, value=token, edit=target: edit.insert(
                "{" + value + "}"))
            flow.addWidget(chip)
        flow.addStretch(1)
        return holder

    def _reset(self) -> None:
        self.path_edit.setText("")
        self.name_edit.setText("")

    def _refresh(self) -> None:
        path_template = self.path_edit.text()
        name_template = self.name_edit.text()
        try:
            template.validate(path_template, name_template)
            self.status.setText(tr("tpl.valid"))
            self.status.setStyleSheet(f"color: {theme.BALLPOINT_LIGHT};")
        except NameError_ as error:
            self.status.setText(tr("tpl.invalid", error=tr(error.key, **error.fields)))
            self.status.setStyleSheet(f"color: {theme.GREASE};")
            self.preview.clear()
            return

        self.preview.clear()
        base = Path(self.binding.folder or "…")
        start = self.seq_start.value()
        if self.engine is not None:
            preview_binding = config.Binding(**self.binding.to_dict())
            preview_binding.sequence_start = start
            start = self.engine.planner.sequence.peek(preview_binding)
        for index, context in enumerate(self.samples[:6], start=start):
            context.sequence = index
            try:
                target = template.render_destination(base, path_template, name_template,
                                                     context, strict=False)
                text = f"{context.source.name}    →    {target}"
                if context.when_is_fallback:
                    text += f"    · {tr('tpl.badge.fallback')}"
            except NameError_ as error:
                text = f"{context.source.name}    →    {tr(error.key, **error.fields)}"
            item = QListWidgetItem(text)
            self.preview.addItem(item)

    def _apply(self) -> None:
        try:
            template.validate(self.path_edit.text(), self.name_edit.text())
        except NameError_ as error:
            warning(self, tr("error.title"), tr(error.key, **error.fields))
            return
        self.binding.path_template = self.path_edit.text().strip()
        self.binding.name_template = self.name_edit.text().strip()
        self.binding.sequence_start = self.seq_start.value()
        self.accept()


class BindingsDialog(QDialog):
    """The ten keys, what each does and where it sends things."""

    def __init__(self, bindings: list[config.Binding], samples=None, parent=None,
                 reserved=(), engine=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("bind.title"))
        self.setMinimumSize(940, 640)
        self.samples = samples or []
        self.engine = engine
        #: Keys the window already answers to; a binding on one would be dead.
        self.reserved = list(reserved)
        self._working = [config.Binding(**b.to_dict()) for b in bindings]
        self.rows: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)
        heading = QLabel(tr("bind.title"))
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        layout.addWidget(caption(tr("bind.subtitle")))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 4, 8, 4)
        rows.setSpacing(8)
        for index, binding in enumerate(self._working):
            rows.addWidget(self._build_row(index, binding))
        rows.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        reset = QPushButton(tr("bind.reset_keys"))
        reset.clicked.connect(self._reset_keys)
        cancel = QPushButton(tr("cancel"))
        cancel.clicked.connect(self.reject)
        accept = QPushButton(tr("save"))
        accept.setObjectName("primaryButton")
        accept.clicked.connect(self._accept)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        set_primary_button(self, accept)

    def _build_row(self, index: int, binding: config.Binding) -> QWidget:
        row = QFrame()
        row.setObjectName("settingsRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(12, 10, 10, 10)
        line.setSpacing(9)

        key_edit = ShortcutEdit(binding.key)
        key_edit.setFixedWidth(84)
        line.addWidget(key_edit)

        action = QComboBox()
        action.setFixedWidth(150)
        for name, (label_key, _desc, _needs) in config.ACTIONS.items():
            action.addItem(tr(label_key), name)
        found = action.findData(binding.action)
        action.setCurrentIndex(max(0, found))
        line.addWidget(action)

        folder = QLineEdit(binding.folder)
        folder.setPlaceholderText(tr("bind.target_folder"))
        line.addWidget(folder, 1)

        browse = QPushButton(tr("browse"))
        browse.setObjectName("compactButton")
        browse.clicked.connect(lambda _=False, edit=folder, key=binding.key: self._browse(edit, key))
        line.addWidget(browse)

        template_button = QPushButton(tr("bind.edit_template"))
        template_button.setObjectName("compactButton")
        template_button.clicked.connect(lambda _=False, i=index: self._edit_template(i))
        line.addWidget(template_button)

        entry = {"key": key_edit, "action": action, "folder": folder,
                 "browse": browse, "template": template_button, "index": index}
        self.rows.append(entry)
        action.currentIndexChanged.connect(lambda _=0, e=entry: self._sync_row(e))
        self._sync_row(entry)
        return row

    def _sync_row(self, entry: dict) -> None:
        needs = config.ACTIONS.get(entry["action"].currentData(), ("", "", False))[2]
        for widget in ("folder", "browse", "template"):
            entry[widget].setEnabled(needs)

    def _browse(self, edit: QLineEdit, key: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("bind.choose_for", key=key),
                                                  edit.text() or str(Path.home()))
        if folder:
            edit.setText(folder)

    def _edit_template(self, index: int) -> None:
        self._harvest()
        binding = self._working[index]
        dialog = TemplateEditor(binding, self.samples, engine=self.engine, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.rows[index]["template"].setToolTip(
                f"{binding.path_template}  /  {binding.name_template}")

    def _reset_keys(self) -> None:
        for index, entry in enumerate(self.rows):
            entry["key"].setText(config.DEFAULT_KEYS[index % config.BINDING_COUNT])

    def _harvest(self) -> None:
        for entry in self.rows:
            binding = self._working[entry["index"]]
            binding.key = entry["key"].sequence()
            binding.action = entry["action"].currentData()
            binding.folder = entry["folder"].text().strip()

    def _accept(self) -> None:
        self._harvest()
        seen: dict[int, list[str]] = {}
        for binding in self._working:
            if binding.key:
                identity = shortcut_identity(binding.key)
                if identity is not None:
                    seen.setdefault(identity, []).append(binding.key)
        clashes = sorted({key.upper() for keys in seen.values() if len(keys) > 1
                          for key in keys})
        if clashes:
            warning(self, tr("error.title"),
                    tr("bind.duplicate_key", key=", ".join(clashes)))
            return
        reserved = {shortcut_identity(key) for key in self.reserved}
        taken = sorted({binding.key for binding in self._working
                        if binding.key and shortcut_identity(binding.key) in reserved})
        if taken:
            warning(self, tr("error.title"),
                    tr("bind.reserved_key", key=", ".join(taken)))
            return
        for binding in self._working:
            if binding.needs_folder() and binding.folder and not Path(binding.folder).expanduser().is_absolute():
                warning(self, tr("error.title"), tr("bind.target_folder"))
                return
        self.accept()

    def result_bindings(self) -> list[config.Binding]:
        return self._working


class SettingsDialog(QDialog):
    """One scrolling page per section, with the section list on the left."""

    def __init__(self, settings: config.Settings, engine=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("settings.title"))
        self.resize(1060, 780)
        self.settings = config.Settings.from_dict(settings.to_dict())
        self.engine = engine
        self._controls: dict[str, object] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(3)
        heading = QLabel(tr("settings.title"))
        heading.setObjectName("dialogTitle")
        titles.addWidget(heading)
        titles.addWidget(caption(tr("settings.subtitle")))
        header.addLayout(titles, 1)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(196)
        self.nav.setFrameShape(QListWidget.Shape.NoFrame)
        self.pages = QStackedWidget()
        for key, icon_name, builder in (
            ("settings.nav.general", "gear", self._page_general),
            ("settings.nav.sidecar", "sidecar", self._page_sidecar),
            ("settings.nav.safety", "shield", self._page_safety),
            ("settings.nav.performance", "sliders", self._page_performance),
            ("settings.nav.about", "info", self._page_about),
        ):
            item = QListWidgetItem(icons.icon(icon_name, 16, theme.PAPER_DIM), tr(key))
            self.nav.addItem(item)
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QScrollArea.Shape.NoFrame)
            page.setWidget(builder())
            self.pages.addWidget(page)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        body.addWidget(self.nav)
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(9)
        buttons.addStretch(1)
        cancel = QPushButton(tr("cancel"))
        cancel.clicked.connect(self.reject)
        save = QPushButton(tr("save"))
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        set_primary_button(self, save)

    # -- pages ---------------------------------------------------------
    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        return page, layout

    def _segmented(self, key: str, options, value: str) -> Segmented:
        widget = Segmented(options)
        widget.set_value(value, quiet=True)
        self._controls[key] = widget
        return widget

    def _switch(self, key: str, value: bool) -> Switch:
        widget = Switch()
        widget.setChecked(bool(value))
        self._controls[key] = widget
        return widget

    def _spin(self, key: str, value: int, low: int, high: int, suffix: str = "") -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(low, high)
        widget.setValue(int(value))
        if suffix:
            widget.setSuffix(suffix)
        self._controls[key] = widget
        return widget

    def _page_general(self) -> QWidget:
        page, layout = self._page()
        card = SectionCard(tr("settings.nav.general"), "gear")
        card.add_row(SettingRow(
            tr("settings.language"), tr("settings.language.desc"),
            self._segmented("language",
                            [(code, name) for code, name in LANGUAGES]
                            + [("system", tr("settings.language.system"))],
                            self.settings.language),
            first=True))
        card.add_row(SettingRow(
            tr("settings.density"), tr("settings.density.desc"),
            self._segmented("density",
                            [("compact", tr("settings.density.compact")),
                             ("standard", tr("settings.density.standard")),
                             ("roomy", tr("settings.density.roomy"))],
                            self.settings.density)))
        card.add_row(SettingRow(
            tr("settings.restore_position"), tr("settings.restore_position.desc"),
            self._switch("restore_position", self.settings.restore_position)))
        card.add_row(SettingRow(
            tr("settings.default_view"), "",
            self._segmented("default_view",
                            [(config.VIEW_SINGLE, tr("view.single")),
                             (config.VIEW_GRID, tr("view.grid"))],
                            self.settings.default_view)))
        if platform_.IS_WINDOWS:
            # The registry is the setting: read it rather than keep a copy that
            # could disagree with what Explorer actually shows.
            self._folder_menu_was = platform_.folder_menu_installed()
            card.add_row(SettingRow(
                tr("settings.folder_menu"), tr("settings.folder_menu.desc"),
                self._switch("folder_menu", self._folder_menu_was)))
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _page_sidecar(self) -> QWidget:
        page, layout = self._page()
        rules = self.settings.sidecar
        card = SectionCard(tr("settings.nav.sidecar"), "sidecar")
        card.add_row(SettingRow(
            tr("settings.sidecar.enable"), tr("settings.sidecar.enable.desc"),
            self._switch("sidecar_enabled", rules.enabled), first=True))
        card.add_row(SettingRow(
            tr("settings.sidecar.prompt"), tr("settings.sidecar.prompt.desc"),
            self._segmented("sidecar_prompt",
                            [(PROMPT_EACH, tr("settings.sidecar.ask_each")),
                             (PROMPT_ONCE, tr("settings.sidecar.ask_once")),
                             (PROMPT_ALWAYS, tr("settings.sidecar.always")),
                             (PROMPT_NEVER, tr("settings.sidecar.never"))],
                            rules.prompt)))
        for key, label, values in (
            ("sidecar_raw", tr("settings.sidecar.groups.raw"), rules.raw_extensions),
            ("sidecar_meta", tr("settings.sidecar.groups.metadata"), rules.metadata_extensions),
            ("sidecar_live", tr("settings.sidecar.groups.live"), rules.live_extensions),
        ):
            edit = QLineEdit(" ".join(sorted(values)))
            edit.setObjectName("mono")
            self._controls[key] = edit
            card.add_row(SettingRow(label, "", None))
            card.body.addWidget(edit)
        card.add_row(SettingRow(
            tr("settings.sidecar.hide"), tr("settings.sidecar.hide.desc"),
            self._switch("sidecar_hide", rules.hide_from_queue)))
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _page_safety(self) -> QWidget:
        page, layout = self._page()
        card = SectionCard(tr("settings.nav.safety"), "shield")
        card.add_row(SettingRow(
            tr("settings.verification"), tr("settings.verification.desc"),
            self._segmented("verification",
                            [(VERIFY_FULL, tr("settings.verification.full")),
                             (VERIFY_FAST, tr("settings.verification.fast"))],
                            self.settings.verification), first=True))
        card.add_row(SettingRow(
            tr("settings.fastpath"), tr("settings.fastpath.desc"),
            self._switch("fast_path", self.settings.fast_path)))
        quota = self.settings.quota
        self._quota_original = quota
        numbers = QWidget()
        row = QHBoxLayout(numbers)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(caption(tr("backup.keep_last")))
        row.addWidget(self._spin("quota_ops", quota.max_operations, 0, 2_147_483_647))
        row.addWidget(caption(tr("backup.disk_cap")))
        quota_gb = QDoubleSpinBox()
        quota_gb.setRange(0, 1_000_000_000)
        quota_gb.setDecimals(3)
        quota_gb.setSingleStep(0.25)
        quota_gb.setSuffix(" GB")
        quota_gb.setSpecialValueText(tr("unlimited"))
        quota_gb.setValue(quota.max_bytes / 1024 ** 3)
        self._controls["quota_gb"] = quota_gb
        row.addWidget(quota_gb)
        row.addWidget(caption(tr("backup.keep_days")))
        row.addWidget(self._spin("quota_days", quota.max_days, 0, 2_147_483_647))
        self._quota_initial = (self._controls["quota_ops"].value(), quota_gb.value(),
                               self._controls["quota_days"].value())
        card.add_row(SettingRow(tr("settings.quota"), tr("backup.policy_text"), None))
        card.body.addWidget(numbers)
        card.add_row(SettingRow(
            tr("settings.recycle"), tr("settings.recycle.soft.desc"),
            self._segmented("recycle",
                            [(config.RECYCLE_SOFT, tr("settings.recycle.soft")),
                             (config.RECYCLE_SYSTEM, tr("settings.recycle.system"))],
                            self.settings.recycle_mode)))
        card.add_row(SettingRow(
            tr("settings.logging"), tr("settings.logging.desc"),
            self._switch("logging", self.settings.logging_enabled)))
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _page_performance(self) -> QWidget:
        page, layout = self._page()
        card = SectionCard(tr("settings.nav.performance"), "sliders")
        card.add_row(SettingRow(
            tr("settings.background_queue"), tr("settings.background_queue.desc"),
            self._switch("background_queue", self.settings.background_queue), first=True))
        card.add_row(SettingRow(tr("settings.workers"), "",
                                self._spin("workers", self.settings.workers, 1, 32)))
        card.add_row(SettingRow(tr("settings.thumb_cache"), "",
                                self._spin("thumb_cache", self.settings.thumb_cache_mb,
                                           64, 65536, " MB")))
        card.add_row(SettingRow(
            tr("settings.hash_cache"), tr("settings.hash_cache.desc"),
            self._switch("hash_cache", self.settings.hash_cache)))
        threshold = QDoubleSpinBox()
        threshold.setRange(0.89, 1.0)
        threshold.setSingleStep(0.01)
        threshold.setDecimals(2)
        threshold.setValue(self.settings.similar_threshold)
        self._controls["similar_threshold"] = threshold
        card.add_row(SettingRow(tr("dup.threshold"), "", threshold))
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _page_about(self) -> QWidget:
        from .. import __display_name__, __version__
        page, layout = self._page()
        card = SectionCard(tr("settings.nav.about"), "info")
        card.body.addWidget(QLabel(f"{__display_name__} · Qingjian {__version__}"))
        if self.engine is not None:
            card.body.addWidget(caption(str(self.engine.data_dir)))
            card.body.addWidget(caption(
                f"{tr('backup.usage')}: {human_size(self.engine.backup_usage())}"))
        export = QPushButton(tr("settings.export_bundle"))
        export.clicked.connect(self._export_bundle)
        card.body.addWidget(export)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _export_bundle(self) -> None:
        if self.engine is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("settings.export_bundle"),
                                                  "qingjian-diagnostics.zip", "Zip (*.zip)")
        if filename:
            try:
                self.engine.diagnostic_bundle(filename)
            except (OSError, zipfile.LargeZipFile) as error:
                log.warning("diagnostic bundle export failed: %s", error, exc_info=True)
                warning(self, tr("error.title"), tr("settings.export_failed", error=error))
            else:
                information(self, tr("settings.export_bundle"),
                            tr("settings.export_saved", path=filename))

    # -- saving --------------------------------------------------------
    def _save(self) -> None:
        get = self._controls.get
        settings = self.settings
        rules = settings.sidecar
        emptied = [key for key, old in (("sidecar_raw", rules.raw_extensions),
                                        ("sidecar_meta", rules.metadata_extensions),
                                        ("sidecar_live", rules.live_extensions))
                   if old and get(key) is not None and not get(key).text().strip()]
        if emptied and ask(self, tr("settings.sidecar.clear_title"),
                           tr("settings.sidecar.clear_confirm"),
                           default=QMessageBox.StandardButton.No) \
                != QMessageBox.StandardButton.Yes:
            return
        settings.language = get("language").value()
        settings.density = get("density").value()
        settings.restore_position = get("restore_position").isChecked()
        settings.default_view = get("default_view").value()
        settings.verification = get("verification").value()
        settings.fast_path = get("fast_path").isChecked()
        settings.recycle_mode = get("recycle").value()
        settings.logging_enabled = get("logging").isChecked()
        settings.background_queue = get("background_queue").isChecked()
        settings.workers = get("workers").value()
        settings.thumb_cache_mb = get("thumb_cache").value()
        settings.hash_cache = get("hash_cache").isChecked()
        settings.similar_threshold = float(get("similar_threshold").value())
        quota_values = (get("quota_ops").value(), get("quota_gb").value(),
                        get("quota_days").value())
        if quota_values != self._quota_initial:
            settings.quota = QuotaPolicy(
                max_operations=quota_values[0],
                max_bytes=round(quota_values[1] * 1024 ** 3),
                max_days=quota_values[2],
                automatic=settings.quota.automatic)
        else:
            settings.quota = self._quota_original

        def parse(key: str, fallback):
            control = get(key)
            if control is None:
                return fallback
            text = control.text()
            values = {("." + part.strip().lstrip(".")).lower()
                      for part in text.replace(",", " ").split() if part.strip(". ")}
            return frozenset(values)

        settings.sidecar = SidecarRules(
            enabled=get("sidecar_enabled").isChecked(),
            prompt=get("sidecar_prompt").value(),
            raw_extensions=parse("sidecar_raw", rules.raw_extensions),
            metadata_extensions=parse("sidecar_meta", rules.metadata_extensions),
            live_extensions=parse("sidecar_live", rules.live_extensions),
            extra_extensions=rules.extra_extensions,
            link_same_stem_media=rules.link_same_stem_media,
            hide_from_queue=get("sidecar_hide").isChecked())

        menu = get("folder_menu")
        if menu is not None and menu.isChecked() != self._folder_menu_was:
            script = Path(__file__).resolve().parents[2] / "main.py"
            command = platform_.launch_command(getattr(sys, "frozen", False), sys.executable,
                                               str(script))
            try:
                platform_.set_folder_menu(menu.isChecked(), tr("app.open_with"), command)
            except OSError as error:
                warning(self, tr("error.title"), str(error))
        self.accept()

    def result_settings(self) -> config.Settings:
        return self.settings
