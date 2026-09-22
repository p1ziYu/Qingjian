"""Regression probes for key actions, conflict memory, and the status row."""
from __future__ import annotations

from unittest import mock

from test_interface import WindowCase, HAVE_QT

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
