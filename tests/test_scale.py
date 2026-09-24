"""Guards against the costs that made a large folder unusable.

Each of these is a property that regressed once already: work proportional to
the size of the folder, done at a moment when only one item is on screen.
"""
from __future__ import annotations

import importlib.util

import ast
from pathlib import Path

from base import ROOT, TempCase, unittest
from qingjian.core import config, scanner
from qingjian.core.engine import Engine

PACKAGE = ROOT / "qingjian"




class ScanCostTests(TempCase):
    """Asking each file about itself is a system call per file.

    On Windows every one of those opens the file. Twenty thousand photographs
    took five seconds to scan and another two and a half to filter, when the
    directory listing already carries the same answer.
    """

    def setUp(self):
        super().setUp()
        self.library = self.tmp / "library"
        for folder in ("", "Day1", "Day2"):
            for index in range(40):
                self.write(self.library / folder / f"IMG_{index:03d}.JPG", b"x")

    def count_file_queries(self) -> dict:
        counted = {"n": 0}
        originals = {name: getattr(Path, name)
                     for name in ("is_file", "is_symlink", "is_dir", "exists", "stat", "lstat")}

        def counting(real):
            def wrapper(this, *args, **kwargs):
                counted["n"] += 1
                return real(this, *args, **kwargs)
            return wrapper

        for name, real in originals.items():
            setattr(Path, name, counting(real))
        self.addCleanup(lambda: [setattr(Path, name, real) for name, real in originals.items()])
        return counted

    def test_scanning_reads_the_listing_not_each_file(self):
        counted = self.count_file_queries()
        found = scanner.scan(self.library, recursive=True)
        self.assertEqual(120, len(found))
        self.assertLessEqual(counted["n"], 10, f"{counted['n']} file queries for 120 files")

    def test_filtering_lists_each_folder_instead_of_asking_each_file(self):
        paths = scanner.scan(self.library, recursive=True)
        gone = self.library / "Day1" / "IMG_007.JPG"
        gone.unlink()
        counted = self.count_file_queries()
        kept = scanner.apply_filter(paths, scanner.FilterSpec())
        self.assertEqual(119, len(kept))
        self.assertNotIn(gone, kept)
        self.assertLessEqual(counted["n"], 10, f"{counted['n']} file queries for 120 files")

    def test_a_narrowing_filter_still_drops_a_file_removed_since_the_scan(self):
        paths = scanner.scan(self.library, recursive=True)
        gone = self.library / "Day2" / "IMG_003.JPG"
        gone.unlink()
        kept = scanner.apply_filter(paths, scanner.FilterSpec(mode="images"))
        self.assertEqual(119, len(kept))
        self.assertNotIn(gone, kept)

    def test_the_hidden_recycle_folder_is_never_scanned(self):
        self.write(self.library / "Day1" / ".qingjian-trash" / "0123abcd.JPG", b"x")
        found = scanner.scan(self.library, recursive=True)
        self.assertEqual(120, len(found))
        self.assertFalse(any(".qingjian-trash" in p.parts for p in found))


