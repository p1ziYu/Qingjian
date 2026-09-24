"""The cost of the things that happen while you are using the program.

Each case here is a measured regression: work proportional to the size of the
folder, or a full-resolution decode, on the thread that paints the window.
"""
from __future__ import annotations

import importlib.util

import ast
import itertools
import tempfile
from pathlib import Path

from base import ROOT, TempCase, unittest
from qingjian.core import config, mediatypes, metadata, scanner
from qingjian.core.engine import Engine
from qingjian.core.sidecar import base_stem, group_key

PACKAGE = ROOT / "qingjian"


def reference_base_stem(path) -> str:
    """What the old Path-based implementation returned."""
    stem = Path(path).stem
    inner = Path(stem)
    if inner.suffix and inner.suffix.lower() in mediatypes.MEDIA_EXTENSIONS:
        return inner.stem
    return stem


def reference_group_key(path) -> tuple[str, str]:
    target = Path(path)
    return (str(target.parent).casefold(), reference_base_stem(target).casefold())


class StemTests(unittest.TestCase):
    """Grouping keys moved off Path; they must answer identically.

    These strings key the folder index and the sidecar grouping, so a changed
    answer means files stop travelling with their raw companion.
    """

    NAMES = ["IMG_0001.JPG", "IMG_0001.JPG.xmp", "IMG_0001.CR2", "IMG_0001.jpg.aae",
             "a.b.c.jpg", ".hidden", ".hidden.jpg", "..hidden.jpg", "no_extension",
             "two..dots.jpg", "MiXeD.JpG.XmP", "trailing.", "a.", "a..", "a.b.",
             "x.JPG.", "IMG.jpg.JPG", "IMG 0002 (1).jpg", "漢字テスト.jpg",
             "IMG_0001.MOV.xmp", "x.mp4", "y.MP4.thm", "name.with.many.parts.arw"]
    FOLDERS = ["/photos", "/photos/2024", "/", "relative", "", "/a/b c/d", "sub/dir"]

    def test_it_matches_the_pathlib_version_exactly(self):
        for folder, name in itertools.product(self.FOLDERS, self.NAMES):
            text = f"{folder}/{name}" if folder else name
            for probe in (text, Path(text)):
                with self.subTest(path=probe):
                    self.assertEqual(base_stem(probe), reference_base_stem(probe))
                    self.assertEqual(group_key(probe), reference_group_key(probe))

    def test_it_builds_no_path_objects(self):
        """Three Paths per file was most of the cost of collapsing a folder."""
        made = []
        real = Path.__init__

        def counted(self, *args, **kwargs):
            made.append(1)
            return real(self, *args, **kwargs)

        Path.__init__ = counted
        try:
            group_key("/photos/IMG_0001.JPG.xmp")
            base_stem("/photos/IMG_0001.JPG")
        finally:
            Path.__init__ = real
        self.assertEqual(made, [], f"{len(made)} Path objects built")


