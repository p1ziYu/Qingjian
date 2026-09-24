from base import unittest
from qingjian.core import i18n


class CatalogueTests(unittest.TestCase):
    def test_clear_all_warns_of_permanent_deletion_in_both_languages(self):
        zh, en = i18n.CATALOG["backup.clear_confirm"]
        self.assertIn("永久", zh)
        self.assertIn("permanent", en.lower())
        self.assertNotIn("回收站", zh)
        self.assertNotIn("recycle bin", en.lower())

    def test_partial_recycle_message_names_remaining_paths(self):
        for language in ("zh", "en"):
            message = i18n.Translator(language).tr(
                "status.partial_recycle", paths="remaining.jpg", error="failed")
            self.assertIn("remaining.jpg", message)

    def test_every_key_has_both_languages_and_matching_placeholders(self):
        self.assertEqual([], i18n.catalog_problems())

    def test_catalogue_is_not_trivially_small(self):
        self.assertGreater(len(i18n.CATALOG), 250)

    def test_translation_switches_with_the_language(self):
        translator = i18n.Translator("zh")
        self.assertEqual("取消", translator.tr("cancel"))
        translator.set_language("en")
        self.assertEqual("Cancel", translator.tr("cancel"))

    def test_formatting_fields_are_substituted(self):
        translator = i18n.Translator("en")
        self.assertEqual("Move all 4 files", translator.tr("sidecar.move_all", count=4))

    def test_a_missing_field_never_raises(self):
        translator = i18n.Translator("en")
        text = translator.tr("sidecar.move_all")           # no count given
        self.assertIn("{count}", text)
        self.assertIn("sidecar.move_all (format)", translator.missing)

    def test_unknown_key_returns_the_key_and_is_recorded(self):
        translator = i18n.Translator("en")
        self.assertEqual("no.such.key", translator.tr("no.such.key"))
        self.assertIn("no.such.key", translator.missing)

    def test_language_resolution(self):
        resolve = i18n.Translator.resolve
        self.assertEqual("zh", resolve("zh"))
        self.assertEqual("zh", resolve("zh_CN"))
        self.assertEqual("zh", resolve("zh-Hant"))
        self.assertEqual("en", resolve("en_GB"))
        self.assertEqual("en", resolve("fr_FR"))
        self.assertEqual("en", resolve("de"))

    def test_system_language_follows_the_environment(self):
        import os
        previous = os.environ.get("QINGJIAN_LANG")
        try:
            os.environ["QINGJIAN_LANG"] = "zh_CN.UTF-8"
            self.assertEqual("zh", i18n.Translator.resolve("system"))
            os.environ["QINGJIAN_LANG"] = "en_US.UTF-8"
            self.assertEqual("en", i18n.Translator.resolve("system"))
        finally:
            if previous is None:
                os.environ.pop("QINGJIAN_LANG", None)
            else:
                os.environ["QINGJIAN_LANG"] = previous

    def test_english_strings_are_not_left_as_chinese(self):
        """A copy-paste slip would leave CJK characters in the English column."""
        offenders = [
            key for key, (_zh, en) in i18n.CATALOG.items()
            if any("一" <= ch <= "鿿" for ch in en)
        ]
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