class FolderWorkTests(TempCase):
    """Counting directory listings, because they are what scaled with N."""

    def setUp(self):
        super().setUp()
        self.library = self.tmp / "library"
        self.library.mkdir()
        from PIL import Image
        seed = self.tmp / "seed.jpg"
        Image.new("RGB", (48, 32), (30, 60, 90)).save(seed, quality=50)
        blob = seed.read_bytes()
        for index in range(400):
            (self.library / f"IMG_{index:05d}.JPG").write_bytes(blob)
        for index in range(20):
            (self.library / f"IMG_{index:05d}.CR2").write_bytes(b"raw" * 40)

        # Age the folder: the index deliberately distrusts a directory that
        # was touched moments ago, and every real library is older than that.
        self.age_folder()

        settings = config.Settings()
        settings.bindings[0].folder = str(self.tmp / "Keepers")
        settings.bindings[0].name_template = "{name}"
        self.settings = settings
        self.engine = Engine(self.data, settings)
        self.engine.open_folder(self.library)

    def age_folder(self, seconds: int = 300) -> None:
        import os
        import time as _time
        when = _time.time() - seconds
        os.utime(self.library, (when, when))

    def tearDown(self):
        self.engine.close()
        super().tearDown()

    def _count_listings(self):
        """Patch Path.iterdir so the test can see how often a folder is read."""
        original = Path.iterdir
        seen = {"n": 0}

        def counting(this):
            seen["n"] += 1
            return original(this)

        Path.iterdir = counting
        return seen, original

    def test_showing_many_items_lists_the_folder_once(self):
        seen, original = self._count_listings()
        try:
            for _ in range(50):
                path = self.engine.current_path()
                self.engine.group_for(path)
                self.engine.step(1)
        finally:
            Path.iterdir = original
        self.assertLessEqual(seen["n"], 1, f"{seen['n']} directory listings for 50 items")

    def test_classifying_does_not_relist_the_folder_each_time(self):
        seen, original = self._count_listings()
        try:
            for _ in range(20):
                path = self.engine.current_path()
                if path is None:
                    break
                self.engine.classify(self.settings.bindings[0], path)
                following = self.engine.current_path()
                if following is not None:
                    self.engine.group_for(following)
        finally:
            Path.iterdir = original
        # One listing for the source folder is fine; one per operation is not.
        self.assertLessEqual(seen["n"], 3, f"{seen['n']} directory listings for 20 operations")

    def test_companions_still_travel_after_the_index_is_updated(self):
        """The index is maintained rather than rebuilt, so prove it stays right."""
        target = self.library / "IMG_00000.JPG"
        self.engine.go_to(target)
        outcome = self.engine.classify(self.settings.bindings[0], target)
        self.assertEqual(2, len(outcome.record.paths))
        moved = sorted(p.name for p in (self.tmp / "Keepers").iterdir())
        self.assertEqual(["IMG_00000.CR2", "IMG_00000.JPG"], moved)
        # And a later shot in the same folder is still grouped correctly.
        later = self.library / "IMG_00001.JPG"
        self.assertEqual(2, self.engine.group_for(later).count)

    def test_an_undo_puts_the_files_back_in_the_index(self):
        target = self.library / "IMG_00002.JPG"
        self.engine.go_to(target)
        self.engine.classify(self.settings.bindings[0], target)
        self.assertEqual(1, self.engine.group_for(target).count)   # gone, nothing beside it
        self.engine.undo()
        self.engine.rescan()
        self.assertEqual(2, self.engine.group_for(target).count)

    def test_the_snapshot_area_is_not_walked_on_every_query(self):
        seen, original = self._count_listings()
        try:
            for _ in range(30):
                self.engine.backup_usage()
        finally:
            Path.iterdir = original
        self.assertLessEqual(seen["n"], 1, f"{seen['n']} listings for 30 usage queries")

    def test_a_sidecar_written_after_the_scan_is_still_found(self):
        """A card importer can drop the raw in seconds after the jpeg.

        The index must notice, or the photograph moves and its raw stays put —
        the worst thing this program could do.
        """
        target = self.library / "IMG_00100.JPG"
        self.engine.go_to(target)
        self.assertEqual(1, self.engine.group_for(target).count)   # index warm, no companion
        (self.library / "IMG_00100.CR2").write_bytes(b"arrived late" * 20)
        self.assertEqual(2, self.engine.group_for(target).count)
        outcome = self.engine.classify(self.settings.bindings[0], target)
        moved = sorted(p.name for p in (self.tmp / "Keepers").iterdir())
        self.assertEqual(["IMG_00100.CR2", "IMG_00100.JPG"], moved)
        self.assertEqual(2, len(outcome.record.paths))

    def test_a_file_removed_outside_the_app_does_not_break_grouping(self):
        target = self.library / "IMG_00101.JPG"
        (self.library / "IMG_00101.CR2").write_bytes(b"raw" * 40)
        self.assertEqual(2, self.engine.group_for(target).count)
        (self.library / "IMG_00101.CR2").unlink()
        self.assertEqual(1, self.engine.group_for(target).count)

    def test_the_review_count_does_not_fetch_every_row(self):
        for index in range(50):
            self.engine.skip(self.library / f"IMG_{index:05d}.JPG")
        self.assertEqual(50, self.engine.state.review_count(str(self.library)))



