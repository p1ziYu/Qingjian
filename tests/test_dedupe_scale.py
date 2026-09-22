"""What the duplicate scan is allowed to cost.

Finding duplicates means touching every file, so the only things that keep it
usable are: read as little of each file as the answer needs, remember what was
read, and never do any of it on the thread that paints the window.
"""
from __future__ import annotations

import ast
import hashlib
import os
import threading
from collections import defaultdict
from pathlib import Path

from base import ROOT, TempCase, unittest
from qingjian.core import dedupe, safestore
from qingjian.core.hashcache import HashCache
from qingjian.core.safestore import Cancelled

PACKAGE = ROOT / "qingjian"


class ExactPrefilterTests(TempCase):
    """Byte-identical files, without reading the library end to end."""

    def test_one_file_under_two_names_is_not_reclaimable(self):
        folder = self.tmp / "links"
        original = self.write(folder / "shot.jpg", b"L" * (16 << 10))
        alias = folder / "alias.jpg"
        try:
            os.link(original, alias)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("hard links unavailable")
        self.assertEqual([], dedupe.find_exact([original, alias]))

    def library(self) -> list[Path]:
        """Files that share sizes without sharing content, plus real twins."""
        folder = self.tmp / "lib"
        folder.mkdir()
        paths = []
        for index in range(12):
            # Same length, different content: the size filter cannot separate
            # these, so before the sample pass every one was read in full.
            body = bytes([index]) + b"\x00" * (2 << 20) + bytes([index])
            paths.append(self.write(folder / f"same_size_{index}.jpg", body))
        twin = b"\x7f" * (2 << 20)
        paths.append(self.write(folder / "twin_a.jpg", twin))
        paths.append(self.write(folder / "twin_b.jpg", twin))
        paths.append(self.write(folder / "nested" / "twin_c.jpg", twin))
        return paths

    def counted(self, paths):
        """Run a scan, recording how many bytes each pass read."""
        read = {"full": 0, "sample": 0, "bytes": 0}
        real_full, real_sample = dedupe.fingerprint, dedupe.sample_digest

        def full(path, *args, **kwargs):
            read["full"] += 1
            read["bytes"] += Path(path).stat().st_size
            return real_full(path, *args, **kwargs)

        def sample(path):
            read["sample"] += 1
            read["bytes"] += min(2 * safestore.SAMPLE, Path(path).stat().st_size)
            return real_sample(path)

        dedupe.fingerprint, dedupe.sample_digest = full, sample
        try:
            groups = dedupe.find_exact(paths)
        finally:
            dedupe.fingerprint, dedupe.sample_digest = real_full, real_sample
        return groups, read

    def test_every_byte_identical_set_is_still_found(self):
        paths = self.library()
        groups, _read = self.counted(paths)
        truth = defaultdict(list)
        for path in paths:
            truth[hashlib.sha256(path.read_bytes()).hexdigest()].append(path)
        expected = {frozenset(v) for v in truth.values() if len(v) > 1}
        found = {frozenset(m.path for m in g.members) for g in groups}
        self.assertEqual(found, expected)

    def test_files_that_only_share_a_size_are_never_read_in_full(self):
        """The head-and-tail digest is what keeps a raw library off the disk."""
        paths = self.library()
        _groups, read = self.counted(paths)
        self.assertEqual(read["full"], 3, "a file with no twin was hashed in full")
        self.assertGreater(read["sample"], 3, "the cheap pass did not run")
        on_disk = sum(p.stat().st_size for p in paths)
        self.assertLess(read["bytes"], on_disk / 2,
                        f"read {read['bytes']} of {on_disk} bytes")

    def test_files_below_the_floor_are_left_alone(self):
        """Icons and web thumbnails are not what anyone opens this for."""
        folder = self.tmp / "small"
        folder.mkdir()
        tiny = b"x" * 64
        first = self.write(folder / "a.jpg", tiny)
        second = self.write(folder / "b.jpg", tiny)
        self.assertEqual(dedupe.find_exact([first, second]), [])

    def test_a_second_scan_reads_nothing(self):
        paths = self.library()
        cache = HashCache(self.tmp / "hashes.db")
        self.addCleanup(cache.close)
        dedupe.find_exact(paths, cache)
        real = dedupe.fingerprint
        calls = []

        def counted(path, *args, **kwargs):
            calls.append(path)
            return real(path, *args, **kwargs)

        dedupe.fingerprint = counted
        try:
            again = dedupe.find_exact(paths, cache)
        finally:
            dedupe.fingerprint = real
        self.assertEqual(calls, [], "the cached digests were not used")
        self.assertEqual(len(again), 1)


    def test_same_head_and_tail_different_middle_is_not_a_duplicate(self):
        """Two raws that a head-and-tail sample cannot tell apart.

        Cameras write the same header and footer into every file, so a scan
        that samples the ends calls whole cards full of duplicates.
        """
        head = bytes(range(256)) * 256                  # 64 KB
        middle = b"\x00" * (3 << 20)
        first = self.write(self.tmp / "a.cr2", head + middle + head)
        second = self.write(self.tmp / "b.cr2", head + middle[:-1] + b"\x01" + head)
        self.assertEqual(safestore.sample_digest(first), safestore.sample_digest(second),
                         "the sample already tells them apart, so this proves nothing")
        self.assertEqual([], dedupe.find_exact([first, second]))


