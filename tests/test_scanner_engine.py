import os
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image

from base import TempCase, unittest
from fixtures import build_library
from qingjian.core import config, dedupe, mediatypes, scanner
from qingjian.core.engine import Engine, human_size
from qingjian.core.opqueue import Job, OperationQueue
from qingjian.core.safestore import Cancelled, SafeStore
from qingjian.core.sidecar import SidecarRules
from qingjian.core.state import StateStore


def photo(path: Path, shade: int = 30) -> Path:
    """A small real JPEG, so anything that reads its header finds one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), (shade, 20, 30)).save(path, quality=90)
    return path


class ScannerTests(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "lib"
        build_library(self.root)
        self.deep = scanner.scan(self.root, recursive=True)
        self.state = StateStore(self.data / "state.db")

    def tearDown(self):
        self.state.close()
        super().tearDown()

    def test_unusual_digits_sort_instead_of_raising(self):
        folder = self.tmp / "odd"
        paths = [self.write(folder / name, b"x" * 64)
                 for name in ("\u246010.jpg", "\u24602.jpg", "1\u00b23.jpg")]
        self.assertEqual(["\u24602.jpg", "\u246010.jpg"],
                         [p.name for p in scanner.sort_paths(paths[:2], "name")])
        for mode in scanner.SORTS:
            with self.subTest(mode=mode):
                scanner.sort_paths(paths, mode, ratings={})

    def test_recursive_and_flat_scans_differ(self):
        self.assertEqual(0, len(scanner.scan(self.root, recursive=False)))
        self.assertGreater(len(self.deep), 10)

    def test_target_folders_are_skipped(self):
        excluded = scanner.scan(self.root, True, excluded=[self.root / "Backup"])
        self.assertNotIn("Backup", {p.parent.name for p in excluded})

    def test_non_media_is_ignored(self):
        self.assertNotIn("notes.txt", {p.name for p in self.deep})

    def test_partial_files_are_ignored(self):
        self.write(self.root / "Day3" / ".qingjian-abc-x.JPG.part", b"junk")
        self.assertNotIn(".qingjian-abc-x.JPG.part",
                         {p.name for p in scanner.scan(self.root, True)})

    def test_scanning_can_be_cancelled(self):
        with self.assertRaises(Cancelled):
            scanner.scan(self.root, True, cancel=lambda: True)

    def test_filters(self):
        expectations = {
            "images": lambda p: mediatypes.is_image(p),
            "videos": lambda p: mediatypes.is_video(p),
            "raw": lambda p: mediatypes.is_raw(p),
        }
        for mode, predicate in expectations.items():
            got = scanner.apply_filter(self.deep, scanner.FilterSpec(mode=mode))
            self.assertTrue(all(predicate(p) for p in got), mode)
            self.assertTrue(got, mode)

    def test_orientation_filters(self):
        portrait = scanner.apply_filter(self.deep, scanner.FilterSpec(mode="portrait"))
        self.assertEqual(["tall.JPG"], [p.name for p in portrait])

    def test_short_video_filter(self):
        short = scanner.apply_filter(self.deep, scanner.FilterSpec(mode="short",
                                                                   short_video_seconds=60))
        self.assertEqual(["MOV_001.MP4"], [p.name for p in short])

    def test_rating_filters(self):
        target = str(self.root / "Day4" / "solo_01.JPG")
        self.state.apply({"tags_set": [[target, 5, "green"]]})
        ratings = self.state.tags_for([str(p) for p in self.deep])
        rated = scanner.apply_filter(self.deep, scanner.FilterSpec(mode="rated", ratings=ratings))
        self.assertEqual(["solo_01.JPG"], [p.name for p in rated])
        unrated = scanner.apply_filter(self.deep,
                                       scanner.FilterSpec(mode="unrated", ratings=ratings))
        self.assertNotIn("solo_01.JPG", [p.name for p in unrated])

    def test_name_pattern(self):
        got = scanner.apply_filter(self.deep, scanner.FilterSpec(name_pattern="burst"))
        self.assertEqual(4, len(got))

    def test_an_unfinished_regex_does_not_raise(self):
        self.assertEqual(
            [], scanner.apply_filter(self.deep, scanner.FilterSpec(name_pattern="burst("))
        )

    def test_date_range(self):
        got = scanner.apply_filter(self.deep, scanner.FilterSpec(
            date_from=datetime(2026, 8, 14, 19, 0), date_to=datetime(2026, 8, 14, 19, 50)))
        self.assertIn("IMG_5511.JPG", [p.name for p in got])
        self.assertNotIn("solo_01.JPG", [p.name for p in got])

    def test_excluded_paths_are_dropped(self):
        first = self.deep[0]
        spec = scanner.FilterSpec(exclude={str(first)})
        self.assertNotIn(first, scanner.apply_filter(self.deep, spec))

    def test_natural_sort(self):
        names = ["a10.jpg", "a2.jpg", "a1.jpg"]
        paths = [self.root / n for n in names]
        self.assertEqual(["a1.jpg", "a2.jpg", "a10.jpg"],
                         [p.name for p in scanner.sort_paths(paths, "name")])

    def test_sorting_modes_do_not_raise(self):
        for mode in scanner.SORTS:
            with self.subTest(mode=mode):
                self.assertCountEqual(
                    self.deep, scanner.sort_paths(self.deep, mode, ratings={})
                )

    def test_reverse(self):
        forward = scanner.sort_paths(self.deep, "name")
        backward = scanner.sort_paths(self.deep, "name", reverse=True)
        self.assertEqual(forward, list(reversed(backward)))

    def test_equal_keys_fall_back_to_the_name(self):
        """Files copied in one go share a timestamp, and often a size.

        With no second key their order was whatever order the list arrived in,
        so undo put a file back somewhere a full sort would not have.
        """
        folder = self.tmp / "same"
        paths = [self.write(folder / name, b"same size")
                 for name in ("b10.jpg", "a.jpg", "b2.jpg")]
        for path in paths:
            os.utime(path, (1_700_000_000, 1_700_000_000))
        orders = {
            "modified": scanner.sort_paths(paths, "modified"),
            "size": scanner.sort_paths(paths, "size"),
            "date": scanner.sort_paths(paths, "date", capture_time=lambda p: 5.0),
        }
        for mode, ordered in orders.items():
            with self.subTest(mode=mode):
                self.assertEqual(["a.jpg", "b2.jpg", "b10.jpg"], [p.name for p in ordered])
        self.assertEqual(["b10.jpg", "b2.jpg", "a.jpg"],
                         [p.name for p in scanner.sort_paths(paths, "modified", reverse=True)])

    def test_random_is_reproducible_with_a_seed(self):
        first = scanner.sort_paths(self.deep, "random", seed=5)
        second = scanner.sort_paths(self.deep, "random", seed=5)
        self.assertEqual(first, second)

    def test_build_queue_collapses_companions(self):
        queue = scanner.build_queue(self.deep, scanner.FilterSpec(), SidecarRules())
        self.assertLess(len(queue), len(self.deep))
        self.assertNotIn("IMG_5511.CR2", [p.name for p in queue])


class QueueTests(TempCase):
    def test_jobs_run_in_order(self):
        order = []
        queue = OperationQueue()
        for index in range(5):
            queue.submit(Job(run=lambda p, c, i=index: order.append(i), label=str(index)))
        self.assertTrue(queue.wait_idle(10))
        queue.stop()
        self.assertEqual([0, 1, 2, 3, 4], order)

    def test_a_failure_is_kept_and_can_be_retried(self):
        attempts = []

        def flaky(progress, cancel):
            attempts.append(1)
            if len(attempts) < 2:
                raise OSError("first attempt fails")

        queue = OperationQueue()
        queue.submit(Job(run=flaky, label="flaky"))
        queue.wait_idle(10)
        self.assertEqual(1, len(queue.failures()))
        queue.retry_failures()
        queue.wait_idle(10)
        queue.stop()
        self.assertEqual([], queue.failures())

    def test_one_failure_does_not_stop_the_rest(self):
        done = []
        queue = OperationQueue()
        queue.submit(Job(run=lambda p, c: (_ for _ in ()).throw(OSError("x"))))
        for index in range(3):
            queue.submit(Job(run=lambda p, c, i=index: done.append(i)))
        queue.wait_idle(10)
        queue.stop()
        self.assertEqual([0, 1, 2], done)

    def test_events_are_reported(self):
        seen = []
        queue = OperationQueue(on_event=lambda event, job: seen.append(event))
        queue.submit(Job(run=lambda p, c: None))
        queue.wait_idle(10)
        queue.stop()
        self.assertIn("queued", seen)
        self.assertIn("finished", seen)

    def test_cancelled_is_classified_by_type_not_name(self):
        from qingjian.core.safestore import Cancelled
        class CancelledLookalike(Exception):
            pass
        CancelledLookalike.__name__ = "Cancelled"
        queue = OperationQueue()
        self.addCleanup(queue.stop)
        real = queue.submit(Job(run=lambda _p, _c: (_ for _ in ()).throw(Cancelled("x"))))
        fake = queue.submit(Job(run=lambda _p, _c: (_ for _ in ()).throw(CancelledLookalike("x"))))
        self.assertTrue(queue.wait_idle(10))
        self.assertEqual("cancelled", real.state)
        self.assertEqual("failed", fake.state)

    def test_stop_timeout_keeps_the_live_worker_and_restart_is_serial(self):
        import threading
        entered = threading.Event()
        release = threading.Event()
        ran_second = threading.Event()
        queue = OperationQueue()
        queue.submit(Job(run=lambda _p, _c: (entered.set(), release.wait(10))))
        self.assertTrue(entered.wait(10))
        worker = queue._thread
        queue.stop(0.001)
        self.assertIs(worker, queue._thread)
        self.assertTrue(worker.is_alive())
        queue.submit(Job(run=lambda _p, _c: ran_second.set()))
        self.assertIs(worker, queue._thread)
        release.set()
        worker.join(10)
        queue.start()
        self.assertTrue(ran_second.wait(10))
        self.assertTrue(queue.wait_idle(10))
        queue.stop()


class EngineTests(TempCase):
    def test_recycling_a_shot_larger_than_the_cap_stays_undoable(self):
        from qingjian.core.safestore import QuotaPolicy
        target = self.root / "Day4" / "solo_01.JPG"
        self.engine.settings.quota = QuotaPolicy(max_operations=0, max_bytes=1,
                                                 max_days=0, automatic=True)
        self.engine.trash(target)
        self.assertTrue(self.engine.can_undo())
        self.engine.undo()
        self.assertTrue(target.exists())

    def test_an_old_restore_copy_is_retired_on_the_next_tag(self):
        import time
        from qingjian.core.safestore import QuotaPolicy
        target = self.root / "Day4" / "solo_01.JPG"
        self.engine.settings.quota = QuotaPolicy(max_operations=200, max_bytes=20000,
                                                 max_days=30, automatic=True)
        record = self.engine.trash(target).record
        newer = self.engine.skip(self.root / "Day4" / "solo_02.JPG").record
        with self.engine.state._lock, self.engine.state._db:
            self.engine.state._db.execute("UPDATE records SET time_epoch=? WHERE id=?",
                                          (time.time() - 31 * 86400, record.id))
        self.engine.tag([target], rating=3)
        self.assertFalse(self.engine.state.record(record.id).undoable)
        self.assertTrue(self.engine.state.record(newer.id).undoable)

    def test_only_expired_record_keeps_last_undo_chance(self):
        import time
        from qingjian.core.safestore import QuotaPolicy
        target = self.root / "Day4" / "solo_01.JPG"
        self.engine.settings.quota = QuotaPolicy(max_operations=200, max_bytes=20000,
                                                 max_days=30, automatic=True)
        record = self.engine.trash(target).record
        with self.engine.state._lock, self.engine.state._db:
            self.engine.state._db.execute("UPDATE records SET time_epoch=? WHERE id=?",
                                          (time.time() - 31 * 86400, record.id))
        self.engine.tag([target], rating=3)
        self.assertTrue(self.engine.state.record(record.id).undoable)
        self.engine.undo()
        self.assertTrue(target.exists())

    def test_retired_history_does_not_open_the_reclaim_gate(self):
        from unittest.mock import patch
        from qingjian.core.safestore import QuotaPolicy
        target = self.root / "Day4" / "solo_01.JPG"
        self.engine.settings.quota = QuotaPolicy(max_operations=200, max_bytes=0,
                                                 max_days=0, automatic=False)
        for _ in range(250):
            self.engine.skip(target)
        ids = [r.id for r in self.engine.state.oldest_undoable(limit=5000)[:200]]
        self.engine.state.retire(ids)
        self.engine.settings.quota = QuotaPolicy(max_operations=200, max_bytes=0,
                                                 max_days=0, automatic=True)
        with patch.object(self.engine.state, "oldest_undoable",
                          wraps=self.engine.state.oldest_undoable) as query:
            self.engine.tag([target], rating=3)
        query.assert_not_called()

    def test_system_recycle_falls_back_where_the_volume_has_no_bin(self):
        from unittest.mock import Mock, patch
        from qingjian.core import platform_
        target = self.root / "Day4" / "solo_01.JPG"
        self.engine.settings.recycle_mode = config.RECYCLE_SYSTEM
        sender = Mock()
        with patch.object(platform_, "trash_available", return_value=True), \
             patch.object(platform_, "can_recycle", return_value=False, create=True), \
             patch.object(platform_, "move_to_trash", sender):
            self.engine.trash(target)
        sender.assert_not_called()
        self.assertTrue(self.engine.can_undo())
        self.assertFalse(target.exists())

    def test_system_recycle_leaves_no_hidden_folder_or_restore_copy(self):
        from pathlib import Path
        from unittest.mock import patch
        from qingjian.core import platform_
        target = self.root / "Day4" / "solo_01.JPG"
        self.engine.settings.recycle_mode = config.RECYCLE_SYSTEM
        with patch.object(platform_, "trash_available", return_value=True), \
             patch.object(platform_, "can_recycle", return_value=True, create=True), \
             patch.object(platform_, "move_to_trash", side_effect=lambda p: Path(p).unlink()):
            self.engine.trash(target)
        self.assertFalse((target.parent / ".qingjian-trash").exists())
        self.assertEqual(0, self.engine.store.usage(refresh=True))

    def test_system_recycle_partial_failure_records_sent_members(self):
        from pathlib import Path
        from unittest.mock import patch
        from qingjian.core import platform_
        jpg = self.root / "Day3" / "IMG_5511.JPG"
        self.engine.settings.recycle_mode = config.RECYCLE_SYSTEM
        sent = []
        def send(path):
            sent.append(Path(path))
            if len(sent) == 2:
                raise OSError("second failed")
            Path(path).unlink()
        with patch.object(platform_, "trash_available", return_value=True), \
             patch.object(platform_, "can_recycle", return_value=True, create=True), \
             patch.object(platform_, "move_to_trash", side_effect=send):
            with self.assertRaises(OSError) as failure:
                self.engine.trash(jpg)
        self.assertIn(str(sent[1]), str(failure.exception))
        import json
        rows = self.engine.state._db.execute(
            "SELECT payload FROM records WHERE action='trash'").fetchall()
        self.assertTrue(any(str(sent[0]) in json.loads(row[0])["paths"] for row in rows))

    def test_system_recycle_success_must_remove_source(self):
        from unittest.mock import Mock, patch
        from qingjian.core import platform_
        jpg = self.root / "Day4" / "solo_01.JPG"
        self.engine.settings.recycle_mode = config.RECYCLE_SYSTEM
        with patch.object(platform_, "trash_available", return_value=True), \
             patch.object(platform_, "can_recycle", return_value=True), \
             patch.object(platform_, "move_to_trash", Mock()):
            with self.assertRaises(OSError):
                self.engine.trash(jpg)
        self.assertTrue(jpg.exists())
        self.assertFalse(self.engine.state.records())

    def test_never_linking_recycles_only_the_master(self):
        from qingjian.core.sidecar import SidecarRules, PROMPT_NEVER
        folder = self.root / "Day3"
        jpg, raw = folder / "IMG_5511.JPG", folder / "IMG_5511.CR2"
        self.engine.settings.sidecar = SidecarRules(prompt=PROMPT_NEVER)
        self.engine.planner.settings = self.engine.settings
        self.engine.trash(jpg)
        self.assertTrue(raw.exists())
        self.engine.undo()
        self.assertTrue(jpg.exists())

    def test_never_linking_folder_actions_leave_companions(self):
        from qingjian.core.sidecar import SidecarRules, PROMPT_NEVER
        folder = self.root / "Day3"
        jpg, raw = folder / "IMG_5511.JPG", folder / "IMG_5511.CR2"
        self.engine.settings.sidecar = SidecarRules(prompt=PROMPT_NEVER)
        self.engine.planner.settings = self.engine.settings
        for action in ("move", "copy", "favorite"):
            binding = config.Binding(key="1", action=action,
                                     folder=str(self.tmp / action), name_template="{name}")
            result = self.engine.classify(binding, jpg)
            self.assertEqual([str(jpg)], result.record.payload["paths"])
            self.assertTrue(raw.exists())
            self.engine.undo()
            self.assertTrue(jpg.exists())

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "lib"
        build_library(self.root)
        settings = config.Settings()
        settings.recursive = True
        self.keep = self.tmp / "Keepers"
        settings.bindings[0].folder = str(self.keep)
        settings.bindings[0].name_template = "{name}"
        settings.bindings[1].action = "copy"
        settings.bindings[1].folder = str(self.tmp / "Deliver")
        settings.bindings[1].name_template = "{name}"
        self.engine = Engine(self.data, settings)
        self.engine.open_folder(self.root)

    def test_preview_uses_a_supplied_master_only_group_without_lookup(self):
        from unittest.mock import patch
        from qingjian.core.sidecar import KIND_MASTER, SidecarGroup, SidecarMember
        path = self.engine.current_path()
        group = SidecarGroup(path, [SidecarMember(path, KIND_MASTER, path.stat().st_size)])
        binding = self.engine.settings.bindings[0]
        expected = self.engine.planner.preview_target(group, binding, self.engine.source_root)
        with patch.object(self.engine, "group_for", side_effect=AssertionError("looked up")):
            self.assertEqual(expected, self.engine.preview_target(binding, path, group))

    def tearDown(self):
        self.engine.close()
        super().tearDown()

    def test_opening_builds_a_queue(self):
        self.assertGreater(len(self.engine.all_files), 10)
        self.assertGreater(len(self.engine.queue_paths), 0)
        self.assertLess(len(self.engine.queue_paths), len(self.engine.all_files))

    def test_opening_a_missing_folder_raises(self):
        with self.assertRaises(Exception):
            self.engine.open_folder(self.tmp / "nope")

    def test_a_cancelled_open_keeps_the_previous_folder(self):
        other = self.tmp / "other"
        self.write(other / "z.jpg", b"x" * 64)
        self.assertGreater(len(self.engine.queue_paths), 3)
        self.engine.index = 3
        shown = self.engine.current_path()
        with self.assertRaises(Cancelled):
            self.engine.open_folder(other, cancel=lambda: True)
        self.assertEqual(self.root.resolve(), self.engine.source_root)
        self.assertEqual(3, self.engine.index)
        self.assertEqual(shown, self.engine.current_path())
        self.assertEqual(str(self.root.resolve()), self.engine.settings.source_folder)
        self.assertFalse(self.engine.review_mode)

    def test_classify_move_and_undo(self):
        self.engine.go_to(self.root / "Day3" / "IMG_5511.JPG")
        outcome = self.engine.classify(self.engine.settings.bindings[0])
        self.assertEqual(4, len(outcome.record.paths))
        self.assertEqual(4, len(self.tree(self.keep)))
        self.engine.undo()
        self.assertEqual([], self.tree(self.keep))

    def test_copy_marks_the_original_handled_and_hides_it(self):
        source = self.root / "Day4" / "solo_01.JPG"
        self.engine.go_to(source)
        self.engine.classify(self.engine.settings.bindings[1])
        self.engine.rebuild_queue()
        self.assertNotIn(source, self.engine.queue_paths)

    def test_moving_and_undoing_read_no_file_content(self):
        """A rename has no copy to prove.

        Hashing the source before and during every move read the whole file
        twice for a directory-entry change: half a second for a 300 MB video,
        and the same again on undo.
        """
        self.assertEqual("full", self.engine.settings.verification)
        hashed = self.count_hashes()
        self.engine.go_to(self.root / "Day4" / "solo_01.JPG")
        self.engine.classify(self.engine.settings.bindings[0])
        self.engine.undo()
        self.assertTrue((self.root / "Day4" / "solo_01.JPG").exists())
        self.assertEqual([], hashed)

    def test_copying_hashes_only_the_copy(self):
        """Full verification still proves the copy byte for byte, once."""
        hashed = self.count_hashes()
        self.engine.go_to(self.root / "Day4" / "solo_01.JPG")
        self.engine.classify(self.engine.settings.bindings[1])
        self.assertTrue((self.tmp / "Deliver" / "solo_01.JPG").exists())
        self.assertEqual(1, len(hashed), hashed)
        self.assertTrue(hashed[0].endswith(".part"), hashed)

    def test_copied_files_can_be_shown_again(self):
        """Copying marks the original handled, which hid it for good."""
        source = self.root / "Day4" / "solo_01.JPG"
        self.engine.go_to(source)
        self.engine.classify(self.engine.settings.bindings[1])
        self.engine.rebuild_queue()
        self.assertEqual(1, self.engine.hidden_handled())
        self.engine.reveal_handled()
        self.assertIn(source, self.engine.queue_paths)
        self.assertEqual(0, self.engine.hidden_handled())

    def test_the_cursor_survives_a_rebuild(self):
        self.engine.go_to(self.root / "Day4" / "solo_02.JPG")
        current = self.engine.current_path()
        self.engine.rebuild_queue()
        self.assertEqual(current, self.engine.current_path())

    def test_stepping_wraps_around(self):
        self.engine.index = len(self.engine.queue_paths) - 1
        self.engine.step(1)
        self.assertEqual(0, self.engine.index)

    def test_review_mode(self):
        source = self.root / "Day4" / "solo_03.JPG"
        self.engine.go_to(source)
        self.engine.skip()
        self.engine.rebuild_queue()
        self.assertNotIn(source, self.engine.queue_paths)
        self.engine.set_review_mode(True)
        self.assertEqual([source], self.engine.queue_paths)
        self.engine.set_review_mode(False)

    def test_filters_change_the_queue(self):
        self.engine.settings.filter_mode = "videos"
        self.engine.rebuild_queue()
        self.assertTrue(all(mediatypes.is_video(p) for p in self.engine.queue_paths))
        self.engine.settings.filter_mode = "all"
        self.engine.rebuild_queue()

    def test_tagging(self):
        target = self.engine.current_path()
        self.engine.tag([target], rating=4, label="green")
        self.assertEqual((4, "green"), self.engine.state.tag(str(target)))

    def test_a_tag_follows_a_rename(self):
        target = self.root / "Day4" / "solo_01.JPG"
        self.engine.tag([target], rating=5)
        self.engine.rename("renamed.JPG", target)
        self.assertEqual((5, ""), self.engine.state.tag(str(target.with_name("renamed.JPG"))))

    def test_rename_tags_follow_undo_redo_and_restore_replaced_target(self):
        source = self.root / "Day4" / "solo_01.JPG"
        target = source.with_name("solo_02.JPG")
        self.engine.tag([source], rating=5, label="red")
        self.engine.tag([target], rating=4, label="green")
        self.engine.rename(target.name, source, resolver=lambda _a, _b: "replace")
        self.assertEqual((5, "red"), self.engine.state.tag(str(target)))
        undo = self.engine.undo()
        self.engine.state.apply(undo.delta)
        self.assertEqual((5, "red"), self.engine.state.tag(str(source)))
        self.assertEqual((4, "green"), self.engine.state.tag(str(target)))
        redo = self.engine.redo()
        self.engine.state.apply(redo.delta)
        self.assertEqual((0, ""), self.engine.state.tag(str(source)))
        self.assertEqual((5, "red"), self.engine.state.tag(str(target)))

    def test_undoing_review_classification_puts_item_back_in_queue(self):
        source = self.root / "Day4" / "solo_01.JPG"
        root = str(self.engine.source_root)
        self.engine.skip(source)
        self.engine.set_review_mode(True)
        self.engine.classify(self.engine.settings.bindings[0], source)
        self.engine.undo()
        self.assertIn(str(source), self.engine.state.review_queue(root))

    def test_skip_binding_is_inert_in_review_mode(self):
        source = self.root / "Day4" / "solo_01.JPG"
        root = str(self.engine.source_root)
        self.engine.skip(source)
        before = self.engine.state.counts()["history"]
        self.engine.set_review_mode(True)
        outcome = self.engine.classify(config.Binding(key="S", action="skip"), source)
        self.assertTrue(outcome.skipped)
        self.assertEqual(before, self.engine.state.counts()["history"])
        self.assertIn(str(source), self.engine.state.review_queue(root))

    def test_duplicate_modes(self):
        for mode in (dedupe.MODE_EXACT, dedupe.MODE_SIMILAR, dedupe.MODE_BURST):
            groups = self.engine.find_duplicates(mode)
            self.assertIsInstance(groups, list)
        self.assertEqual(1, len(self.engine.find_duplicates(dedupe.MODE_EXACT)))

    def test_ignoring_and_restoring_duplicates(self):
        groups = self.engine.find_duplicates(dedupe.MODE_EXACT)
        self.engine.ignore_duplicates(groups[0].extras, self.engine.source_root)
        self.assertEqual([], self.engine.find_duplicates(dedupe.MODE_EXACT))
        self.engine.restore_ignored()
        self.assertEqual(1, len(self.engine.find_duplicates(dedupe.MODE_EXACT)))

    def test_ignoring_never_touches_a_file(self):
        groups = self.engine.find_duplicates(dedupe.MODE_EXACT)
        extra = groups[0].extras[0]
        before = extra.path.read_bytes()
        self.engine.ignore_duplicates([extra], self.engine.source_root)
        self.assertEqual(before, extra.path.read_bytes())

    def test_recycling_renames_into_a_hidden_folder_beside_the_file(self):
        """No copy and no hash: a rename on the same disk, undone by renaming back."""
        target = self.root / "Day4" / "solo_01.JPG"
        content = target.read_bytes()
        hashed = self.count_hashes()
        self.engine.go_to(target)
        outcome = self.engine.trash()
        trash = self.root / "Day4" / ".qingjian-trash"
        self.assertFalse(target.exists())
        self.assertEqual(1, len(list(trash.iterdir())))
        self.assertEqual([], list(self.engine.store.snapshot_root.iterdir()),
                         "a restore copy was written")
        self.assertEqual([], hashed)
        self.engine.absorb(outcome.record)
        self.assertFalse(any(".qingjian-trash" in p.parts for p in self.engine.all_files))
        self.engine.undo()
        self.assertEqual(content, target.read_bytes())
        self.assertEqual([], list(trash.iterdir()))

    def test_recycled_files_count_as_restore_space_until_reclaimed(self):
        target = self.root / "Day4" / "solo_02.JPG"
        size = target.stat().st_size
        trash = self.root / "Day4" / ".qingjian-trash"
        self.engine.go_to(target)
        self.engine.trash()
        self.assertTrue(trash.is_dir())
        self.assertEqual(size, self.engine.backup_usage())
        self.assertEqual(size, SafeStore(self.data / "store").usage(), "lost after a restart")
        self.engine.settings.quota = self.engine.settings.quota.__class__(
            max_operations=0, max_bytes=1, max_days=0, automatic=False)
        freed, retired = self.engine.reclaim(force=True)
        self.assertEqual((size, 1), (freed, retired))
        self.assertEqual(0, self.engine.backup_usage())
        self.assertFalse(trash.exists(), "the emptied hidden folder was left behind")

    def test_recycling_falls_back_to_a_copy_where_the_folder_cannot_be_made(self):
        folder = self.root / "Day4"
        self.write(folder / ".qingjian-trash", b"a file where the folder would go")
        target = folder / "solo_03.JPG"
        content = target.read_bytes()
        self.engine.go_to(target)
        self.engine.trash()
        self.assertFalse(target.exists())
        self.engine.undo()
        self.assertEqual(content, target.read_bytes())

    def test_reclaim_retires_the_oldest_records(self):
        self.engine.settings.quota = self.engine.settings.quota.__class__(
            max_operations=0, max_bytes=1, max_days=0, automatic=False)
        self.engine.go_to(self.root / "Day3" / "IMG_5511.JPG")
        self.engine.trash()
        self.assertGreater(self.engine.backup_usage(), 0)
        freed, retired = self.engine.reclaim(force=True)
        self.assertGreater(freed, 0)
        self.assertEqual(1, retired)
        self.assertFalse(self.engine.can_undo())

    def test_clearing_backups(self):
        self.engine.go_to(self.root / "Day3" / "IMG_5511.JPG")
        self.engine.trash()
        self.engine.clear_backups()
        self.assertEqual(0, self.engine.backup_usage())
        self.assertFalse(self.engine.can_undo())

    def test_statistics_and_export(self):
        self.engine.go_to(self.root / "Day4" / "solo_01.JPG")
        self.engine.classify(self.engine.settings.bindings[0])
        rows = self.engine.statistics()
        self.assertTrue(any(row[0] for row in rows))
        out = self.engine.export_csv(self.tmp / "records.csv")
        text = out.read_text(encoding="utf-8-sig")
        self.assertIn("solo_01.JPG", text)

    def test_csv_export_defuses_formula_injection(self):
        tricky = self.root / "Day4" / "=cmd.JPG"
        (self.root / "Day4" / "solo_01.JPG").rename(tricky)
        self.engine.rescan()
        self.engine.go_to(tricky)
        self.engine.classify(self.engine.settings.bindings[0])
        text = self.engine.export_csv(self.tmp / "r.csv").read_text(encoding="utf-8-sig")
        self.assertNotIn(",=", text.replace(",='", ",X"))

    def test_info_rows(self):
        rows = self.engine.info_rows(self.root / "Day3" / "IMG_5511.JPG")
        labels = [row[0] for row in rows]
        self.assertTrue(any("Sony" in row[1] for row in rows))
        self.assertEqual(len(labels), len(rows))

    def test_diagnostic_bundle_has_no_media(self):
        import zipfile
        bundle = self.engine.diagnostic_bundle(self.tmp / "b.zip")
        names = zipfile.ZipFile(bundle).namelist()
        self.assertIn("environment.json", names)
        self.assertFalse(any(name.endswith(".JPG") for name in names))

    def test_recover_is_a_no_op_when_nothing_is_pending(self):
        self.assertFalse(self.engine.has_pending())
        self.assertFalse(self.engine.recover())

    def test_settings_changes_reach_the_store(self):
        settings = self.engine.settings
        settings.fast_path = False
        settings.verification = "fast"
        self.engine.apply_settings(settings)
        self.assertFalse(self.engine.store.fast_path)
        self.assertEqual("fast", self.engine.store.verify)

    def test_human_size(self):
        self.assertEqual("512 B", human_size(512))
        self.assertEqual("1.0 KB", human_size(1024))
        self.assertEqual("1.0 GB", human_size(1024 ** 3))


def _junction(link, target) -> bool:
    if os.name != "nt":
        return False
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True)
    return done.returncode == 0 and os.path.isjunction(link)


class JunctionTests(TempCase):
    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        self.real = self.src / "real"
        self.only = self.write(self.real / "only.jpg", bytes(range(256)) * 64)
        self.link = self.src / "link"
        if not _junction(self.link, self.real):
            self.skipTest("directory junctions unavailable")
        self.addCleanup(self.drop, self.link)

    def drop(self, link) -> None:
        try:
            os.rmdir(link)
        except OSError:
            pass

    def test_a_junction_is_not_walked_into(self):
        self.assertEqual([self.only], scanner.scan(self.src, recursive=True))

    def test_one_file_reached_twice_is_not_a_duplicate(self):
        self.assertEqual([], dedupe.find_exact([self.link / "only.jpg", self.only]))

    def test_a_self_referential_junction_still_finishes(self):
        loop = self.src / "loop"
        if not _junction(loop, self.src):
            self.skipTest("directory junctions unavailable")
        self.addCleanup(self.drop, loop)
        self.assertEqual([self.only], scanner.scan(self.src, recursive=True))

    def test_a_file_under_a_junction_does_not_belong(self):
        settings = config.Settings()
        settings.recursive = True
        engine = Engine(self.data, settings)
        self.addCleanup(engine.close)
        engine.open_folder(self.src)
        self.assertFalse(engine._belongs(self.link / "only.jpg", engine.source_root, []))


class PrunedTargetTests(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "lib"
        self.write(self.root / "sub" / "deep.jpg", b"x" * 64)
        self.write(self.root / "top.jpg", b"x" * 64)

    def names(self, excluded):
        return sorted(p.name for p in scanner.scan(self.root, True, excluded=excluded))

    def test_a_target_at_or_above_the_root_prunes_nothing(self):
        self.assertEqual(["deep.jpg", "top.jpg"], self.names([self.root]))
        self.assertEqual(["deep.jpg", "top.jpg"], self.names([self.root.parent]))

    def test_a_target_below_the_root_is_still_pruned(self):
        self.assertEqual(["top.jpg"], self.names([self.root / "sub"]))


class FileListTests(TempCase):
    """When absorb leaves the queue to a rebuild, the rebuild must see the change.

    Random order and the review queue have no insert point, so absorb answers
    None and the caller rebuilds -- from a file list absorb had not touched,
    so a file the record created or put back was nowhere to be found.
    """

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        for shade, name in enumerate("ABCD"):
            photo(self.src / f"{name}.JPG", 40 * shade)
        settings = config.Settings()
        settings.bindings[0].action = "move"
        settings.bindings[0].folder = str(self.tmp / "Out")
        settings.bindings[0].name_template = "{name}"
        self.binding = settings.bindings[0]
        self.engine = Engine(self.data, settings)
        self.addCleanup(self.engine.close)
        self.engine.open_folder(self.src)

    def names(self) -> list[str]:
        return sorted(path.name for path in self.engine.queue_paths)

    def test_a_rename_in_random_order_survives_the_rebuild(self):
        self.engine.settings.sort_mode = "random"
        self.engine.rebuild_queue()
        outcome = self.engine.rename("Zed.JPG", self.src / "B.JPG")
        self.assertIsNone(self.engine.absorb(outcome.record))
        self.engine.rebuild_queue()
        self.assertEqual(["A.JPG", "C.JPG", "D.JPG", "Zed.JPG"], self.names())

    def test_a_rename_in_random_order_can_go_to_the_end_instead(self):
        """The background queue would rather not reshuffle the whole list to
        place one file, so it asks for arrivals at the end; the rest stay put."""
        self.engine.settings.sort_mode = "random"
        self.engine.rebuild_queue()
        before = [path for path in self.engine.queue_paths if path.name != "B.JPG"]
        outcome = self.engine.rename("Zed.JPG", self.src / "B.JPG")
        change = self.engine.absorb(outcome.record, unordered=True)
        self.assertEqual([*before, self.src / "Zed.JPG"], self.engine.queue_paths)
        self.assertEqual([(3, self.src / "Zed.JPG")], change["added"])
        self.assertEqual([self.src / "B.JPG"], change["removed"])

    def test_an_undone_move_in_random_order_comes_back(self):
        """The background queue takes the row out first and patches only removals."""
        self.engine.settings.sort_mode = "random"
        self.engine.rebuild_queue()
        target = self.src / "C.JPG"
        self.engine.drop_from_queue(target)
        outcome = self.engine.classify(self.binding, target)
        self.assertIsNone(self.engine.absorb(outcome.record))
        for path in self.engine.touched_paths(outcome.record):
            if not path.exists():
                if path in self.engine.all_files:
                    self.engine.all_files.remove(path)
                self.engine.drop_from_queue(path)
        self.assertIsNone(self.engine.absorb(self.engine.undo().record))
        self.engine.rebuild_queue()
        self.assertEqual(["A.JPG", "B.JPG", "C.JPG", "D.JPG"], self.names())

    def test_an_undo_made_in_review_mode_is_seen_after_leaving_it(self):
        self.engine.absorb(self.engine.classify(self.binding, self.src / "C.JPG").record)
        self.engine.set_review_mode(True)
        self.assertIsNone(self.engine.absorb(self.engine.undo().record))
        self.engine.rebuild_queue()
        self.engine.set_review_mode(False)
        self.assertEqual(["A.JPG", "B.JPG", "C.JPG", "D.JPG"], self.names())

    def test_renaming_an_item_in_review_keeps_it_in_review(self):
        """A new name is not a decision about the photograph.

        The rename took the old name off the review list and put nothing in its
        place, so the file left the review queue and, being on no list, did not
        show in the main one either.
        """
        source, renamed = self.src / "A.JPG", self.src / "Renamed.JPG"
        root = str(self.engine.source_root)
        self.engine.skip(source)
        self.engine.set_review_mode(True)
        self.assertIsNone(self.engine.absorb(self.engine.rename(renamed.name, source).record))
        self.engine.rebuild_queue()
        self.assertEqual([renamed.name], self.names())
        self.engine.set_review_mode(False)
        self.assertEqual(["B.JPG", "C.JPG", "D.JPG"], self.names())
        self.engine.undo()
        self.assertEqual([str(source)], self.engine.state.review_queue(root))
        self.engine.redo()
        self.assertEqual([str(renamed)], self.engine.state.review_queue(root))


    def test_undoing_a_rename_that_replaced_a_file_in_review_keeps_both_there(self):
        """Undo took the new name off the review list, and that is the very name
        the replaced file comes back under: it left review for the main queue."""
        from qingjian.core import ops
        first, second = self.src / "A.JPG", self.src / "B.JPG"
        root = str(self.engine.source_root)
        self.engine.send_to_review([first, second], self.engine.source_root)
        self.engine.set_review_mode(True)
        self.engine.rename(second.name, first, resolver=lambda _a, _b: ops.CONFLICT_REPLACE)
        self.assertEqual([str(second)], self.engine.state.review_queue(root))
        self.engine.undo()
        self.assertEqual({str(first), str(second)}, set(self.engine.state.review_queue(root)))
        self.engine.redo()
        self.assertEqual([str(second)], self.engine.state.review_queue(root))


class SystemFolderTests(TempCase):
    """Opening a whole drive must not list what the system keeps on it."""

    JUNK = ("$RECYCLE.BIN/S-1-5-21-1/$RAB12CD.JPG", "$Recycle.Bin/$IAB12CD.JPG",
            "System Volume Information/c.jpg", ".Trashes/501/e.jpg",
            ".Trash-1000/files/f.jpg", "._a.jpg", "DCIM/._b.jpg")

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "drive"
        self.kept = [self.write(self.root / "a.jpg", b"x" * 64),
                     self.write(self.root / "DCIM" / "b.jpg", b"x" * 64)]
        for junk in self.JUNK:
            self.write(self.root / junk, b"x" * 64)

    def test_recycle_bins_and_volume_folders_are_not_walked(self):
        """The bin's $R copies of deleted photographs were listed as if still there."""
        self.assertEqual(sorted(self.kept), sorted(scanner.scan(self.root, recursive=True)))

    def test_a_folder_of_ones_own_called_recycled_is_still_walked(self):
        """"Recycled" for a craft project is a name a person gives a folder; the
        bins of old Windows and of a Linux desktop by that name sit at the root
        of a drive, and only there are they stepped over."""
        mine = [self.write(self.root / name / "p.jpg", b"x" * 64)
                for name in ("Recycled", "RECYCLER", ".Trash")]
        self.assertEqual(sorted(self.kept + mine), sorted(scanner.scan(self.root, recursive=True)))
        drive = Path(Path.cwd().anchor)
        for name in ("Recycled", "RECYCLER", ".Trash"):
            with self.subTest(name=name):
                self.assertTrue(scanner.system_folder(name, drive))
                self.assertFalse(scanner.system_folder(name, self.root))

    def test_a_hidden_system_folder_is_not_walked_on_windows(self):
        """Whatever its name, a folder Windows marks hidden and system is its own."""
        import contextlib
        from types import SimpleNamespace
        from unittest import mock
        self.write(self.root / "Config.Msi" / "g.jpg", b"x" * 64)
        real_scandir = os.scandir

        class Entry:
            def __init__(self, entry):
                self._entry = entry

            def __getattr__(self, name):
                return getattr(self._entry, name)

            def stat(self, follow_symlinks=True):
                hidden_system = 0x2 | 0x4 if self._entry.name == "Config.Msi" else 0
                return SimpleNamespace(st_file_attributes=hidden_system)

        def listing(path):
            return contextlib.nullcontext([Entry(entry) for entry in real_scandir(path)])

        with mock.patch.object(scanner, "IS_WINDOWS", True), \
                mock.patch.object(scanner.os, "scandir", listing):
            found = scanner.scan(self.root, recursive=True)
        self.assertEqual(sorted(self.kept), sorted(found))

    def test_absorbing_agrees_with_the_scan(self):
        settings = config.Settings()
        settings.recursive = True
        engine = Engine(self.data, settings)
        self.addCleanup(engine.close)
        engine.open_folder(self.root)
        root = engine.source_root
        for junk in self.JUNK:
            with self.subTest(path=junk):
                self.assertFalse(engine._belongs(root / junk, root, []))
        for kept in self.kept:
            self.assertTrue(engine._belongs(root / kept.relative_to(self.root), root, []))


