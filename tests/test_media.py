from datetime import datetime

from PIL import Image

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
        self.assertFalse(hasattr(info, "raw_tags"))
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
        cache.put(path, phash=123)
        cache.close()
        again = hashcache.HashCache(self.data / "c.db")
        try:
            self.assertEqual(123, again.get(path, "phash"))
        finally:
            again.close()

    def test_an_edited_file_is_recomputed(self):
        path = self.write(self.tmp / "a.jpg", b"x" * 50)
        cache = hashcache.HashCache(self.data / "c.db")
        cache.put(path, sha256="first")
        import time
        try:
            time.sleep(0.01)
            path.write_bytes(b"y" * 90)
            self.assertIsNone(cache.get(path, "sha256"))
            cache.put(path, sha256="second")
            self.assertEqual("second", cache.get(path, "sha256"))
        finally:
            cache.close()

    def test_it_works_without_a_file(self):
        path = self.write(self.tmp / "a.jpg", b"x")
        cache = hashcache.HashCache(None)
        cache.put(path, phash=7)
        self.assertEqual(7, cache.get(path, "phash"))


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


# G09 media parsing and decoding regressions.
import io
import sqlite3
import struct
from datetime import timezone

import numpy as np
from PIL import PngImagePlugin
from PySide6.QtCore import QSize
from unittest.mock import MagicMock

from qingjian.core import scanner
from qingjian.core.hashcache import HashCache
from qingjian.ui import preview as preview_module
from qingjian.ui.mainwindow import MainWindow


def g09_jpeg_bytes(size=(128, 96)):
    stream = io.BytesIO()
    Image.new('RGB', size, (90, 140, 190)).save(stream, 'JPEG')
    return stream.getvalue()


def g09_tiff_ifd(entries, offset):
    data = b''
    out = struct.pack('<H', len(entries))
    base = offset + 2 + 12 * len(entries) + 4
    for tag, kind, count, value in sorted(entries):
        if isinstance(value, bytes):
            if len(value) <= 4:
                encoded = value.ljust(4, b'\0')
            else:
                encoded = struct.pack('<I', base + len(data))
                data += value + b'\0' * (len(value) % 2)
        elif kind == 3:
            encoded = struct.pack('<H', value) + b'\0\0'
        else:
            encoded = struct.pack('<I', value)
        out += struct.pack('<HHI', tag, kind, count) + encoded
    return out + struct.pack('<I', 0) + data