class CachePressureTests(TempCase):
    """The cache must not flush to disk once per photograph."""

    def test_a_batch_commits_once(self):
        """Twenty thousand rows used to mean twenty thousand disk flushes."""
        cache = HashCache(self.tmp / "hashes.db")
        self.addCleanup(cache.close)
        paths = [self.write(self.tmp / f"f{i}.jpg", b"x" * (100 + i)) for i in range(50)]
        statements = []
        cache._db.set_trace_callback(statements.append)
        with cache.batch():
            for path in paths:
                cache.put(path, phash=1234)
        cache._db.set_trace_callback(None)
        commits = [s for s in statements if s.strip().upper().startswith("COMMIT")]
        self.assertEqual(len(commits), 1, f"{len(commits)} commits for 50 rows")
        self.assertEqual(cache.get(paths[0], "phash"), 1234)

    def test_without_a_batch_every_row_is_flushed(self):
        """The guard above only means something if the default really differs."""
        cache = HashCache(self.tmp / "hashes.db")
        self.addCleanup(cache.close)
        paths = [self.write(self.tmp / f"h{i}.jpg", b"x" * (100 + i)) for i in range(10)]
        statements = []
        cache._db.set_trace_callback(statements.append)
        for path in paths:
            cache.put(path, phash=1)
        cache._db.set_trace_callback(None)
        commits = [s for s in statements if s.strip().upper().startswith("COMMIT")]
        self.assertEqual(len(commits), 10)

    def test_preload_reads_the_folder_in_one_pass(self):
        path = self.tmp / "hashes.db"
        cache = HashCache(path)
        paths = [self.write(self.tmp / f"g{i}.jpg", b"y" * (100 + i)) for i in range(30)]
        with cache.batch():
            for item in paths:
                cache.put(item, phash=7)
        cache.close()

        fresh = HashCache(path)
        self.addCleanup(fresh.close)
        statements = []
        fresh._db.set_trace_callback(statements.append)
        self.assertEqual(fresh.preload(paths), 30)
        fresh._db.set_trace_callback(None)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 1, f"{len(selects)} queries for 30 files")
        # And the rows are usable afterwards without touching the file again.
        statements.clear()
        fresh._db.set_trace_callback(statements.append)
        self.assertEqual(fresh.get(paths[5], "phash"), 7)
        fresh._db.set_trace_callback(None)
        self.assertEqual(statements, [], "a preloaded row still hit the database")


