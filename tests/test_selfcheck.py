import importlib
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from base import TempCase, unittest
from qingjian import selfcheck
from qingjian.core import i18n


class EntryPointTests(unittest.TestCase):
    def test_script_and_module_entries_share_selfcheck_dispatch(self):
        import main as script_entry
        from qingjian import __main__ as module_entry
        calls = []
        with patch.object(sys, "argv", ["qingjian", "--selfcheck", "--json"]), \
             patch.object(selfcheck, "run", side_effect=lambda args: calls.append(args) or 17):
            self.assertEqual(17, script_entry.main())
            self.assertEqual(17, module_entry.main())
        self.assertEqual([["--json"], ["--json"]], calls)


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is not installed")
class StartupArgumentTests(unittest.TestCase):
    @staticmethod
    def parse(*args):
        from qingjian.ui.app import _parse
        return _parse(["MediaSorter.exe", *args])

    def test_the_equals_spelling_of_an_option_is_honoured(self):
        """``--data-dir=D:\\QData`` was dropped without a word, and the run then
        used the real library -- the one thing ``--data-dir`` is there to rule out."""
        self.assertEqual(Path(r"D:\QData"), self.parse(r"--data-dir=D:\QData")["data_dir"])
        self.assertEqual("en", self.parse("--lang=en")["language"])

    def test_a_data_directory_keeps_its_closing_backslash(self):
        """``--data-dir "E:\\Q Data\\"`` arrives as ``E:\\Q Data"``: Windows reads
        a backslash before the closing quote as escaping it. Only the folder to
        open used to be repaired, so the run looked for a directory ending in a quote."""
        self.assertEqual(Path("E:\\Q Data\\"),
                         self.parse("--data-dir", 'E:\\Q Data"')["data_dir"])

    def test_the_value_of_a_qt_option_is_not_the_folder_to_open(self):
        """Qt reads its own options from the same command line."""
        for args in (("-platform", "offscreen"), ("--platform", "offscreen"),
                     ("-style", "fusion"), ("-stylesheet", "dark.qss")):
            with self.subTest(args=args):
                self.assertEqual("", self.parse(*args)["folder"])
        self.assertEqual(r"D:\Pics", self.parse("-platform", "offscreen", r"D:\Pics")["folder"])
        self.assertEqual(r"D:\Pics", self.parse("-style=fusion", r"D:\Pics")["folder"])

    def test_a_language_from_the_command_line_is_one_the_settings_page_offers(self):
        """It is kept in the settings, which offer nothing called "EN" or "auto"."""
        self.assertEqual("en", self.parse("--lang", "EN")["language"])
        self.assertEqual("system", self.parse("--lang", "auto")["language"])


class StartupLocaleTests(TempCase):
    def test_windows_ui_language_wins_when_environment_is_empty(self):
        kernel = Mock()
        kernel.GetUserDefaultUILanguage.return_value = 0x0804
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "platform", "win32"), \
             patch.object(i18n.locale, "windows_locale", {0x0804: "zh_CN"}), \
             patch("ctypes.windll", Mock(kernel32=kernel), create=True), \
             patch.object(i18n.locale, "getlocale", return_value=("en_US", "UTF-8")):
            self.assertEqual("zh", i18n.Translator.resolve("system"))

    def test_chinese_locale_name_is_recognized(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "platform", "linux"), \
             patch.object(i18n.locale, "getlocale",
                          return_value=("Chinese (Simplified)_China", "936")):
            self.assertEqual("zh", i18n.Translator.resolve("system"))


