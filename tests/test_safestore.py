import json
import os
import time
from pathlib import Path

from base import TempCase, unittest
from qingjian.core import safestore as ss
from qingjian.core.safestore import (Cancelled, Plan, QuotaPolicy, SafeStore, TransactionError,
                                     VERIFY_FAST, VERIFY_FULL, identity, reclaim_candidates,
                                     step_copy, step_move, step_unlink)


class StoreCase(TempCase):
    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        self.dst = self.tmp / "dst"
        self.src.mkdir()
        self.dst.mkdir()
        self.store = SafeStore(self.data / "store")

    def file(self, name, content=b"payload" * 100):
        return self.write(self.src / name, content)

    def move_plan(self, path, target):
        plan = Plan(forward=[step_move(path, target, identity(path))])
        plan.inverse = SafeStore.invert(plan.forward)
        return plan

    def patch_module(self, owner, name, value) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)


class MoveTests(StoreCase):
    def test_same_volume_move_writes_no_snapshot(self):
        source = self.file("a.bin")
        plan = self.move_plan(source, self.dst / "2026" / "a.bin")
        self.assertEqual(0, plan.bytes_written())
        self.store.run(plan)
        self.assertTrue((self.dst / "2026" / "a.bin").is_file())
        self.assertFalse(source.exists())
        self.assertEqual(0, self.store.usage())

    def test_move_is_reversible_byte_for_byte(self):
        content = b"exact bytes" * 500
        source = self.file("a.bin", content)
        plan = self.move_plan(source, self.dst / "a.bin")
        self.store.run(plan)
        self.store.run(Plan(forward=plan.inverse))
        self.assertEqual(content, source.read_bytes())
        self.assertFalse((self.dst / "a.bin").exists())

    def test_cross_volume_move_copies_verifies_then_unlinks(self):
        source = self.file("a.bin")
        original = ss.same_volume
        ss.same_volume = lambda a, b: False
        try:
            plan = self.move_plan(source, self.dst / "a.bin")
            self.assertGreater(plan.bytes_written(), 0)
            self.store.run(plan)
        finally:
            ss.same_volume = original
        self.assertTrue((self.dst / "a.bin").is_file())
        self.assertFalse(source.exists())
        self.assertEqual([], [p for p in self.dst.iterdir() if p.name.startswith(".qingjian-")])

    def test_a_target_that_already_exists_is_refused(self):
        source = self.file("a.bin")
        self.write(self.dst / "a.bin", b"someone else")
        with self.assertRaises(TransactionError):
            self.store.run(self.move_plan(source, self.dst / "a.bin"))
        self.assertEqual(b"someone else", (self.dst / "a.bin").read_bytes())