class CancellationTests(TempCase):
    """A scan the user stopped must stop."""

    def test_a_stopped_scan_raises_rather_than_finishing(self):
        folder = self.tmp / "lib"
        folder.mkdir()
        paths = [self.write(folder / f"f{i}.jpg", bytes([i]) * 20000) for i in range(40)]
        stop = threading.Event()
        seen = []

        def progress(_message, _percent):
            seen.append(1)
            if len(seen) >= 3:
                stop.set()

        with self.assertRaises(Cancelled):
            dedupe.find_exact(paths + paths, progress=progress, cancel=stop.is_set)


class ScanThreadingTests(unittest.TestCase):
    """The window must stay alive while the scan runs."""

    @staticmethod
    def _method(path: Path, klass: str, method: str):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == klass:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == method:
                        return item
        raise AssertionError(f"{klass}.{method} not found in {path.name}")

    def test_the_dialog_never_scans_on_the_interface_thread(self):
        """`find_duplicates` may only be reached from the worker task."""
        source = PACKAGE / "ui" / "duplicates.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        callers = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, ast.FunctionDef):
                    continue
                for call in ast.walk(item):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                            and call.func.attr == "find_duplicates":
                        callers.add(f"{node.name}.{item.name}")
        self.assertEqual(callers, {"_ScanTask.run"},
                         f"the scan is also started from {sorted(callers)}")

    def test_the_scan_is_given_a_way_to_stop(self):
        body = ast.dump(self._method(PACKAGE / "ui" / "duplicates.py", "_ScanTask", "run"))
        self.assertIn("is_set", body, "the worker ignores the stop flag")

    def test_the_table_is_not_measured_cell_by_cell(self):
        """resizeColumnsToContents walks every cell of every row."""
        text = (PACKAGE / "ui" / "duplicates.py").read_text(encoding="utf-8")
        self.assertNotIn("resizeColumnsToContents", text)

    def test_the_table_is_filled_in_chunks(self):
        body = ast.dump(self._method(PACKAGE / "ui" / "duplicates.py",
                                     "DuplicatesDialog", "_fill"))
        self.assertIn("_fill_timer", body, "the whole table is built in one go")

    def test_switching_tabs_reuses_what_was_already_found(self):
        body = ast.dump(self._method(PACKAGE / "ui" / "duplicates.py",
                                     "DuplicatesDialog", "reload"))
        self.assertIn("_results", body, "every tab switch rescans the library")


if __name__ == "__main__":
    unittest.main()


class ScanLifetimeTests(unittest.TestCase):
    """Closing the window mid-scan must not strand or restart the worker."""

    @staticmethod
    def _method(klass: str, method: str):
        source = PACKAGE / "ui" / "duplicates.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == klass:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == method:
                        return item
        raise AssertionError(f"{klass}.{method} not found")

    def test_closing_cuts_the_signals_before_waiting(self):
        """A queued `cancelled` arriving after close would start a new scan.

        The pool is a child of the dialog, so Qt would then block in the
        destructor with no timeout, on the thread that paints.
        """
        body = ast.dump(self._method("DuplicatesDialog", "done"))
        self.assertIn("disconnect", body, "the scan signals outlive the dialog")
        self.assertIn("waitForDone", body, "the dialog is destroyed mid-scan")
        order = (PACKAGE / "ui" / "duplicates.py").read_text(encoding="utf-8")
        cut = order.index("def done(self, result: int)")
        tail = order[cut:]
        self.assertLess(tail.index("disconnect"), tail.index("waitForDone"),
                        "the signals are cut only after the wait")

    def test_a_scan_is_not_started_while_closing(self):
        for name in ("_start_scan", "_catch_up"):
            with self.subTest(method=name):
                self.assertIn("_closing", ast.dump(self._method("DuplicatesDialog", name)),
                              f"{name} can start a scan on a dialog being closed")

    def test_stopping_does_not_restart_the_same_mode(self):
        body = ast.dump(self._method("DuplicatesDialog", "_catch_up"))
        self.assertIn("_abandoned", body, "Stop restarts the scan it just stopped")