class FilterFastPathTests(TempCase):
    """The pass-through filter must agree with the general one."""

    def library(self):
        folder = self.tmp / "lib"
        folder.mkdir()
        paths = []
        for index in range(9):
            paths.append(self.write(folder / f"img{index}.jpg", b"x" * (100 + index)))
        paths.append(self.write(folder / "clip.mp4", b"v" * 400))
        paths.append(self.write(folder / "shot.cr2", b"r" * 400))
        return paths

    def test_the_fast_path_keeps_the_same_files(self):
        paths = self.library()
        plain = scanner.FilterSpec(mode="all")
        self.assertTrue(plain.narrows_nothing())
        general = scanner.FilterSpec(mode="all", min_pixels=1)
        self.assertFalse(general.narrows_nothing())
        # min_pixels=1 keeps everything with known dimensions, so compare against
        # a spec that narrows nothing but does not take the fast path.
        forced = scanner.FilterSpec(mode="all", name_pattern=".")
        self.assertFalse(forced.narrows_nothing())
        self.assertEqual(scanner.apply_filter(paths, plain),
                         scanner.apply_filter(paths, forced))

    def test_the_fast_path_still_honours_the_done_list(self):
        paths = self.library()
        spec = scanner.FilterSpec(mode="all", exclude={str(paths[3])})
        self.assertTrue(spec.narrows_nothing())
        kept = scanner.apply_filter(paths, spec)
        self.assertNotIn(paths[3], kept)
        self.assertEqual(len(kept), len(paths) - 1)

    def test_the_fast_path_still_drops_files_that_are_gone(self):
        paths = self.library()
        paths[2].unlink()
        spec = scanner.FilterSpec(mode="all")
        self.assertNotIn(paths[2], scanner.apply_filter(paths, spec))

    def test_a_real_filter_is_not_taken_as_pass_through(self):
        for spec in (scanner.FilterSpec(mode="images"),
                     scanner.FilterSpec(mode="all", min_rating=3),
                     scanner.FilterSpec(mode="all", label="red"),
                     scanner.FilterSpec(mode="all", name_pattern="IMG"),
                     scanner.FilterSpec(mode="all", min_pixels=100),
                     scanner.FilterSpec(mode="all", max_pixels=100)):
            with self.subTest(spec=spec.mode):
                self.assertFalse(spec.narrows_nothing())


class CaptureTimeTests(TempCase):
    """Sorting by date must not parse every file's full metadata."""

    def library(self, count: int = 12):
        import numpy as np
        from PIL import Image
        from datetime import datetime, timedelta

        folder = self.tmp / "lib"
        folder.mkdir()
        rng = np.random.default_rng(4)
        start = datetime(2023, 7, 1, 8, 0, 0)
        paths = []
        for index in range(count):
            when = start + timedelta(hours=index)
            exif = Image.Exif()
            exif[36867] = when.strftime("%Y:%m:%d %H:%M:%S")
            exif[306] = when.strftime("%Y:%m:%d %H:%M:%S")
            base = rng.integers(0, 255, (30, 40, 3), dtype=np.uint8)
            path = folder / f"P_{index:02d}.jpg"
            Image.fromarray(base).resize((400, 300)).save(path, quality=80, exif=exif)
            paths.append(path)
        return folder, paths

    def test_the_focused_reader_agrees_with_the_full_one(self):
        _folder, paths = self.library()
        for path in paths:
            metadata.clear_cache()
            full = metadata.capture_time(path)
            metadata.clear_cache()
            quick = metadata.capture_only(path)
            self.assertIsNotNone(quick, f"{path.name} fell back")
            self.assertAlmostEqual(quick, full, places=6)

    def test_an_unreadable_file_defers_to_the_full_reader(self):
        """None means "ask the real reader", which is what the caller does."""
        plain = self.write(self.tmp / "plain.jpg", b"not really a jpeg")
        metadata.clear_cache()
        self.assertIsNone(metadata.capture_only(plain))
        metadata.clear_cache()
        self.assertGreater(metadata.capture_time(plain), 0)

    def test_an_image_without_a_date_tag_uses_its_mtime(self):
        import numpy as np
        from PIL import Image

        path = self.tmp / "undated.jpg"
        Image.fromarray(np.zeros((20, 20, 3), dtype=np.uint8)).save(path, quality=70)
        metadata.clear_cache()
        full = metadata.capture_time(path)
        metadata.clear_cache()
        quick = metadata.capture_only(path)
        self.assertIsNotNone(quick)
        self.assertAlmostEqual(quick, full, places=6)

    def test_raw_and_video_defer_to_the_full_reader(self):
        for name in ("shot.cr2", "clip.mp4"):
            with self.subTest(name=name):
                path = self.write(self.tmp / name, b"header" * 40)
                self.assertIsNone(metadata.capture_only(path))

    def test_warming_reads_the_cache_once_and_writes_once(self):
        folder, paths = self.library()
        settings = config.Settings()
        settings.source_folder = str(folder)
        settings.sort_mode = "date"
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(folder)

        statements = []
        engine.cache._db.set_trace_callback(statements.append)
        times = engine.warm_capture_times(paths)
        engine.cache._db.set_trace_callback(None)
        self.assertEqual(len(times), len(paths))
        commits = [s for s in statements if s.strip().upper().startswith("COMMIT")]
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertLessEqual(len(commits), 1, f"{len(commits)} commits for {len(paths)} files")
        self.assertLessEqual(len(selects), 1, f"{len(selects)} queries for {len(paths)} files")

    def test_a_second_warm_reads_nothing_from_the_files(self):
        folder, paths = self.library()
        settings = config.Settings()
        settings.source_folder = str(folder)
        settings.sort_mode = "date"
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(folder)
        engine.warm_capture_times(paths)
        reads = []
        real = metadata.capture_only

        def counted(path):
            reads.append(path)
            return real(path)

        metadata.capture_only = counted
        try:
            self.assertEqual(len(engine.warm_capture_times(paths)), len(paths))
        finally:
            metadata.capture_only = real
        self.assertEqual(reads, [], "cached capture times were parsed again")

    def test_sorting_by_date_does_not_stat_once_per_file(self):
        """Asking the cache per file meant a stat per file to build its key."""
        folder, paths = self.library(count=10)
        settings = config.Settings()
        settings.source_folder = str(folder)
        settings.sort_mode = "date"
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(folder)          # warms the cache
        stats = []
        real = Path.stat

        def counted(self, **kwargs):
            stats.append(str(self))
            return real(self, **kwargs)

        Path.stat = counted
        try:
            engine.rebuild_queue()
        finally:
            Path.stat = real
        # Two passes are expected and unavoidable: the filter asks whether each
        # file is still there, and the cache stats it to decide whether its
        # stored timestamp is still valid. What must not happen is a third pass,
        # which is what the sort did when it asked the cache per file instead of
        # being handed the timestamps this rebuild had already loaded.
        self.assertLessEqual(len(stats), 2 * len(paths) + 4,
                             f"{len(stats)} stats for {len(paths)} files")


