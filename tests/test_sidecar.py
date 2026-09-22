import os
from pathlib import Path

from base import TempCase, unittest
from qingjian.core import mediatypes
from qingjian.core.sidecar import (KIND_LIVE, KIND_METADATA, KIND_RAW, SidecarRules,
                                   base_stem, collapse_groups, find_group, group_key,
                                   representative)


class SidecarTests(TempCase):
    def test_unlinked_same_stem_media_get_separate_rows(self):
        jpg = self.write(self.tmp / "1.jpg", b"a")
        png = self.write(self.tmp / "1.png", b"b")
        self.assertEqual(collapse_groups(
            [jpg, png], SidecarRules(link_same_stem_media=False)), [jpg, png])

    def build(self, names):
        folder = self.tmp / "shots"
        folder.mkdir(exist_ok=True)
        for name in names:
            self.write(folder / name, name.encode())
        return folder

    def kinds(self, group):
        return {m.path.name: m.kind for m in group.members}

    def test_raw_and_metadata_travel_with_the_jpeg(self):
        folder = self.build(["IMG_1.JPG", "IMG_1.CR2", "IMG_1.XMP", "IMG_1.AAE"])
        group = find_group(folder / "IMG_1.JPG")
        self.assertEqual(4, group.count)
        self.assertEqual(KIND_RAW, self.kinds(group)["IMG_1.CR2"])
        self.assertEqual(KIND_METADATA, self.kinds(group)["IMG_1.XMP"])

    def test_adobe_style_double_extension(self):
        folder = self.build(["IMG_1.JPG", "IMG_1.JPG.xmp"])
        self.assertEqual(2, find_group(folder / "IMG_1.JPG").count)

    def test_the_relation_is_symmetric(self):
        """Acting on the raw must find the same shot as acting on the jpeg."""
        folder = self.build(["IMG_1.JPG", "IMG_1.CR2", "IMG_1.JPG.xmp", "IMG_1.CR2.xmp"])
        from_jpeg = {p.name for p in find_group(folder / "IMG_1.JPG").paths}
        from_raw = {p.name for p in find_group(folder / "IMG_1.CR2").paths}
        self.assertEqual(from_jpeg, from_raw)

    def test_a_longer_stem_is_not_a_match(self):
        folder = self.build(["IMG_1.JPG", "IMG_10.JPG", "IMG_1x.JPG"])
        self.assertEqual(1, find_group(folder / "IMG_1.JPG").count)

    def test_case_insensitive_stems(self):
        folder = self.build(["img_1.JPG", "IMG_1.cr2"])
        self.assertEqual(2, find_group(folder / "img_1.JPG").count)

    def test_unrelated_extension_is_left_alone(self):
        folder = self.build(["IMG_1.JPG", "IMG_1.txt", "IMG_1.doc"])
        self.assertEqual(1, find_group(folder / "IMG_1.JPG").count)

    def test_live_photo_pairing(self):
        folder = self.build(["IMG_1.HEIC", "IMG_1.MOV"])
        group = find_group(folder / "IMG_1.HEIC")
        self.assertEqual(KIND_LIVE, self.kinds(group)["IMG_1.MOV"])

    def test_two_videos_sharing_a_stem_are_not_a_live_photo(self):
        folder = self.build(["clip.MP4", "clip.MOV"])
        group = find_group(folder / "clip.MP4")
        self.assertNotEqual(KIND_LIVE, self.kinds(group)["clip.MOV"])

    def test_disabled_rules_return_the_master_alone(self):
        folder = self.build(["IMG_1.JPG", "IMG_1.CR2"])
        group = find_group(folder / "IMG_1.JPG", SidecarRules(enabled=False))
        self.assertEqual(1, group.count)
        self.assertFalse(bool(group))

    def test_link_same_stem_media_can_be_turned_off(self):
        folder = self.build(["IMG_1.JPG", "IMG_1.PNG", "IMG_1.CR2"])
        rules = SidecarRules(link_same_stem_media=False)
        names = {p.name for p in find_group(folder / "IMG_1.JPG", rules).paths}
        self.assertIn("IMG_1.CR2", names)
        self.assertNotIn("IMG_1.PNG", names)

    def test_group_key_reduces_every_member_to_one_identity(self):
        keys = {group_key(self.tmp / n)[1]
                for n in ("IMG_1.JPG", "IMG_1.CR2", "IMG_1.JPG.xmp", "IMG_1.XMP")}
        self.assertEqual(1, len(keys))
        self.assertNotEqual(group_key(self.tmp / "IMG_1.JPG"), group_key(self.tmp / "IMG_10.JPG"))

    def test_base_stem(self):
        self.assertEqual("IMG_1", base_stem("IMG_1.JPG.xmp"))
        self.assertEqual("IMG_1", base_stem("IMG_1.CR2"))
        self.assertEqual("a.b", base_stem("a.b.txt"))

    def test_representative_prefers_an_ordinary_image(self):
        folder = self.build(["IMG_1.CR2", "IMG_1.JPG", "IMG_1.MP4"])
        chosen = representative([folder / "IMG_1.CR2", folder / "IMG_1.JPG", folder / "IMG_1.MP4"])
        self.assertEqual("IMG_1.JPG", chosen.name)

    def test_collapse_shows_each_shot_once(self):
        folder = self.build(["IMG_1.JPG", "IMG_1.CR2", "IMG_2.JPG", "IMG_3.CR2"])
        media = sorted(p for p in folder.iterdir() if mediatypes.is_media(p))
        collapsed = collapse_groups(media, SidecarRules())
        self.assertEqual(["IMG_1.JPG", "IMG_2.JPG", "IMG_3.CR2"],
                         sorted(p.name for p in collapsed))

    def test_collapse_is_a_no_op_when_hiding_is_off(self):
        folder = self.build(["IMG_1.JPG", "IMG_1.CR2"])
        media = sorted(p for p in folder.iterdir() if mediatypes.is_media(p))
        rules = SidecarRules(hide_from_queue=False)
        self.assertEqual(len(media), len(collapse_groups(media, rules)))

    def test_rules_round_trip_through_a_dict(self):
        rules = SidecarRules(prompt="always", extra_extensions=frozenset({".foo"}))
        again = SidecarRules.from_dict(rules.to_dict())
        self.assertEqual("always", again.prompt)
        self.assertIn(".foo", again.extra_extensions)

    def test_rules_normalise_extensions_written_without_a_dot(self):
        rules = SidecarRules.from_dict({"extra_extensions": ["FOO", ".Bar"]})
        self.assertEqual({".foo", ".bar"}, set(rules.extra_extensions))

    def test_symlinks_are_never_pulled_into_a_group(self):
        folder = self.build(["IMG_1.JPG"])
        try:
            (folder / "IMG_1.CR2").symlink_to(folder / "IMG_1.JPG")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(1, find_group(folder / "IMG_1.JPG").count)


