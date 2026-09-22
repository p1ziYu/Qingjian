"""The window itself, driven offscreen.

test_static stands in for these on a machine without Qt. Where PySide6 is
installed they run the real widgets, because the failures they guard -- a
double-click that filed two photographs, a key that silently did nothing --
only exist once events are flowing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from unittest import mock
from pathlib import Path

from base import ROOT, TempCase, unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QContextMenuEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    HAVE_QT = True
except ImportError:                                     # pragma: no cover - depends on the install
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class QtCase(TempCase):
    def setUp(self):
        super().setUp()
        from qingjian.ui.app import create_app
        self.app = create_app(["qingjian-tests"])

    def patch(self, owner, name, value) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def wait_until(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(20)
        return bool(predicate())

    def until(self, predicate, message: str, timeout: float = 20.0) -> None:
        """Wait for what was supposed to happen, not for a fixed delay.

        Anything that arrives on a worker thread -- a thumbnail, a queued
        move -- lands a beat after the call that asked for it.
        """
        self.assertTrue(self.wait_until(predicate, timeout), message)


class WindowCase(QtCase):
    """A real window over eight photographs, sorting synchronously."""

    def setUp(self):
        super().setUp()
        from PIL import Image
        from qingjian.core import config
        from qingjian.core.engine import Engine
        from qingjian.ui.mainwindow import MainWindow
        self.source = self.tmp / "src"
        self.source.mkdir()
        for index in range(8):
            Image.new("RGB", (320, 240), (index * 30, 90, 140)).save(
                self.source / f"IMG_{index:04d}.JPG")
        self.keep = self.tmp / "keep"
        settings = config.Settings()
        settings.background_queue = False
        settings.restore_position = False
        settings.logging_enabled = False
        settings.bindings[0].folder = str(self.keep)
        self.settings = settings
        self.engine = Engine(self.data, settings)
        self.window = MainWindow(self.engine)
        self.addCleanup(self.close_window)
        self.window.show()
        self.app.processEvents()
        self.window.open_folder(self.source)
        self.app.processEvents()

    def close_window(self) -> None:
        self.window.close()
        # Delete it as well: a closed window keeps its shortcuts, and the next
        # case's Ctrl+Z then went to the window this case left behind.
        self.window.deleteLater()
        self.app.processEvents()

    def activate(self) -> None:
        self.window.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.window, 2000))

    def recycled(self) -> list[Path]:
        """Files sitting in any `.qingjian-trash` under the test's directory."""
        return [p for p in self.tmp.rglob("*") if p.is_file() and ".qingjian-trash" in p.parts]


