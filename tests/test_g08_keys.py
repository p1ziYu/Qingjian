"""Regression probes for key actions, conflict memory, and the status row."""
from __future__ import annotations

from unittest import mock

from test_interface import QtCase, WindowCase, HAVE_QT

if HAVE_QT:
    from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtTest import QTest
    from qingjian.core import config, ops
    from qingjian.core.engine import Engine
    from qingjian.core.i18n import tr
    from qingjian.ui.mainwindow import ConflictDialog, MainWindow


class G08KeysTests(WindowCase):
    def test_f022_conflict_memory_scoped_to_folder_and_destination(self):
        first = self.source / "IMG_0000.JPG"
        second = self.source / "IMG_0001.JPG"
        other = self.tmp / "other"
        other.mkdir()
        (other / first.name).write_bytes(b"new")
        self.keep.mkdir()
        (self.keep / first.name).write_bytes(b"old")
        (self.keep / second.name).write_bytes(b"old")
        binding = config.Binding("1", "copy", str(self.keep))
        with mock.patch.object(ConflictDialog, "ask", return_value=(ops.CONFLICT_REPLACE, True)) as ask, \
             mock.patch.object(self.window, "_run_operation"):
            self.window._classify_one(binding, first)
            self.window._classify_one(binding, second)
            self.assertEqual(1, ask.call_count)
            self.assertIn("IMG_0001", self.window.status_label.text())
            alt = config.Binding("2", "copy", str(other))
            self.window._classify_one(alt, first)
            self.assertEqual(2, ask.call_count)
            self.window.open_folder(other)
            self.window._classify_one(binding, first)
            self.assertEqual(3, ask.call_count)

    def test_f022_remembered_replace_survives_operation_completion(self):
        self.keep.mkdir()
        first = self.source / "IMG_0000.JPG"
        second = self.source / "IMG_0001.JPG"
        for path in (first, second):
            (self.keep / path.name).write_bytes(b"old")
        binding = config.Binding("1", "copy", str(self.keep))
        with mock.patch.object(ConflictDialog, "ask", return_value=(ops.CONFLICT_REPLACE, True)) as ask:
            self.assertTrue(self.window._classify_one(binding, first))
            self.assertTrue(self.window._classify_one(binding, second))
        self.assertEqual(1, ask.call_count)
        self.assertIn("IMG_0001", self.window.status_label.text())
        self.assertIn(tr("status.remembered_replace", name=second.name),
                      self.window.status_label.text())

    def test_f022_rescan_preset_and_target_change_clear_memory(self):
        from PySide6.QtWidgets import QFileDialog
        memory = self.window._conflict_defaults
        memory[str(self.keep)] = ops.CONFLICT_REPLACE
        self.window.rescan()
        self.assertFalse(memory)
        memory[str(self.keep)] = ops.CONFLICT_REPLACE
        self.window._switch_preset(self.settings.current_profile)
        self.assertFalse(memory)
        memory[str(self.keep)] = ops.CONFLICT_REPLACE
        with mock.patch.object(QFileDialog, "getExistingDirectory",
                               return_value=str(self.tmp / "new-target")):
            self.window.choose_binding_folder(0)
        self.assertFalse(memory)

    def test_f022_sync_error_clears_remembered_replace_marker(self):
        from qingjian.core.safestore import TransactionError
        path = self.engine.current_path()
        self.window._remembered_replace_paths.add(path)

        def fail(_progress, _cancel):
            raise TransactionError("error.external_change", "injected")

        with mock.patch.object(self.window, "_report"):
            self.window._run_operation(path, fail, "injected")
        self.assertNotIn(path, self.window._remembered_replace_paths)

    def test_f074_held_arrow_key_settles_without_filing_unseen_photo(self):
        self.window.thumbs.clear()
        self.window.preloader.clear()
        self.window._arrows.repeating = True
        self.window._arrow(1)
        self.window._arrow(1)
        self.addCleanup(self.window._settle_timer.stop)
        current = self.engine.current_path()
        self.assertNotEqual(current, self.window.preview.current_path)
        self.window.classify_index(0)
        self.assertTrue(current.exists())
        self.assertFalse(self.window._settle_pending)
        self.assertEqual(current, self.window.preview.current_path)

    def test_f075_empty_search_return_does_not_file(self):
        current = self.engine.current_path()
        self.window.search_edit.setFocus()
        QTest.keyClick(self.window.search_edit, Qt.Key.Key_Return)
        self.window._drain_queue()
        self.assertTrue(current.exists())
        self.assertFalse(self.keep.exists())

    def test_f079_ledger_fits_after_text_grows(self):
        self.window._change_language("zh")
        self.window.resize(1180, 740)
        self.window._handled_count = 1234
        self.window.handled_button.setVisible(True)
        self.window.progress_label.setText(tr("status.session_progress", done=1234, total=20000))
        self.window.queue_label.setText(tr("status.queue_pending", count=12))
        self.window._update_actions()
        self.app.processEvents()
        self.assertEqual("1234", self.window.handled_button.text())
        self.assertIn("1234", self.window.handled_button.toolTip())
        row = self.window._ledger_row
        row.invalidate()
        margins = self.window.centralWidget().layout().contentsMargins()
        room = self.window.centralWidget().width() - margins.left() - margins.right()
        self.assertLessEqual(row.minimumSize().width(), room)

    def test_f079_three_pixel_growth_schedules_refit(self):
        self.window._fit_ledger()
        self.app.processEvents()
        old = self.window._ledger_fit_sizes
        self.window._ledger_fit_sizes = (old[0] - 3, old[1], old[2], old[3])
        self.window._schedule_ledger_fit()
        self.assertTrue(self.window._ledger_fit_timer.isActive())
        self.app.processEvents()
        self.assertEqual(self.window._ledger_sizes(), self.window._ledger_fit_sizes)

    def test_f079_unchanged_text_width_does_not_refit_each_action(self):
        self.window._fit_ledger()
        self.app.processEvents()
        with mock.patch.object(self.window, "_overflows",
                               wraps=self.window._overflows) as overflow:
            for _ in range(10):
                self.window._update_actions()
            self.app.processEvents()
        overflow.assert_not_called()

    def test_f080_startup_keeps_reserved_key_notice(self):
        settings = config.Settings()
        settings.logging_enabled = False
        settings.source_folder = str(self.source)
        settings.bindings[5].key = "G"
        engine = Engine(self.tmp / "startup-data", settings)
        window = MainWindow(engine)
        self.addCleanup(window.close)
        window.show()
        QTest.qWait(300)
        self.assertIn("G", window.status_label.text())
        self.assertIn(tr("status.reserved_key", keys="G"), window.status_label.text())

    def test_f073_web_drop_ignores_remote_url_and_uses_local_one(self):
        import os
        original = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, original)
        local = self.tmp / "second"
        local.mkdir()
        from PIL import Image
        Image.new("RGB", (32, 24)).save(local / "next.jpg")
        remote = QUrl("https://example.com/photo.jpg")

        def drop(urls):
            mime = QMimeData()
            mime.setUrls(urls)
            enter = QDragEnterEvent(QPoint(1, 1), Qt.DropAction.CopyAction, mime,
                                   Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.window.dragEnterEvent(enter)
            event = QDropEvent(QPointF(1, 1), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.window.dropEvent(event)
            return enter.isAccepted()

        self.assertFalse(drop([remote]))
        self.assertEqual(self.source, self.engine.source_root)
        self.assertEqual(str(self.source), self.settings.source_folder)
        self.assertTrue(drop([remote, QUrl.fromLocalFile(str(local))]))
        self.assertEqual(local, self.engine.source_root)


class G08EditorTests(QtCase):
    """The bindings and settings editors, built on their own."""

    def _press(self, edit, key, text):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication
        QApplication.sendEvent(edit, QKeyEvent(QEvent.Type.KeyPress, key,
                                               Qt.KeyboardModifier.NoModifier, text))

    def test_a_key_whose_capital_is_two_letters_is_captured_as_typed(self):
        """"ß".upper() is "SS", which QKeySequence reads as no key at all: the
        dialog saved it without a word, and pressing ß then did nothing."""
        from PySide6.QtGui import QKeySequence, QShortcut
        from PySide6.QtWidgets import QWidget
        from qingjian.ui.editors import ShortcutEdit
        edit = ShortcutEdit("1")
        self.addCleanup(edit.deleteLater)
        self._press(edit, Qt.Key.Key_ssharp, "ß")
        self.assertEqual("ß", edit.sequence())
        target = QWidget()
        self.addCleanup(target.deleteLater)
        fired = []
        QShortcut(QKeySequence(edit.sequence()), target).activated.connect(
            lambda: fired.append(True))
        target.show()
        target.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(target, 2000))
        QTest.keyClick(target, Qt.Key.Key_ssharp)
        self.assertEqual([True], fired)
        # Ordinary letters are still stored as capitals.
        self._press(edit, Qt.Key.Key_A, "a")
        self.assertEqual("A", edit.sequence())

    def test_a_key_that_does_not_read_back_as_one_is_refused_rather_than_saved_dead(self):
        from PySide6.QtGui import QKeySequence
        from qingjian.ui.editors import ShortcutEdit
        self.assertEqual(Qt.Key.Key_unknown, QKeySequence("İ")[0].key())   # what Qt makes of it
        edit = ShortcutEdit("1")
        self.addCleanup(edit.deleteLater)
        self._press(edit, Qt.Key.Key_unknown, "İ")
        self.assertEqual("1", edit.sequence())

    def test_escape_in_a_key_field_still_cancels_the_dialog(self):
        """The field took every key it was given, Escape included, so the
        bindings dialog could not be cancelled from the keyboard while a key
        field had the focus."""
        from qingjian.ui.editors import BindingsDialog
        dialog = BindingsDialog(config.default_bindings())
        self.addCleanup(dialog.deleteLater)
        rejected = []
        dialog.rejected.connect(lambda: rejected.append(True))
        dialog.show()
        field = dialog.rows[0]["key"]
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_Escape)
        self.assertEqual([True], rejected)
        self.assertFalse(dialog.isVisible())
        self.assertEqual("1", field.sequence())

    def test_a_language_the_page_has_no_option_for_is_not_saved_as_chinese(self):
        """"EN", "en-US" and "auto" all run the window, but the page offered
        none of them, showed its first option -- 中文 -- and saved it."""
        from qingjian.ui.editors import SettingsDialog
        from qingjian.ui.widgets import Segmented
        for value, shown in (("EN", "en"), ("en-US", "en"), ("auto", "system")):
            with self.subTest(value=value):
                settings = config.Settings()
                settings.language = value
                dialog = SettingsDialog(settings)
                self.addCleanup(dialog.deleteLater)
                self.assertEqual(shown, dialog._control("language", Segmented).value())
                dialog._save()
                self.assertEqual(shown, dialog.result_settings().language)

    def test_saving_with_the_folder_menu_on_repairs_an_entry_another_copy_left(self):
        """A portable folder that moved leaves Explorer's entry starting a
        program that is gone. The switch read it as on, and saving with it on
        never wrote the entry again; and the command never carried the
        ``--data-dir`` this copy was started with."""
        from types import SimpleNamespace
        from test_config_logging import FakeRegistry
        from qingjian.core import platform_
        from qingjian.ui.editors import SettingsDialog
        from qingjian.ui.widgets import Switch
        registry = FakeRegistry()
        self.patch(platform_, "IS_WINDOWS", True)
        self.patch(platform_, "_registry", lambda _registry=None: registry)
        platform_.set_folder_menu(True, "Open with Qingjian", [r"F:\Gone\MediaSorter.exe"])
        engine = SimpleNamespace(data_dir=self.data, backup_usage=lambda: 0)
        dialog = SettingsDialog(config.Settings(), engine)
        self.addCleanup(dialog.deleteLater)
        switch = dialog._control("folder_menu", Switch)
        self.assertFalse(switch.isChecked())                # not this copy's menu
        switch.setChecked(True)
        dialog._save()
        written = registry.keys[platform_.FOLDER_MENU_KEYS[0][0] + "\\command"][""]
        self.assertIn(f'"--data-dir" "{self.data}"', written)
        self.assertNotIn("Gone", written)
        again = SettingsDialog(config.Settings(), engine)
        self.addCleanup(again.deleteLater)
        self.assertTrue(again._control("folder_menu", Switch).isChecked())
