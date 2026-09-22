from datetime import datetime
from pathlib import Path

from base import TempCase, unittest
from qingjian.core import naming, template
from qingjian.core.naming import NameError_


class NamingTests(TempCase):
    def test_suffix_only_recognizes_media_and_sidecar_extensions(self):
        self.assertEqual("Party v1.2.jpg", naming.ensure_suffix("Party v1.2", ".jpg"))
        self.assertEqual("photo.xmp", naming.ensure_suffix("photo.xmp", ".jpg"))

    def test_illegal_characters_are_replaced(self):
        self.assertEqual("a_b_c", naming.sanitize_component('a<b>c'))
        self.assertEqual("a_b", naming.sanitize_component('a"b'))
        self.assertEqual("a_b", naming.sanitize_component("a/b"))

    def test_trailing_dots_and_spaces_go(self):
        # Windows drops these silently, which makes a later exists() check lie.
        self.assertEqual("report", naming.sanitize_component("report. "))
        self.assertEqual("report", naming.sanitize_component("report..."))

    def test_reserved_device_names_are_defused(self):
        for reserved in ("CON", "con.txt", "PRN", "aux", "NUL", "COM1", "lpt9"):
            cleaned = naming.sanitize_component(reserved)
            self.assertTrue(cleaned.startswith("_"), reserved)
        self.assertEqual("console.txt", naming.sanitize_component("console.txt"))

    def test_validate_rejects_what_it_should(self):
        for bad in ("", "   ", ".", "..", "a?b", "a|b", "trailing.", "CON", "x" * 300):
            with self.assertRaises(NameError_, msg=bad):
                naming.validate_filename(bad)
        naming.validate_filename("perfectly fine (2).jpg")

    def test_unique_destination_adds_a_sequence(self):
        folder = self.tmp / "dest"
        folder.mkdir()
        self.write(folder / "a.jpg")
        self.assertEqual("a (2).jpg", naming.unique_destination(folder, "a.jpg").name)
        self.write(folder / "a (2).jpg")
        self.assertEqual("a (3).jpg", naming.unique_destination(folder, "a.jpg").name)

    def test_unique_destination_respects_reservations(self):
        """Two files planned in one batch must not both claim the same slot."""
        folder = self.tmp / "dest"
        folder.mkdir()
        taken = {str(folder / "a.jpg")}
        self.assertEqual("a (2).jpg", naming.unique_destination(folder, "a.jpg", taken).name)

    def test_sequences_do_not_stack(self):
        folder = self.tmp / "dest"
        folder.mkdir()
        self.write(folder / "a (2).jpg")
        # Re-filing "a (2).jpg" produces "a (3).jpg", not "a (2) (2).jpg".
        self.assertEqual("a (3).jpg", naming.unique_destination(folder, "a (2).jpg").name)

    def test_compound_suffix_split(self):
        self.assertEqual(("IMG_1.JPG", ".xmp"), naming.split_name("IMG_1.JPG.xmp"))