class FarDateTests(TempCase):
    """SQLite integers are 64 bits; a nanosecond timestamp after 2262 is not."""

    def test_the_cache_keeps_such_a_row_in_memory(self):
        from qingjian.core.hashcache import HashCache
        cache = HashCache(self.data / "hashes.db")
        self.addCleanup(cache.close)
        for key in (("late.jpg", 10, 2**63 + 5), ("early.jpg", 10, -2**63 - 5)):
            with self.subTest(key=key):
                cache.put_by_key(key, captured=1.0)
                self.assertEqual(1.0, cache.get_by_key(key, "captured"))

    def test_one_odd_file_does_not_stop_a_folder_opening(self):
        """Sorting by date cached the capture time, and the write raised.

        Opening the folder failed outright, every time, for one file.
        """
        src = self.tmp / "src"
        for name in "ABC":
            photo(src / f"{name}.JPG")
        late = src / "B.JPG"
        try:
            os.utime(late, (9.3e9, 9.3e9))
        except (OverflowError, OSError, ValueError):
            self.skipTest("this filesystem cannot hold a date after 2262")
        if late.stat().st_mtime_ns < 2**63:
            self.skipTest("this filesystem clamps dates after 2262")
        settings = config.Settings()
        settings.sort_mode = "date"
        engine = Engine(self.data, settings)
        self.addCleanup(engine.close)
        engine.open_folder(src)
        self.assertEqual(3, len(engine.queue_paths))


