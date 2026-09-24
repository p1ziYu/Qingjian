import importlib
import inspect
import os
import sys
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