class IgnoreListTests(TempCase):
    """Ignoring a group has to survive a rescan, in every mode."""

    def library(self):
        import numpy as np
        from PIL import Image

        folder = self.tmp / "lib"
        folder.mkdir()
        rng = np.random.default_rng(5)
        base = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
        picture = Image.fromarray(base).resize((1000, 750), Image.BILINEAR)
        picture.save(folder / "a.jpg", quality=88)
        picture.save(folder / "b.jpg", quality=55)
        import shutil
        shutil.copy2(folder / "a.jpg", folder / "a_copy.jpg")
        return sorted(folder.iterdir())

    def test_the_key_stored_is_the_key_looked_up(self):
        """`ignore_duplicates` falls back to the cached digest; so must the scan.

        With the exact tab caching a sha256 for every candidate and the similar
        tab reporting an empty digest, the two keys never matched and ignoring
        a near-match did nothing at all.
        """
        paths = self.library()
        cache = HashCache(self.tmp / "hashes.db")
        self.addCleanup(cache.close)
        dedupe.find_exact(paths, cache)          # the order the dialog uses
        groups = dedupe.find_similar(paths, cache=cache)
        self.assertTrue(groups)
        looked_up = {dedupe.identity_key(m.path, m.digest or "")
                     for g in groups for m in g.members}
        stored = {dedupe.identity_key(
                      m.path, m.digest or str(cache.get(m.path, "sha256") or ""))
                  for g in groups for m in g.members}
        self.assertEqual(looked_up, stored)
        self.assertEqual(dedupe.find_similar(paths, cache=cache, ignored=stored), [])

    def test_bursts_honour_the_ignore_list(self):
        import shutil
        from datetime import datetime, timedelta

        import numpy as np
        from PIL import Image

        folder = self.tmp / "burst"
        folder.mkdir()
        rng = np.random.default_rng(9)
        base = rng.integers(0, 255, (60, 80, 3), dtype=np.uint8)
        start = datetime(2024, 3, 2, 10, 0, 0)
        paths = []
        for index in range(5):
            when = start + timedelta(seconds=index)
            exif = Image.Exif()
            exif[36867] = when.strftime("%Y:%m:%d %H:%M:%S")
            exif[306] = when.strftime("%Y:%m:%d %H:%M:%S")
            frame = np.clip(base.astype(int) + rng.integers(-3, 4, base.shape), 0, 255)
            path = folder / f"B_{index}.jpg"
            Image.fromarray(frame.astype("uint8")).resize(
                (1000, 750), Image.BILINEAR).save(path, quality=85, exif=exif)
            paths.append(path)
        self.assertTrue(shutil.which is not None)

        found = dedupe.find_bursts(paths, minimum=3)
        self.assertTrue(found, "no burst was detected to ignore")
        keys = {dedupe.identity_key(m.path, m.digest or "")
                for g in found for m in g.members}
        self.assertEqual(dedupe.find_bursts(paths, minimum=3, ignored=keys), [])



class G09DedupeTests(TempCase):
    def test_f062_old_files_do_not_break_dedupe_or_filter(self):
        import numpy as np
        from PIL import Image
        from qingjian.core import metadata, scanner

        metadata.clear_cache()
        noise = np.random.default_rng(5).integers(0, 256, (128, 128, 3), dtype=np.uint8)
        exif = Image.Exif()
        exif[306] = '1970:01:01 00:00:00'
        a, b = self.tmp / 'a.jpg', self.tmp / 'b.jpg'
        Image.fromarray(noise).save(a, exif=exif)
        b.write_bytes(a.read_bytes())
        self.assertEqual(len(dedupe.find_exact([a, b])), 1)
        self.assertEqual(len(dedupe.find_similar([a, b])), 1)
        self.assertEqual(len(dedupe.find_bursts([a, b], minimum=2)), 1)
        old = self.tmp / 'old.png'
        Image.new('RGB', (64, 48)).save(old)
        os.utime(old, (-34560000, -34560000))
        self.assertIsNotNone(metadata.read(old, use_cache=False).captured)
        scanner.apply_filter([old], scanner.FilterSpec(mode='portrait'))