class WorkerSettingTests(TempCase):
    def test_duplicate_scans_read_with_the_threads_settings_asks_for(self):
        """"Worker threads" was saved and never read: every scan used its own
        count, and a slow share could not be eased by turning it down."""
        from unittest import mock
        src = self.tmp / "src"
        for name in "AB":
            photo(src / f"{name}.JPG")
        (src / "C.JPG").write_bytes((src / "A.JPG").read_bytes())
        settings = config.Settings()
        settings.workers = 3
        engine = Engine(self.data, settings)
        self.addCleanup(engine.close)
        engine.open_folder(src)
        asked = []
        real = dedupe._mapped

        def recording(work, items, progress=None, cancel=None, workers=0, **kwargs):
            asked.append(workers)
            return real(work, items, progress or (lambda *_: None),
                        cancel or (lambda: False), workers, **kwargs)

        with mock.patch.object(dedupe, "_mapped", recording):
            for mode in (dedupe.MODE_EXACT, dedupe.MODE_SIMILAR, dedupe.MODE_BURST):
                engine.find_duplicates(mode)
        self.assertTrue(asked)
        self.assertEqual({3}, set(asked) - {2})     # the keeper scoring keeps its pair


class HashCacheSwitchTests(TempCase):
    def test_turning_the_hash_cache_off_or_on_takes_effect_at_once(self):
        """The switch was read only when the engine was made."""
        settings = config.Settings()
        engine = Engine(self.data, settings)
        self.addCleanup(engine.close)
        self.assertIsNotNone(engine.cache.path)
        off = config.Settings.from_dict(settings.to_dict())
        off.hash_cache = False
        engine.apply_settings(off)
        self.assertIsNone(engine.cache.path)
        on = config.Settings.from_dict(off.to_dict())
        on.hash_cache = True
        engine.apply_settings(on)
        self.assertEqual(self.data / "cache" / "hashes.db", engine.cache.path)