class GuardTests(StoreCase):
    def test_a_file_edited_behind_our_back_stops_the_transaction(self):
        source = self.file("a.bin")
        plan = self.move_plan(source, self.dst / "a.bin")
        source.write_bytes(b"changed by another program")
        with self.assertRaises(TransactionError) as caught:
            self.store.run(plan)
        self.assertEqual("error.external_change", caught.exception.key)
        self.assertTrue(source.exists())

    def test_a_refusal_before_any_change_leaves_no_journal(self):
        """Otherwise one rejected key press blocks every later operation."""
        source = self.file("a.bin")
        plan = self.move_plan(source, self.dst / "a.bin")
        source.write_bytes(b"changed")
        with self.assertRaises(TransactionError):
            self.store.run(plan)
        self.assertFalse(self.store.has_pending())
        # And the store still works afterwards.
        self.store.run(self.move_plan(source, self.dst / "a.bin"))
        self.assertTrue((self.dst / "a.bin").is_file())

    def test_a_partial_failure_keeps_the_journal(self):
        first = self.file("a.bin")
        second = self.file("b.bin")
        plan = Plan(forward=[step_move(first, self.dst / "a.bin", identity(first)),
                             step_move(second, self.dst / "b.bin", identity(second))])

        def break_second(name, percent):
            # The precheck refuses a plan that is doomed before it starts, so
            # the damage has to happen while the plan is already running.
            if name == "b.bin" and second.exists():
                second.write_bytes(b"changed part-way")

        with self.assertRaises(TransactionError) as caught:
            self.store.run(plan, progress=break_second)
        self.assertEqual("error.unfinished", caught.exception.key)
        self.assertTrue(self.store.has_pending())
        self.assertTrue((self.dst / "a.bin").is_file())

    def test_symlinks_are_refused(self):
        source = self.file("a.bin")
        link = self.src / "link.bin"
        try:
            link.symlink_to(source)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(TransactionError):
            identity(link)

    def test_disk_full_is_caught_before_anything_is_written(self):
        source = self.file("a.bin", b"x" * 200000)
        original = ss.free_space
        ss.free_space = lambda path: 1024
        try:
            with self.assertRaises(TransactionError) as caught:
                self.store.run(Plan(forward=[step_copy(source, self.dst / "a.bin",
                                                       identity(source))]))
            self.assertEqual("error.disk_full", caught.exception.key)
        finally:
            ss.free_space = original
        self.assertTrue(source.exists())
        self.assertFalse((self.dst / "a.bin").exists())
        self.assertFalse(self.store.has_pending())

    def test_an_interrupted_copy_leaves_no_partial_file(self):
        source = self.file("a.bin")
        original = ss.copy_verified

        def explode(src, target, *args, **kwargs):
            Path(target).write_bytes(b"half")
            raise OSError("simulated write failure")

        ss.copy_verified = explode
        try:
            with self.assertRaises(OSError):
                self.store.run(Plan(forward=[step_copy(source, self.dst / "a.bin",
                                                       identity(source))]))
        finally:
            ss.copy_verified = original
        leftovers = [p.name for p in self.dst.iterdir()]
        self.assertEqual([], leftovers)

    def test_cancelling_before_a_write_changes_nothing(self):
        source = self.file("a.bin")
        with self.assertRaises(Cancelled):
            self.store.run(self.move_plan(source, self.dst / "a.bin"), cancel=lambda: True)
        self.assertTrue(source.exists())
        self.assertFalse(self.store.has_pending())


