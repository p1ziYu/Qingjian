import os
from datetime import datetime

from base import TempCase, unittest
from fixtures import build_library
from qingjian.core import config, dedupe, mediatypes, scanner
from qingjian.core.engine import Engine, human_size
from qingjian.core.opqueue import Job, OperationQueue
from qingjian.core.safestore import Cancelled, SafeStore
from qingjian.core.sidecar import SidecarRules
from qingjian.core.state import StateStore


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
        scanner.apply_filter(self.deep, scanner.FilterSpec(name_pattern="burst("))

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
            scanner.sort_paths(self.deep, mode, ratings={})

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

    def test_duplicate_modes(self):
        for mode in (dedupe.MODE_EXACT, dedupe.MODE_SIMILAR, dedupe.MODE_BURST):
            groups = self.engine.find_duplicates(mode)
            self.assertIsInstance(groups, list)
        self.assertEqual(1, len(self.engine.find_duplicates(dedupe.MODE_EXACT)))

    def test_ignoring_and_restoring_duplicates(self):
        groups = self.engine.find_duplicates(dedupe.MODE_EXACT)
        self.engine.ignore_duplicates(groups[0].extras)
        self.assertEqual([], self.engine.find_duplicates(dedupe.MODE_EXACT))
        self.engine.restore_ignored()
        self.assertEqual(1, len(self.engine.find_duplicates(dedupe.MODE_EXACT)))

    def test_ignoring_never_touches_a_file(self):
        groups = self.engine.find_duplicates(dedupe.MODE_EXACT)
        extra = groups[0].extras[0]
        before = extra.path.read_bytes()
        self.engine.ignore_duplicates([extra])
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


if __name__ == "__main__":
    unittest.main()