class LockedCaptureTimeTests(TempCase):
    def test_a_capture_time_read_from_a_locked_file_is_not_remembered(self):
        """A photo locked once while the folder was sorted kept its mtime as
        its capture date in the cache until the file next changed."""
        from unittest import mock
        from qingjian.core import metadata
        src = self.tmp / "src"
        path = src / "A.JPG"
        src.mkdir()
        exif = Image.Exif()
        exif.get_ifd(0x8769)[0x9003] = "2015:07:04 12:00:00"
        Image.new("RGB", (64, 64)).save(path, exif=exif)
        engine = Engine(self.data, config.Settings())
        self.addCleanup(engine.close)

        def locked(*_args, **_kwargs):
            raise PermissionError(13, "being used by another process")

        metadata.clear_cache()
        with mock.patch.object(Image, "open", locked):
            self.assertEqual(engine.warm_capture_times([path]), {})
            self.assertAlmostEqual(path.stat().st_mtime, engine.capture_time(path), places=3)
        self.assertIsNone(engine.cache.get(path, "captured"))
        metadata.clear_cache()
        taken = datetime(2015, 7, 4, 12).timestamp()
        self.assertEqual(taken, engine.capture_time(path))
        self.assertEqual(taken, engine.cache.get(path, "captured"))


class RescanTests(TempCase):
    def test_a_cancelled_rescan_leaves_the_lists_as_they_were(self):
        """Cancelling while the queue was rebuilt kept the new file list.

        The old queue then sat beside a file list it was not built from, and
        the next patch to either worked from the wrong one.
        """
        from unittest import mock
        src = self.tmp / "src"
        for name in ("A", "B"):
            photo(src / f"{name}.JPG")
        for name in ("X", "Y", "Z"):
            photo(src / "sub" / f"{name}.JPG")
        engine = Engine(self.data, config.Settings())
        self.addCleanup(engine.close)
        engine.open_folder(src)
        engine.index = 1
        files, queue = list(engine.all_files), list(engine.queue_paths)
        engine.settings.recursive = True
        scans = []
        real_scan = scanner.scan

        def scan(*args, **kwargs):
            scans.append(real_scan(*args, **kwargs))
            return scans[-1]

        with mock.patch.object(scanner, "scan", scan), self.assertRaises(Cancelled):
            engine.rescan(cancel=lambda: bool(scans))
        self.assertEqual(5, len(scans[0]), "the walk itself should have finished")
        self.assertEqual(files, engine.all_files)
        self.assertEqual(queue, engine.queue_paths)
        self.assertEqual(1, engine.index)