class BindingFolderTests(TempCase):
    """Pointing a key at a folder must not re-read the library."""

    def build(self, recursive: bool = True):
        source = self.tmp / "src"
        nested = source / "already-sorted"
        nested.mkdir(parents=True)
        for index in range(12):
            self.write(source / f"img{index:02d}.jpg", bytes([65 + index]) * 500)
        for index in range(5):
            self.write(nested / f"done{index}.jpg", bytes([90 + index]) * 500)
        settings = config.Settings()
        settings.source_folder = str(source)
        settings.recursive = recursive
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        engine.open_folder(source)
        self.addCleanup(engine.close)
        return engine, source, nested

    def test_a_destination_outside_the_library_changes_nothing(self):
        """The usual case: the folder you file into is somewhere else."""
        engine, _source, _nested = self.build()
        before = list(engine.all_files)
        engine.settings.bindings[0].folder = str(self.tmp / "elsewhere")
        self.assertIsNone(engine.exclude_targets())
        self.assertEqual(engine.all_files, before)

    def test_a_destination_inside_the_library_is_taken_out_of_the_queue(self):
        engine, _source, nested = self.build()
        self.assertEqual(len(engine.all_files), 17)
        engine.settings.bindings[0].folder = str(nested)
        change = engine.exclude_targets()
        self.assertIsNotNone(change)
        self.assertEqual(len(change["removed"]), 5)
        self.assertEqual(len(engine.all_files), 12)
        self.assertFalse([p for p in engine.queue_paths if p.parent == nested])

    def test_it_does_not_list_a_single_directory(self):
        """A rescan here cost as much as reopening the folder."""
        engine, _source, nested = self.build()
        engine.settings.bindings[0].folder = str(nested)
        listings = []
        real = Path.iterdir

        def counted(self):
            listings.append(str(self))
            return real(self)

        Path.iterdir = counted
        try:
            engine.exclude_targets()
        finally:
            Path.iterdir = real
        self.assertEqual(listings, [], f"the tree was walked: {listings}")

    def test_it_resolves_each_folder_once_not_each_file(self):
        """`resolve` is a syscall; per-file it was slower than the rescan."""
        engine, _source, nested = self.build()
        engine.settings.bindings[0].folder = str(nested)
        calls = []
        real = Path.resolve

        def counted(self, *args, **kwargs):
            calls.append(str(self))
            return real(self, *args, **kwargs)

        Path.resolve = counted
        try:
            engine.exclude_targets()
        finally:
            Path.resolve = real
        # Two folders hold the files, plus the configured destinations.
        self.assertLess(len(calls), 8, f"{len(calls)} resolves for 17 files")

    def test_the_cursor_stays_on_the_same_file(self):
        engine, _source, nested = self.build()
        engine.index = 7
        staying = engine.current_path()
        engine.settings.bindings[0].folder = str(nested)
        engine.exclude_targets()
        self.assertEqual(engine.current_path(), staying)

    def test_choosing_the_source_folder_itself_empties_nothing(self):
        """The picker opens at the source folder, so one mis-click lands here.

        `scan` only ever prunes subdirectories, so a destination that *is* the
        source folder excludes nothing -- treating it as exclusion wiped the
        whole library and left the window showing "no media".
        """
        engine, source, _nested = self.build()
        before = list(engine.all_files)
        engine.settings.bindings[0].folder = str(source)
        self.assertIsNone(engine.exclude_targets())
        self.assertEqual(engine.all_files, before)

    def test_a_destination_in_another_preset_counts_too(self):
        engine, _source, nested = self.build()
        engine.settings.profiles["other"] = [
            config.Binding(key="1", action="move", folder=str(nested))]
        change = engine.exclude_targets()
        self.assertIsNotNone(change)
        self.assertEqual(len(change["removed"]), 5)

    def test_nothing_happens_when_the_scan_is_not_recursive(self):
        """A non-recursive scan never saw the nested folder to begin with."""
        engine, _source, nested = self.build(recursive=False)
        engine.settings.bindings[0].folder = str(nested)
        self.assertIsNone(engine.exclude_targets())


@unittest.skipIf(importlib.util.find_spec("PySide6") is not None,
                 "runtime Qt tests cover this contract")
class LazyLoadingTests(unittest.TestCase):
    """The browsers must not ask for a thumbnail they are not showing."""

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

    def test_populating_a_list_does_not_decode_anything(self):
        """set_paths runs once per file; a decode there is a decode per file."""
        source = PACKAGE / "ui" / "browsers.py"
        calls = self._calls(self._method(source, "_Browser", "set_paths"))
        self.assertNotIn("cache.request", calls)
        self.assertNotIn("cache.peek", calls)

    def test_thumbnails_are_only_asked_for_by_the_visible_pass(self):
        source = PACKAGE / "ui" / "browsers.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        requesting = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if "cache.request" in self._calls(node):
                    requesting.add(node.name)
        self.assertEqual({"request_visible"}, requesting)

    def test_the_window_asks_the_cache_to_forget_the_rest(self):
        source = PACKAGE / "ui" / "browsers.py"
        calls = self._calls(self._method(source, "_Browser", "request_visible"))
        self.assertIn("cache.set_wanted", calls)


if importlib.util.find_spec("PySide6") is not None:
    del LazyLoadingTests


if __name__ == "__main__":
    unittest.main()