class TemplateTests(unittest.TestCase):
    def test_source_text_and_template_suffix(self):
        ctx = template.TemplateContext(source=Path("_DSC0001.JPG"),
                                       when=datetime(2024, 5, 1))
        self.assertEqual("_DSC0001.JPG", template.render_name("{name}", ctx))
        ctx.source = Path("IMG_1.HEIC.jpg")
        self.assertEqual("IMG_1.HEIC.jpg", template.render_name("{name}", ctx))
        self.assertEqual("IMG_1.HEIC.jpg", template.render_name("{name}.{ext}", ctx))
        self.assertEqual("Trip - Day 1.jpg", template.render_name("Trip - Day 1", ctx))
        self.assertEqual("photo.jpg", template.render_name("photo.jpg", ctx))
        ctx.source_root = Path("lib")
        ctx.source = Path("lib/_Trips/Day 1/IMG_1.JPG")
        self.assertEqual(("_Trips", "Day 1"), template.render_path("{relpath}", ctx))

    def test_source_internal_spaces_are_preserved(self):
        ctx = template.TemplateContext(source=Path("lib/Day  1/A  B.JPG"),
                                       source_root=Path("lib"), when=datetime(2024, 5, 1))
        self.assertEqual("A  B.JPG", template.render_name("{name}", ctx))
        self.assertEqual(("Day  1",), template.render_path("{relpath}", ctx))
        ctx.source = Path("lib/  Day  1/  A  B.JPG")
        self.assertEqual("  A  B.JPG", template.render_name("{name}", ctx))
        self.assertEqual(("  Day  1",), template.render_path("{relpath}", ctx))

    def ctx(self, **kw):
        base = dict(source=Path("/lib/Iceland/Day3/IMG_4821.JPG"),
                    when=datetime(2026, 8, 14, 19, 42, 8), camera="Sony ILCE-7M4",
                    lens="FE 24mm F2.8 G", iso="400", rating=3, label="green",
                    sequence=1, source_root=Path("/lib/Iceland"))
        base.update(kw)
        return template.TemplateContext(**base)

    def test_date_tokens(self):
        ctx = self.ctx()
        self.assertEqual(("2026", "2026-08"), template.render_path("{YYYY}/{YYYY-MM}", ctx))
        self.assertEqual("20260814_194208.JPG",
                         template.render_name("{YYYYMMDD}_{HHmmss}", ctx))

    def test_month_and_minute_are_distinct(self):
        ctx = self.ctx()
        self.assertEqual("08", template.render_path("{MM}", ctx)[0])
        self.assertEqual("42", template.render_path("{mm}", ctx)[0])

    def test_chinese_aliases(self):
        ctx = self.ctx()
        self.assertEqual(("2026", "08"), template.render_path("{年}/{月}", ctx))
        self.assertEqual("IMG_4821.JPG", template.render_name("{原名}", ctx))

    def test_sequence_padding(self):
        ctx = self.ctx(sequence=7)
        self.assertEqual("0007_IMG_4821.JPG", template.render_name("{seq:4}_{name}", ctx))
        self.assertEqual("007_IMG_4821.JPG", template.render_name("{序号:3}_{原名}", ctx))

    def test_relative_folder_preserves_structure(self):
        self.assertEqual(("Day3",), template.render_path("{relpath}", self.ctx()))

    def test_empty_token_collapses_instead_of_leaving_a_gap(self):
        ctx = self.ctx(camera="", lens="")
        self.assertEqual(("2026",), template.render_path("{camera}/{YYYY}", ctx))
        self.assertEqual("IMG_4821.JPG", template.render_name("{camera}_{name}", ctx))

    def test_fallback_text(self):
        ctx = self.ctx(camera="")
        self.assertEqual(("Unknown", "2026"),
                         template.render_path("{camera|Unknown}/{YYYY}", ctx))

    def test_extension_is_appended_when_the_template_omits_it(self):
        self.assertEqual("2026-08-14.JPG", template.render_name("{YYYY-MM-DD}", self.ctx()))

    def test_explicit_extension_is_not_doubled(self):
        self.assertEqual("IMG_4821.JPG", template.render_name("{name}.{ext}", self.ctx()))

    def test_illegal_data_is_repaired_at_render_time(self):
        """Sorting must not stall because one camera name contains a slash."""
        ctx = self.ctx(camera="Weird/Brand")
        self.assertEqual(("Weird_Brand",), template.render_path("{camera}", ctx))

    def test_validation_rejects_bad_templates(self):
        cases = [
            ("../escape", "{name}", "tpl.escapes_root"),
            ("C:/absolute", "{name}", "tpl.absolute_not_allowed"),
            ("{nope}", "{name}", "tpl.unknown_token"),
            ("{YYYY", "{name}", "tpl.unbalanced"),
            ("{YYYY}", "a/b", "error.name_invalid"),
            ("{YYYY}", "CON", "error.name_reserved"),
            ("{YYYY}", "what?", "error.name_invalid"),
        ]
        for path_tpl, name_tpl, key in cases:
            with self.subTest(path=path_tpl, name=name_tpl):
                with self.assertRaises(NameError_) as caught:
                    template.validate(path_tpl, name_tpl)
                self.assertEqual(key, caught.exception.key)

    def test_valid_templates_pass(self):
        template.validate("{YYYY}/{YYYY-MM}", "{YYYY-MM-DD}_{seq:4}_{name}")
        template.validate("", "")
        template.validate("{camera|Unknown}", "{name}")

    def test_full_destination(self):
        got = template.render_destination(Path("/out"), "{YYYY}/{YYYY-MM}",
                                          "{YYYY-MM-DD}_{seq:4}_{name}", self.ctx())
        self.assertEqual(Path("/out/2026/2026-08/2026-08-14_0001_IMG_4821.JPG"), got)


if __name__ == "__main__":
    unittest.main()