class PrecheckTests(StoreCase):
    """A plan is checked step by step before the journal exists."""

    def test_a_plan_whose_later_member_changed_is_refused_before_anything_moves(self):
        first = self.file("a.bin")
        second = self.file("b.bin", b"payload" * 99)
        stale = identity(second)
        second.write_bytes(b"changed after planning")
        plan = Plan(forward=[step_move(first, self.dst / "a.bin", identity(first)),
                             step_move(second, self.dst / "b.bin", stale)])
        with self.assertRaises(TransactionError) as caught:
            self.store.run(plan)
        self.assertEqual("error.external_change", caught.exception.key)
        self.assertTrue(first.exists(), "the first member moved although the plan was doomed")
        self.assertEqual([], self.tree(self.dst))
        self.assertFalse(self.store.has_pending())

    def test_a_plan_that_unlinks_its_own_source_is_refused(self):
        """F-002 and F-012 fall back to this: no plan may delete what it also reads."""
        photo = self.file("IMG.JPG")
        current = identity(photo)
        unlink = step_unlink(photo, current)
        unlink["snapshot"] = self.store.snapshot(photo)
        plan = Plan(forward=[unlink, step_copy(photo, photo, current)])
        with self.assertRaises(TransactionError) as caught:
            self.store.run(plan)
        self.assertEqual("error.plan_collision", caught.exception.key)
        self.assertTrue(photo.is_file(), "the file was deleted by its own plan")
        self.assertFalse(self.store.has_pending())

    def test_two_members_that_want_the_same_target_are_refused(self):
        heic = self.file("IMG_9.HEIC", b"h" * 300)
        jpg = self.file("IMG_9.JPG", b"j" * 500)
        plan = Plan(forward=[step_move(heic, self.dst / "photo.jpg", identity(heic)),
                             step_move(jpg, self.dst / "photo.JPG", identity(jpg))])
        with self.assertRaises(TransactionError) as caught:
            self.store.run(plan)
        self.assertEqual("error.plan_collision", caught.exception.key)
        self.assertTrue(heic.is_file())
        self.assertTrue(jpg.is_file())
        self.assertFalse(self.store.has_pending())

    def test_cross_volume_moves_are_counted_before_the_disk_fills(self):
        """F-029: a group half-moved onto a full card is what jams the journal."""
        first = self.file("a.bin", b"a" * 200000)
        second = self.file("b.bin", b"b" * 200000)
        plan = Plan(forward=[step_move(first, self.dst / "a.bin", identity(first)),
                             step_move(second, self.dst / "b.bin", identity(second))])
        self.patch_module(ss, "same_volume", lambda a, b: False)
        self.patch_module(ss, "free_space", lambda path: 1024 * 1024)
        with self.assertRaises(TransactionError) as caught:
            self.store.run(plan)
        self.assertEqual("error.disk_full", caught.exception.key)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())
        self.assertEqual([], self.tree(self.dst))
        self.assertFalse(self.store.has_pending())


    def test_a_target_that_cannot_be_stated_is_refused_before_anything_moves(self):
        """"I cannot tell" is not "nothing is there".

        Reading a refused or unreachable target as absent waves the plan
        through, and the group half-moves -- the state F-008 exists to prevent.
        """
        first = self.file("a.bin")
        second = self.file("b.bin", b"payload" * 99)
        blocked = self.dst / "b.bin"
        plan = Plan(forward=[step_move(first, self.dst / "a.bin", identity(first)),
                             step_move(second, blocked, identity(second))])
        target, real = str(blocked), os.stat

        def refusing(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError(13, "Access is denied")
            return real(path, *args, **kwargs)

        self.patch_module(os, "stat", refusing)
        with self.assertRaises(PermissionError):
            self.store.run(plan)
        self.assertFalse(self.store.has_pending(), "a journal was written for a doomed plan")
        self.assertTrue(first.is_file(), "the first member moved although the plan was doomed")
        self.assertTrue(second.is_file())
        self.assertEqual([], self.tree(self.dst))


class AbandonTests(StoreCase):
    """The way out of a journal that can never be replayed."""

    def stuck(self, snapshots=None, state=None):
        """Step one done, step two impossible: the journal a part-way failure leaves."""
        first = self.file("a.bin")
        second = self.file("b.bin", b"payload" * 99)
        plan = Plan(forward=[step_move(first, self.dst / "a.bin", identity(first)),
                             step_move(second, self.dst / "b.bin", identity(second))],
                    snapshots=list(snapshots or []), state=state)
        plan.inverse = SafeStore.invert(plan.forward)

        def break_second(name, percent):
            if name == "b.bin" and second.exists():
                second.write_bytes(b"changed while the plan was running")

        with self.assertRaises(TransactionError):
            self.store.run(plan, progress=break_second)
        self.assertTrue(self.store.has_pending())
        return plan, first, second

    def test_abandoning_with_rollback_puts_the_finished_part_back(self):
        plan, first, second = self.stuck()
        self.assertTrue(self.store.abandon(True))
        self.assertFalse(self.store.has_pending())
        self.assertTrue(first.is_file(), "the finished step was not rolled back")
        self.assertEqual([], self.tree(self.dst))
        self.store.run(Plan(forward=[step_move(first, self.dst / "a.bin", identity(first))]))
        self.assertTrue((self.dst / "a.bin").is_file())

    def test_abandoning_without_rollback_keeps_the_files_and_the_reference(self):
        spare = self.file("c.bin")
        snapshot = self.store.snapshot(spare)
        record = {"id": "rec-1", "action": "move", "original": str(self.src / "a.bin"),
                  "destination": str(self.dst / "a.bin"), "undoable": True,
                  "payload": {"forward": [], "inverse": [], "snapshots": [snapshot["file"]]}}
        committed = []
        self.stuck(snapshots=[snapshot["file"]], state={"records_add": [record]})
        self.assertTrue(self.store.abandon(False, save_state=committed.append))
        self.assertFalse(self.store.has_pending())
        self.assertTrue((self.dst / "a.bin").is_file(), "the finished step was undone")
        self.assertTrue(Path(snapshot["file"]).is_file(), "the restore copy was thrown away")
        kept = committed[-1]["records_add"][0]
        self.assertFalse(kept["undoable"])
        self.assertIn(snapshot["file"], kept["payload"]["snapshots"])

    def test_keeping_the_files_refuses_to_orphan_a_snapshot(self):
        """Without somewhere to record it, the restore copy would be unreachable."""
        spare = self.file("c.bin")
        snapshot = self.store.snapshot(spare)
        self.stuck(snapshots=[snapshot["file"]])
        with self.assertRaises(ValueError):
            self.store.abandon(False)
        self.assertTrue(self.store.has_pending(), "the journal went away with the record")
        self.assertTrue(Path(snapshot["file"]).is_file())

    def test_a_damaged_journal_can_be_set_aside(self):
        self.store.journal_path.write_text("{ not json", encoding="utf-8")
        self.assertTrue(self.store.abandon(False, save_state=lambda delta: None))
        self.assertFalse(self.store.has_pending())
        aside = [p.name for p in (self.data / "store").iterdir()
                 if p.name.startswith("journal.damaged-")]
        self.assertEqual(1, len(aside), "the damaged journal was not kept for diagnosis")

    def test_a_damaged_journal_cannot_be_rolled_back(self):
        self.store.journal_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(TransactionError) as caught:
            self.store.abandon(True)
        self.assertEqual("error.journal_damaged", caught.exception.key)
        self.assertTrue(self.store.has_pending())


class RecoveryTests(StoreCase):
    def test_a_crash_between_steps_is_finished_on_restart(self):
        first = self.file("a.bin")
        second = self.file("b.bin")
        plan = Plan(forward=[step_move(first, self.dst / "a.bin", identity(first)),
                             step_move(second, self.dst / "b.bin", identity(second))])
        payload = plan.to_dict()
        payload["stage_id"] = "stage"
        ss.atomic_json(self.store.journal_path, payload)
        self.store._run_step(payload["forward"][0], VERIFY_FULL, "stage", lambda *a: None)

        restarted = SafeStore(self.data / "store")
        self.assertTrue(restarted.has_pending())
        restarted.recover()
        self.assertTrue((self.dst / "a.bin").is_file())
        self.assertTrue((self.dst / "b.bin").is_file())
        self.assertFalse(restarted.has_pending())

    def test_recovery_is_idempotent(self):
        source = self.file("a.bin")
        plan = self.move_plan(source, self.dst / "a.bin")
        self.store.run(plan)
        payload = plan.to_dict()
        payload["stage_id"] = "stage"
        ss.atomic_json(self.store.journal_path, payload)
        self.store.recover()                     # replaying a finished plan
        self.assertTrue((self.dst / "a.bin").is_file())

    def test_a_copy_that_finished_before_the_crash_is_recognised(self):
        source = self.file("a.bin")
        step = step_copy(source, self.dst / "a.bin", identity(source))
        plan = Plan(forward=[step])
        payload = plan.to_dict()
        payload["stage_id"] = "stage"
        ss.atomic_json(self.store.journal_path, payload)
        self.store._run_step(payload["forward"][0], VERIFY_FULL, "stage", lambda *a: None)
        # The journal on disk still has result=None, as it would after a crash.
        restarted = SafeStore(self.data / "store")
        restarted.recover()
        self.assertTrue((self.dst / "a.bin").is_file())
        self.assertTrue(source.exists())

    def test_state_is_committed_only_after_the_files(self):
        source = self.file("a.bin")
        committed = []
        plan = self.move_plan(source, self.dst / "a.bin")
        plan.state = {"marker": True}
        self.store.run(plan, save_state=committed.append)
        self.assertEqual([{"marker": True}], committed)

    def test_recovery_keeps_the_journal_when_a_replay_step_fails(self):
        """A journal read back from disk may already have changed files."""
        first = self.file("a.bin")
        second = self.file("b.bin", b"payload" * 99)
        plan = Plan(forward=[step_move(first, self.dst / "a.bin", identity(first)),
                             step_move(second, self.dst / "b.bin", identity(second))])
        payload = plan.to_dict()
        payload["stage_id"] = "stage"
        ss.atomic_json(self.store.journal_path, payload)     # a kill leaves no "mutated"
        os.replace(first, self.dst / "a.bin")                # the first step had finished
        os.utime(second, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        restarted = SafeStore(self.data / "store")
        with self.assertRaises(TransactionError) as caught:
            restarted.recover()
        self.assertEqual("error.unfinished", caught.exception.key)
        self.assertTrue(restarted.has_pending(),
                        "the journal was deleted, so the finished move has no record")
        self.assertTrue((self.dst / "a.bin").is_file())

    def test_a_damaged_journal_is_not_silently_dropped(self):
        self.store.journal_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.store.recover()


class ImpostorTests(StoreCase):
    """A file of the same size and mtime is not the same file."""

    def journal(self, plan) -> None:
        payload = plan.to_dict()
        payload["stage_id"] = "stage"
        ss.atomic_json(self.store.journal_path, payload)

    def test_undoing_a_recycle_refuses_to_delete_the_slot_for_an_impostor(self):
        photo = self.file("a.JPG", b"photo" * 100)
        current = identity(photo)
        slot = self.store.trash_slot(photo)
        self.assertFalse(slot.parent.exists(), "planning recycle created its hidden folder")
        plan = Plan(forward=[step_move(photo, slot, current)])
        plan.inverse = SafeStore.invert(plan.forward)
        self.store.run(plan)
        self.assertTrue(slot.parent.is_dir())
        self.assertTrue(slot.is_file())
        self.write(photo, b"other" * 100)                 # same size, same mtime, other bytes
        os.utime(photo, ns=(current["mtime_ns"], current["mtime_ns"]))
        self.journal(Plan(forward=plan.inverse, verify=VERIFY_FULL))
        with self.assertRaises(TransactionError):
            SafeStore(self.data / "store").recover()
        self.assertTrue(slot.is_file(), "the recycled original was deleted for an impostor")

    def test_a_finished_cross_volume_move_is_checked_against_its_hash(self):
        photo = self.file("b.JPG", b"photo" * 100)
        plan = Plan(forward=[step_move(photo, self.dst / "b.JPG", identity(photo))],
                    verify=VERIFY_FULL)
        self.patch_module(ss, "same_volume", lambda a, b: False)
        self.store.run(plan)
        landed = self.dst / "b.JPG"
        stamp = landed.stat().st_mtime_ns
        landed.write_bytes(b"other" * 100)
        os.utime(landed, ns=(stamp, stamp))
        self.journal(plan)
        with self.assertRaises(TransactionError):
            SafeStore(self.data / "store").recover()


class SyncInverseTests(StoreCase):
    """What a cross-volume copy really got must reach the right step, and only it.

    A journal may carry steps that a record does not, so the record's own forward
    is the only list its inverse lines up with.
    """

    def build(self, record_forward):
        first = step_move(self.src / "a.JPG", self.dst / "a.JPG", {"size": 111, "mtime_ns": 11})
        second = step_move(self.src / "b.JPG", self.dst / "b.JPG", {"size": 222, "mtime_ns": 22})
        forward = [first, second]
        body = {"forward": record_forward, "inverse": SafeStore.invert(record_forward)}
        payload = {"forward": forward, "inverse": SafeStore.invert(forward),
                   "state": {"records_add": [{"payload": body}]}}
        first["result"] = {"size": 111, "mtime_ns": 8_000_000_000}   # the card rounded the clock
        return payload, first, body

    def test_a_record_shorter_than_the_journal_still_learns_what_landed(self):
        mine = step_move(self.src / "a.JPG", self.dst / "a.JPG", {"size": 111, "mtime_ns": 11})
        payload, first, body = self.build([mine])
        SafeStore._sync_inverse(payload, 0, first)
        self.assertEqual(first["result"], body["inverse"][0]["src_id"],
                         "undoing this record still expects the clock the plan guessed")

    def test_a_record_holding_another_step_is_left_alone(self):
        other = step_move(self.src / "b.JPG", self.dst / "b.JPG", {"size": 222, "mtime_ns": 22})
        payload, first, body = self.build([other])
        SafeStore._sync_inverse(payload, 0, first)
        self.assertEqual(222, body["forward"][0]["result"]["size"],
                         "one file's identity was written onto another file's step")


class DeleteAndReplaceTests(StoreCase):
    def test_delete_keeps_a_restore_copy_and_undo_puts_it_back(self):
        content = b"precious" * 200
        source = self.file("a.bin", content)
        snapshot = self.store.snapshot(source)
        step = step_unlink(source, identity(source))
        step["snapshot"] = snapshot
        plan = Plan(forward=[step])
        plan.inverse = SafeStore.invert(plan.forward)
        self.store.run(plan)
        self.assertFalse(source.exists())
        self.store.run(Plan(forward=plan.inverse))
        self.assertEqual(content, source.read_bytes())

    def test_a_damaged_restore_copy_is_refused(self):
        source = self.file("a.bin")
        snapshot = self.store.snapshot(source)
        Path(snapshot["file"]).write_bytes(b"corrupted")
        step = step_unlink(source, identity(source))
        step["snapshot"] = snapshot
        plan = Plan(forward=[step])
        plan.inverse = SafeStore.invert(plan.forward)
        self.store.run(plan)
        with self.assertRaises(TransactionError) as caught:
            self.store.run(Plan(forward=plan.inverse))
        self.assertIn(caught.exception.key, ("error.snapshot_missing", "error.unfinished"))

    def test_replacing_preserves_the_old_file_for_undo(self):
        source = self.file("a.bin", b"new content")
        target = self.write(self.dst / "a.bin", b"old content")
        snapshot = self.store.snapshot(target)
        unlink = step_unlink(target, identity(target))
        unlink["snapshot"] = snapshot
        plan = Plan(forward=[unlink, step_move(source, target, identity(source))])
        plan.inverse = SafeStore.invert(plan.forward)
        self.store.run(plan)
        self.assertEqual(b"new content", target.read_bytes())
        self.store.run(Plan(forward=plan.inverse))
        self.assertEqual(b"old content", target.read_bytes())
        self.assertEqual(b"new content", source.read_bytes())


class VerificationTests(StoreCase):
    def test_fast_mode_uses_size_and_mtime(self):
        source = self.file("a.bin")
        record = identity(source, VERIFY_FAST)
        self.assertNotIn("hash", record)
        self.assertIn("mtime_ns", record)

    def test_full_mode_records_a_hash(self):
        source = self.file("a.bin")
        self.assertIn("hash", identity(source, VERIFY_FULL))

    def test_copy_verification_detects_a_changing_source(self):
        source = self.file("a.bin", b"x" * (2 * 1024 * 1024))
        target = self.dst / "a.bin"
        real_stat = Path.stat
        calls = {"n": 0}

        class Fake:
            def __init__(self, real, bump):
                self.__dict__.update({k: getattr(real, k) for k in
                                      ("st_size", "st_mtime_ns", "st_atime_ns")})
                self.st_mtime_ns = real.st_mtime_ns + bump

        def patched(self, *args, **kwargs):
            real = real_stat(self, *args, **kwargs)
            if self == source:
                calls["n"] += 1
                if calls["n"] > 1:
                    return Fake(real, 1000)
            return real

        Path.stat = patched
        try:
            with self.assertRaises(TransactionError) as caught:
                ss.copy_verified(source, target)
            self.assertEqual("error.source_changed", caught.exception.key)
        finally:
            Path.stat = real_stat


class QuotaTests(StoreCase):
    def test_cached_snapshot_size_avoids_stat(self):
        from unittest.mock import patch
        rows = [{"time_epoch": 1, "snapshots": ["missing"], "snapshot_bytes": 1000},
                {"time_epoch": 2, "snapshots": ["missing"], "snapshot_bytes": 1000}]
        policy = QuotaPolicy(max_operations=0, max_bytes=1500, max_days=0)
        with patch("pathlib.Path.stat", side_effect=AssertionError("stat called")):
            self.assertEqual([0], reclaim_candidates(rows, policy, now=3))

    def test_byte_limit_spares_newest_and_empty_records(self):
        rows, now = self.rows([3, 2, 1], sizes=[0, 1000, 5000])
        policy = QuotaPolicy(max_operations=0, max_bytes=1500, max_days=0)
        self.assertEqual([1], reclaim_candidates(rows, policy, now, keep_newest=True))

    def rows(self, ages_days, sizes=None):
        now = time.time()
        sizes = sizes or [0] * len(ages_days)
        out = []
        for age, size in zip(ages_days, sizes):
            snapshots = []
            if size:
                path = self.store.snapshot_root / f"snap{len(out)}"
                path.write_bytes(b"x" * size)
                snapshots.append(str(path))
            out.append({"time_epoch": now - age * 86400, "snapshots": snapshots})
        return out, now

    def test_age_limit(self):
        rows, now = self.rows([40, 10, 0])
        policy = QuotaPolicy(max_operations=0, max_bytes=0, max_days=30)
        self.assertEqual([0], reclaim_candidates(rows, policy, now))

    def test_count_limit_drops_the_oldest(self):
        rows, now = self.rows([5, 4, 3, 2, 1])
        policy = QuotaPolicy(max_operations=2, max_bytes=0, max_days=0)
        self.assertEqual([0, 1, 2], reclaim_candidates(rows, policy, now))

    def test_byte_limit(self):
        rows, now = self.rows([3, 2, 1], sizes=[1000, 1000, 1000])
        policy = QuotaPolicy(max_operations=0, max_bytes=1500, max_days=0)
        self.assertEqual([0, 1], reclaim_candidates(rows, policy, now))

    def test_nothing_is_dropped_inside_every_limit(self):
        rows, now = self.rows([1, 1], sizes=[10, 10])
        self.assertEqual([], reclaim_candidates(rows, QuotaPolicy(), now))

    def test_discarding_snapshots_reports_the_bytes_freed(self):
        source = self.file("a.bin", b"y" * 5000)
        snapshot = self.store.snapshot(source)
        self.assertEqual(5000, self.store.usage())
        self.assertEqual(5000, self.store.discard_snapshots([snapshot["file"]]))
        self.assertEqual(0, self.store.usage())

    def test_sweeping_partials(self):
        (self.dst / ".qingjian-abc-a.bin.part").write_bytes(b"junk")
        (self.dst / "keep.bin").write_bytes(b"keep")
        self.assertEqual(1, self.store.sweep_partials([self.dst]))
        self.assertEqual(["keep.bin"], [p.name for p in self.dst.iterdir()])


if __name__ == "__main__":
    unittest.main()