class SubfolderTests(TempCase):
    """Turning off "include subfolders" only removes files."""

    def build(self):
        source = self.tmp / "src"
        nested = source / "inner"
        nested.mkdir(parents=True)
        for index in range(6):
            self.write(source / f"top{index}.jpg", b"t" * (200 + index))
        for index in range(4):
            self.write(nested / f"deep{index}.jpg", b"d" * (200 + index))
        settings = config.Settings()
        settings.source_folder = str(source)
        settings.recursive = True
        engine = Engine(data_dir=self.tmp / "app", settings=settings)
        self.addCleanup(engine.close)
        engine.open_folder(source)
        return engine, source, nested

    def test_it_keeps_only_the_top_level(self):
        engine, source, _nested = self.build()
        self.assertEqual(len(engine.all_files), 10)
        change = engine.drop_subfolders()
        self.assertIsNotNone(change)
        self.assertEqual(len(change["removed"]), 4)
        self.assertEqual(len(engine.all_files), 6)
        self.assertTrue(all(p.parent == source for p in engine.queue_paths))

    def test_it_does_not_walk_the_tree(self):
        engine, _source, _nested = self.build()
        listings = []
        real = Path.iterdir

        def counted(self):
            listings.append(str(self))
            return real(self)

        Path.iterdir = counted
        try:
            engine.drop_subfolders()
        finally:
            Path.iterdir = real
        self.assertEqual(listings, [])

    def test_the_cursor_survives(self):
        engine, source, _nested = self.build()
        engine.go_to(source / "top3.jpg")
        staying = engine.current_path()
        engine.drop_subfolders()
        self.assertEqual(engine.current_path(), staying)