class ShotRowTests(TempCase):
    """A shot is one row: it leaves the queue as one and comes back as one.

    Patching the queue in place has to agree with a rebuild row for row, or the
    next rebuild moves the cursor onto something the user has already seen.
    """

    def build(self, name: str, rules: SidecarRules | None = None,
              action: str = "copy") -> Engine:
        base = self.tmp / name
        self.src, self.out = base / "src", base / "Out"
        self.jpeg, self.raw = photo(self.src / "IMG_1.JPG"), self.src / "IMG_1.CR2"
        self.write(self.raw, b"raw" * 100)
        self.write(self.src / "IMG_1.JPG.xmp", b"<x:xmpmeta/>")
        photo(self.src / "IMG_2.JPG", 90)
        settings = config.Settings()
        if rules is not None:
            settings.sidecar = rules
        self.binding = settings.bindings[0]
        self.binding.action = action
        self.binding.folder = str(self.out)
        self.binding.name_template = "{name}"
        engine = Engine(base / "data", settings)
        self.addCleanup(engine.close)
        engine.open_folder(self.src)
        return engine

    @staticmethod
    def rows(engine: Engine) -> list[str]:
        return [path.name for path in engine.queue_paths]

    def assert_a_rebuild_agrees(self, engine: Engine) -> None:
        patched = list(engine.queue_paths)
        engine.rebuild_queue()
        self.assertEqual(patched, engine.queue_paths)

    def test_a_copied_shot_does_not_come_back_as_its_raw(self):
        """Copying marked only the JPEG handled, so the raw became the shot's row.

        The next key press then filed the whole shot again, as IMG_1 (2).JPG
        and IMG_1 (2).CR2.
        """
        for action in ("copy", "favorite"):
            with self.subTest(action=action):
                engine = self.build(action, action=action)
                engine.absorb(engine.classify(self.binding, self.jpeg).record)
                self.assertEqual(["IMG_2.JPG"], self.rows(engine))
                self.assert_a_rebuild_agrees(engine)
                self.assertEqual(["IMG_1.CR2", "IMG_1.JPG", "IMG_1.JPG.xmp"],
                                 self.tree(self.out))
                # Two files of the shot could have been rows; the XMP never could.
                self.assertEqual(2, engine.hidden_handled())

    def test_undoing_the_copy_brings_back_one_row(self):
        engine = self.build("undo")
        engine.absorb(engine.classify(self.binding, self.jpeg).record)
        engine.absorb(engine.undo().record)
        self.assertEqual(["IMG_1.JPG", "IMG_2.JPG"], self.rows(engine))
        self.assertEqual(set(), engine.state.done_paths())
        self.assert_a_rebuild_agrees(engine)

    def test_a_skipped_shot_is_one_row_in_either_queue(self):
        """After a skip and any rebuild the raw stood for the shot in the main queue."""
        engine = self.build("skip")
        engine.absorb(engine.skip(self.jpeg).record)
        self.assertEqual(["IMG_2.JPG"], self.rows(engine))
        self.assert_a_rebuild_agrees(engine)
        engine.set_review_mode(True)
        self.assertEqual(["IMG_1.JPG"], self.rows(engine))
        # One shot is one entry, as the count beside the review button shows it.
        self.assertEqual(1, engine.state.review_count(str(engine.source_root)))

    def test_a_shot_marked_by_an_earlier_version_is_still_one_row(self):
        """Lists written before this fix name only the JPEG of a handled shot."""
        engine = self.build("earlier")
        engine.state.apply({"done_add": [str(self.jpeg)]})
        engine.rebuild_queue()
        self.assertEqual(["IMG_2.JPG"], self.rows(engine))

    def test_with_companions_listed_a_copied_raw_leaves_its_row_too(self):
        engine = self.build("listed", SidecarRules(hide_from_queue=False))
        self.assertEqual(["IMG_1.CR2", "IMG_1.JPG", "IMG_2.JPG"], self.rows(engine))
        engine.absorb(engine.classify(self.binding, self.jpeg).record)
        self.assertEqual(["IMG_2.JPG"], self.rows(engine))
        self.assert_a_rebuild_agrees(engine)

    def test_a_raw_the_copy_left_behind_keeps_a_row(self):
        """With linking set to never, the raw was not copied, so it is not handled."""
        engine = self.build("never", SidecarRules(prompt="never"))
        outcome = engine.classify(self.binding, self.jpeg)
        self.assertEqual([str(self.jpeg)], outcome.record.payload["paths"])
        engine.absorb(outcome.record)
        self.assertEqual(["IMG_1.CR2", "IMG_2.JPG"], self.rows(engine))
        self.assert_a_rebuild_agrees(engine)

    def test_an_undone_move_lists_the_raw_again_when_companions_are_shown(self):
        """A restored raw was folded into its JPEG even with hiding turned off."""
        engine = self.build("shown", SidecarRules(hide_from_queue=False, prompt="always"),
                            action="move")
        engine.absorb(engine.classify(self.binding, self.jpeg).record)
        self.assertEqual(["IMG_2.JPG"], self.rows(engine))
        engine.absorb(engine.undo().record)
        self.assertEqual(["IMG_1.CR2", "IMG_1.JPG", "IMG_2.JPG"], self.rows(engine))
        self.assert_a_rebuild_agrees(engine)

    def test_a_raw_a_move_left_behind_takes_the_shots_row(self):
        """The record names only what moved, so the raw that stayed was never re-tested.

        A rebuild listed it; the patched queue did not, until the next rebuild
        moved everything under the cursor.
        """
        from qingjian.core.sidecar import KIND_MASTER, SidecarGroup, SidecarMember
        for name, rules, alone in (("never", SidecarRules(prompt="never"), False),
                                   ("unticked", None, True)):
            with self.subTest(name=name):
                engine = self.build(name, rules, action="move")
                group = (SidecarGroup(self.jpeg, [SidecarMember(self.jpeg, KIND_MASTER, 1)])
                         if alone else None)
                engine.absorb(engine.classify(self.binding, self.jpeg, group=group).record)
                self.assertEqual(["IMG_1.CR2", "IMG_2.JPG"], self.rows(engine))
                self.assert_a_rebuild_agrees(engine)
                engine.absorb(engine.undo().record)
                self.assertEqual(["IMG_1.JPG", "IMG_2.JPG"], self.rows(engine))
                self.assert_a_rebuild_agrees(engine)
                engine.absorb(engine.redo().record)
                self.assertEqual(["IMG_1.CR2", "IMG_2.JPG"], self.rows(engine))
                self.assert_a_rebuild_agrees(engine)

    def test_the_raw_surfaces_when_the_row_was_taken_out_first(self):
        """The background queue takes the row out before the move runs."""
        engine = self.build("background", SidecarRules(prompt="never"), action="move")
        engine.drop_from_queue(self.jpeg)
        change = engine.absorb(engine.classify(self.binding, self.jpeg).record)
        self.assertEqual([self.raw], [path for _where, path in change["added"]])
        self.assertEqual(["IMG_1.CR2", "IMG_2.JPG"], self.rows(engine))
        self.assert_a_rebuild_agrees(engine)


