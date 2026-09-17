import os
import time
from pathlib import Path

from base import TempCase, unittest
from fixtures import build_library
from qingjian.core import config, ops
from qingjian.core.naming import NameError_
from qingjian.core.safestore import SafeStore, TransactionError
from qingjian.core.state import STACK_HISTORY, STACK_REDO, Record, StateStore, empty_delta


class StateTests(TempCase):
    def setUp(self):
        super().setUp()
        self.state = StateStore(self.data / "state.db")

    def tearDown(self):
        self.state.close()
        super().tearDown()

    def record(self, **kw):
        base = dict(id="r1", action="move", original="/a/1.jpg", destination="/b/1.jpg",
                    root="/a", payload={"snapshots": ["/s/1"], "paths": ["/a/1.jpg"]})
        base.update(kw)
        return Record(**base)

    def test_a_delta_is_applied_atomically(self):
        delta = empty_delta()
        delta["records_add"].append(self.record().to_dict())
        delta["reviews_add"].append(["/a", "/a/2.jpg"])
        delta["done_add"].append("/a/3.jpg")
        delta["tags_set"].append(["/a/1.jpg", 4, "green"])
        self.state.apply(delta)
        self.assertEqual(1, self.state.counts()["history"])
        self.assertEqual(["/a/2.jpg"], self.state.review_queue("/a"))
        self.assertEqual({"/a/3.jpg"}, self.state.done_paths())
        self.assertEqual((4, "green"), self.state.tag("/a/1.jpg"))

    def test_applying_the_same_delta_twice_is_harmless(self):
        """Crash recovery replays the journal, so this must not double up."""
        delta = empty_delta()
        delta["records_add"].append(self.record().to_dict())
        delta["reviews_add"].append(["/a", "/a/2.jpg"])
        self.state.apply(delta)
        self.state.apply(delta)
        self.assertEqual(1, self.state.counts()["history"])
        self.assertEqual(1, len(self.state.review_queue("/a")))

    def test_moving_a_record_between_stacks(self):
        self.state.apply({"records_add": [self.record().to_dict()]})
        self.state.apply({"records_move": [{"id": "r1", "to": STACK_REDO}]})
        self.assertIsNone(self.state.top(STACK_HISTORY))
        self.assertIsNotNone(self.state.top(STACK_REDO, undoable_only=False))

    def test_retiring_drops_the_snapshot_references(self):
        self.state.apply({"records_add": [self.record().to_dict()]})
        self.state.retire(["r1"])
        record = self.state.record("r1")
        self.assertFalse(record.undoable)
        self.assertEqual([], record.snapshots)

    def test_a_retired_record_is_not_offered_for_undo(self):
        self.state.apply({"records_add": [self.record().to_dict()]})
        self.state.retire(["r1"])
        self.assertIsNone(self.state.top(STACK_HISTORY))

    def test_clearing_reports_the_snapshots_to_delete(self):
        self.state.apply({"records_add": [self.record().to_dict()]})
        self.assertEqual(["/s/1"], self.state.clear_records())
        self.assertEqual(0, self.state.counts()["history"])

    def test_a_tag_follows_its_file(self):
        self.state.apply({"tags_set": [["/a/1.jpg", 5, "red"]]})
        self.state.rename_tag("/a/1.jpg", "/b/1.jpg")
        self.assertEqual((5, "red"), self.state.tag("/b/1.jpg"))
        self.assertEqual((0, ""), self.state.tag("/a/1.jpg"))

    def test_handled_files_are_counted_and_cleared_one_folder_at_a_time(self):
        root = os.path.join(str(self.tmp), "lib")
        inside = os.path.join(root, "a.jpg")
        deeper = os.path.join(root, "sub", "b.jpg")
        neighbour = os.path.join(str(self.tmp), "lib2", "c.jpg")   # shares the prefix "lib"
        self.state.apply({"done_add": [inside, deeper, neighbour]})
        self.assertEqual(1, self.state.done_under(root, recursive=False))
        self.assertEqual(2, self.state.done_under(root, recursive=True))
        self.assertEqual(1, self.state.clear_done_under(root, recursive=False))
        self.assertEqual({deeper, neighbour}, self.state.done_paths())

    def test_clearing_a_tag_removes_the_row(self):
        self.state.apply({"tags_set": [["/a/1.jpg", 5, "red"]]})
        self.state.apply({"tags_set": [["/a/1.jpg", 0, ""]]})
        self.assertEqual(0, self.state.counts()["tags"])

    def test_tags_for_handles_more_paths_than_sqlite_allows_variables(self):
        paths = [f"/a/{i}.jpg" for i in range(1200)]
        self.state.apply({"tags_set": [[p, 1, ""] for p in paths]})
        self.assertEqual(1200, len(self.state.tags_for(paths)))

    def test_insertion_cost_does_not_grow_with_history(self):
        bulk = {"records_add": [Record(id=f"x{i}", action="move",
                                       original=f"/a/{i}.jpg").to_dict()
                                for i in range(3000)]}
        self.state.apply(bulk)
        start = time.perf_counter()
        self.state.apply({"records_add": [Record(id="last", action="move",
                                                 original="/a/z.jpg").to_dict()]})
        self.assertLess(time.perf_counter() - start, 0.25)
        self.assertEqual(3001, self.state.counts()["history"])

    def test_the_ignore_list_is_per_source_folder(self):
        self.state.apply({"ignored_add": [["/a", "k1"], ["/b", "k2"]]})
        self.assertEqual({"k1"}, self.state.ignored_keys("/a"))
        self.state.apply({"ignored_clear_root": ["/a"]})
        self.assertEqual(set(), self.state.ignored_keys("/a"))
        self.assertEqual({"k2"}, self.state.ignored_keys("/b"))