class BlockingDecodeTests(unittest.TestCase):
    """A format that cannot be scaled while decoding goes to a worker."""

    def test_the_gate_picks_the_formats_that_cannot_be_scaled_while_read(self):
        from qingjian.core import imaging as preview
        from unittest.mock import patch

        cases = {
            "big.png": (preview.HEAVY_BYTES + 1, True),
            "big.tif": (preview.HEAVY_BYTES + 1, True),
            "big.webp": (preview.HEAVY_BYTES + 1, True),
            "big.jpg": (preview.HEAVY_BYTES + 1, False),
            "big.cr2": (preview.HEAVY_BYTES + 1, False),
            "enormous.jpg": (preview.ALWAYS_HEAVY_BYTES + 1, True),
            "enormous.cr2": (preview.ALWAYS_HEAVY_BYTES + 1, True),
            "small.png": (1024, False),
            # Video never reaches this decoder: it goes to the media player,
            # which is asynchronous already. Deferring it left the preview
            # showing a placeholder that nothing would ever replace.
            "clip.mp4": (700 << 20, False),
            "clip.mov": (preview.HEAVY_BYTES + 1, False),
            "clip.mkv": (preview.ALWAYS_HEAVY_BYTES + 1, False),
            "clip.avi": (preview.HEAVY_BYTES + 1, False),
        }
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name, (size, expected) in cases.items():
                with self.subTest(name=name):
                    path = folder / name
                    path.write_bytes(b"\0" * min(size, 4096))
                    with patch.object(preview, "_file_size", return_value=size):
                        self.assertEqual(preview.decodes_slowly(path), expected)










@unittest.skipIf(importlib.util.find_spec("PySide6") is not None,
                 "runtime Qt tests cover this contract")