class BelongsTests(TempCase):
    def setUp(self):
        super().setUp()
        self.lib = self.tmp / "lib"
        self.inbox = self.lib / "inbox"
        for name in ("a.jpg", "b.jpg"):
            self.write(self.inbox / name, b"x" * 64)
        settings = config.Settings()
        settings.recursive = True
        settings.bindings[0].action = "move"
        settings.bindings[0].folder = str(self.lib)
        self.engine = Engine(self.data, settings)
        self.engine.open_folder(self.inbox)
        self.addCleanup(self.engine.close)

    def test_an_undone_recycle_returns_to_the_queue(self):
        photo = self.inbox / "a.jpg"
        self.engine.absorb(self.engine.trash(photo).record)
        self.assertNotIn(photo, self.engine.queue_paths)
        change = self.engine.absorb(self.engine.undo().record)
        self.assertEqual([photo], [path for _where, path in change["added"]])
        self.assertIn(photo, self.engine.queue_paths)

    def test_a_favourite_target_equal_to_the_source_also_belongs(self):
        settings = self.engine.settings
        settings.bindings[0].action = "favorite"
        settings.bindings[0].folder = str(self.inbox)
        self.assertTrue(self.engine._belongs(
            self.inbox / "b.jpg", self.engine.source_root,
            scanner.pruned_targets(self.engine.source_root, settings.target_folders())))


if __name__ == "__main__":
    unittest.main()