class PlannerCase(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "lib"
        build_library(self.root)
        self.store = SafeStore(self.data / "store")
        self.state = StateStore(self.data / "state.db")
        self.settings = config.Settings()
        self.keep = self.tmp / "Keepers"
        self.settings.bindings[0].folder = str(self.keep)
        self.settings.bindings[0].name_template = "{name}"
        self.planner = ops.Planner(self.store, self.state, self.settings)

    def tearDown(self):
        self.state.close()
        super().tearDown()

    def commit(self, outcome):
        if outcome.plan is not None:
            self.store.run(outcome.plan, save_state=self.state.apply)
            self.planner.sequence.commit()
        elif outcome.delta:
            self.state.apply(outcome.delta)
        return outcome

    def binding(self, **kw):
        binding = self.settings.bindings[0]
        for key, value in kw.items():
            setattr(binding, key, value)
        return binding


class SidecarMoveTests(PlannerCase):
    def test_the_whole_shot_moves_together(self):
        master = self.root / "Day3" / "IMG_5511.JPG"
        group = self.planner.group_for(master)
        self.assertEqual(4, group.count)
        self.commit(self.planner.plan_folder_action("move", group, self.binding(),
                                                 source_root=self.root))
        self.assertEqual(["IMG_5511.CR2", "IMG_5511.JPG", "IMG_5511.JPG.xmp", "IMG_5511.XMP"],
                         self.tree(self.keep))
        for member in group.paths:
            self.assertFalse(member.exists(), member.name)

    def test_renaming_keeps_the_companions_matched(self):
        master = self.root / "Day3" / "IMG_5511.JPG"
        self.commit(self.planner.plan_folder_action(
            "move", self.planner.group_for(master),
            self.binding(path_template="{YYYY}/{YYYY-MM}",
                         name_template="{YYYY-MM-DD}_{seq:4}_{name}"),
            source_root=self.root))
        self.assertEqual(
            ["2026/2026-08/2026-08-14_0001_IMG_5511.CR2",
             "2026/2026-08/2026-08-14_0001_IMG_5511.JPG",
             "2026/2026-08/2026-08-14_0001_IMG_5511.JPG.xmp",
             "2026/2026-08/2026-08-14_0001_IMG_5511.XMP"],
            self.tree(self.keep))

    def test_undo_returns_every_member(self):
        master = self.root / "Day3" / "IMG_5511.JPG"
        group = self.planner.group_for(master)
        before = {p.name: p.read_bytes() for p in group.paths}
        self.commit(self.planner.plan_folder_action("move", group, self.binding(),
                                                 source_root=self.root))
        self.commit(self.planner.plan_undo(self.state.top(STACK_HISTORY)))
        self.assertEqual([], self.tree(self.keep))
        for path in group.paths:
            self.assertEqual(before[path.name], path.read_bytes())

    def test_redo_reapplies_the_whole_group(self):
        master = self.root / "Day3" / "IMG_5511.JPG"
        self.commit(self.planner.plan_folder_action("move", self.planner.group_for(master),
                                                 self.binding(), source_root=self.root))
        self.commit(self.planner.plan_undo(self.state.top(STACK_HISTORY)))
        self.commit(self.planner.plan_redo(self.state.top(STACK_REDO, undoable_only=False)))
        self.assertEqual(4, len(self.tree(self.keep)))

    def test_a_shot_with_no_companions_still_works(self):
        solo = self.root / "Day4" / "solo_01.JPG"
        self.commit(self.planner.plan_folder_action("move", self.planner.group_for(solo),
                                                 self.binding(), source_root=self.root))
        self.assertEqual(["solo_01.JPG"], self.tree(self.keep))

    def test_companions_are_left_alone_when_the_rule_is_off(self):
        self.settings.sidecar = self.settings.sidecar.__class__(enabled=False)
        master = self.root / "Day3" / "IMG_5511.JPG"
        self.commit(self.planner.plan_folder_action("move", self.planner.group_for(master),
                                                 self.binding(), source_root=self.root))
        self.assertEqual(["IMG_5511.JPG"], self.tree(self.keep))
        self.assertTrue((self.root / "Day3" / "IMG_5511.CR2").exists())


class ConflictTests(PlannerCase):
    def setUp(self):
        super().setUp()
        self.keep.mkdir(parents=True, exist_ok=True)
        self.source = self.root / "Day4" / "solo_01.JPG"

    def test_sequence_leaves_the_existing_file_untouched(self):
        self.write(self.keep / "solo_01.JPG", b"OLD")
        self.commit(self.planner.plan_folder_action(
            "move", self.planner.group_for(self.source), self.binding(),
            resolver=lambda a, b: ops.CONFLICT_SEQUENCE, source_root=self.root))
        self.assertEqual(b"OLD", (self.keep / "solo_01.JPG").read_bytes())
        self.assertTrue((self.keep / "solo_01 (2).JPG").exists())

    def test_replace_keeps_the_old_file_for_undo(self):
        self.write(self.keep / "solo_01.JPG", b"OLD")
        self.commit(self.planner.plan_folder_action(
            "move", self.planner.group_for(self.source), self.binding(),
            resolver=lambda a, b: ops.CONFLICT_REPLACE, source_root=self.root))
        self.assertNotEqual(b"OLD", (self.keep / "solo_01.JPG").read_bytes())
        self.commit(self.planner.plan_undo(self.state.top(STACK_HISTORY)))
        self.assertEqual(b"OLD", (self.keep / "solo_01.JPG").read_bytes())
        self.assertTrue(self.source.exists())

    def test_skip_does_nothing_at_all(self):
        self.write(self.keep / "solo_01.JPG", b"OLD")
        outcome = self.planner.plan_folder_action(
            "move", self.planner.group_for(self.source), self.binding(),
            resolver=lambda a, b: ops.CONFLICT_SKIP, source_root=self.root)
        self.assertTrue(outcome.skipped)
        self.assertIsNone(outcome.plan)
        self.assertTrue(self.source.exists())

    def test_cancel_rolls_back_the_sequence_counter(self):
        self.write(self.keep / "solo_01.JPG", b"OLD")
        before = self.planner.sequence.peek(self.binding())
        outcome = self.planner.plan_folder_action(
            "move", self.planner.group_for(self.source), self.binding(),
            resolver=lambda a, b: ops.CONFLICT_CANCEL, source_root=self.root)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(before, self.planner.sequence.peek(self.binding()))

    def test_moving_into_the_files_own_folder_is_refused(self):
        with self.assertRaises(TransactionError):
            self.planner.plan_folder_action(
                "move", self.planner.group_for(self.source),
                self.binding(folder=str(self.source.parent)), source_root=self.root)


class OtherActionTests(PlannerCase):
    def test_copy_leaves_the_original_and_marks_it_handled(self):
        source = self.root / "Day4" / "solo_02.JPG"
        binding = self.binding(action="copy", folder=str(self.tmp / "Deliver"))
        self.commit(self.planner.plan_folder_action("copy", self.planner.group_for(source),
                                                 binding, source_root=self.root))
        self.assertTrue(source.exists())
        self.assertIn(str(source), self.state.done_paths())
        self.commit(self.planner.plan_undo(self.state.top(STACK_HISTORY)))
        self.assertNotIn(str(source), self.state.done_paths())
        self.assertFalse((self.tmp / "Deliver" / "solo_02.JPG").exists())

    def test_rename_moves_the_companions_too(self):
        master = self.root / "Day3" / "IMG_5511.JPG"
        self.commit(self.planner.plan_rename(self.planner.group_for(master), "sunset",
                                          source_root=self.root))
        names = sorted(p.name for p in (self.root / "Day3").iterdir())
        for name in ("sunset.JPG", "sunset.CR2", "sunset.JPG.xmp", "sunset.XMP"):
            self.assertIn(name, names)
        # And nothing kept the old name: an upper-case sidecar left behind is
        # how a shot gets split in two.
        for name in ("IMG_5511.JPG", "IMG_5511.CR2", "IMG_5511.JPG.xmp", "IMG_5511.XMP"):
            self.assertNotIn(name, names)

    def test_rename_rejects_a_reserved_name(self):
        master = self.root / "Day4" / "solo_01.JPG"
        with self.assertRaises(NameError_) as caught:
            self.planner.plan_rename(self.planner.group_for(master), "CON.JPG")
        self.assertEqual("error.name_reserved", caught.exception.key)

    def test_rename_to_the_same_name_is_a_no_op(self):
        master = self.root / "Day4" / "solo_01.JPG"
        self.assertTrue(self.planner.plan_rename(self.planner.group_for(master),
                                                 "solo_01.JPG").skipped)

    def test_trash_removes_the_group_and_undo_restores_it(self):
        master = self.root / "Day3" / "IMG_5511.JPG"
        group = self.planner.group_for(master)
        before = {p.name: p.read_bytes() for p in group.paths}
        self.commit(self.planner.plan_trash(group, source_root=self.root))
        self.assertTrue(all(not p.exists() for p in group.paths))
        self.commit(self.planner.plan_undo(self.state.top(STACK_HISTORY)))
        for path in group.paths:
            self.assertEqual(before[path.name], path.read_bytes())

    def test_skip_and_its_undo(self):
        master = self.root / "Day4" / "solo_03.JPG"
        self.commit(self.planner.plan_skip(self.planner.group_for(master), self.root))
        self.assertEqual([str(master)], self.state.review_queue(str(self.root)))
        self.commit(self.planner.plan_undo(self.state.top(STACK_HISTORY)))
        self.assertEqual([], self.state.review_queue(str(self.root)))

    def test_classifying_takes_a_file_out_of_the_review_queue(self):
        master = self.root / "Day4" / "solo_03.JPG"
        self.commit(self.planner.plan_skip(self.planner.group_for(master), self.root))
        self.commit(self.planner.plan_folder_action("move", self.planner.group_for(master),
                                                 self.binding(), source_root=self.root))
        self.assertEqual([], self.state.review_queue(str(self.root)))

    def test_tags(self):
        master = self.root / "Day4" / "solo_03.JPG"
        self.commit(self.planner.plan_tag([master], rating=4))
        self.assertEqual((4, ""), self.state.tag(str(master)))
        self.commit(self.planner.plan_tag([master], label="green"))
        self.assertEqual((4, "green"), self.state.tag(str(master)))

    def test_sequence_numbers_continue_after_a_restart(self):
        binding = self.binding(name_template="{seq:3}_{name}")
        for name in ("solo_01.JPG", "solo_02.JPG"):
            self.commit(self.planner.plan_folder_action(
                "move", self.planner.group_for(self.root / "Day4" / name), binding,
                source_root=self.root))
        reopened = ops.Planner(self.store, StateStore(self.data / "state.db"), self.settings)
        outcome = reopened.plan_folder_action(
            "move", reopened.group_for(self.root / "Day4" / "solo_03.JPG"), binding,
            source_root=self.root)
        self.store.run(outcome.plan, save_state=self.state.apply)
        reopened.sequence.commit()
        reopened.state.close()
        self.assertEqual(["001_solo_01.JPG", "002_solo_02.JPG", "003_solo_03.JPG"],
                         self.tree(self.keep))

    def test_sidecar_target_naming(self):
        self.assertEqual("new.CR2", ops.sidecar_target_name("new.JPG", "IMG_1", "IMG_1.CR2"))
        self.assertEqual("new.JPG.xmp",
                         ops.sidecar_target_name("new.JPG", "IMG_1", "IMG_1.JPG.xmp"))
        self.assertEqual("new.XMP", ops.sidecar_target_name("new.JPG", "IMG_1", "IMG_1.XMP"))


class RenameHistoryTests(TempCase):
    """Renaming through the engine: undo, redo, and the two conflict answers.

    A rename moves four files at once, so a half-undone rename is how the
    photograph and its raw end up with different names.
    """

    def setUp(self):
        super().setUp()
        from qingjian.core.engine import Engine
        self.root = self.tmp / "lib"
        build_library(self.root)
        settings = config.Settings()
        settings.recursive = True
        self.keep = self.tmp / "Keepers"
        settings.bindings[0].folder = str(self.keep)
        settings.bindings[0].name_template = "{name}"
        self.settings = settings
        self.engine = Engine(self.data, settings)
        self.addCleanup(self.engine.close)
        self.engine.open_folder(self.root)

    def folder(self, path: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}

    def test_a_rename_undone_and_redone_returns_every_member(self):
        master = self.root / "Day3" / "IMG_5511.JPG"
        before = self.folder(master.parent)
        self.engine.rename("sunset.JPG", master)
        self.engine.undo()
        self.assertEqual(before, self.folder(master.parent), "undo did not restore the shot")
        self.engine.redo()
        names = set(self.folder(master.parent))
        old = {"IMG_5511.JPG", "IMG_5511.CR2", "IMG_5511.JPG.xmp", "IMG_5511.XMP"}
        self.assertFalse(old & names, sorted(names))
        self.assertTrue({"sunset.JPG", "sunset.CR2", "sunset.JPG.xmp", "sunset.XMP"} <= names,
                        sorted(names))

    def test_a_case_only_rename_is_refused(self):
        """Windows would treat it as renaming a file onto itself."""
        target = self.root / "Day4" / "solo_01.JPG"
        content = target.read_bytes()
        with self.assertRaises(NameError_) as caught:
            self.engine.rename("SOLO_01.JPG", target)
        self.assertEqual("error.name_case_only", caught.exception.key)
        self.assertEqual(content, target.read_bytes())
        self.assertEqual(["solo_01.JPG"], [p.name for p in target.parent.iterdir()
                                           if p.name.casefold().startswith("solo_01")
                                           and p.suffix.casefold() == ".jpg"])

    def test_a_rename_conflict_answered_with_keep_both_leaves_the_other_file(self):
        target = self.root / "Day4" / "solo_01.JPG"
        other = self.root / "Day4" / "solo_02.JPG"
        content, other_content = target.read_bytes(), other.read_bytes()
        self.engine.rename("solo_02.JPG", target, resolver=lambda a, b: ops.CONFLICT_SEQUENCE)
        self.assertEqual(other_content, other.read_bytes(), "the other photo was replaced")
        landed = [p for p in other.parent.iterdir() if p.is_file() and p.read_bytes() == content]
        self.assertEqual(1, len(landed), [p.name for p in landed])
        self.assertNotEqual(other, landed[0])
        self.engine.undo()
        self.assertEqual(content, target.read_bytes())
        self.assertEqual(other_content, other.read_bytes())

    def test_a_rename_conflict_answered_with_replace_is_undone_byte_for_byte(self):
        target = self.root / "Day4" / "solo_01.JPG"
        other = self.root / "Day4" / "solo_02.JPG"
        content, other_content = target.read_bytes(), other.read_bytes()
        self.engine.rename("solo_02.JPG", target, resolver=lambda a, b: ops.CONFLICT_REPLACE)
        self.assertEqual(content, other.read_bytes(), "the rename did not replace the file")
        self.assertFalse(target.exists())
        self.engine.undo()
        self.assertEqual(other_content, other.read_bytes(), "the replaced photo is gone")
        self.assertEqual(content, target.read_bytes())

    def test_undoing_a_move_made_by_a_template_puts_every_file_back(self):
        """The folders the template made may stay behind; the files may not."""
        binding = self.settings.bindings[0]
        binding.path_template = "{YYYY}/{YYYY-MM}"
        master = self.root / "Day3" / "IMG_5511.JPG"
        before = self.folder(master.parent)
        self.engine.classify(binding, master)
        moved = [p for p in self.keep.rglob("*") if p.is_file()]
        self.assertEqual(4, len(moved), [p.name for p in moved])
        self.assertTrue(all(p.parent != self.keep for p in moved),
                        "the template did not make any folders")
        self.engine.undo()
        self.assertEqual(before, self.folder(master.parent), "undo did not restore the shot")
        self.assertEqual([], [p for p in self.keep.rglob("*") if p.is_file()],
                         "a file was left in the folders the template made")


if __name__ == "__main__":
    unittest.main()
