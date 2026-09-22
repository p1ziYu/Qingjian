from datetime import datetime

from PIL import Image, ImageFilter

from base import TempCase, unittest
from fixtures import build_library, exif_bytes, scene
from qingjian.core import dedupe, exifread, hashcache, imaging, isobmff, mediatypes, metadata


class MediaTypeTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(mediatypes.KIND_RAW, mediatypes.kind("a.CR2"))
        self.assertEqual(mediatypes.KIND_VIDEO, mediatypes.kind("a.MP4"))
        self.assertEqual(mediatypes.KIND_IMAGE, mediatypes.kind("a.jpeg"))
        self.assertEqual(mediatypes.KIND_OTHER, mediatypes.kind("a.txt"))

    def test_raw_counts_as_an_image_but_not_a_plain_one(self):
        self.assertTrue(mediatypes.is_image("a.NEF"))
        self.assertTrue(mediatypes.is_raw("a.NEF"))
        self.assertFalse(mediatypes.is_video("a.NEF"))

    def test_the_common_raw_formats_are_covered(self):
        for ext in (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"):
            self.assertIn(ext, mediatypes.RAW_EXTENSIONS, ext)

    def test_case_is_irrelevant(self):
        self.assertTrue(mediatypes.is_media("A.JpG"))


class ExifTests(TempCase):
    def make_tiff(self, name="raw.tif", when=datetime(2026, 8, 14, 19, 42, 8)):
        path = self.tmp / name
        scene(1, (400, 300)).save(path, exif=exif_bytes(when))
        return path

    def test_tiff_exif_is_read_without_a_raw_decoder(self):
        tags = exifread.read_tiff_exif(self.make_tiff())
        self.assertEqual("ILCE-7M4", tags["Model"])
        self.assertEqual("2026:08:14 19:42:08", tags["DateTimeOriginal"])
        self.assertEqual(400, tags["ISOSpeedRatings"])

    def test_a_non_tiff_file_returns_nothing_rather_than_raising(self):
        junk = self.write(self.tmp / "junk.bin", b"not a tiff at all")
        self.assertEqual({}, exifread.read_tiff_exif(junk))

    def test_a_truncated_file_does_not_raise(self):
        path = self.make_tiff()
        data = path.read_bytes()
        self.write(self.tmp / "cut.tif", data[:64])
        exifread.read_tiff_exif(self.tmp / "cut.tif")


class IsoBmffTests(TempCase):
    def test_duration_and_size_are_read_without_a_video_library(self):
        from fixtures import _write_mp4
        clip = _write_mp4(self.tmp / "clip.mp4", 74)
        header = isobmff.read_header(clip)
        self.assertAlmostEqual(74.0, header["duration"], places=2)
        self.assertEqual(1920, header["width"])
        self.assertIsInstance(header["created"], datetime)

    def test_a_non_video_returns_nothing(self):
        junk = self.write(self.tmp / "j.bin", b"\x00" * 64)
        self.assertEqual({}, isobmff.read_header(junk))


class MetadataTests(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "lib"
        self.made = build_library(self.root)

    def test_jpeg_metadata(self):
        info = metadata.read(self.root / "Day3" / "IMG_5511.JPG")
        self.assertEqual("Sony ILCE-7M4", info.camera)
        self.assertEqual("400", info.iso)
        self.assertEqual("f/2.8", info.aperture)
        self.assertEqual("1/250", info.shutter)
        self.assertFalse(info.captured_is_fallback)

    def test_capture_time_falls_back_to_mtime(self):
        plain = self.tmp / "plain.png"
        Image.new("RGB", (10, 10)).save(plain)
        info = metadata.read(plain)
        self.assertTrue(info.captured_is_fallback)
        self.assertIsNotNone(info.captured)

    def test_video_duration_without_pyav(self):
        info = metadata.read(self.root / "Day4" / "MOV_001.MP4")
        self.assertAlmostEqual(12.0, info.duration, places=1)
        self.assertEqual("0:12", metadata.format_duration(info.duration))

    def test_orientation_helpers(self):
        self.assertTrue(metadata.read(self.root / "Day3" / "tall.JPG").is_portrait)
        self.assertTrue(metadata.read(self.root / "Day4" / "solo_01.JPG").is_landscape)

    def test_a_missing_file_reports_an_error_instead_of_raising(self):
        info = metadata.read(self.tmp / "nope.jpg")
        self.assertTrue(info.error)

    def test_a_corrupt_image_does_not_raise(self):
        broken = self.write(self.tmp / "broken.jpg", b"\xff\xd8\xff" + b"\x00" * 200)
        info = metadata.read(broken)
        self.assertEqual(mediatypes.KIND_IMAGE, info.kind)

    def test_the_cache_returns_the_same_object_until_the_file_changes(self):
        path = self.root / "Day3" / "IMG_5511.JPG"
        first = metadata.read(path)
        self.assertIs(first, metadata.read(path))
        metadata.clear_cache()
        self.assertIsNot(first, metadata.read(path))

    def test_duration_formatting(self):
        self.assertEqual("", metadata.format_duration(None))
        self.assertEqual("0:07", metadata.format_duration(7))
        self.assertEqual("1:01", metadata.format_duration(61))
        self.assertEqual("1:00:00", metadata.format_duration(3600))


class ImagingTests(TempCase):
    def setUp(self):
        super().setUp()
        self.base = scene(3)
        self.original = self.tmp / "orig.jpg"
        self.base.save(self.original, quality=95)

    def test_identical_images_hash_the_same(self):
        copy = self.tmp / "copy.jpg"
        copy.write_bytes(self.original.read_bytes())
        self.assertEqual(imaging.phash(self.original), imaging.phash(copy))

    def test_a_resized_export_is_still_a_near_match(self):
        small = self.tmp / "small.jpg"
        self.base.resize((450, 300)).save(small, quality=80)
        self.assertGreaterEqual(
            imaging.similarity(imaging.phash(self.original), imaging.phash(small)), 0.92)

    def test_a_different_picture_is_not_a_match(self):
        other = self.tmp / "other.jpg"
        scene(99).save(other, quality=95)
        self.assertLess(
            imaging.similarity(imaging.phash(self.original), imaging.phash(other)), 0.92)

    def test_blur_lowers_the_sharpness_score(self):
        blurred = self.tmp / "blur.jpg"
        self.base.filter(ImageFilter.GaussianBlur(4)).save(blurred, quality=95)
        self.assertGreater(imaging.sharpness(self.original), imaging.sharpness(blurred) * 3)

    def test_defects_are_named(self):
        white = self.tmp / "white.png"
        Image.new("RGB", (200, 200), (255, 255, 255)).save(white)
        black = self.tmp / "black.png"
        Image.new("RGB", (200, 200), (0, 0, 0)).save(black)
        self.assertEqual("overexposed", imaging.looks_unusable(imaging.quality_score(white)))
        self.assertEqual("black", imaging.looks_unusable(imaging.quality_score(black)))
        self.assertEqual("", imaging.looks_unusable(imaging.quality_score(self.original)))

    def test_hamming_and_similarity(self):
        self.assertEqual(0, imaging.hamming(0b1010, 0b1010))
        self.assertEqual(2, imaging.hamming(0b1010, 0b0000))
        self.assertEqual(1.0, imaging.similarity(5, 5))
        self.assertEqual(0.0, imaging.similarity(None, 5))

    def test_an_unreadable_file_returns_none_rather_than_raising(self):
        junk = self.write(self.tmp / "junk.jpg", b"nope")
        self.assertIsNone(imaging.phash(junk))
        self.assertEqual(0.0, imaging.sharpness(junk))

    def test_embedded_preview_extraction(self):
        raw = self.tmp / "fake.cr2"
        raw.write_bytes(b"HEADER" * 100 + self.original.read_bytes() + b"TRAILER")
        blob = imaging.extract_embedded_jpeg(raw)
        self.assertIsNotNone(blob)
        self.assertTrue(blob.startswith(b"\xff\xd8\xff"))
        self.assertIsNotNone(imaging.phash(raw))


class DedupeTests(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "lib"
        build_library(self.root)
        self.paths = sorted(p for p in self.root.rglob("*")
                            if p.is_file() and mediatypes.is_media(p))
        self.cache = hashcache.HashCache(None)

    def test_exact_finds_the_byte_identical_copy(self):
        groups = dedupe.find_exact(self.paths, self.cache)
        self.assertEqual(1, len(groups))
        self.assertEqual({"IMG_5511.JPG"}, {m.path.name for m in groups[0].members})
        self.assertEqual(2, len(groups[0].members))

    def test_similar_finds_the_web_export(self):
        groups = dedupe.find_similar(self.paths, 0.92, self.cache)
        names = {m.path.name for group in groups for m in group.members}
        self.assertIn("IMG_5511_web.jpg", names)

    def test_similar_keeps_the_largest_frame(self):
        for group in dedupe.find_similar(self.paths, 0.92, self.cache):
            if any(m.path.name == "IMG_5511_web.jpg" for m in group.members):
                self.assertEqual("IMG_5511.JPG", group.keep().path.name)
                break
        else:
            self.fail("the web export was not grouped")

    def test_bursts_are_found_and_the_sharpest_is_kept(self):
        groups = dedupe.find_bursts(self.paths, cache=self.cache)
        self.assertEqual(1, len(groups))
        group = groups[0]
        self.assertEqual(4, len(group.members))
        self.assertEqual("burst_03.JPG", group.keep().path.name)

    def test_a_burst_stays_in_shooting_order(self):
        group = dedupe.find_bursts(self.paths, cache=self.cache)[0]
        names = [m.path.name for m in group.members]
        self.assertEqual(sorted(names), names)

    def test_the_ignore_list_removes_a_pair(self):
        groups = dedupe.find_exact(self.paths, self.cache)
        extra = groups[0].extras[0]
        key = dedupe.identity_key(extra.path, extra.digest)
        self.assertEqual([], dedupe.find_exact(self.paths, self.cache, ignored={key}))

    def test_editing_a_file_puts_it_back_into_consideration(self):
        groups = dedupe.find_exact(self.paths, self.cache)
        extra = groups[0].extras[0]
        key = dedupe.identity_key(extra.path, extra.digest)
        extra.path.write_bytes(extra.path.read_bytes() + b"edited")
        # The old key no longer matches, so the file is judged afresh.
        self.assertNotEqual(key, dedupe.identity_key(extra.path, "different"))

    def test_the_banded_index_finds_every_close_pair(self):
        hashes = [0, 1, 3, 0xFFFF_FFFF_FFFF_FFFF]
        pairs = dedupe.banded_pairs(hashes, 2)
        self.assertIn((0, 1), pairs)
        self.assertIn((0, 2), pairs)
        self.assertNotIn((0, 3), pairs)

    def test_a_higher_threshold_finds_fewer_groups(self):
        loose = len(dedupe.find_similar(self.paths, 0.88, self.cache))
        tight = len(dedupe.find_similar(self.paths, 0.98, self.cache))
        self.assertGreaterEqual(loose, tight)

    def test_summary(self):
        summary = dedupe.summarise(dedupe.find_exact(self.paths, self.cache))
        self.assertEqual(1, summary["groups"])
        self.assertGreater(summary["reclaimable"], 0)


class HashCacheTests(TempCase):
    def test_a_value_survives_a_reopen(self):
        path = self.write(self.tmp / "a.jpg", b"x" * 50)
        cache = hashcache.HashCache(self.data / "c.db")
        calls = []
        cache.compute(path, "phash", lambda p: calls.append(p) or 123)
        cache.close()
        again = hashcache.HashCache(self.data / "c.db")
        try:
            self.assertEqual(123, again.compute(path, "phash", lambda p: calls.append(p) or 999))
            self.assertEqual(1, len(calls))
        finally:
            again.close()

    def test_an_edited_file_is_recomputed(self):
        path = self.write(self.tmp / "a.jpg", b"x" * 50)
        cache = hashcache.HashCache(self.data / "c.db")
        cache.compute(path, "sha256", lambda p: "first")
        import time
        try:
            time.sleep(0.01)
            path.write_bytes(b"y" * 90)
            self.assertEqual("second", cache.compute(path, "sha256", lambda p: "second"))
        finally:
            cache.close()

    def test_it_works_without_a_file(self):
        path = self.write(self.tmp / "a.jpg", b"x")
        cache = hashcache.HashCache(None)
        self.assertEqual(7, cache.compute(path, "phash", lambda p: 7))
        self.assertEqual(7, cache.compute(path, "phash", lambda p: 9))


if __name__ == "__main__":
    unittest.main()


import os
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from qingjian.ui import preview
from qingjian.core import video
from qingjian.ui.app import create_app
APP = create_app(["qingjian-tests"])

class G06Task2Tests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ["QINGJIAN_DATA_DIR"] = str(self.root / "data")

    def test_f086_sharing_violation_retries_move(self):
        from qingjian.core import config, safestore
        from qingjian.core.engine import Engine
        source = self.root / "source"
        source.mkdir()
        path = source / "a.jpg"
        Image.new("RGB", (20, 20), "red").save(path)
        settings = config.Settings()
        settings.recycle_mode = config.RECYCLE_SOFT
        engine = Engine(self.root / "engine-data", settings)
        self.addCleanup(engine.close)
        engine.open_folder(source)
        real_replace = safestore.os.replace
        attempts = []
        def busy_once(src, dst):
            if Path(src) == path:
                attempts.append(1)
                if len(attempts) == 1:
                    error = PermissionError(13, "sharing violation")
                    error.winerror = 32
                    raise error
            return real_replace(src, dst)
        with patch.object(safestore.os, "replace", side_effect=busy_once):
            engine.trash(path, allow_system=False)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertFalse(path.exists())

    def test_f086_sharing_violation_retries_system_recycle(self):
        from qingjian.core import config, platform_
        from qingjian.core.engine import Engine
        source = self.root / "source"
        source.mkdir()
        path = source / "a.jpg"
        Image.new("RGB", (20, 20), "red").save(path)
        settings = config.Settings()
        settings.recycle_mode = config.RECYCLE_SYSTEM
        engine = Engine(self.root / "engine-data", settings)
        self.addCleanup(engine.close)
        engine.open_folder(source)
        attempts = []
        def busy_once(target):
            attempts.append(1)
            if len(attempts) == 1:
                error = PermissionError(13, "sharing violation")
                error.winerror = 32
                raise error
            Path(target).unlink()
        with patch.object(platform_, "trash_available", return_value=True), \
             patch.object(platform_, "can_recycle", return_value=True), \
             patch.object(platform_, "move_to_trash", side_effect=busy_once):
            engine.trash(path)
        self.assertEqual(2, len(attempts))
        self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing semantics")
    def test_f117_real_windows_sharing_lock_retries_move(self):
        import ctypes
        from qingjian.core.safestore import _retry_sharing
        path = self.root / "held.jpg"
        target = self.root / "moved.jpg"
        path.write_bytes(b"photo")
        create = ctypes.windll.kernel32.CreateFileW
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p]
        create.restype = ctypes.c_void_p
        handle = create(str(path), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(ctypes.c_void_p(-1).value, handle)
        release = threading.Timer(0.12, ctypes.windll.kernel32.CloseHandle,
                                  args=(handle,))
        release.start()
        try:
            _retry_sharing(os.replace, path, target)
        finally:
            release.join(1)
        self.assertTrue(target.exists())


class G06Task6Tests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ["QINGJIAN_DATA_DIR"] = str(self.root / "data")

    def test_f017_previous_frame_at_end_stops_after_one_window(self):
        from fractions import Fraction
        class Frame:
            width, height = 64, 48
            def __init__(self, number):
                self.pts = number
            def reformat(self, **kwargs):
                return self
            def to_image(self):
                return Image.new("RGB", (64, 48))
        class Container:
            def __init__(self):
                self.streams = SimpleNamespace(video=[SimpleNamespace(
                    start_time=0, time_base=Fraction(1, 25))])
                self.start = 0
                self.decoded = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def seek(self, pts, **kwargs):
                self.start = max(0, (int(pts) // 25) * 25)
            def decode(self, stream):
                for number in range(self.start, 300):
                    self.decoded += 1
                    yield Frame(number)
        container = Container()
        with patch.object(video, "_av", return_value=SimpleNamespace(
                open=lambda path: container)):
            result = video.frame_at(self.root / "clip.mp4", 12.0, -1)
        self.assertAlmostEqual(11.96, result[1], places=2)
        self.assertLessEqual(container.decoded, 120)

    def test_f017_step_uses_position_before_pause(self):
        pane = preview.MediaPreview()
        pane.current_path = self.root / "clip.mp4"
        class Player:
            def __init__(self):
                self.current = 12000
            def pause(self):
                self.current = 0
            def position(self):
                return self.current
        pane.player = Player()
        with patch.object(video, "available", return_value=True), \
             patch.object(video, "frame_at",
                          return_value=(Image.new("RGB", (16, 16)), 11.96)) as frame:
            pane.step_frame(-1)
        self.assertEqual(12.0, frame.call_args.args[1])
        pane.close()

    def test_f057_first_import_is_atomic(self):
        real_import = __import__
        entered = threading.Event()
        release = threading.Event()
        fake_av = SimpleNamespace()
        def slow_import(name, *args, **kwargs):
            if name == "av":
                entered.set()
                release.wait(2)
                return fake_av
            return real_import(name, *args, **kwargs)
        old_checked, old_av = video._CHECKED, video._AV
        video._CHECKED, video._AV = False, None
        self.addCleanup(setattr, video, "_CHECKED", old_checked)
        self.addCleanup(setattr, video, "_AV", old_av)
        with patch("builtins.__import__", side_effect=slow_import):
            with ThreadPoolExecutor(max_workers=4) as pool:
                first = pool.submit(video.available)
                self.assertTrue(entered.wait(2))
                others = [pool.submit(video.available) for _ in range(3)]
                time.sleep(0.05)
                release.set()
                results = [first.result(2)] + [f.result(2) for f in others]
        self.assertEqual([True] * 4, results)