class SelfcheckTests(TempCase):
    def setUp(self):
        super().setUp()
        selfcheck.RESULTS.clear()

    def test_frozen_import_check_rejects_missing_recommended_modules(self):
        real_import = importlib.import_module

        def importing(name):
            if name in ("av", "send2trash"):
                raise ImportError(name)
            return real_import(name)

        with patch.object(importlib, "import_module", side_effect=importing), \
             patch.object(sys, "frozen", True, create=True):
            self.assertFalse(selfcheck._check_imports())
        self.assertEqual("imports", selfcheck.RESULTS[-1][0])
        self.assertFalse(selfcheck.RESULTS[-1][1])

    def test_source_import_check_allows_missing_recommended_modules(self):
        real_import = importlib.import_module

        def importing(name):
            if name in ("av", "send2trash"):
                raise ImportError(name)
            return real_import(name)

        frozen = getattr(sys, "frozen", None)
        if hasattr(sys, "frozen"):
            del sys.frozen
        self.addCleanup(lambda: setattr(sys, "frozen", frozen) if frozen is not None else None)
        with patch.object(importlib, "import_module", side_effect=importing):
            self.assertTrue(selfcheck._check_imports())

    def test_run_prints_each_result_as_soon_as_it_finishes(self):
        checks = ["_check_imports", "_check_catalogue", "_check_core", "_check_scale",
                  "_check_history", "_check_duplicates", "_check_ui"]
        calls = []

        def result(name):
            def fake(*_args):
                selfcheck.RESULTS.append((name, True, "ok"))
                calls.append(name)
                return True
            return fake

        patches = [patch.object(selfcheck, name, result(name)) for name in checks]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch("builtins.print") as output, \
             patch.object(selfcheck.faulthandler, "dump_traceback_later") as arm, \
             patch.object(selfcheck.faulthandler, "cancel_dump_traceback_later") as cancel:
            self.assertEqual(0, selfcheck.run([]))
        progress = [call.args[0] for call in output.call_args_list
                    if call.args and isinstance(call.args[0], str) and call.args[0].startswith("[")]
        self.assertEqual([f"[PASS] {name}: ok" for name in calls], progress)
        arm.assert_called_once()
        cancel.assert_called_once()

    def test_modal_guard_rejects_and_records_the_dialog(self):
        dialog = Mock()
        dialog.windowTitle.return_value = "Question"
        dialog.text.return_value = "Continue?"
        errors = []
        selfcheck._reject_active_modal(errors, active=lambda: dialog)
        dialog.reject.assert_called_once()
        self.assertIn("Question", errors[0])
        self.assertIn("Continue?", errors[0])

    def test_core_check_requires_every_sidecar_to_be_restored(self):
        source = inspect.getsource(selfcheck._check_core.__wrapped__)
        self.assertIn('("JPG", "CR2", "XMP")', source)
        self.assertIn("undo left files", source)

    def test_no_check_rests_on_an_assert_statement(self):
        """``python -O`` strips every one, and a check made of them passes
        whatever the program did."""
        import ast
        tree = ast.parse(inspect.getsource(selfcheck))
        self.assertEqual([], [node.lineno for node in ast.walk(tree)
                              if isinstance(node, ast.Assert)])

    def test_a_check_still_fails_under_python_minus_o(self):
        """Run optimised, the core check passed with no duplicates found at all."""
        import subprocess
        from base import ROOT
        script = self.tmp / "optimised.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "from qingjian import selfcheck\n"
            "from qingjian.core.engine import Engine\n"
            "work = Path(sys.argv[1])\n"
            "with patch.object(Engine, 'find_duplicates', lambda self, *args, **kwargs: []):\n"
            "    selfcheck._check_core(work / 'library', work / 'data')\n"
            "print(selfcheck.RESULTS[-1][1], selfcheck.RESULTS[-1][2])\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-O", str(script), str(self.tmp)],
                                capture_output=True, text=True, timeout=120,
                                env=dict(os.environ, PYTHONPATH=str(ROOT)))
        self.assertTrue(result.stdout.startswith("False AssertionError: expected one "
                                                 "duplicate group, got 0"),
                        result.stdout + result.stderr)

    def test_scale_and_duplicates_have_result_count_assertions(self):
        scale = inspect.getsource(selfcheck._check_scale.__wrapped__)
        dupes = inspect.getsource(selfcheck._check_duplicates.__wrapped__)
        self.assertIn("expected {count} queue rows", scale)
        self.assertIn("expected at least {expected} similar sets", dupes)

    def test_callback_monitor_records_ui_event_exceptions(self):
        monitor = selfcheck._CallbackExceptionMonitor()
        code = compile("raise RuntimeError('paint failed')", "qingjian/ui/fake.py", "exec")
        with monitor:
            try:
                exec(code, {"__name__": "paintEvent"})
            except RuntimeError:
                monitor.record(sys.exc_info())
        self.assertTrue(monitor.errors)

    def test_missing_translation_keys_fail_the_interface_check_contract(self):
        source = selfcheck._check_ui.__wrapped__.__code__.co_names
        self.assertIn("translator", source)
        self.assertIn("missing", source)


if __name__ == "__main__":
    unittest.main()
