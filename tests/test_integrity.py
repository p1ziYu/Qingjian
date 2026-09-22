"""History must survive being used quickly.

Every case here is a failure that reached a real folder: the redo button stayed
lit after the branch it belonged to was gone, and sorting on the queue thread
raced undo on the interface thread over a single journal file.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

from base import ROOT, TempCase, unittest
from qingjian.core import config, ops, safestore
from qingjian.core.engine import Engine
from qingjian.core.safestore import TransactionError

PACKAGE = ROOT / "qingjian"


class QueueCursorTests(TempCase):
    def test_queue_helpers_preserve_cursor(self):
        src = self.tmp / "src"
        src.mkdir()
        paths = [self.write(src / f"p{i}.jpg", b"photo") for i in range(5)]
        engine = Engine(self.data, config.Settings())
        self.addCleanup(engine.close)
        engine.open_folder(src)
        engine.go_to(paths[3])
        before = engine.current_path()
        engine.drop_from_queue(paths[0])
        engine.insert_at(paths[0], 0)
        self.assertEqual(engine.current_path(), before)


class TagWithoutQueueLockTests(TempCase):
    def test_tag_does_not_take_exclusive_lock(self):
        photo = self.write(self.tmp / "p.jpg", b"photo")
        engine = Engine(self.data, config.Settings())
        self.addCleanup(engine.close)
        with mock.patch.object(engine, "exclusive",
                               side_effect=AssertionError("exclusive called")):
            engine.tag([photo], rating=5)
        self.assertEqual(engine.state.tag(str(photo))[0], 5)


def digests(root: Path) -> dict[str, list[str]]:
    """Content -> where it lives, so a doubled file is impossible to miss."""
    out: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "app" in path.parts:
            continue
        key = hashlib.sha256(path.read_bytes()).hexdigest()
        out.setdefault(key, []).append(str(path.relative_to(root)))
    return out


def assert_each_content_once(case: unittest.TestCase, root: Path,
                             before: dict[str, list[str]]) -> None:
    """Every content recorded before the test is still on disk, exactly once.

    "No content is in two places" alone passes when a file is simply gone.
    """
    after = digests(root)
    for key, places in before.items():
        now = after.get(key, [])
        case.assertEqual(1, len(now), f"the content first at {places} is now at {now}")


class HistoryBranchTests(TempCase):
    def test_retired_redo_top_is_rejected_without_replaying_files(self):
        engine, source, binding = self.build()
        target = source / "img0.jpg"
        engine.classify(binding, target)
        engine.undo()
        top = engine.state.top("redo", undoable_only=False)
        engine.state.retire([top.id])
        self.assertFalse(engine.can_redo())
        self.assertTrue(engine.redo().skipped)
        self.assertTrue(target.exists())

    """A new operation ends the branch that undo created."""

    def build(self, files: int = 3) -> tuple[Engine, Path, config.Binding]:
        source = self.tmp / "src"
        source.mkdir()
        for index in range(files):
            self.write(source / f"img{index}.jpg", bytes([65 + index]) * (400 + index))
        settings = config.Settings()
        settings.source_folder = str(source)
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        engine.open_folder(source)
        self.addCleanup(engine.close)
        return engine, source, config.Binding(key="1", action="move",
                                              folder=str(self.tmp / "f1"))

    def test_a_new_operation_clears_the_redo_branch(self):
        """Sort, undo, sort the same file again: there is nothing left to redo.

        Leaving the record there kept the button live, and pressing it replayed
        a move whose source had already gone somewhere else.
        """
        engine, source, binding = self.build()
        engine.classify(binding, source / "img0.jpg")
        engine.undo()
        self.assertTrue(engine.can_redo())
        engine.classify(binding, source / "img0.jpg")
        self.assertFalse(engine.can_redo())
        self.assertEqual(engine.state.counts()["redo"], 0)

    def test_the_file_is_never_in_two_places_after_that_sequence(self):
        engine, source, binding = self.build()
        before = digests(self.tmp)
        engine.classify(binding, source / "img0.jpg")
        engine.undo()
        engine.classify(binding, source / "img0.jpg")
        engine.redo()
        engine.redo()
        for places in digests(self.tmp).values():
            self.assertEqual(len(places), 1, f"the same content is in {places}")
        assert_each_content_once(self, self.tmp, before)

    def test_undo_then_redo_still_works_when_nothing_intervenes(self):
        """Truncating the branch must not break the ordinary case."""
        engine, source, binding = self.build()
        engine.classify(binding, source / "img0.jpg")
        engine.undo()
        self.assertTrue((source / "img0.jpg").exists())
        engine.redo()
        self.assertTrue((self.tmp / "f1" / "img0.jpg").exists())
        self.assertFalse((source / "img0.jpg").exists())

    def test_tagging_does_not_end_the_redo_branch(self):
        """Only operations that push history should truncate it."""
        engine, source, binding = self.build()
        engine.classify(binding, source / "img0.jpg")
        engine.undo()
        engine.tag([source / "img1.jpg"], rating=3)
        self.assertTrue(engine.can_redo())

    def test_dropping_the_branch_releases_its_restore_copies(self):
        """A truncated record's snapshots are deleted, not orphaned on disk.

        Replacing a file is what keeps a restore copy after undo: redo needs it
        to replace that file again.
        """
        engine, source, binding = self.build()
        self.write(self.tmp / "f1" / "img2.jpg", b"the file that gets replaced")
        engine.classify(binding, source / "img2.jpg",
                        resolver=lambda _source, _target: ops.CONFLICT_REPLACE)
        engine.undo()
        before = engine.store.usage(refresh=True)
        engine.classify(binding, source / "img0.jpg")
        after = engine.store.usage(refresh=True)
        self.assertLess(after, before)
        self.assertEqual(engine.state.counts()["redo"], 0)


class StuckJournalTests(TempCase):
    """A failure that can never be replayed must not brick the app."""

    def build(self):
        source = self.tmp / "lib"
        self.write(source / "IMG_1.JPG", b"J" * 400)
        self.write(source / "IMG_1.CR2", b"R" * 900)
        self.write(source / "IMG_2.JPG", b"K" * 500)
        settings = config.Settings()
        settings.logging_enabled = False
        settings.source_folder = str(source)
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(source)
        return engine, source, self.tmp / "keep"

    def test_undo_after_a_member_was_deleted_outside_leaves_the_app_usable(self):
        engine, source, keep = self.build()
        binding = config.Binding(key="1", action="move", folder=str(keep))
        engine.classify(binding, source / "IMG_1.JPG")
        (keep / "IMG_1.JPG").unlink()                   # a cleanup tool took it away
        with self.assertRaises(TransactionError):
            engine.undo()
        self.assertTrue((keep / "IMG_1.CR2").is_file(), "the RAW was moved back and left dangling")
        self.assertFalse(engine.has_pending())
        engine.classify(binding, source / "IMG_2.JPG")
        self.assertTrue((keep / "IMG_2.JPG").is_file())


class ReplaceRecoveryTests(TempCase):
    """Every step ran, the state commit did not: recovery must see that."""

    def test_a_replace_whose_state_commit_failed_recovers_and_can_be_undone(self):
        source, keep = self.tmp / "lib", self.tmp / "keep"
        new = self.write(source / "IMG_1.JPG", b"NEW-" * 200)
        old = self.write(keep / "IMG_1.JPG", b"OLD-ONLY-COPY-" * 100)
        os.utime(old, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        settings = config.Settings()
        settings.logging_enabled = False
        settings.source_folder = str(source)
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(source)

        def failing(delta):
            raise sqlite3.OperationalError("disk I/O error")

        real_apply = engine.state.apply
        engine.state.apply = failing
        binding = config.Binding(key="1", action="move", folder=str(keep))
        with self.assertRaises(sqlite3.OperationalError):
            engine.classify(binding, new, resolver=lambda a, b: ops.CONFLICT_REPLACE)
        engine.state.apply = real_apply
        engine.recover()
        self.assertTrue(engine.can_undo(), "the replaced file has no record to bring it back")
        engine.undo()
        self.assertTrue(old.read_bytes().startswith(b"OLD-"))
        self.assertFalse(engine.has_pending())


@unittest.skipUnless(sys.platform == "win32", "read-only only blocks deletion on Windows")
class ReadOnlySourceTests(TempCase):
    """A camera-protected photo must still move to another disk (F-011, F-117)."""

    def build(self):
        source = self.tmp / "lib"
        source.mkdir()
        settings = config.Settings()
        settings.logging_enabled = False
        settings.source_folder = str(source)
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        self.addCleanup(self.make_writable)
        return engine, source, self.tmp / "keep"

    def make_writable(self) -> None:
        for path in self.tmp.rglob("*"):
            if path.is_file():
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)

    def cross_volume(self) -> None:
        original = safestore.same_volume
        safestore.same_volume = lambda a, b: False
        self.addCleanup(setattr, safestore, "same_volume", original)

    def test_a_read_only_photo_moves_across_volumes(self):
        engine, source, keep = self.build()
        photo = self.write(source / "IMG_1.JPG", b"P" * 400)
        os.chmod(photo, stat.S_IREAD)
        engine.open_folder(source)
        self.cross_volume()
        engine.classify(config.Binding(key="1", action="move", folder=str(keep)), photo)
        self.assertFalse(photo.exists(), "the protected source was left behind")
        self.assertTrue((keep / "IMG_1.JPG").is_file())
        self.assertFalse(engine.has_pending())

    def test_a_group_whose_raw_is_read_only_arrives_complete(self):
        engine, source, keep = self.build()
        jpg = self.write(source / "IMG_2.JPG", b"J" * 400)
        raw = self.write(source / "IMG_2.CR2", b"R" * 900)
        os.chmod(raw, stat.S_IREAD)
        engine.open_folder(source)
        self.cross_volume()
        engine.classify(config.Binding(key="1", action="move", folder=str(keep)), jpg)
        self.assertEqual(["IMG_2.CR2", "IMG_2.JPG"], self.tree(keep))
        self.assertEqual([], self.tree(source))
        self.assertFalse(engine.has_pending())


class CoarseMtimeTests(TempCase):
    """FAT and exFAT round the modification time; undo must still recognise the file."""

    def sort_and_undo(self, action: str):
        root = self.tmp / action
        usb = root / "usb"
        photo = self.write(root / "lib" / "IMG_5.JPG", b"f" * 3000)
        real_utime = os.utime

        def rounded(path, *args, ns=None, **kwargs):
            if ns is not None and str(usb) in str(path):
                ns = tuple((value // 2_000_000_000) * 2_000_000_000 for value in ns)
            if ns is None:
                return real_utime(path, *args, **kwargs)
            return real_utime(path, *args, ns=ns, **kwargs)

        os.utime = rounded
        self.addCleanup(setattr, os, "utime", real_utime)
        original = safestore.same_volume
        safestore.same_volume = lambda a, b: False
        self.addCleanup(setattr, safestore, "same_volume", original)
        settings = config.Settings()
        settings.logging_enabled = False
        settings.source_folder = str(root / "lib")
        engine = Engine(data_dir=root / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(root / "lib")
        engine.classify(config.Binding(key="1", action=action, folder=str(usb)), photo)
        engine.undo()
        return photo, usb / "IMG_5.JPG"

    def test_undoing_a_move_to_a_coarse_mtime_volume(self):
        photo, copy = self.sort_and_undo("move")
        self.assertTrue(photo.is_file(), "the photo did not come back")
        self.assertFalse(copy.exists())

    def test_undoing_a_favourite_to_a_coarse_mtime_volume(self):
        photo, copy = self.sort_and_undo("favorite")
        self.assertTrue(photo.is_file())
        self.assertFalse(copy.exists(), "the copy was left on the card")


@unittest.skipUnless(sys.platform == "win32", "the 259-character limit is a Windows default")
class LongStagedPathTests(TempCase):
    """The staged copy is 48 characters longer than the destination that was checked."""

    def refuse_long_staged_copies(self) -> None:
        """Stand in for a default Windows install, where LongPathsEnabled is 0."""
        real = safestore.copy_verified

        def limited(source, target, *args, **kwargs):
            text = str(target)
            if len(text) > 259 and not text.startswith("\\\\?\\"):
                raise FileNotFoundError(2, "The system cannot find the path specified", text)
            return real(source, target, *args, **kwargs)

        safestore.copy_verified = limited
        self.addCleanup(setattr, safestore, "copy_verified", real)

    def folder_of_length(self, parent, total: int, tag: str):
        folder = parent / (tag + "_" * (total - len(str(parent)) - 1 - len(tag)))
        folder.mkdir(parents=True)
        self.assertEqual(total, len(str(folder)))
        return folder

    def engine_over(self, source):
        settings = config.Settings()
        settings.logging_enabled = False
        settings.source_folder = str(source)
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(source)
        return engine

    def test_copying_into_a_deep_folder(self):
        source = self.tmp / "lib"
        photo = self.write(source / "IMG_0001.JPG", b"J" * 5000)
        self.write(source / "IMG_0001.JPG.xmp", b"<x:xmpmeta/>")
        target = self.folder_of_length(self.tmp, 198, "copy_target")
        self.refuse_long_staged_copies()
        engine = self.engine_over(source)
        engine.classify(config.Binding(key="1", action="copy", folder=str(target)), photo)
        self.assertEqual(["IMG_0001.JPG", "IMG_0001.JPG.xmp"], self.tree(target))
        self.assertFalse(engine.has_pending())

    def test_undoing_a_recycle_from_a_deep_folder(self):
        base = self.tmp / "lib"
        deep = self.folder_of_length(base, 220, "deep_folder")
        photo = self.write(deep / "IMG_0002.JPG", b"precious" * 100)
        content = photo.read_bytes()
        self.refuse_long_staged_copies()
        engine = self.engine_over(base)
        engine.trash(photo)
        self.assertFalse(photo.exists())
        engine.undo()
        self.assertEqual(content, photo.read_bytes())
        self.assertFalse(engine.has_pending())

    def test_a_slot_path_of_260_still_recycles_and_undoes(self):
        """F-117: the slot is refused, and the restore copy behind it must work."""
        base = self.tmp / "lib"
        deep = self.folder_of_length(base, 207, "deep_folder")
        photo = self.write(deep / "a.JPG", b"photo" * 300)
        content = photo.read_bytes()
        self.assertEqual(260, len(str(deep / safestore.TRASH_DIR / ("0" * 32 + ".JPG"))))
        self.refuse_long_staged_copies()
        engine = self.engine_over(base)
        engine.trash(photo)
        self.assertFalse(photo.exists())
        self.assertFalse((deep / safestore.TRASH_DIR).exists(), "a 260-character slot was used")
        engine.undo()
        self.assertEqual(content, photo.read_bytes())
        self.assertFalse(engine.has_pending())

    def test_undoing_a_replace_in_a_deep_folder(self):
        source = self.tmp / "lib"
        new = self.write(source / "IMG_0003.JPG", b"NEW" * 300)
        target = self.folder_of_length(self.tmp, 217, "replace_target")
        old = self.write(target / "IMG_0003.JPG", b"OLD" * 400)
        self.refuse_long_staged_copies()
        engine = self.engine_over(source)
        engine.classify(config.Binding(key="1", action="move", folder=str(target)), new,
                        resolver=lambda a, b: ops.CONFLICT_REPLACE)
        self.assertTrue(old.read_bytes().startswith(b"NEW"))
        engine.undo()
        self.assertTrue(old.read_bytes().startswith(b"OLD"), "the replaced file did not come back")
        self.assertFalse(engine.has_pending())


class ExclusionTests(TempCase):
    """Two mutations must never be in flight at once."""

    def build(self) -> tuple[Engine, Path, config.Binding]:
        source = self.tmp / "src"
        source.mkdir()
        for index in range(8):
            self.write(source / f"img{index}.jpg", bytes([65 + index]) * 20000)
        settings = config.Settings()
        settings.source_folder = str(source)
        settings.background_queue = True
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        engine.open_folder(source)
        self.addCleanup(engine.close)
        return engine, source, config.Binding(key="1", action="move",
                                              folder=str(self.tmp / "f1"))

    def test_a_second_transaction_waits_for_the_first(self):
        """Hold one transaction open and start another; they must not overlap.

        There is one journal file. Two transactions in flight meant one thread
        renaming the temporary the other was still writing, which left a
        transaction running with nothing on disk to recover it.
        """
        engine, source, binding = self.build()
        engine.classify(binding, source / "img0.jpg")   # something to undo

        guard = threading.Lock()
        active = 0
        overlapped: list[str] = []
        inside = threading.Event()
        proceed = threading.Event()
        original = engine.store._apply

        def spy(payload, progress, save_state):
            nonlocal active
            with guard:
                active += 1
                if active > 1:
                    overlapped.append(str(payload.get("label")))
            inside.set()
            proceed.wait(3.0)
            try:
                return original(payload, progress, save_state)
            finally:
                with guard:
                    active -= 1

        engine.store._apply = spy
        self.addCleanup(setattr, engine.store, "_apply", original)

        errors: list[BaseException] = []

        def sort_one():
            try:
                engine.classify(binding, source / "img1.jpg")
            except BaseException as error:      # noqa: BLE001 - recorded
                errors.append(error)

        def undo_one():
            try:
                engine.undo()
            except BaseException as error:      # noqa: BLE001 - recorded
                errors.append(error)

        first = threading.Thread(target=sort_one)
        first.start()
        self.assertTrue(inside.wait(3.0), "the first transaction never started")
        second = threading.Thread(target=undo_one)
        second.start()
        second.join(0.4)
        self.assertTrue(second.is_alive(), "the second transaction did not wait")
        proceed.set()
        first.join(5)
        second.join(5)
        self.assertEqual(overlapped, [], "two transactions were in flight at once")
        self.assertEqual([type(e).__name__ for e in errors], [])

    def test_sorting_in_the_background_while_undoing_stays_consistent(self):
        """The queue thread and the interface thread sharing one journal.

        Before the store took a lock this raised `pending_block_write`, and the
        two threads' atomic writes clobbered each other's temporary file.

        The first move is let through alone and the rest are held back, so at
        least one undo really runs while sorting is still queued; without that
        the loop could finish before the queue had done anything to undo.
        """
        engine, source, binding = self.build()
        before = digests(self.tmp)
        first_done = threading.Event()
        release = threading.Event()

        def sort(path, progress, cancel, first):
            if not first:
                release.wait(10)
            try:
                return engine.classify(binding, path, progress=progress, cancel=cancel)
            finally:
                if first:
                    first_done.set()

        for index, path in enumerate(sorted(source.iterdir())):
            engine.enqueue("sort", lambda progress, cancel, path=path, first=index == 0:
                           sort(path, progress, cancel, first),
                           {"path": str(path)})
        self.addCleanup(release.set)
        self.assertTrue(first_done.wait(10), "the queue never ran the first move")
        errors: list[str] = []
        undone = 0
        for attempt in range(20):
            if attempt == 1:
                release.set()
            try:
                if engine.can_undo() and not engine.undo().skipped:
                    undone += 1
            except TransactionError as error:
                errors.append(str(error))
        release.set()
        self.assertTrue(engine.queue.wait_idle(30), "the queue did not finish")
        failures = [str(job.error) for job in engine.queue.failures()]
        self.assertEqual(failures, [], "a queued operation failed")
        self.assertEqual(errors, [], "an interactive operation was refused")
        self.assertGreaterEqual(undone, 1, "no undo ran while sorting was queued")
        self.assertFalse(engine.store.has_pending(), "a journal was left behind")
        for places in digests(self.tmp).values():
            self.assertEqual(len(places), 1, f"the same content is in {places}")
        assert_each_content_once(self, self.tmp, before)

    def test_a_mutation_started_inside_another_is_refused(self):
        """`processEvents` can dispatch a key press mid-transaction.

        A reentrant lock would wave that second operation through, because it
        arrives on the same thread as the first.
        """
        engine, source, binding = self.build()
        seen: list[str] = []

        def meddle(_message, _percent):
            if seen:
                return
            seen.append("tried")
            with self.assertRaises(TransactionError):
                engine.classify(binding, source / "img1.jpg")

        engine.classify(binding, source / "img0.jpg", progress=meddle)
        self.assertEqual(seen, ["tried"], "the progress hook never ran")
        self.assertTrue((source / "img1.jpg").exists(), "the nested move went through")


class UndoIndexTests(TempCase):
    """Undo must put files back in the stem index without a rescan.

    A stale index after undo means the next move takes the photograph and
    leaves its raw behind, and nothing on screen says so.
    """

    def setUp(self):
        super().setUp()
        import time
        from PIL import Image
        self.library = self.tmp / "library"
        self.library.mkdir()
        seed = self.tmp / "seed.jpg"
        Image.new("RGB", (48, 32), (30, 60, 90)).save(seed, quality=50)
        blob = seed.read_bytes()
        for index in range(12):
            (self.library / f"IMG_{index:05d}.JPG").write_bytes(blob)
        for index in range(4):
            (self.library / f"IMG_{index:05d}.CR2").write_bytes(b"raw" * 40)
        # The index distrusts a folder touched moments ago; a real one is older.
        when = time.time() - 300
        os.utime(self.library, (when, when))
        self.settings = config.Settings()
        self.settings.bindings[0].folder = str(self.tmp / "Keepers")
        self.settings.bindings[0].name_template = "{name}"
        self.engine = Engine(self.data, self.settings)
        self.addCleanup(self.engine.close)
        self.engine.open_folder(self.library)

    def test_an_undo_puts_the_files_back_in_the_index_without_a_rescan(self):
        target = self.library / "IMG_00002.JPG"
        self.engine.go_to(target)
        self.engine.classify(self.settings.bindings[0], target)
        self.engine.undo()
        self.assertEqual(2, self.engine.group_for(target).count)

    def test_after_undo_the_raw_still_travels_with_the_photo(self):
        target = self.library / "IMG_00002.JPG"
        self.engine.go_to(target)
        self.engine.classify(self.settings.bindings[0], target)
        self.engine.undo()
        self.engine.classify(self.settings.bindings[0], target)
        self.assertFalse(target.with_suffix(".CR2").exists(), "the raw stayed behind")
        self.assertTrue((self.tmp / "Keepers" / "IMG_00002.CR2").exists())


class NonAsciiNameTests(TempCase):
    """Chinese and emoji names through a crash, recovery and undo."""

    def test_non_ascii_names_survive_a_crash_recover_and_undo(self):
        from qingjian.core.safestore import Plan, SafeStore, identity, step_move
        src, dst = self.tmp / "源", self.tmp / "目标"
        first = self.write(src / "照片_🌅.JPG", "jpg-内容".encode() * 300)
        second = self.write(src / "照片_🌅.CR2", "raw-内容".encode() * 300)
        plan = Plan(forward=[step_move(first, dst / first.name, identity(first)),
                             step_move(second, dst / second.name, identity(second))])
        plan.inverse = SafeStore.invert(plan.forward)
        # A real kill runs no exception handler, so the process really exits
        # between the two moves.
        (self.tmp / "plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False),
                                            encoding="utf-8")
        script = (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "from qingjian.core.safestore import Plan, SafeStore\n"
            "tmp = Path(sys.argv[1])\n"
            "plan = Plan.from_dict(json.loads((tmp / 'plan.json').read_text(encoding='utf-8')))\n"
            "def die(_name, percent):\n"
            "    if percent > 0:\n"
            "        os._exit(9)\n"
            "SafeStore(tmp / 'data' / 'store').run(plan, progress=die)\n"
            "os._exit(0)\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run([sys.executable, "-c", script, str(self.tmp)], env=env,
                              capture_output=True, timeout=60)
        self.assertEqual(9, done.returncode, done.stderr.decode(errors="replace")[-400:])
        self.assertEqual(1, len(list(dst.iterdir())), "the crash was not between the moves")
        restarted = SafeStore(self.data / "store")
        self.assertTrue(restarted.has_pending(), "the crash left no journal to recover from")
        self.assertTrue(restarted.recover())
        self.assertFalse(restarted.has_pending())
        self.assertEqual(["照片_🌅.CR2", "照片_🌅.JPG"], sorted(p.name for p in dst.iterdir()))
        undo = Plan(forward=plan.inverse)
        undo.inverse = plan.forward
        restarted.run(undo)
        self.assertEqual("jpg-内容".encode() * 300, first.read_bytes())
        self.assertEqual("raw-内容".encode() * 300, second.read_bytes())
        self.assertEqual([], self.tree(dst))


class AtomicWriteTests(TempCase):
    """The journal is one file; writing it from two threads must still work."""

    def test_concurrent_writers_do_not_destroy_each_other(self):
        target = self.tmp / "journal.json"
        errors: list[BaseException] = []

        def write(index: int) -> None:
            try:
                for round_ in range(40):
                    safestore.atomic_json(target, {"who": index, "round": round_})
            except BaseException as error:      # noqa: BLE001 - recorded, not raised
                errors.append(error)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([type(e).__name__ for e in errors], [])
        # Whatever landed last must still be a whole, readable document.
        self.assertIn("who", json.loads(target.read_text(encoding="utf-8")))
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.endswith(".writing")]
        self.assertEqual(leftovers, [])


class TransitionCostTests(unittest.TestCase):
    """Undo must not pay the price of opening the folder again."""

    @staticmethod
    def _method(path: Path, klass: str, method: str):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == klass:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == method:
                        return item
        raise AssertionError(f"{klass}.{method} not found in {path.name}")

    @staticmethod
    def _calls(node: ast.AST) -> set[str]:
        names = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                owner = child.func.value
                if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) \
                        and owner.value.id == "self":
                    names.add(f"{owner.attr}.{child.func.attr}")
        return names

    def test_undo_and_redo_do_not_walk_the_folder(self):
        """`rescan` re-lists every file and drops every parsed header."""
        source = PACKAGE / "ui" / "mainwindow.py"
        calls = self._calls(self._method(source, "MainWindow", "_transition"))
        self.assertNotIn("engine.rescan", calls,
                         "undo re-scans the whole source folder")
        self.assertIn("engine.absorb", calls,
                      "undo no longer patches the lists from the record")

    def test_undo_waits_for_queued_sorting(self):
        tree = ast.parse((PACKAGE / "ui" / "mainwindow.py").read_text(encoding="utf-8"))
        body = ast.dump(self._method(PACKAGE / "ui" / "mainwindow.py",
                                     "MainWindow", "_transition"))
        self.assertIn("_drain_queue", body,
                      "undo can start while a queued move is still running")
        self.assertTrue(tree)


if __name__ == "__main__":
    unittest.main()


class AbsorbTests(TempCase):
    """Undo patches the lists instead of re-reading the folder."""

    _runs = 0

    def build(self, sort_mode: str = "name", count: int = 12):
        # A subTest loop builds several libraries in one test; keep them apart.
        AbsorbTests._runs += 1
        home = self.tmp / f"run{AbsorbTests._runs}"
        source = home / "src"
        source.mkdir(parents=True)
        for index in range(count):
            self.write(source / f"img{index:02d}.jpg", bytes([65 + index]) * (400 + index))
        settings = config.Settings()
        settings.source_folder = str(source)
        settings.sort_mode = sort_mode
        engine = Engine(data_dir=home / "app", settings=settings)
        engine.open_folder(source)
        self.addCleanup(engine.close)
        return engine, source, config.Binding(key="1", action="move",
                                              folder=str(home / "f1"))

    @staticmethod
    def sort_one(engine, binding, target):
        """What pressing a binding key does: drop the row, then move the file.

        Doing it any other way makes these tests pass for the wrong reason --
        the row is still in the queue, so "it came back" proves nothing.
        """
        engine.queue_paths.remove(target)
        return engine.classify(binding, target)

    def test_a_restored_file_lands_where_a_full_sort_would_put_it(self):
        engine, source, binding = self.build()
        expected = list(engine.queue_paths)
        target = source / "img05.jpg"
        self.sort_one(engine, binding, target)
        self.assertNotIn(target, engine.queue_paths)
        outcome = engine.undo()
        change = engine.absorb(outcome.record)
        self.assertIsNotNone(change)
        self.assertEqual([path for _where, path in change["added"]], [target])
        self.assertEqual(engine.queue_paths, expected)
        self.assertIn(target, engine.all_files)

    def test_the_same_holds_with_the_order_reversed(self):
        engine, source, binding = self.build()
        engine.settings.sort_reverse = True
        engine.rebuild_queue()
        expected = list(engine.queue_paths)
        target = source / "img07.jpg"
        self.sort_one(engine, binding, target)
        outcome = engine.undo()
        self.assertIsNotNone(engine.absorb(outcome.record))
        self.assertEqual(engine.queue_paths, expected)

    def test_every_sort_order_agrees_with_a_full_rebuild(self):
        """A bisect into a list sorted by another key lands anywhere.

        Sorting by rating reads the tag table, so a key built from only the
        touched file's rating scored every existing row as unrated.
        """
        for mode in ("name", "date", "modified", "size", "rating"):
            with self.subTest(mode=mode):
                engine, source, binding = self.build(sort_mode=mode)
                for index, path in enumerate(sorted(source.iterdir())):
                    engine.tag([path], rating=(index % 5) + 1)
                engine.rebuild_queue()
                expected = list(engine.queue_paths)
                target = expected[len(expected) // 2]
                self.sort_one(engine, binding, target)
                outcome = engine.undo()
                self.assertIsNotNone(engine.absorb(outcome.record))
                self.assertEqual(engine.queue_paths, expected,
                                 f"{mode} order disagrees with a full sort")

    def test_undoing_a_skip_brings_the_row_back(self):
        """The file never moved; only its place in the done list changed."""
        engine, source, _binding = self.build()
        target = source / "img02.jpg"
        engine.queue_paths.remove(target)
        engine.skip(target)
        outcome = engine.undo()
        change = engine.absorb(outcome.record)
        self.assertIsNotNone(change)
        self.assertIn(target, engine.queue_paths)

    def test_undoing_a_copy_brings_the_row_back(self):
        engine, source, _binding = self.build()
        copy = config.Binding(key="3", action="copy",
                                     folder=str(source.parent / "f3"))
        target = source / "img03.jpg"
        engine.queue_paths.remove(target)
        engine.classify(copy, target)
        outcome = engine.undo()
        self.assertIsNotNone(engine.absorb(outcome.record))
        self.assertIn(target, engine.queue_paths)
        self.assertFalse((source.parent / "f3" / "img03.jpg").exists())

    def test_an_action_that_leaves_the_file_alone_still_takes_the_row_out(self):
        """Copy, favourite and skip do not move anything; they change the list.

        Synchronous mode no longer rebuilds the queue, so absorb has to notice.
        It did not, and holding the key down copied the same photograph over
        and over while the queue never moved on.
        """
        for action in ("copy", "favorite", "skip"):
            with self.subTest(action=action):
                engine, source, _binding = self.build()
                binding = config.Binding(key="1", action=action,
                                         folder=str(source.parent / "out"))
                target = engine.queue_paths[2]
                outcome = engine.classify(binding, target)
                change = engine.absorb(outcome.record)
                self.assertIsNotNone(change, "absorb gave up and forced a rebuild")
                self.assertIn(target, [p for p in change["removed"]])
                self.assertNotIn(target, engine.queue_paths)
                self.assertTrue(target.exists(), "the original was moved, not marked")

    def test_it_agrees_with_a_full_rebuild_for_those_actions(self):
        for action in ("copy", "favorite", "skip"):
            with self.subTest(action=action):
                engine, source, _binding = self.build()
                binding = config.Binding(key="1", action=action,
                                         folder=str(source.parent / "out"))
                outcome = engine.classify(binding, engine.queue_paths[2])
                engine.absorb(outcome.record)
                patched = list(engine.queue_paths)
                engine.rebuild_queue()
                self.assertEqual(patched, engine.queue_paths)

    def test_a_file_the_filter_excludes_is_not_forced_back(self):
        """Undo restores the file, not a row the current filter rejects."""
        engine, source, binding = self.build()
        target = source / "img04.jpg"
        self.sort_one(engine, binding, target)
        engine.settings.filter_mode = "videos"
        outcome = engine.undo()
        change = engine.absorb(outcome.record)
        self.assertIsNotNone(change)
        self.assertEqual(change["added"], [])
        self.assertTrue(target.exists())

    def test_absorbing_does_not_list_the_folder(self):
        """The record already names the files; walking the tree is the cost."""
        engine, source, binding = self.build()
        engine.classify(binding, source / "img03.jpg")
        outcome = engine.undo()
        listings = []
        real = Path.iterdir

        def counted(self):
            listings.append(str(self))
            return real(self)

        Path.iterdir = counted
        try:
            self.assertIsNotNone(engine.absorb(outcome.record))
        finally:
            Path.iterdir = real
        self.assertEqual(listings, [], f"the folder was listed: {listings}")

    def test_a_sort_it_cannot_reproduce_falls_back(self):
        """Random order has no insert point, so the caller must rebuild."""
        engine, source, binding = self.build(sort_mode="random")
        engine.classify(binding, source / "img02.jpg")
        outcome = engine.undo()
        self.assertIsNone(engine.absorb(outcome.record))

    def test_review_mode_falls_back(self):
        engine, source, binding = self.build()
        engine.classify(binding, source / "img02.jpg")
        outcome = engine.undo()
        engine.set_review_mode(True)
        self.assertIsNone(engine.absorb(outcome.record))

    def test_redo_takes_the_file_back_out(self):
        engine, source, binding = self.build()
        target = source / "img04.jpg"
        self.sort_one(engine, binding, target)
        outcome = engine.undo()
        engine.absorb(outcome.record)
        self.assertIn(target, engine.queue_paths)
        outcome = engine.redo()
        change = engine.absorb(outcome.record)
        self.assertIsNotNone(change)
        self.assertNotIn(target, engine.queue_paths)
        self.assertNotIn(target, engine.all_files)

    def test_a_sidecar_does_not_add_a_second_row(self):
        """A restored raw joins the jpeg already in the queue, not beside it."""
        engine, source, binding = self.build()
        self.write(source / "img06.arw", b"raw payload")
        engine.rescan()
        before = len(engine.queue_paths)
        self.sort_one(engine, binding, source / "img06.jpg")
        outcome = engine.undo()
        engine.absorb(outcome.record)
        self.assertEqual(len(engine.queue_paths), before)


class ReentrancyGuardTests(unittest.TestCase):
    """Everything a shortcut can reach has to refuse to run mid-operation."""

    def test_every_mutating_handler_checks_the_busy_flag(self):
        tree = ast.parse((PACKAGE / "ui" / "mainwindow.py").read_text(encoding="utf-8"))
        window = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                      and node.name == "MainWindow")
        methods = {node.name: node for node in window.body
                   if isinstance(node, ast.FunctionDef)}
        shortcuts = methods["_build_shortcuts"]
        handlers = {node.attr for node in ast.walk(shortcuts)
                    if isinstance(node, ast.Attribute) and
                    isinstance(node.value, ast.Name) and node.value.id == "self" and
                    node.attr in methods}
        handlers.discard("_shortcut")
        handlers.discard("_install_binding_shortcuts")
        readonly = {"_arrow", "_escape", "_step_frame", "toggle_fullscreen"}

        def guarded(name: str, seen: set[str]) -> bool:
            if name in readonly:
                return True
            if name in seen:
                return False
            body = methods[name].body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            if body and isinstance(body[0], ast.If):
                condition = body[0].test
                if (any(isinstance(node, ast.Attribute) and node.attr in ("_busy", "_blocked")
                        for node in ast.walk(condition)) and
                        len(body[0].body) == 1 and isinstance(body[0].body[0], ast.Return)):
                    return True
            calls = {node.func.attr for node in ast.walk(methods[name])
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and isinstance(node.func.value, ast.Name)
                     and node.func.value.id == "self" and node.func.attr in methods}
            return bool(calls) and all(guarded(target, seen | {name}) for target in calls)

        unguarded = sorted(name for name in handlers if not guarded(name, set()))
        self.assertEqual(unguarded, [], f"unguarded handlers: {unguarded}")

    def test_the_grid_stays_dirty_until_its_build_finishes(self):
        """A row inserted into a half-built grid puts it out of step for good."""
        text = (PACKAGE / "ui" / "mainwindow.py").read_text(encoding="utf-8")
        start = text.index("def _fill_grid(self)")
        body = text[start:text.index("def _decorations(self")]
        self.assertIn("_grid_building", body)
        self.assertNotIn("self._grid_dirty = False\n        paths", body,
                         "the dirty flag is cleared before the build runs")

    def test_the_strip_is_never_redrawn_with_a_partial_decoration_map(self):
        """set_paths replaces what the strip holds, so a short map wipes badges."""
        text = (PACKAGE / "ui" / "mainwindow.py").read_text(encoding="utf-8")
        for line, content in enumerate(text.splitlines(), 1):
            if "filmstrip.set_queue" in content:
                window = "\n".join(text.splitlines()[line - 1:line + 2])
                self.assertNotIn("self._decorations([path])", window,
                                 f"line {line} passes a one-file decoration map")