class RecycleTests(WindowCase):
    def test_background_delete_keeps_reclaim_and_volume_checks_off_ui_thread(self):
        import ctypes
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from qingjian.core import config, platform_
        self.settings.background_queue = True
        self.settings.recycle_mode = config.RECYCLE_SYSTEM
        ui_thread = threading.get_ident()
        gate_threads = []
        probe_threads = []
        limit_threads = []
        policy_threads = []
        errors = []
        original = self.engine.state.retention_gate
        original_probe = platform_.can_recycle
        kernel, shell = Mock(), Mock()
        kernel.GetDriveTypeW.return_value = 3
        shell.SHQueryRecycleBinW.return_value = 0
        def gate():
            gate_threads.append(threading.get_ident())
            return original()
        def probe(path, bytes_needed=0):
            probe_threads.append(threading.get_ident())
            with patch.object(ctypes, "windll",
                              SimpleNamespace(kernel32=kernel, shell32=shell)):
                return original_probe(path, bytes_needed)
        def limit(_root):
            limit_threads.append(threading.get_ident())
            return 1_000_000
        def policy():
            policy_threads.append(threading.get_ident())
            return None, False  # Force the safe soft-recycle fallback.
        with patch.object(self.engine.state, "retention_gate", side_effect=gate), \
             patch.object(platform_, "trash_available", return_value=True), \
             patch.object(platform_, "IS_WINDOWS", True), \
             patch.object(platform_, "can_recycle", side_effect=probe), \
             patch.object(platform_, "_volume_recycle_limit", side_effect=limit), \
             patch.object(platform_, "_recycle_policy", side_effect=policy), \
             patch.object(platform_, "move_to_trash",
                          side_effect=AssertionError("real system recycle attempted")), \
             patch.object(QMessageBox, "warning",
                          side_effect=lambda _parent, _title, message: errors.append(message)):
            self.window.trash_current()
            self.engine.queue.wait_idle(10)
            self.app.processEvents()
        self.assertEqual([], errors)
        self.assertEqual(1, len(probe_threads))
        self.assertEqual(1, len(limit_threads))
        self.assertEqual(1, len(policy_threads))
        self.assertTrue(all(thread != ui_thread for thread in
                            probe_threads + limit_threads + policy_threads))
        self.assertTrue(gate_threads)
        self.assertTrue(all(thread != ui_thread for thread in gate_threads))
        self.assertTrue(self.recycled())

    def test_background_file_key_filters_group_on_ui_then_worker(self):
        import threading
        from unittest.mock import patch
        from qingjian.core.sidecar import SidecarRules, PROMPT_NEVER
        from qingjian.ui.dialogs import ConflictDialog
        self.settings.background_queue = True
        self.settings.sidecar = SidecarRules(prompt=PROMPT_NEVER)
        self.engine.planner.settings = self.settings
        jpg = self.source / "IMG_0000.JPG"
        raw = self.source / "IMG_0000.CR2"
        raw.write_bytes(b"raw")
        self.keep.mkdir(exist_ok=True)
        conflicting_raw = self.keep / raw.name
        conflicting_raw.write_bytes(b"existing")
        self.window.rescan()
        ui_thread = threading.get_ident()
        calls = []
        original = self.engine._operation_group
        def group(actual):
            filtered = original(actual)
            calls.append((threading.get_ident(),
                          [member.path for member in actual.members],
                          [member.path for member in filtered.members]))
            return filtered
        with patch.object(self.engine, "_operation_group", side_effect=group), \
             patch.object(ConflictDialog, "ask",
                          side_effect=AssertionError("unused RAW target conflict asked")):
            self.window.classify_index(0)
            self.engine.queue.wait_idle(10)
            self.app.processEvents()
        self.assertEqual(2, len(calls))
        self.assertEqual(ui_thread, calls[0][0])
        self.assertNotEqual(ui_thread, calls[1][0])
        self.assertCountEqual([jpg, raw], calls[0][1])
        self.assertEqual([jpg], calls[0][2])
        self.assertEqual([jpg], calls[1][1])
        self.assertEqual([jpg], calls[1][2])
        self.assertTrue(conflicting_raw.exists())
        self.assertFalse(jpg.exists())
        self.assertTrue((self.keep / jpg.name).exists())
        self.assertTrue(raw.exists())

    def test_never_sidecar_conflict_does_not_prompt_for_unused_target(self):
        from unittest.mock import patch
        from qingjian.core.sidecar import SidecarRules, PROMPT_NEVER
        from qingjian.ui.dialogs import ConflictDialog
        jpg = self.source / "IMG_0000.JPG"
        raw = self.source / "IMG_0000.CR2"
        raw.write_bytes(b"raw")
        self.keep.mkdir(exist_ok=True)
        (self.keep / raw.name).write_bytes(b"existing")
        self.settings.sidecar = SidecarRules(prompt=PROMPT_NEVER)
        self.engine.planner.settings = self.settings
        self.window.rescan()
        with patch.object(ConflictDialog, "ask",
                          side_effect=AssertionError("unused sidecar conflict asked")):
            self.window.classify_index(0)
        self.assertFalse(jpg.exists())
        self.assertTrue((self.keep / jpg.name).exists())
        self.assertTrue(raw.exists())

    def test_synchronous_delete_uses_soft_recycle_without_volume_probe(self):
        from unittest.mock import patch
        from qingjian.core import config, platform_
        self.settings.recycle_mode = config.RECYCLE_SYSTEM
        current = self.engine.current_path()
        with patch.object(platform_, "can_recycle") as probe, \
             patch.object(platform_, "move_to_trash") as sender:
            self.window.trash_current()
        probe.assert_not_called()
        sender.assert_not_called()
        self.assertFalse(current.exists())
        self.assertTrue(self.recycled())

    def test_synchronous_trash_binding_uses_soft_recycle(self):
        from unittest.mock import patch
        from qingjian.core import config, platform_
        self.settings.recycle_mode = config.RECYCLE_SYSTEM
        self.settings.bindings[2].action = "trash"
        current = self.engine.current_path()
        with patch.object(platform_, "can_recycle") as probe, \
             patch.object(platform_, "move_to_trash") as sender:
            self.window.classify_index(2)
        probe.assert_not_called()
        sender.assert_not_called()
        self.assertFalse(current.exists())
        self.assertTrue(self.recycled())

    def refuse_questions(self) -> list:
        asked = []

        def question(*args, **kwargs):
            asked.append(args)
            return QMessageBox.StandardButton.No

        self.patch(QMessageBox, "question", question)
        return asked

    def test_delete_recycles_at_once_and_undo_brings_it_back(self):
        asked = self.refuse_questions()
        current = self.engine.current_path()
        content = current.read_bytes()
        self.window.trash_current()
        self.app.processEvents()
        self.assertEqual([], asked)
        self.assertFalse(current.exists())
        self.assertEqual([content], [p.read_bytes() for p in self.recycled()])
        self.window.undo()
        self.app.processEvents()
        self.assertTrue(current.exists())
        self.assertEqual(content, current.read_bytes())
        self.assertEqual([], self.recycled(), "undo left a copy in .qingjian-trash")

    def test_recycling_a_grid_selection_asks_nothing(self):
        from qingjian.core import config
        asked = self.refuse_questions()
        self.settings.bindings[2].action = "trash"
        self.window._set_view(config.VIEW_GRID)
        self.app.processEvents()
        chosen = list(self.engine.queue_paths[:3])
        contents = sorted(path.read_bytes() for path in chosen)
        self.window.grid.select_all_paths(chosen)
        self.window.classify_index(2)
        self.app.processEvents()
        self.assertEqual([], asked)
        self.assertEqual([False, False, False], [path.exists() for path in chosen])
        self.assertEqual(contents, sorted(p.read_bytes() for p in self.recycled()),
                         "the three photos are not in .qingjian-trash")
        for _ in chosen:
            if all(path.exists() for path in chosen):
                break
            self.window.undo()
            self.app.processEvents()
        self.assertEqual(contents, sorted(path.read_bytes() for path in chosen
                                          if path.exists()), "Ctrl+Z did not bring them back")
        self.assertEqual([], self.recycled())


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class GridTargetTests(WindowCase):
    """The grid acts on what is chosen in it, never on the hidden cursor."""

    def to_grid(self, chosen=()) -> None:
        from qingjian.core import config
        self.window._set_view(config.VIEW_GRID)
        self.app.processEvents()
        if chosen:
            self.window.grid.select_all_paths([str(path) for path in chosen])
            self.app.processEvents()

    def test_delete_in_the_grid_recycles_the_selected_file(self):
        queue = list(self.engine.queue_paths)
        self.to_grid([queue[5]])
        self.window.trash_current()
        self.app.processEvents()
        self.assertFalse(queue[5].exists(), "the selected file is still there")
        self.assertTrue(queue[0].exists(), "a file nobody chose was recycled")

    def test_deleting_many_at_once_recycles_every_one_of_them(self):
        """A closure that did not bind the loop variable files the last one twice.

        The queue is the shipped default, and on it the work runs a beat after
        the loop has moved on. Waiting on a real worker makes that a race: it
        often wins before the loop's second turn, and the bug slips through. So
        hold every job back until the loop has finished, then run them in order
        -- late binding then has nowhere to hide.
        """
        from types import SimpleNamespace
        self.settings.background_queue = True
        self.patch(self.window, "_report", lambda error: None)
        held = []

        def hold(label, work, meta):
            held.append(work)
            return SimpleNamespace(id=f"held-{len(held)}")

        self.patch(self.engine, "enqueue", hold)
        queue = list(self.engine.queue_paths)
        chosen = [queue[2], queue[5]]
        contents = sorted(path.read_bytes() for path in chosen)
        self.to_grid(chosen)
        self.window.trash_current()
        self.assertEqual(2, len(held), "one job per chosen file")
        for work in held:
            work(lambda message, percent: None, lambda: False)
        self.app.processEvents()
        self.assertEqual([False, False], [path.exists() for path in chosen])
        self.assertEqual([True, True], [queue[index].exists() for index in (0, 7)])
        # Recycling renames as it goes, so match on what the files hold: both
        # pictures must be in the bin, not the same one twice.
        self.assertEqual(contents, sorted(path.read_bytes() for path in self.recycled()))

    def test_rename_in_the_grid_offers_the_selected_name(self):
        from PySide6.QtWidgets import QInputDialog
        queue = list(self.engine.queue_paths)
        self.to_grid([queue[5]])
        seen = []

        def get_text(parent, title, label, text="", **kwargs):
            seen.append(text)
            return "", False

        self.patch(QInputDialog, "getText", get_text)
        self.window.rename_current()
        self.assertEqual([queue[5].name], seen)

    def test_renaming_many_at_once_says_so_and_touches_nothing(self):
        from PySide6.QtWidgets import QInputDialog
        from qingjian.core.i18n import tr
        queue = list(self.engine.queue_paths)
        self.to_grid([queue[2], queue[5]])
        seen = []

        def get_text(parent, title, label, text="", **kwargs):
            seen.append(text)
            return "", False

        self.patch(QInputDialog, "getText", get_text)
        self.window.rename_current()
        self.assertEqual([], seen)
        self.assertEqual(tr("status.rename_one_only"), self.window.status_label.text())

    def test_with_nothing_selected_the_grid_files_nothing(self):
        from qingjian.core.i18n import tr
        queue = list(self.engine.queue_paths)
        self.to_grid()
        self.window.grid.clearSelection()
        self.app.processEvents()
        self.assertEqual([], self.window._targets())
        self.window.classify_index(0)
        self.app.processEvents()
        self.assertTrue(all(path.exists() for path in queue))
        self.assertEqual(tr("status.nothing_chosen"), self.window.status_label.text())


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class GridSelectionTests(WindowCase):
    """What is chosen in the grid survives a view switch, and the count is honest."""

    def test_switching_views_keeps_the_chosen_files(self):
        from qingjian.core import config
        from qingjian.core.i18n import tr
        queue = list(self.engine.queue_paths)
        chosen = [queue[1], queue[3], queue[5]]
        self.window._set_view(config.VIEW_GRID)
        self.app.processEvents()
        self.window.grid.select_all_paths([str(path) for path in chosen])
        self.app.processEvents()
        self.window._toggle_view()
        self.window._toggle_view()
        self.app.processEvents()
        self.assertEqual(sorted(str(path) for path in chosen),
                         sorted(self.window.grid.selected_paths()))
        self.assertEqual(sorted(chosen), sorted(self.window._targets()))
        self.assertEqual(tr("side.bulk_subtitle", count=3), self.window.grid_selected.text())

    def test_filing_a_selection_leaves_the_count_honest(self):
        from qingjian.core import config
        queue = list(self.engine.queue_paths)
        self.window._set_view(config.VIEW_GRID)
        self.app.processEvents()
        self.window.grid.select_all_paths([str(queue[1]), str(queue[2])])
        self.app.processEvents()
        self.window.classify_index(0)
        self.app.processEvents()
        QTest.qWait(50)
        self.app.processEvents()
        self.assertEqual([], self.window.grid.selected_paths())
        self.assertFalse(self.window.grid_selected.isVisible(),
                         "the label still counts files that are gone")
        self.assertEqual([], self.window._targets())


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class GridColumnTests(QtCase):
    """84e83d2 made the grid re-layout in a loop and drop a column."""

    def build(self, width: int, height: int, count: int):
        from qingjian.ui.browsers import ThumbnailGrid
        from qingjian.ui.thumbs import ThumbnailCache
        cache = ThumbnailCache()
        self.addCleanup(cache.shutdown)
        view = ThumbnailGrid(cache)
        self.addCleanup(view.deleteLater)
        view.resize(width, height)
        view.show()
        view.set_paths([str(self.tmp / f"img{i:05d}.jpg") for i in range(count)])
        self.app.processEvents()
        return view

    def test_the_grid_settles_instead_of_relaying_out_forever(self):
        view = self.build(986, 640, 9)
        calls = []
        real = view._sync_grid

        def counting():
            calls.append(1)
            return real()

        view._sync_grid = counting
        self.addCleanup(lambda: setattr(view, "_sync_grid", real))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.app.processEvents()
        self.assertLessEqual(len(calls), 3, f"{len(calls)} re-layouts in a second")

    def test_a_short_grid_still_fills_the_row(self):
        from PySide6.QtWidgets import QStyle
        view = self.build(984, 900, 10)
        extent = view.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent,
                                          None, view.verticalScrollBar())
        expected = max(1, (view.maximumViewportSize().width() - extent - 1)
                       // (view.edge + 18))
        top = view.visualItemRect(view.item(0)).top()
        first_row = sum(1 for row in range(view.count())
                        if view.visualItemRect(view.item(row)).top() == top)
        self.assertEqual(expected, first_row, f"cell {view.gridSize().width()}")


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class GridProbeTests(QtCase):
    """A 4K window with the slider at its minimum shows more than 400 cells."""

    def test_a_big_grid_asks_for_everything_on_screen(self):
        from qingjian.ui.browsers import ThumbnailGrid
        from qingjian.ui.thumbs import ThumbnailCache
        cache = ThumbnailCache()
        self.addCleanup(cache.shutdown)
        view = ThumbnailGrid(cache)
        self.addCleanup(view.deleteLater)
        view.resize(3800, 1900)
        view.set_show_names(False)
        view.set_edge(96)
        view.show()
        view.set_paths([str(self.tmp / f"img{i:05d}.jpg") for i in range(1000)])
        self.app.processEvents()
        QTest.qWait(150)
        self.app.processEvents()
        cell = view.gridSize()
        port = view.viewport()
        columns = max(1, port.width() // cell.width())
        rows = port.height() // cell.height()
        on_screen = columns * rows
        start, end = view.visible_rows()
        self.assertGreaterEqual(end, on_screen - 1, f"visible_rows() = {(start, end)}")
        last = view.paths()[on_screen - 1]
        self.assertIn((last, view.edge), cache._wanted)
        self.assertGreaterEqual(view._tile_room, on_screen)


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class FilmstripDecorationTests(QtCase):
    def test_a_rebuilt_strip_keeps_the_stars_it_was_given(self):
        from qingjian.ui.browsers import Filmstrip
        from qingjian.ui.thumbs import ThumbnailCache
        cache = ThumbnailCache()
        self.addCleanup(cache.shutdown)
        strip = Filmstrip(cache)
        self.addCleanup(strip.deleteLater)
        paths = [str(self.tmp / f"img{i:04d}.jpg") for i in range(400)]
        marks = {path: {"rating": 3, "label": ""} for path in paths[::5]}
        strip.set_queue(paths, 0, marks)
        self.app.processEvents()
        self.assertEqual(80, len(strip._decorations))
        strip.follow(200)          # a jump, e.g. clicking far along the strip
        self.app.processEvents()
        self.assertEqual(80, len(strip._decorations), "the stars were dropped")


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class SelectAllCostTests(QtCase):
    """Twenty thousand items used to freeze the window for a minute."""

    def grid(self, count: int):
        from qingjian.ui.browsers import ThumbnailGrid
        from qingjian.ui.thumbs import ThumbnailCache
        cache = ThumbnailCache()
        self.addCleanup(cache.shutdown)
        view = ThumbnailGrid(cache)
        self.addCleanup(view.deleteLater)
        view.resize(1200, 800)
        view.set_paths([str(self.tmp / f"img{i:05d}.jpg") for i in range(count)])
        self.app.processEvents()
        return view

    def timed(self, action) -> float:
        started = time.perf_counter()
        action()
        self.app.processEvents()
        return time.perf_counter() - started

    def test_choosing_every_file_and_flipping_it_is_instant(self):
        view = self.grid(5000)
        every = view.paths()
        spent = self.timed(lambda: view.select_all_paths(every))
        self.assertEqual(5000, len(view.selectedItems()))
        self.assertLess(spent, 0.5, f"select all took {spent:.2f}s")
        spent = self.timed(view.invert_selection)
        self.assertEqual(0, len(view.selectedItems()))
        self.assertLess(spent, 0.5, f"invert took {spent:.2f}s")

    def test_flipping_an_alternating_choice_keeps_the_other_half(self):
        view = self.grid(100)
        view.select_all_paths(view.paths()[::2])
        view.invert_selection()
        self.assertEqual(list(range(1, 100, 2)),
                         sorted(view.row(item) for item in view.selectedItems()))


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class RecoverExitTests(WindowCase):
    """Recovery that cannot finish offers a way out; a clean start says nothing."""

    def silence_warnings(self) -> None:
        self.patch(QMessageBox, "warning",
                   staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))

    def answers(self, answer: str) -> list:
        from qingjian.ui.mainwindow import MainWindow
        asked: list = []

        def ask(_self, message, can_rollback):
            asked.append(can_rollback)
            return answer

        self.patch(MainWindow, "_ask_recover_exit", ask)
        return asked

    def stuck_journal(self):
        from qingjian.core.safestore import (Plan, SafeStore, TransactionError, identity,
                                             step_move)
        first, second = self.source / "IMG_0000.JPG", self.source / "IMG_0001.JPG"
        target = self.tmp / "keep"
        target.mkdir(exist_ok=True)
        plan = Plan(forward=[step_move(first, target / first.name, identity(first)),
                             step_move(second, target / second.name, identity(second))])
        plan.inverse = SafeStore.invert(plan.forward)

        def break_second(name, percent):
            if name == second.name and second.exists():
                second.write_bytes(b"changed while the plan was running")

        with self.assertRaises(TransactionError):
            self.engine.store.run(plan, progress=break_second)
        self.assertTrue(self.engine.has_pending())
        return first

    def test_a_damaged_journal_offers_an_exit_instead_of_crashing(self):
        self.silence_warnings()
        asked = self.answers("keep")
        self.engine.store.journal_path.write_text("{ not json", encoding="utf-8")
        self.window.recover_pending()
        self.app.processEvents()
        self.assertEqual([False], asked, "a damaged journal cannot be rolled back")
        self.assertFalse(self.engine.has_pending())

    def test_a_recovery_that_cannot_finish_offers_to_roll_back(self):
        self.silence_warnings()
        first = self.stuck_journal()
        asked = self.answers("rollback")
        self.window.recover_pending()
        self.app.processEvents()
        self.assertEqual([True], asked)
        self.assertTrue(first.is_file(), "the finished step was not rolled back")
        self.assertFalse(self.engine.has_pending())

    def test_trying_again_later_leaves_the_journal_and_the_files_alone(self):
        """"Later" is the escape: nothing is undone, nothing is cleared."""
        self.silence_warnings()
        first = self.stuck_journal()
        moved = self.tmp / "keep" / first.name
        asked = self.answers("")
        self.window.recover_pending()
        self.app.processEvents()
        self.assertEqual([True], asked)
        self.assertTrue(self.engine.has_pending(), "the pending operation was cleared anyway")
        self.assertFalse(first.exists(), "the finished step was rolled back without being asked")
        self.assertTrue(moved.is_file())

    def test_cancelling_the_rollback_keeps_the_journal_and_says_so(self):
        from qingjian.core.engine import Engine
        from qingjian.core.safestore import Cancelled
        self.silence_warnings()
        self.stuck_journal()
        self.answers("rollback")

        def cancelled(_self, rollback, progress=None):
            raise Cancelled("cancelled")

        self.patch(Engine, "abandon_pending", cancelled)
        self.window.recover_pending()
        self.app.processEvents()
        self.assertTrue(self.engine.has_pending(), "the journal went away on a cancel")

    def test_a_clean_start_asks_nothing(self):
        """The exit is for failures only; it must never look like a delete confirmation."""
        self.silence_warnings()
        asked = self.answers("keep")
        self.window.recover_pending()
        self.app.processEvents()
        self.assertEqual([], asked)


class BackgroundQueueTests(WindowCase):
    """The shipped default: keys file photographs on the queue thread.

    Every case here drives the real key, not the handler, because the chain
    from shortcut to queue to the row in the list is what broke in the field.
    """

    def setUp(self):
        super().setUp()
        self.settings.background_queue = True            # the shipped default
        self.settings.bindings[1].folder = str(self.tmp / "other")
        self.reported: list = []
        self.patch(self.window, "_report", self.reported.append)
        # A queue failure pops a modal box; offscreen it would never close.
        self.patch(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)
        self.patch(QMessageBox, "critical", lambda *a, **k: QMessageBox.StandardButton.Ok)
        self.activate()

    def settled(self, timeout: float = 20.0) -> bool:
        return self.wait_until(lambda: self.engine.queue.pending == 0, timeout)

    def press(self, key, modifier=Qt.KeyboardModifier.NoModifier) -> None:
        """Send a real key press and wait for the queue to go quiet again.

        The window is activated first: a press that arrives while another
        case's window still holds activation goes nowhere, and the queue is
        then trivially idle, so waiting on it proves nothing.
        """
        self.activate()
        QTest.keyClick(self.window, key, modifier)
        self.assertTrue(self.settled(), "the queue never went idle")

    def slow_classify(self, seconds: float = 15.0):
        """Hold the next queued classify open, so the queue is really busy.

        The gate is opened by whoever waits for the queue (`_drain_queue`),
        not by a timer: the point is that the wait happens at all.
        """
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        real_drain = self.window._drain_queue

        def draining():
            gate.set()
            return real_drain()

        self.patch(self.window, "_drain_queue", draining)
        real = self.engine.classify

        def slow(*args, **kwargs):
            gate.wait(seconds)
            return real(*args, **kwargs)

        self.patch(self.engine, "classify", slow)
        return gate

    def test_a_binding_key_files_the_photo_through_the_queue(self):
        current = self.engine.current_path()
        before = len(self.engine.queue_paths)
        self.press(Qt.Key.Key_1)
        self.until(lambda: (self.keep / current.name).exists(), "not in the key's folder")
        self.assertFalse(current.exists())
        self.until(lambda: len(self.engine.queue_paths) == before - 1,
                   f"the list still has {len(self.engine.queue_paths)} rows")

    def test_a_failed_move_goes_back_to_its_row(self):
        from qingjian.core.safestore import TransactionError
        self.engine.go_to(self.engine.queue_paths[3])
        path = self.engine.current_path()
        row = self.engine.queue_paths.index(path)

        def refuse(*args, **kwargs):
            raise TransactionError("error.external_change", "injected")

        self.patch(self.engine, "classify", refuse)
        self.window.classify_index(0)
        self.assertTrue(self.settled())
        self.until(lambda: path in self.engine.queue_paths, "the item vanished from the list")
        self.assertEqual(row, self.engine.queue_paths.index(path), "put back on the wrong row")
        self.assertTrue(path.exists())

    def test_undo_waits_for_the_move_still_in_the_queue(self):
        """A plan is only valid for the filesystem it was built against.

        What matters is the state when undo begins: the queue must be empty by
        then, not "empty a moment later".
        """
        first = self.engine.current_path()
        pending: list[int] = []
        real_undo = self.engine.undo

        def undoing(*args, **kwargs):
            pending.append(self.engine.queue.pending)
            return real_undo(*args, **kwargs)

        self.patch(self.engine, "undo", undoing)
        self.slow_classify()            # opens only once something waits for the queue
        self.window.classify_index(0)
        self.window.undo()
        self.assertTrue(self.settled())
        self.assertEqual([0], pending, "undo began while a move was still queued")
        self.until(lambda: first.exists(), "undo did not put the queued move back")
        self.assertFalse((self.keep / first.name).exists())

    def test_delete_then_ctrl_z_and_ctrl_y_through_the_keys(self):
        current = self.engine.current_path()
        content = current.read_bytes()
        self.press(Qt.Key.Key_Delete)
        self.until(lambda: not current.exists(), "Delete did not recycle it")
        self.assertEqual([content], [p.read_bytes() for p in self.recycled()])
        self.press(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.until(lambda: current.exists(), "Ctrl+Z did not bring it back")
        self.assertEqual(content, current.read_bytes())
        self.assertEqual([], self.recycled())
        self.press(Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        self.until(lambda: not current.exists(), "Ctrl+Y did not recycle it again")

    def test_f2_renames_and_ctrl_z_puts_the_old_name_back(self):
        from unittest import mock
        from PySide6.QtWidgets import QInputDialog
        current = self.engine.current_path()
        content = current.read_bytes()
        target = current.with_name("renamed.JPG")
        with mock.patch.object(QInputDialog, "getText", return_value=("renamed.JPG", True)):
            self.press(Qt.Key.Key_F2)
            self.until(lambda: target.exists(), "the file was not renamed")
        self.assertEqual(content, target.read_bytes())
        self.assertFalse(current.exists())
        self.press(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.until(lambda: current.exists(), "the old name did not come back")
        self.assertFalse(target.exists())

    def conflict(self, decision):
        from unittest import mock
        from qingjian.ui import mainwindow
        patcher = mock.patch.object(mainwindow.ConflictDialog, "ask",
                                    return_value=(decision, False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_conflict_answered_with_keep_both_keeps_both(self):
        from qingjian.core import ops
        current = self.engine.current_path()
        content = current.read_bytes()
        older = self.write(self.keep / current.name, b"OLDER FILE")
        self.conflict(ops.CONFLICT_SEQUENCE)
        self.press(Qt.Key.Key_1)
        self.until(lambda: not current.exists(), "the photo was never filed")
        self.assertEqual(b"OLDER FILE", older.read_bytes(), "the existing file was replaced")
        landed = [p for p in self.keep.iterdir() if p.read_bytes() == content]
        self.assertEqual(1, len(landed), sorted(p.name for p in self.keep.iterdir()))

    def test_a_conflict_answered_with_replace_can_be_undone(self):
        from qingjian.core import ops
        current = self.engine.current_path()
        content = current.read_bytes()
        older = self.write(self.keep / current.name, b"OLDER FILE")
        self.conflict(ops.CONFLICT_REPLACE)
        self.press(Qt.Key.Key_1)
        self.until(lambda: older.read_bytes() == content,
                   "the photo did not replace the old file")
        self.assertEqual([older], sorted(self.keep.iterdir()))
        self.press(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.until(lambda: older.read_bytes() == b"OLDER FILE", "the replaced file is gone")
        self.assertEqual(content, current.read_bytes())

    def test_closing_with_a_queued_move_drains_it_first(self):
        from unittest import mock
        current = self.engine.current_path()
        # The engine's own shutdown waits for the queue, so watch what the
        # window hands over: by then the queue must already be empty.
        pending: list[int] = []
        real_close = self.engine.close

        def closing():
            pending.append(self.engine.queue.pending)
            return real_close()

        self.patch(self.engine, "close", closing)
        self.slow_classify()            # opens only once closing waits for the queue
        self.window.classify_index(0)
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            self.window.close()
        self.assertEqual([0], pending, "the window closed over a queued move")
        self.assertTrue((self.keep / current.name).exists())


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class DuplicateReviewTests(WindowCase):
    """Duplicates, then review, then recycling: the whole chain in one case."""

    def setUp(self):
        super().setUp()
        from PIL import Image
        for index in (1, 2):
            # Noise, because the exact scan ignores anything under 8 KB.
            original = self.source / f"DUP_{index}.JPG"
            Image.effect_noise((640, 480), 60).convert("RGB").save(original, quality=95)
            self.write(self.source / f"DUP_{index}_copy.JPG", original.read_bytes())
        self.groups: list = []
        self.window.open_folder(self.source)
        self.app.processEvents()

    def scan_and_send(self, dialog):
        from qingjian.core import dedupe
        self.assertTrue(self.wait_until(lambda: dedupe.MODE_EXACT in dialog._results, 30),
                        "the exact scan never finished")
        self.groups = list(dialog._results[dedupe.MODE_EXACT])
        dialog._extras_to_review()
        return QDialog.DialogCode.Accepted

    def test_the_extras_go_to_review_and_recycling_spares_the_keeper(self):
        self.patch(self.window, "_run_dialog", self.scan_and_send)
        self.window.open_duplicates()
        self.app.processEvents()
        self.assertEqual(2, len(self.groups))
        extras = sorted(str(member.path) for group in self.groups for member in group.extras)
        keepers = sorted(str(group.keep().path) for group in self.groups)
        self.assertEqual(2, len(extras))
        queued = sorted(self.engine.state.review_queue(str(self.engine.source_root)))
        self.assertEqual(extras, queued, "the review queue is not exactly the extras")
        for keeper in keepers:
            self.assertNotIn(keeper, queued, "a keeper was sent to review")
        self.window.toggle_review()
        self.app.processEvents()
        self.assertTrue(self.engine.review_mode)
        self.assertEqual(extras, sorted(str(path) for path in self.engine.queue_paths))
        content = [Path(path).read_bytes() for path in extras]
        for _ in extras:
            self.window.trash_current()
            self.app.processEvents()
        self.assertEqual([], [path for path in extras if Path(path).exists()])
        self.assertEqual(sorted(content), sorted(p.read_bytes() for p in self.recycled()))
        for keeper in keepers:
            self.assertTrue(Path(keeper).exists(), "a keeper was recycled")
        for _ in extras:
            self.window.undo()
            self.app.processEvents()
        self.assertEqual(content, [Path(path).read_bytes() for path in extras],
                         "Ctrl+Z did not restore the reviewed files")
        self.assertEqual([], self.recycled())


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class BindingCardTests(QtCase):
    def setUp(self):
        super().setUp()
        from qingjian.ui.widgets import BindingCard
        self.card = BindingCard(3)
        self.addCleanup(self.card.deleteLater)
        self.acted: list[int] = []
        self.asked: list[int] = []
        self.card.activated.connect(self.acted.append)
        self.card.folder_requested.connect(self.asked.append)

    def mouse(self, kind, buttons) -> None:
        point = QPointF(10, 10)
        QApplication.sendEvent(self.card, QMouseEvent(kind, point, point,
                                                      Qt.MouseButton.LeftButton, buttons,
                                                      Qt.KeyboardModifier.NoModifier))

    def test_a_double_click_acts_once_and_never_opens_the_folder_picker(self):
        """Qt delivers press, release, press, double-click, release.

        Both releases used to classify, so double-clicking a card to change its
        folder filed two photographs before the folder picker even opened.
        """
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)
        self.mouse(QEvent.Type.MouseButtonDblClick, Qt.MouseButton.LeftButton)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.assertEqual([3], self.acted)
        self.assertEqual([], self.asked)

    def test_a_right_click_asks_for_a_folder_and_files_nothing(self):
        QApplication.sendEvent(self.card, QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                                            QPoint(10, 10)))
        self.assertEqual([], self.acted)
        self.assertEqual([3], self.asked)


class ReservedKeyTests(WindowCase):
    def test_the_key_editor_refuses_a_key_the_window_already_uses(self):
        from qingjian.core import config
        from qingjian.ui.editors import BindingsDialog
        warned = []
        self.patch(QMessageBox, "warning", lambda *args, **kwargs: warned.append(args[2]))
        bindings = [config.Binding(key=key) for key in config.DEFAULT_KEYS]
        bindings[1].key = "G"
        dialog = BindingsDialog(bindings, [], self.window, reserved=self.window.reserved_keys())
        self.addCleanup(dialog.deleteLater)
        dialog._accept()
        self.assertNotEqual(QDialog.DialogCode.Accepted, dialog.result())
        self.assertEqual(1, len(warned))
        self.assertIn("G", warned[0])

    def test_a_saved_clash_warns_and_the_window_key_keeps_working(self):
        from qingjian.core import config
        self.settings.bindings[1].key = "G"
        self.settings.bindings[1].folder = str(self.tmp / "other")
        self.window._install_binding_shortcuts()
        self.assertIn("G", self.window.status_label.text())
        self.activate()
        QTest.keyClick(self.window, Qt.Key.Key_G)
        self.app.processEvents()
        self.assertEqual(config.VIEW_GRID, self.window.view_mode)
        self.assertEqual([], list((self.tmp / "other").glob("*")))


class SidecarPromptTests(WindowCase):
    def record_prompt(self, prompt: str) -> list[bool]:
        from qingjian.ui import mainwindow
        from qingjian.ui.dialogs import SidecarDialog
        seen: list[bool] = []

        class Recording(SidecarDialog):
            def exec(self):
                seen.append(self.remembered())
                return QDialog.DialogCode.Rejected

        self.patch(mainwindow, "SidecarDialog", Recording)
        self.settings.sidecar = self.settings.sidecar.with_prompt(prompt)
        current = self.engine.current_path()
        self.write(current.with_suffix(".CR2"), b"raw")
        self.window.classify_index(0)
        self.assertTrue(current.exists())
        return seen

    def test_ask_each_time_leaves_remember_unticked(self):
        """Ticked by default, the first answer quietly switched asking off."""
        from qingjian.core.sidecar import PROMPT_EACH
        self.assertEqual([False], self.record_prompt(PROMPT_EACH))

    def test_ask_once_still_offers_to_remember(self):
        from qingjian.core.sidecar import PROMPT_ONCE
        self.assertEqual([True], self.record_prompt(PROMPT_ONCE))


class HandledFilesTests(WindowCase):
    def test_copied_files_can_be_brought_back_from_the_status_bar(self):
        from qingjian.core import config
        self.settings.bindings[1] = config.Binding(key="2", action="copy",
                                                   folder=str(self.tmp / "copies"))
        self.window._install_binding_shortcuts()
        current = self.engine.current_path()
        self.window.classify_index(1)
        self.app.processEvents()
        self.assertNotIn(current, self.engine.queue_paths)
        self.assertTrue(self.window.handled_button.isVisible())
        self.window.handled_button.click()
        self.app.processEvents()
        self.assertIn(current, self.engine.queue_paths)
        self.assertFalse(self.window.handled_button.isVisible())


class GridSliderTests(WindowCase):
    def test_dragging_the_size_slider_rebuilds_the_grid_once(self):
        """Every tick rebuilt every row: 69 ms a tick with five thousand items."""
        edges: list[int] = []
        self.patch(self.window.grid, "set_edge", edges.append)
        for value in range(170, 230, 5):
            self.window.grid_size.setValue(value)
        self.assertEqual([], edges, "the grid was rebuilt while the slider was still moving")
        self.assertTrue(self.wait_until(lambda: edges))
        QTest.qWait(100)
        self.assertEqual([225], edges)


class HoldArrowTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.activate()
        self.rendered: list[str] = []
        real = self.window.preview.show_path

        def counting(path, cached=None):
            self.rendered.append(Path(path).name)
            return real(path, cached)

        self.patch(self.window.preview, "show_path", counting)

    def right(self, press: bool = True, repeat: bool = False) -> None:
        QTest.simulateEvent(self.window, press, int(Qt.Key.Key_Right),
                            Qt.KeyboardModifier.NoModifier, "", repeat, -1)

    def test_holding_right_flips_through_without_decoding_each_picture(self):
        start = self.engine.index
        self.right()
        for _ in range(4):
            self.right(repeat=True)
        self.assertEqual(start + 5, self.engine.index)
        self.assertEqual(1, len(self.rendered), self.rendered)

    def test_letting_go_renders_the_picture_it_stopped_on(self):
        start = self.engine.index
        self.right()
        for _ in range(3):
            self.right(repeat=True)
        self.right(press=False)
        self.app.processEvents()
        self.assertEqual(start + 4, self.engine.index)
        self.assertEqual(self.engine.current_path().name, self.rendered[-1])

    def test_holding_in_the_grid_does_not_run_the_cursor_away(self):
        """The grid shows no cursor, so a held key would file a photo nobody saw."""
        from qingjian.core import config
        self.window._set_view(config.VIEW_GRID)
        start = self.engine.index
        self.right()
        for _ in range(4):
            self.right(repeat=True)
        self.right(press=False)
        self.assertEqual(start, self.engine.index)

    def hold(self, repeats: int = 3) -> None:
        """Press and keep holding: the release never arrives during the test.

        It is sent at cleanup, before the window closes, so the settle timer
        does not fire on a closed window (that is F-067, left for later).
        """
        self.addCleanup(self.right, False)
        self.right()
        for _ in range(repeats):
            self.right(repeat=True)

    def still(self):
        from PySide6.QtGui import QColor, QPixmap
        pixmap = QPixmap(64, 48)
        pixmap.fill(QColor(200, 30, 30))
        return pixmap

    def test_a_held_picture_shows_its_thumbnail_when_it_arrives(self):
        from qingjian.ui import mainwindow
        shown: list[str] = []
        real = self.window.preview.show_still

        def spy(path, pixmap):
            shown.append(Path(path).name)
            return real(path, pixmap)

        self.patch(self.window.preview, "show_still", spy)
        self.hold()
        current = self.engine.current_path()
        # Hold the settle back: it would render this very picture in full, and
        # the case would then pass without the arrival being shown at all.
        self.window._settle_timer.stop()
        drawn = len(shown)
        self.window.thumbs.ready.emit(str(current), mainwindow.HOLD_EDGE, self.still())
        # The decoder's own thumbnails keep arriving for the pictures the hold
        # went past, so wait for this one to be drawn rather than for it to be
        # the last thing drawn.
        self.until(lambda: current.name in shown[drawn:],
                   f"the arrival was not shown; drew {shown[drawn:]}")

    def test_a_late_thumbnail_does_not_replace_the_full_render(self):
        from qingjian.ui import mainwindow
        self.hold()
        early = list(self.window._flipped)[0]
        self.right(press=False)
        self.until(lambda: self.window.preview.current_path == self.engine.current_path(),
                   "letting go never rendered the picture it stopped on")
        self.window.thumbs.ready.emit(early, mainwindow.HOLD_EDGE, self.still())
        self.app.processEvents()
        self.assertEqual(self.engine.current_path(), self.window.preview.current_path)

    def test_a_lost_release_still_settles_on_the_picture(self):
        self.hold()
        name = self.engine.current_path().name
        self.until(lambda: self.rendered[-1] == name,
                   f"the timer never settled; still showing {self.rendered[-1]}, not {name}")

    def test_the_settle_waits_for_an_open_dialog(self):
        self.hold()
        # Only this case's own `timeout` may drive the settle, or the timer
        # could go off while the dialog is still on its way up.
        self.window._settle_timer.stop()
        drawn = len(self.rendered)
        dialog = QDialog(self.window)
        dialog.setModal(True)
        dialog.show()
        self.addCleanup(dialog.close)
        self.app.processEvents()
        self.window._settle_timer.timeout.emit()
        self.app.processEvents()
        self.assertEqual(drawn, len(self.rendered), "rendered in full behind a dialog")
        dialog.close()
        name = self.engine.current_path().name
        self.until(lambda: self.rendered[-1] == name, "never rendered after the dialog closed")


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class GridArrowTests(WindowCase):
    def test_an_arrow_in_the_grid_moves_the_grid_not_the_hidden_cursor(self):
        from qingjian.core import config
        self.activate()
        self.window._set_view(config.VIEW_GRID)
        self.app.processEvents()
        self.window.grid.setFocus()
        self.window.grid.setCurrentRow(0)
        self.app.processEvents()
        rendered: list[str] = []
        real = self.window.preview.show_path

        def counting(path, cached=None):
            rendered.append(Path(path).name)
            return real(path, cached)

        self.patch(self.window.preview, "show_path", counting)
        index = self.engine.index
        row = self.window.grid.currentRow()
        QTest.keyClick(self.window.grid, Qt.Key.Key_Right)
        self.app.processEvents()
        self.assertEqual(row + 1, self.window.grid.currentRow())
        self.assertEqual(index, self.engine.index, "the hidden cursor moved")
        self.assertEqual([], rendered, "the hidden page was rendered")


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class ViewSwitchRenderTests(WindowCase):
    def setUp(self):
        super().setUp()
        from PIL import Image
        first = Image.new("RGB", (240, 180), (200, 40, 40))
        second = Image.new("RGB", (240, 180), (40, 40, 200))
        self.clip = self.source / "MOVING.gif"
        first.save(self.clip, save_all=True, append_images=[second], duration=120, loop=0)
        # QMovie keeps the file open; let go before the temp directory is removed.
        self.addCleanup(self.let_go_of_the_gif)
        self.window.open_folder(self.source)
        self.app.processEvents()

    def let_go_of_the_gif(self) -> None:
        """Close the handle QMovie keeps open; that leak is F-026, not this group.

        `release()` only drops the reference, so on Windows the temp directory
        cannot be removed while any movie this case made is still alive.
        """
        import gc
        import shiboken6
        from PySide6.QtGui import QMovie
        self.window.preview.release()
        self.app.processEvents()
        for movie in [obj for obj in gc.get_objects() if isinstance(obj, QMovie)]:
            if shiboken6.isValid(movie):
                movie.stop()
                shiboken6.delete(movie)
        gc.collect()
        self.app.processEvents()

    def test_coming_back_to_the_single_view_draws_the_animation_again(self):
        self.assertTrue(self.engine.go_to(str(self.clip)))
        self.window._refresh_view()
        self.app.processEvents()
        self.assertIsNotNone(self.window.preview._movie, "the gif never played")
        self.window._toggle_view()
        self.window._toggle_view()
        self.app.processEvents()
        self.assertIsNotNone(self.window.preview._movie, "blank after coming back")

    def test_filing_from_the_grid_renders_nothing_behind_it(self):
        from qingjian.core import config
        queue = list(self.engine.queue_paths)
        self.assertTrue(self.engine.go_to(str(queue[2])))
        self.window._set_view(config.VIEW_GRID)
        self.app.processEvents()
        rendered: list[str] = []
        real = self.window.preview.show_path

        def counting(path, cached=None):
            rendered.append(Path(path).name)
            return real(path, cached)

        self.patch(self.window.preview, "show_path", counting)
        self.window.grid.select_all_paths([str(queue[1])])
        self.app.processEvents()
        self.window.classify_index(0)
        self.app.processEvents()
        self.assertEqual([], rendered, "the hidden page was rendered")


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class PreviewAudioTests(QtCase):
    """Opening the audio device costs one and a half seconds on some machines."""

    def setUp(self):
        super().setUp()
        from qingjian.ui import preview
        self.opened: list[int] = []
        real = preview.QAudioOutput

        def counting(*args):
            self.opened.append(1)
            return real(*args)

        self.patch(preview, "QAudioOutput", counting)
        self.pane = preview.MediaPreview()
        self.addCleanup(self.pane.deleteLater)

    def let_go(self, clip: Path) -> None:
        """The player closes its file asynchronously; wait so the folder can be removed."""
        self.pane.release()

        def removed() -> bool:
            try:
                clip.unlink(missing_ok=True)
            except PermissionError:
                return False
            return True

        self.wait_until(removed)

    def test_photos_never_open_the_audio_device(self):
        from PIL import Image
        photo = self.tmp / "a.jpg"
        Image.new("RGB", (64, 48)).save(photo)
        self.pane.show_path(photo)
        self.pane.toggle_mute()
        self.assertEqual([], self.opened)

    def test_the_first_video_opens_it_once_and_keeps_the_mute_setting(self):
        try:
            import av
            import numpy as np
        except ImportError:
            self.skipTest("PyAV is not installed")
        clip = self.tmp / "clip.mp4"
        container = av.open(str(clip), "w")
        stream = container.add_stream("mpeg4", rate=24)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for index in range(12):
            frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), index * 20, np.uint8),
                                               format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        self.pane.toggle_mute()
        self.pane.show_path(clip)
        self.pane.show_path(clip)
        self.addCleanup(self.let_go, clip)
        self.assertEqual([1], self.opened)
        self.assertTrue(self.pane.audio.isMuted())


@unittest.skipUnless(HAVE_QT, "PySide6 is not installed")
class SecondLaunchTests(QtCase):
    def test_a_second_launch_hands_its_folder_to_the_running_window(self):
        """The second launch is a separate process, as it is from Explorer.

        A thread cannot stand in for it: Qt's Windows pipes finish their reads
        through the owning thread's event dispatcher, which a bare thread lacks.
        """
        from qingjian.ui import app as app_module
        name = f"qingjian-test-{uuid.uuid4().hex}"
        received: list[str] = []
        bell = app_module.Doorbell(name)
        self.addCleanup(bell.close)
        bell.arrived.connect(received.append)
        script = self.write(self.tmp / "second_launch.py", (
            "import sys\n"
            "from PySide6.QtCore import QCoreApplication\n"
            "app = QCoreApplication([])\n"
            "from qingjian.ui.app import hand_over\n"
            "print(hand_over(sys.argv[1], sys.argv[2]))\n").encode("utf-8"))
        child = subprocess.Popen([sys.executable, str(script), name, r"D:\Photos\Trip"],
                                 stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.addCleanup(child.kill)
        self.assertTrue(self.wait_until(lambda: child.poll() is not None, timeout=30))
        self.assertEqual("True", child.stdout.read().strip())
        self.assertEqual([r"D:\Photos\Trip"], received)

    def test_with_nobody_listening_the_hand_over_fails(self):
        from qingjian.ui import app as app_module
        self.assertFalse(app_module.hand_over(f"qingjian-test-{uuid.uuid4().hex}", "",
                                              timeout_ms=200))


@unittest.skipUnless(sys.platform == "win32", "the folder menu is a Windows feature")
class FolderMenuSettingTests(WindowCase):
    def test_turning_the_switch_on_installs_the_folder_menu(self):
        from qingjian.core import platform_
        from qingjian.ui.editors import SettingsDialog
        calls = []
        self.patch(platform_, "folder_menu_installed", lambda registry=None: False)
        self.patch(platform_, "set_folder_menu",
                   lambda enabled, *args, **kwargs: calls.append(enabled))
        dialog = SettingsDialog(self.settings, self.engine, self.window)
        self.addCleanup(dialog.deleteLater)
        dialog._controls["folder_menu"].setChecked(True)
        dialog._save()
        self.assertEqual([True], calls)

    def test_saving_without_touching_the_switch_leaves_the_registry_alone(self):
        from qingjian.core import platform_
        from qingjian.ui.editors import SettingsDialog
        calls = []
        self.patch(platform_, "folder_menu_installed", lambda registry=None: True)
        self.patch(platform_, "set_folder_menu",
                   lambda enabled, *args, **kwargs: calls.append(enabled))
        dialog = SettingsDialog(self.settings, self.engine, self.window)
        self.addCleanup(dialog.deleteLater)
        dialog._save()
        self.assertEqual([], calls)


class BaggingTests(WindowCase):
    """The copy that drops into an envelope must never cover the next print."""

    def setUp(self):
        super().setUp()
        self.window._motion = True          # the machine's own setting must not decide this

    def test_a_later_filing_never_shows_the_last_print_at_full_size(self):
        """Setting a drop's end points on the stopped animation reported a final
        frame, and the copy appeared over the next print. It takes a different
        envelope from the last drop: moving the end point is what reports it."""
        from PySide6.QtCore import QAbstractAnimation
        self.window._drop(self.window._binding_cards[1], self.window._take_off())
        self.window._fall.setCurrentTime(self.window._fall.duration())
        self.assertEqual(QAbstractAnimation.State.Stopped, self.window._fall.state())

        self.window._drop(self.window._binding_cards[2], self.window._take_off())
        self.assertFalse(self.window._flyer.isVisible())

    def test_the_copy_appears_only_once_it_is_under_half_size(self):
        flight = self.window._take_off()
        self.window._drop(self.window._binding_cards[1], flight)
        fall = self.window._fall
        fall.pause()
        fall.setCurrentTime(1)
        self.assertFalse(self.window._flyer.isVisible())
        fall.setCurrentTime(fall.duration() // 3)
        self.assertTrue(self.window._flyer.isVisible())
        self.assertLess(self.window._flyer.width(), flight[1].width() / 2)


class NoAnimationTests(WindowCase):
    """With Windows' "show animations" turned off, nothing on the counter animates."""

    def setUp(self):
        from qingjian.core import platform_
        self.patch(platform_, "animations_enabled", lambda: False)
        super().setUp()

    def test_filing_stamps_the_envelope_without_animating(self):
        from PySide6.QtCore import QAbstractAnimation
        card = self.window._binding_cards[0]
        self.window.classify_index(0)
        self.app.processEvents()
        self.assertIsNone(self.window._flyer)
        self.assertEqual(QAbstractAnimation.State.Stopped, card._stamp_animation.state())
        self.assertGreater(card._stamp, 0)

    def test_hovering_an_envelope_lifts_it_without_animating(self):
        from PySide6.QtCore import QAbstractAnimation
        from PySide6.QtGui import QEnterEvent
        card = self.window._binding_cards[0]
        point = QPointF(5, 5)
        QApplication.sendEvent(card, QEnterEvent(point, point, point))
        self.assertEqual(QAbstractAnimation.State.Stopped, card._lift_animation.state())
        self.assertEqual(1.0, card._lift)


class NarrowWindowTests(WindowCase):
    """The smallest window keeps the words that matter; a wide one gives up nothing."""

    def setUp(self):
        super().setUp()
        from qingjian.core.i18n import get_language, set_language
        self.addCleanup(set_language, get_language())
        self.window._change_language("en")          # the longer language is the harder case

    def show_at(self, width: int, height: int) -> None:
        self.window.resize(width, height)
        QTest.qWait(50)

    def test_no_row_is_squeezed_below_its_own_minimum_in_the_smallest_window(self):
        """An explicit window minimum lets Qt overlap widgets instead of growing."""
        for language in ("en", "zh"):
            with self.subTest(language=language):
                self.window._change_language(language)
                self.show_at(1180, 740)
                central = self.window.centralWidget()
                self.assertLessEqual(central.layout().minimumSize().width(), central.width())

    def test_the_last_filing_stays_readable_in_the_smallest_window(self):
        """With the long "show hidden files again" button competing for the same row."""
        from qingjian.core.i18n import tr
        self.window.handled_button.setText(tr("status.hidden_handled", count=12))
        self.window.handled_button.setVisible(True)
        self.show_at(1180, 740)
        message = tr("status.done_action", action=tr("action.move"), name="IMG_0007.JPG")
        self.window.status(message, "success")
        QTest.qWait(20)
        label = self.window.status_label
        self.assertGreaterEqual(label.width(), label.fontMetrics().horizontalAdvance(message))

    def test_the_snapshot_bar_never_shows_without_its_figures(self):
        for width in (1180, 1540, 1920):
            with self.subTest(width=width):
                self.show_at(width, 900)
                self.assertEqual(self.window.quota_label.isVisible(),
                                 self.window.quota_bar.isVisible())

    def test_a_wide_window_gives_up_nothing(self):
        from qingjian.core.i18n import tr
        self.show_at(1920, 1030)
        window = self.window
        self.assertTrue(window.preset_label.isVisible())
        self.assertTrue(window.quota_label.isVisible())
        self.assertEqual(tr("side.search_targets"), window.search_edit.placeholderText())
        self.assertEqual(tr("header.choose_folder"), window.choose_button.text())
        self.assertEqual(tr("scan.recursive"), window.recursive_check.text())


class StampsFollowTheViewTests(WindowCase):
    def test_the_rating_stars_are_on_screen_in_either_view(self):
        from qingjian.core import config
        for mode in (config.VIEW_GRID, config.VIEW_SINGLE, config.VIEW_GRID):
            with self.subTest(view=mode):
                self.window._set_view(mode)
                self.app.processEvents()
                self.assertTrue(self.window.rating.isVisible())


class G03WindowTests(WindowCase):
    def test_absent_target_is_checked_once_before_classify(self):
        from qingjian.core import config
        source = self.source / "IMG_0000.JPG"
        binding = config.Binding("1", "copy", str(self.keep))
        target = self.keep / source.name
        original = Path.exists
        calls = 0
        def counted(path):
            nonlocal calls
            if path == target:
                calls += 1
            return original(path)
        with mock.patch.object(Path, "exists", counted), \
             mock.patch.object(self.engine.planner, "_same_path",
                               wraps=self.engine.planner._same_path) as same, \
             mock.patch.object(self.window, "_run_operation"):
            self.assertTrue(self.window._classify_one(binding, source))
        self.assertEqual(1, calls)
        same.assert_not_called()

    def test_same_source_avoids_conflict_dialog(self):
        from qingjian.core import config
        from qingjian.ui.mainwindow import ConflictDialog
        source = self.source / "IMG_0000.JPG"
        binding = config.Binding("1", "copy", str(self.source))
        with mock.patch.object(ConflictDialog, "ask") as ask, \
             mock.patch.object(self.window, "_run_operation") as run:
            self.assertFalse(self.window._classify_one(binding, source))
        ask.assert_not_called()
        run.assert_not_called()

    def test_rename_precheck_uses_completed_suffix(self):
        from PySide6.QtWidgets import QInputDialog
        from qingjian.core import ops
        from qingjian.ui.mainwindow import ConflictDialog
        self.engine.go_to(self.source / "IMG_0000.JPG")
        with mock.patch.object(QInputDialog, "getText", return_value=("IMG_0001", True)), \
             mock.patch.object(ConflictDialog, "ask", return_value=(ops.CONFLICT_CANCEL, False)) as ask:
            self.window.rename_current()
        ask.assert_called_once()

    def test_invalid_rename_reports_error(self):
        from PySide6.QtWidgets import QInputDialog
        self.engine.go_to(self.source / "IMG_0000.JPG")
        with mock.patch.object(QInputDialog, "getText", return_value=("a/b", True)), \
             mock.patch.object(self.window, "_report") as report:
            self.window.rename_current()
        report.assert_called_once()


class G03EditorTests(QtCase):
    def test_relative_binding_is_rejected(self):
        from qingjian.core import config
        from qingjian.ui.editors import BindingsDialog
        dialog = BindingsDialog([config.Binding("1", "move", "Keep")])
        self.addCleanup(dialog.close)
        with mock.patch.object(QMessageBox, "warning"):
            dialog._accept()
        self.assertNotEqual(QDialog.DialogCode.Accepted, dialog.result())

    @unittest.skipUnless(sys.platform == "win32", "Windows drive and root path syntax")
    def test_windows_non_absolute_bindings_are_rejected(self):
        from qingjian.core import config
        from qingjian.ui.editors import BindingsDialog
        for folder in ("Keep", "D:Keep", "\\Keep"):
            with self.subTest(folder=folder):
                dialog = BindingsDialog([config.Binding("1", "move", folder)])
                with mock.patch.object(QMessageBox, "warning"):
                    dialog._accept()
                self.assertNotEqual(QDialog.DialogCode.Accepted, dialog.result())
                dialog.close()

    def test_preview_uses_counter(self):
        from qingjian.core import config
        from qingjian.core.engine import Engine
        from qingjian.ui.editors import TemplateEditor
        source = self.tmp / "A.JPG"
        source.write_bytes(b"x")
        engine = Engine(self.data / "engine", config.Settings())
        self.addCleanup(engine.close)
        binding = config.Binding("1", "move", str(self.tmp / "out"),
                                 name_template="{seq:4}_{name}")
        engine.planner.sequence.take(binding, 3)
        context = engine.planner.context_for(source, binding, self.tmp, 1)
        editor = TemplateEditor(binding, [context], engine=engine)
        self.addCleanup(editor.close)
        self.assertIn("0004_", editor.preview.item(0).text())
        editor.seq_start.setValue(500)
        self.assertIn("0500_", editor.preview.item(0).text())
        editor.seq_start.setValue(1)
        self.assertIn("0004_", editor.preview.item(0).text())
        binding.sequence_start = 500
        editor2 = TemplateEditor(binding, [context], engine=engine)
        self.addCleanup(editor2.close)
        editor2.seq_start.setValue(1)
        self.assertIn("0004_", editor2.preview.item(0).text())


class OpenFolderFailureTests(WindowCase):
    def test_an_unexpected_scan_failure_is_reported(self):
        shown = []
        self.patch(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a)))

        def explode(folder, progress=None, cancel=None):
            raise ValueError("invalid literal for int() with base 10: '\u2460'")

        self.patch(self.engine, "open_folder", explode)
        self.window.open_folder(self.tmp / "other")
        self.app.processEvents()
        self.assertTrue(shown, "the failure was never shown")
        self.assertFalse(self.window._busy)
        self.assertEqual("error", self.window.status_label.property("tone"))


if __name__ == "__main__":
    unittest.main()