class CompanionIndexTests(TempCase):
    def build(self, name: str):
        from qingjian.core import config
        from qingjian.core.engine import Engine
        source = self.tmp / name / "src"
        target = self.tmp / name / "dst"
        settings = config.Settings()
        settings.bindings[0].action = "move"
        settings.bindings[0].folder = str(target)
        settings.bindings[0].name_template = "{name}"
        engine = Engine(self.tmp / name / "data", settings)
        self.addCleanup(engine.close)
        return source, target, engine

    def age(self, folder: Path) -> None:
        old = os.stat(folder).st_mtime - 10
        os.utime(folder, (old, old))

    def test_an_outside_write_during_our_move_is_not_swallowed(self):
        source, target, engine = self.build("during")
        self.write(source / "a.jpg", b"x" * 64)
        self.write(source / "b.jpg", b"y" * 64)
        engine.open_folder(source)
        real_run = engine.store.run

        def run_and_write(plan, *args, **kwargs):
            (source / "b.xmp").write_text("<x/>", encoding="utf-8")
            return real_run(plan, *args, **kwargs)

        engine.store.run = run_and_write
        engine.classify(engine.settings.bindings[0], source / "a.jpg")
        engine.store.run = real_run
        engine.classify(engine.settings.bindings[0], source / "b.jpg")
        self.assertEqual(["a.jpg", "b.jpg", "b.xmp"], self.tree(target))

    def test_a_failed_listing_is_not_cached(self):
        source, target, engine = self.build("failed")
        self.write(source / "c.jpg", b"x" * 64)
        (source / "c.xmp").write_text("<x/>", encoding="utf-8")
        self.age(source)
        engine.open_folder(source)
        engine._stem_index.clear()
        real_iterdir = Path.iterdir
        fail = {"once": True}

        def flaky(self):
            if fail["once"] and self == source:
                fail["once"] = False
                raise OSError(59, "simulated")
            return real_iterdir(self)

        Path.iterdir = flaky
        self.addCleanup(setattr, Path, "iterdir", real_iterdir)
        engine.group_for(source / "c.jpg")
        Path.iterdir = real_iterdir
        engine.classify(engine.settings.bindings[0], source / "c.jpg")
        self.assertEqual(["c.jpg", "c.xmp"], self.tree(target))

    def test_a_file_written_during_the_listing_is_seen(self):
        source, target, engine = self.build("listing")
        self.write(source / "d.jpg", b"x" * 64)
        engine.open_folder(source)
        engine.INDEX_GRACE_SECONDS = 0.0
        engine._stem_index.clear()
        self.age(source)
        real_iterdir = Path.iterdir
        added = {"done": False}

        def listing_then_write(self):
            items = list(real_iterdir(self))
            if self == source and not added["done"]:
                added["done"] = True
                (source / "d.xmp").write_text("<x/>", encoding="utf-8")
            return iter(items)

        Path.iterdir = listing_then_write
        self.addCleanup(setattr, Path, "iterdir", real_iterdir)
        engine.group_for(source / "d.jpg")
        Path.iterdir = real_iterdir
        engine.classify(engine.settings.bindings[0], source / "d.jpg")
        self.assertEqual(["d.jpg", "d.xmp"], self.tree(target))


if __name__ == "__main__":
    unittest.main()