class AsyncPreviewContractTests(unittest.TestCase):
    """Whatever is deferred must be something a worker can actually deliver."""

    def test_only_files_the_prefetcher_accepts_are_deferred(self):
        """`request` refuses video, so deferring video hangs the preview."""
        source = (PACKAGE / "ui" / "preview.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        refused = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "request":
                for call in ast.walk(node):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                            and call.func.attr in ("is_video", "may_animate", "is_raw"):
                        refused.append(call.func.attr)
        self.assertEqual(sorted(set(refused)), ["is_video"],
                         "the worker refuses a category the gate may still defer")
        # And the gate must refuse exactly that category.
        from qingjian.core import imaging
        gate = ast.dump(ast.parse(
            (PACKAGE / "core" / "imaging.py").read_text(encoding="utf-8")))
        self.assertIn("is_video", gate)
        self.assertTrue(imaging.HEAVY_BYTES < imaging.ALWAYS_HEAVY_BYTES)

    def test_a_missed_cache_is_retried_rather_than_called_a_failure(self):
        """The target size is part of the key, so a resize mid-decode misses."""
        source = (PACKAGE / "ui" / "mainwindow.py").read_text(encoding="utf-8")
        start = source.index("def _preview_arrived(self")
        body = source[start:source.index("def _preview_target(self")]
        self.assertIn("_preview_retries", body,
                      "resizing during a heavy decode fails the file for good")

@unittest.skipIf(importlib.util.find_spec("PySide6") is not None,
                 "runtime Qt tests cover this contract")
class TableCostTests(unittest.TestCase):
    """Shared table dialogs must not measure every cell they hold."""

    def test_the_columns_are_sized_from_a_sample(self):
        text = (PACKAGE / "ui" / "dialogs.py").read_text(encoding="utf-8")
        self.assertIn("setResizeContentsPrecision", text,
                      "a long history measures every cell on open")

    def test_long_tables_are_filled_in_chunks(self):
        tree = ast.parse((PACKAGE / "ui" / "dialogs.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TableDialog":
                names = {item.name for item in node.body
                         if isinstance(item, ast.FunctionDef)}
                self.assertIn("_write_chunk", names)
                return
        raise AssertionError("TableDialog not found")

@unittest.skipIf(importlib.util.find_spec("PySide6") is not None,
                 "runtime Qt tests cover this contract")
class RebuildCostTests(unittest.TestCase):
    """Nothing may rebuild the whole queue for a change it can describe."""

    @staticmethod
    def _method(klass: str, method: str):
        source = PACKAGE / "ui" / "mainwindow.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == klass:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == method:
                        return item
        raise AssertionError(f"{klass}.{method} not found")

    def test_assigning_a_folder_to_a_key_does_not_rescan(self):
        body = ast.dump(self._method("MainWindow", "choose_binding_folder"))
        self.assertNotIn("rescan", body, "picking a destination re-reads the library")
        self.assertIn("_drop_excluded", body)

    def test_sorting_one_file_does_not_rebuild_the_queue(self):
        """Synchronous mode did a full filter and sort per key press."""
        body = ast.dump(self._method("MainWindow", "_finish_operation"))
        self.assertIn("absorb", body)

    def test_renaming_one_file_does_not_rebuild_the_queue(self):
        body = ast.dump(self._method("MainWindow", "rename_current"))
        self.assertIn("_finish_operation", body)
        self.assertNotIn("attr='absorb'", body,
                         "rename must use the finish handler's single absorb")

    def test_closing_the_duplicates_window_rebuilds_only_if_it_changed_something(self):
        body = ast.dump(self._method("MainWindow", "open_duplicates"))
        self.assertIn("changed_the_queue", body)

    def test_turning_off_subfolders_does_not_rescan(self):
        body = ast.dump(self._method("MainWindow", "_recursive_changed"))
        self.assertIn("drop_subfolders", body)

    def test_closing_the_window_drains_with_the_window_alive(self):
        body = ast.dump(self._method("MainWindow", "closeEvent"))
        self.assertIn("_drain_queue", body)
        self.assertNotIn("wait_idle", body, "closing blocks with nothing on screen")

@unittest.skipIf(importlib.util.find_spec("PySide6") is not None,
                 "runtime Qt tests cover this contract")
class NoQtBehaviorFallbackTests(unittest.TestCase):
    def test_the_preview_asks_a_worker_for_heavy_files(self):
        source = PACKAGE / "ui" / "mainwindow.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "_refresh_view":
                        body = ast.dump(item)
                        self.assertIn("decodes_slowly", body,
                                      "every image is decoded on the interface thread")
                        self.assertIn("show_loading", body)
                        return
        raise AssertionError("MainWindow._refresh_view not found")

    def test_a_worker_result_is_shown_when_it_lands(self):
        text = (PACKAGE / "ui" / "mainwindow.py").read_text(encoding="utf-8")
        self.assertIn("self.preloader.arrived.connect", text,
                      "a decode finishing on a worker never reaches the screen")


if __name__ == "__main__":
    unittest.main()


import os
import threading
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage
from qingjian.ui import preview, thumbs
from qingjian.ui.app import create_app
APP = create_app(["qingjian-tests"])

class G06Task4Tests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ["QINGJIAN_DATA_DIR"] = str(self.root / "data")

    def test_f089_setting_interest_does_not_stat_files(self):
        paths = [self.root / f"{index}.jpg" for index in range(4)]
        fetcher = preview.PreviewPrefetcher()
        self.addCleanup(fetcher.shutdown)
        with patch.object(Path, "stat", side_effect=AssertionError("unexpected stat")):
            fetcher.set_wanted(paths, QSize(800, 600))
        self.assertEqual(4, len(fetcher._wanted))

    def test_f089_replaced_path_rejects_inflight_result(self):
        path = self.root / "same.jpg"
        path.write_bytes(b"old")
        entered = threading.Event()
        release = threading.Event()
        def gated_decode(*args):
            entered.set()
            release.wait(2)
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0)
            return image, ""
        fetcher = preview.PreviewPrefetcher()
        self.addCleanup(fetcher.shutdown)
        self.addCleanup(release.set)
        arrived = []
        fetcher.arrived.connect(arrived.append)
        target = QSize(800, 600)
        with patch.object(preview, "decode_qimage", side_effect=gated_decode):
            fetcher.request(path, target)
            self.assertTrue(entered.wait(2))
            path.write_bytes(b"replacement")
            fetcher.set_wanted([path], target)
            release.set()
            fetcher._pool.waitForDone(3000)
            APP.processEvents()
        self.assertEqual({}, fetcher._cache)
        self.assertEqual([], arrived)
        self.assertEqual(set(), fetcher._pending)

    def test_f089_replaced_path_skips_queued_old_version(self):
        blocker = self.root / "blocker.jpg"
        target_path = self.root / "same.jpg"
        blocker.write_bytes(b"blocker")
        target_path.write_bytes(b"old")
        entered = threading.Event()
        release = threading.Event()
        decoded = []
        def gated_decode(path, target):
            decoded.append(Path(path))
            if Path(path) == blocker:
                entered.set()
                release.wait(2)
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0)
            return image, ""
        fetcher = preview.PreviewPrefetcher()
        self.addCleanup(fetcher.shutdown)
        self.addCleanup(release.set)
        fetcher._pool.setMaxThreadCount(1)
        target = QSize(800, 600)
        with patch.object(preview, "decode_qimage", side_effect=gated_decode):
            fetcher.request(blocker, target)
            self.assertTrue(entered.wait(2))
            fetcher.request(target_path, target, priority=0)
            target_path.write_bytes(b"replacement")
            fetcher.set_wanted([target_path], target)
            fetcher.request(target_path, target)
            release.set()
            fetcher._pool.waitForDone(3000)
            APP.processEvents()
        self.assertEqual(1, decoded.count(target_path))
        self.assertEqual(1, len(fetcher._cache))

    def test_f089_clear_rejects_inflight_result(self):
        path = self.root / "a.jpg"
        path.write_bytes(b"one")
        entered = threading.Event()
        release = threading.Event()
        def gated_decode(*args):
            entered.set()
            release.wait(2)
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0)
            return image, ""
        fetcher = preview.PreviewPrefetcher()
        with patch.object(preview, "decode_qimage", side_effect=gated_decode):
            fetcher.request(path, QSize(800, 600))
            self.assertTrue(entered.wait(2))
            fetcher.clear()
            release.set()
            fetcher._pool.waitForDone(3000)
            APP.processEvents()
        self.assertEqual(0, len(fetcher._cache))
        fetcher.shutdown()

    def test_f089_stale_queued_decodes_are_skipped(self):
        paths = [self.root / f"{name}.jpg" for name in ("running", "stale", "current")]
        for path in paths:
            path.write_bytes(b"x")
        entered = threading.Event()
        release = threading.Event()
        decoded = []
        def gated_decode(path, target):
            decoded.append(Path(path).name)
            if Path(path) == paths[0]:
                entered.set()
                release.wait(2)
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0)
            return image, ""
        fetcher = preview.PreviewPrefetcher()
        self.addCleanup(fetcher.shutdown)
        self.addCleanup(release.set)
        fetcher._pool.setMaxThreadCount(1)
        with patch.object(preview, "decode_qimage", side_effect=gated_decode):
            fetcher.request(paths[0], QSize(800, 600))
            self.assertTrue(entered.wait(2))
            fetcher.request(paths[1], QSize(800, 600))
            fetcher.set_wanted([paths[2]], QSize(800, 600))
            fetcher.request(paths[2], QSize(800, 600))
            release.set()
            fetcher._pool.waitForDone(3000)
            APP.processEvents()
        self.assertNotIn("stale.jpg", decoded)
        self.assertIn("current.jpg", decoded)
        fetcher.shutdown()

    def test_f090_moved_thumbnail_leaves_no_pending_key(self):
        path = self.root / "a.jpg"
        Image.new("RGB", (20, 20), "red").save(path)
        cache = thumbs.ThumbnailCache()
        cache.request(path, 96)
        cache._pool.waitForDone(3000)
        moved = self.root / "moved.jpg"
        os.replace(path, moved)
        APP.processEvents()
        self.assertEqual(0, cache.pending())
        cache.shutdown()


class G06Task5Tests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ["QINGJIAN_DATA_DIR"] = str(self.root / "data")

    def test_f088_key_tracks_target_height(self):
        path = self.root / "a.jpg"
        path.write_bytes(b"one")
        self.assertTrue(preview.PreviewPrefetcher._key(path, QSize(2800, 1000)) != preview.PreviewPrefetcher._key(path, QSize(2800, 2400)), "target height is absent from key")


if importlib.util.find_spec("PySide6") is not None:
    del AsyncPreviewContractTests, TableCostTests, RebuildCostTests, NoQtBehaviorFallbackTests