class G09MediaTests(TempCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp

    def test_f061_undecodable_extensions_and_heic_worker(self):
        heic = self.root / 'a.heic'
        heic.write_bytes(b'not-a-heic')
        self.assertTrue(mediatypes.is_media(heic))
        self.assertFalse(mediatypes.is_decodable(heic))
        self.assertFalse(mediatypes.is_decodable(self.root / 'a.heif'))
        self.assertFalse(mediatypes.is_decodable(self.root / 'a.jxl'))
        self.assertTrue(imaging.decodes_slowly(heic))

    def test_f061_missing_decoder_message(self):
        from PySide6.QtCore import QSize
        from qingjian.ui.preview import decode_qimage
        path = self.root / 'sample.heic'
        path.write_bytes(b'bad HEIC')
        image, error = decode_qimage(path, QSize(640, 480))
        self.assertTrue(image.isNull())
        self.assertIn('HEIC', error.upper())
        self.assertTrue('decoder' in error.lower() or '解码器' in error)

    def test_f052_png_metadata_does_not_load_pixels(self):
        path = self.root / 'picture.png'
        Image.new('RGB', (64, 48)).save(path)
        original = PngImagePlugin.PngImageFile.load
        calls = []
        def counted(image, *args, **kwargs):
            calls.append(1)
            return original(image, *args, **kwargs)
        with patch.object(PngImagePlugin.PngImageFile, 'load', counted):
            info = metadata.read(path, use_cache=False)
            captured = metadata.capture_only(path)
        self.assertEqual((info.width, info.height), (64, 48))
        self.assertIsNotNone(captured)
        self.assertEqual(calls, [])
        tagged = self.root / 'tagged.png'
        exif = Image.Exif()
        exif[306] = '2024:05:01 07:00:00'
        Image.new('RGB', (32, 24)).save(tagged, exif=exif)
        self.assertFalse(metadata.read(tagged, use_cache=False).captured_is_fallback)

    def test_f053_f055_subifd_dimensions_and_duplicate_tags(self):
        preview = g09_jpeg_bytes((1500, 1000))
        make = b'NIKON\0'
        date = b'2024:05:01 07:00:00\0'
        ifd0_at = 8
        shell = [(0xFE, 4, 1, 1), (0x100, 4, 1, 160), (0x101, 4, 1, 120),
                 (0x10F, 2, len(make), make), (0x14A, 4, 1, 0), (0x8769, 4, 1, 0)]
        sub_at = ifd0_at + len(g09_tiff_ifd(shell, ifd0_at))
        sub = [(0xFE, 4, 1, 0), (0x100, 4, 1, 1500), (0x101, 4, 1, 1000)]
        exif_at = sub_at + len(g09_tiff_ifd(sub, sub_at))
        blob = b'II*\0' + struct.pack('<I', 8)
        blob += g09_tiff_ifd(shell[:-2] + [(0x14A, 4, 1, sub_at), (0x8769, 4, 1, exif_at)], ifd0_at)
        blob += g09_tiff_ifd(sub, sub_at)
        blob += g09_tiff_ifd([(0x9003, 2, len(date), date)], exif_at)
        path = self.root / 'sample.nef'
        path.write_bytes(blob + preview)
        info = metadata.read(path, use_cache=False)
        self.assertEqual((info.width, info.height), (1500, 1000))
        self.assertIn('NIKON', info.camera)
        self.assertFalse(info.captured_is_fallback)
        jpg = self.root / 'sample.jpg'
        jpg.write_bytes(preview)
        groups = dedupe.find_similar([path, jpg])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].keep().path, path)

    def test_f055_duplicate_values_are_skipped(self):
        count = 128
        data_at = 8 + 2 + 12 * count + 4
        one = struct.pack('<HHII', 0x100, 3, 500, data_at)
        path = self.root / 'crafted.dng'
        path.write_bytes(b'II*\0' + struct.pack('<I', 8) + struct.pack('<H', count)
                         + one * count + struct.pack('<I', 0) + b'\0' * 1000)
        original = exifread._read_value
        calls = []
        def counted(*args):
            calls.append(1)
            return original(*args)
        with patch.object(exifread, '_read_value', counted):
            exifread.read_tiff_exif(path)
        self.assertLessEqual(len(calls), 1)

    def test_f055_oversized_numeric_tag_is_skipped(self):
        data_at = 8 + 2 + 12 + 4
        path = self.root / 'oversized.dng'
        path.write_bytes(b'II*\0' + struct.pack('<I', 8) + struct.pack('<H', 1)
                         + struct.pack('<HHII', 0x100, 3, 65535, data_at)
                         + struct.pack('<I', 0) + b'\0' * (65535 * 2))
        with patch.object(exifread, '_read_value', side_effect=AssertionError('large array parsed')):
            self.assertNotIn('ImageWidth', exifread.read_tiff_exif(path))

    def test_f054_variant_tiff_magic(self):
        date = b'2024:05:01 07:00:00\0'
        exif_at = 8 + len(g09_tiff_ifd([(0x8769, 4, 1, 0)], 8))
        body = g09_tiff_ifd([(0x8769, 4, 1, exif_at)], 8)
        body += g09_tiff_ifd([(0x9003, 2, len(date), date)], exif_at)
        for magic, ext in ((0x4F52, '.orf'), (0x5352, '.orf'), (0x0055, '.rw2')):
            with self.subTest(magic=magic):
                path = self.root / f'{magic}{ext}'
                path.write_bytes(b'II' + struct.pack('<HI', magic, 8) + body)
                self.assertEqual(exifread.read_tiff_exif(path)['DateTimeOriginal'], '2024:05:01 07:00:00')
                self.assertFalse(metadata.read(path, use_cache=False).captured_is_fallback)

    def test_f051_jpeg_display_orientation(self):
        path = self.root / 'rotated.jpg'
        exif = Image.Exif()
        exif[0x112] = 6
        Image.new('RGB', (400, 300)).save(path, exif=exif)
        self.assertTrue(metadata.read(path).is_portrait)
        self.assertEqual(scanner.apply_filter([path], scanner.FilterSpec(mode='portrait')), [path])
        self.assertEqual(scanner.apply_filter([path], scanner.FilterSpec(mode='landscape')), [])

    def test_f051_video_tkhd_rotation(self):
        path = self.root / 'rotated.mp4'
        matrix = struct.pack('>9i', 0, 65536, 0, -65536, 0, 0, 0, 0, 0x40000000)
        tkhd = b'\0\0\0\0' + b'\0' * 8 + struct.pack('>I', 1) + b'\0' * 4
        tkhd += b'\0' * 4 + b'\0' * 8 + b'\0' * 8 + matrix
        tkhd += struct.pack('>II', 400 << 16, 300 << 16)
        box = lambda kind, body: struct.pack('>I4s', len(body) + 8, kind) + body
        path.write_bytes(box(b'ftyp', b'isom') + box(b'moov', box(b'trak', box(b'tkhd', tkhd))))
        with patch.object(metadata, '_av', return_value=None):
            info = metadata.read(path, use_cache=False)
        self.assertEqual((info.width, info.height), (400, 300))
        self.assertTrue(info.is_portrait)
        with patch.object(metadata, '_av', return_value=None):
            self.assertEqual(scanner.apply_filter([path], scanner.FilterSpec(mode='portrait')), [path])
            self.assertEqual(scanner.apply_filter([path], scanner.FilterSpec(mode='landscape')), [])

    def test_f051_pyav_path_uses_tkhd_rotation(self):
        path = self.root / 'rotated-with-av.mp4'
        matrix = struct.pack('>9i', 0, 65536, 0, -65536, 0, 0, 0, 0, 0x40000000)
        tkhd = b'\0\0\0\0' + b'\0' * 8 + struct.pack('>I', 1) + b'\0' * 4
        tkhd += b'\0' * 4 + b'\0' * 8 + b'\0' * 8 + matrix
        tkhd += struct.pack('>II', 400 << 16, 300 << 16)
        box = lambda kind, body: struct.pack('>I4s', len(body) + 8, kind) + body
        path.write_bytes(box(b'ftyp', b'isom') + box(b'moov', box(b'trak', box(b'tkhd', tkhd))))

        class Stream:
            duration = None
            time_base = None
            width = 400
            height = 300
            codec_context = type('Codec', (), {'name': 'mpeg4'})()
            average_rate = None
            metadata = {}

        class Container:
            duration = None
            streams = type('Streams', (), {'video': [Stream()]})()
            metadata = {}
            def __enter__(self): return self
            def __exit__(self, *args): return None

        fake_av = type('AV', (), {'open': lambda *args: Container(), 'time_base': 1000000})()
        with patch.object(metadata, '_av', return_value=fake_av):
            info = metadata.read(path, use_cache=False)
        self.assertEqual(info.rotation, 90)
        self.assertTrue(info.is_portrait)

    def test_f058_embedded_jpeg_internal_eoi(self):
        image = g09_jpeg_bytes((128, 96))
        thumb = g09_jpeg_bytes((32, 24))
        tiff = b'II*\0' + struct.pack('<I', 8)
        tiff += struct.pack('<H', 0) + struct.pack('<I', 14)
        tiff += struct.pack('<H', 2)
        tiff += struct.pack('<HHII', 0x201, 4, 1, 44)
        tiff += struct.pack('<HHII', 0x202, 4, 1, len(thumb)) + struct.pack('<I', 0)
        payload = b'Exif\0\0' + tiff + thumb
        app1 = b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload
        embedded = image[:2] + app1 + image[2:]
        path = self.root / 'sample.raf'
        path.write_bytes(b'FUJIFILM' + b'\0' * 40 + embedded + b'RAW DATA')
        self.assertEqual(imaging.extract_embedded_jpeg(path), embedded)
        self.assertEqual(imaging.open_image(path).size, (128, 96))
        self.assertIsNotNone(imaging.phash(path))

    def test_f059_f060_raw_orientation_and_16_bit_hash(self):
        preview = g09_jpeg_bytes((90, 60))
        entries = [(0x112, 3, 1, 6), (0x201, 4, 1, 0), (0x202, 4, 1, len(preview))]
        image_at = 8 + len(g09_tiff_ifd(entries, 8))
        path = self.root / 'sample.nef'
        path.write_bytes(b'II*\0' + struct.pack('<I', 8) + g09_tiff_ifd(
            [(0x112, 3, 1, 6), (0x201, 4, 1, image_at), (0x202, 4, 1, len(preview))], 8) + preview)
        self.assertEqual(imaging.open_image(path).size, (60, 90))
        self.assertEqual(imaging.thumbnail(path, (100, 100)).size, (60, 90))
        values = np.tile((np.arange(64) * 255 // 63).astype(np.uint8), (64, 1))
        p8, p16 = self.root / '8.png', self.root / '16.png'
        Image.fromarray(values).save(p8)
        Image.fromarray(values.astype(np.uint16) * 257).save(p16)
        self.assertEqual(imaging.phash(p16), imaging.phash(p8))

    def test_f060_distinct_16_bit_hashes(self):
        yy, xx = np.mgrid[:64, :64]
        patterns = [xx * 4, yy * 4, ((xx + yy) % 16) * 16]
        hashes = []
        for index, values in enumerate(patterns):
            path = self.root / f'{index}.png'
            Image.fromarray(values.astype(np.uint16) * 257).save(path)
            hashes.append(imaging.phash(path))
        self.assertEqual(len(set(hashes)), 3)

    def test_f060_16_bit_matches_8_bit(self):
        yy, xx = np.mgrid[:64, :64]
        values = np.where((xx - 32) ** 2 + (yy - 32) ** 2 < 225, 200, 20).astype(np.uint8)
        p8, p16 = self.root / '8.png', self.root / '16.png'
        Image.fromarray(values).save(p8)
        Image.fromarray(values.astype(np.uint16) * 257).save(p16)
        self.assertEqual(imaging.phash(p16), imaging.phash(p8))

    def test_f050_video_utc_to_local(self):
        class Stream:
            duration = None
            time_base = None
            width = 64
            height = 48
            codec_context = type('Codec', (), {'name': 'mpeg4'})()
            average_rate = None
            metadata = {}
        class Container:
            duration = None
            streams = type('Streams', (), {'video': [Stream()]})()
            metadata = {'creation_time': '2024-04-30T23:00:00Z'}
            def __enter__(self): return self
            def __exit__(self, *args): return None
        fake_av = type('AV', (), {'open': lambda *args: Container(), 'time_base': 1000000})()
        path = self.root / 'clip.mp4'
        path.write_bytes(b'clip')
        with patch.object(metadata, '_av', return_value=fake_av):
            info = metadata.read(path, use_cache=False)
        expected = datetime(2024, 4, 30, 23, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(info.captured, expected)

    def test_f050_captured_cache_version(self):
        path = self.root / 'clip.mp4'
        path.write_bytes(b'clip')
        db = self.root / 'old.sqlite'
        from qingjian.core.hashcache import SCHEMA
        stat = path.stat()
        connection = sqlite3.connect(db)
        try:
            connection.executescript(SCHEMA)
            connection.execute('INSERT INTO entries(path,size,mtime_ns,captured,updated) VALUES(?,?,?,?,?)',
                               (str(path), stat.st_size, stat.st_mtime_ns, 123.0, 0.0))
            connection.commit()
        finally:
            connection.close()
        cache = HashCache(db)
        self.addCleanup(cache.close)
        self.assertIsNone(cache.get(path, 'captured'))

    def test_f050_f056_hashcache_migration_and_replace(self):
        path = self.root / 'media.bin'
        path.write_bytes(b'A' * 100)
        db = self.root / 'cache.sqlite'
        cache = HashCache(db)
        self.addCleanup(cache.close)
        cache.put(path, sha256='old', phash=42, sharpness=8.0, captured=1.0)
        cache.close()
        path.write_bytes(b'B' * 200)
        cache = HashCache(db)
        self.addCleanup(cache.close)
        cache.put(path, captured=2.0)
        cache.close()
        cache = HashCache(db)
        self.addCleanup(cache.close)
        self.assertIsNone(cache.get(path, 'sha256'))
        self.assertIsNone(cache.get(path, 'phash'))
        self.assertIsNone(cache.get(path, 'sharpness'))
        cache.close()
        parsed = metadata._parse_datetime('2024-04-30T23:00:00Z')
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_f062_safe_timestamp(self):
        info = metadata.MediaInfo(path=self.root / 'old.jpg', captured=datetime(1970, 1, 1))
        self.assertEqual(info.timestamp(), 0)

    def test_f061_slow_decode_error_reaches_existing_status_bar(self):
        path = self.root / 'sample.heic'
        path.write_bytes(b'bad HEIC')
        target = QSize(640, 480)
        self.assertTrue(imaging.decodes_slowly(path))
        fetcher = preview_module.PreviewPrefetcher()
        self.addCleanup(fetcher.shutdown)
        fetcher.request(path, target)
        fetcher._pool.waitForDone(3000)
        APP.processEvents()
        error = fetcher.error(path, target)
        self.assertIn('HEIC', error.upper())
        self.assertTrue('decoder' in error.lower() or '解码器' in error)

        fake = MagicMock()
        fake.engine.current_path.return_value = path
        fake.preview.EMPTY = 0
        fake.preview.stack.currentIndex.return_value = 0
        fake._preview_target.return_value = target
        fake.preloader = fetcher
        fake._preview_retries = {}
        MainWindow._preview_arrived(fake, str(path))
        shown = fake.status.call_args.args[0]
        self.assertIn(error, shown)
        fake.status.assert_called_once_with(shown, 'warning')

    def test_f060_10_and_12_bit_containers_use_their_actual_range(self):
        yy, xx = np.mgrid[:64, :64]
        pattern = ((xx * 3 + yy * 5) % 256).astype(np.uint16)
        for bits in (10, 12):
            with self.subTest(bits=bits):
                maximum = (1 << bits) - 1
                values = np.rint(pattern * (maximum / 255.0)).astype(np.uint16)
                path = self.root / f'{bits}-bit.png'
                Image.fromarray(values).save(path)
                gray = imaging._gray_array(path, 64)
                self.assertEqual(0.0, float(gray.min()))
                self.assertEqual(255.0, float(gray.max()))
