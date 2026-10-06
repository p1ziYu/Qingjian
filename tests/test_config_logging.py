import os
import re
import zipfile
import json
from pathlib import Path

from base import TempCase, unittest
from qingjian.core import appdirs, config, i18n, logsetup, platform_
from qingjian.core.safestore import VERIFY_FAST


class ConfigTests(TempCase):
    def test_non_finite_numbers_fall_back_and_bad_settings_are_preserved(self):
        path = self.data / "settings.json"
        path.write_text(json.dumps({
            "workers": float("inf"),
            "quota": {"max_bytes": float("inf")},
            "profiles": {"P": [{"sequence_start": float("-inf")}]},
            "current_profile": "P",
        }), encoding="utf-8")
        settings = config.load(path)
        self.assertEqual(4, settings.workers)
        self.assertEqual(config.Settings().quota.max_bytes, settings.quota.max_bytes)
        self.assertEqual(1, settings.bindings[0].sequence_start)
        from unittest.mock import patch
        with patch.object(config.Settings, "from_dict", side_effect=TypeError("bad shape")):
            settings = config.load(path)
        self.assertEqual(4, settings.workers)
        self.assertTrue(path.with_suffix(".broken.json").exists())

    def test_defaults_are_sane(self):
        settings = config.Settings()
        settings.ensure_profile()
        self.assertEqual(10, len(settings.bindings))
        self.assertEqual("full", settings.verification)
        self.assertTrue(settings.fast_path)
        self.assertTrue(settings.sidecar.enabled)

    def test_round_trip(self):
        settings = config.load()
        settings.language = "en"
        settings.bindings[0].folder = "/tmp/keep"
        settings.bindings[0].path_template = "{YYYY}"
        settings.verification = VERIFY_FAST
        settings.quota = settings.quota.__class__(max_operations=7)
        config.save(settings)
        again = config.load()
        self.assertEqual("en", again.language)
        self.assertEqual("/tmp/keep", again.bindings[0].folder)
        self.assertEqual(VERIFY_FAST, again.verification)
        self.assertEqual(7, again.quota.max_operations)

    def test_a_corrupt_file_is_preserved_and_defaults_are_used(self):
        appdirs.settings_path().write_text("{ nope", encoding="utf-8")
        settings = config.load()
        self.assertEqual("system", settings.language)
        self.assertTrue((self.data / "settings.broken.json").exists())

    def test_unknown_values_fall_back(self):
        settings = config.Settings.from_dict({"density": "enormous", "verification": "magic",
                                              "workers": "many", "similar_threshold": 5})
        self.assertEqual("standard", settings.density)
        self.assertEqual("full", settings.verification)
        self.assertEqual(4, settings.workers)
        self.assertLessEqual(settings.similar_threshold, 1.0)

    def test_similarity_threshold_is_clamped_to_index_capability(self):
        settings = config.Settings.from_dict({"similar_threshold": 0.80})
        self.assertEqual(0.89, settings.similar_threshold)

    def test_a_short_profile_is_padded_to_ten_keys(self):
        settings = config.Settings.from_dict(
            {"profiles": {"P": [{"key": "1", "action": "move"}]}, "current_profile": "P"})
        self.assertEqual(10, len(settings.bindings))

    def test_duplicate_key_detection(self):
        settings = config.Settings()
        settings.ensure_profile()
        settings.bindings[1].key = settings.bindings[0].key
        self.assertEqual([settings.bindings[0].key], settings.duplicate_keys())

    def test_a_key_the_window_already_uses_is_reported(self):
        """Two shortcuts on one key cancel each other out, so neither fires."""
        bindings = [config.Binding(key="1"), config.Binding(key="G"),
                    config.Binding(key="space"), config.Binding(key="")]
        self.assertEqual(["G", "space"],
                         config.reserved_conflicts(bindings, ["g", "Space", "Ctrl+Z"]))

    def test_target_folders_span_every_preset(self):
        settings = config.Settings()
        settings.ensure_profile()
        settings.bindings[0].folder = str(self.tmp / "A")
        settings.profiles["Other"] = config.default_bindings()
        settings.profiles["Other"][0].folder = str(self.tmp / "B")
        folders = {p.name for p in settings.target_folders()}
        self.assertEqual({"A", "B"}, folders)

    def test_target_folders_are_not_resolved_again_by_scanner(self):
        from collections import Counter
        from pathlib import Path
        from unittest.mock import patch
        from qingjian.core import scanner
        settings = config.Settings()
        settings.ensure_profile()
        targets = [self.tmp / "A", self.tmp / "B"]
        for binding, target in zip(settings.bindings, targets):
            binding.folder = str(target)
        real = Path.resolve
        seen = Counter()

        def counted(path, *args, **kwargs):
            seen[str(path)] += 1
            return real(path, *args, **kwargs)

        with patch.object(Path, "resolve", counted):
            normalized = settings.target_folders()
            scanner.pruned_targets(self.tmp, normalized)
        self.assertEqual([1, 1], [seen[str(path)] for path in targets])

    def test_actions_that_need_a_folder(self):
        self.assertTrue(config.Binding("1", "move").needs_folder())
        self.assertFalse(config.Binding("1", "skip").needs_folder())
        self.assertTrue(config.Binding("1", "skip").is_configured())
        self.assertFalse(config.Binding("1", "move").is_configured())


class AppDirsTests(TempCase):
    def test_the_environment_override_wins(self):
        self.assertEqual(self.data, appdirs.data_dir())

    def test_subdirectories_are_created(self):
        for maker in (appdirs.store_dir, appdirs.state_dir, appdirs.cache_dir, appdirs.log_dir):
            self.assertTrue(maker().is_dir())

    def test_a_directory_that_refuses_files_is_not_called_usable(self):
        """``mkdir(exist_ok=True)`` and ``os.access`` both say yes to it."""
        denied = self.deny_writes(self.tmp / "denied")
        appdirs.data_dir()                              # the existing check: no complaint
        if os.name == "nt":
            self.assertTrue(os.access(denied, os.W_OK))  # and Windows says yes as well
        self.assertFalse(appdirs.is_usable(denied))
        self.assertTrue(appdirs.is_usable(self.tmp / "fresh" / "deeper"))

    def test_a_usable_data_directory_is_kept_and_left_as_it_was(self):
        before = sorted(path.name for path in self.data.iterdir())
        root, refused = appdirs.usable_data_dir()
        self.assertEqual(self.data, root)
        self.assertIsNone(refused)
        self.assertEqual(before, sorted(path.name for path in self.data.iterdir()))

    def test_an_unwritable_data_directory_moves_aside_and_names_what_it_refused(self):
        from unittest.mock import patch
        denied = self.plain_launch(self.deny_writes(self.tmp / "program files"))
        spare = self.tmp / "spare"
        with patch.object(appdirs, "fallback_roots", lambda: [spare]):
            root, refused = appdirs.usable_data_dir()
        self.assertTrue(root.is_relative_to(spare))
        self.assertEqual(denied, refused)
        self.assertTrue(root.is_dir())
        self.assertEqual([], self.tree(denied))

    def test_a_data_directory_that_cannot_be_made_moves_aside(self):
        """A per-user directory whose parent is not a directory at all: mkdir
        raises ``OSError`` rather than answering no."""
        from unittest.mock import patch
        blocked = self.plain_launch(self.write(self.tmp / "not-a-folder.txt") / "data")
        spare = self.tmp / "spare"
        with patch.object(appdirs, "fallback_roots", lambda: [spare]):
            root, refused = appdirs.usable_data_dir()
        self.assertTrue(root.is_relative_to(spare))
        self.assertEqual(blocked, refused)

    def test_with_nowhere_writable_it_says_which_places_were_tried(self):
        from unittest.mock import patch
        denied = self.plain_launch(self.deny_writes(self.tmp / "denied"))
        elsewhere = self.tmp / "elsewhere"
        with patch.object(appdirs, "fallback_roots", lambda: [elsewhere]):
            with patch.object(appdirs, "is_usable", lambda root: False):
                with self.assertRaises(appdirs.DataDirUnusable) as caught:
                    appdirs.usable_data_dir()
        self.assertEqual([denied, appdirs._relocated(elsewhere, denied)],
                         caught.exception.paths)

    def test_a_fallback_below_the_refused_directory_is_not_offered(self):
        """It would be refused for the same reason, and named as if it were not."""
        from unittest.mock import patch
        denied = self.plain_launch(self.deny_writes(self.tmp / "denied"))
        with patch.object(appdirs, "fallback_roots", lambda: [denied / "below"]):
            with self.assertRaises(appdirs.DataDirUnusable) as caught:
                appdirs.usable_data_dir()
        self.assertEqual([denied], caught.exception.paths)

    def test_an_explicit_data_directory_is_never_swapped_for_the_real_library(self):
        """``QINGJIAN_DATA_DIR`` promises one thing: that this run cannot touch
        a real library's history. The fallback broke exactly that promise --
        a run pointed at a denied temporary directory relocated into the
        per-user application data folder and left a lock file, logs and
        sqlite's ``-wal``/``-shm`` in somebody's real library. Explicit intent
        now fails loudly instead of quietly writing somewhere else.
        """
        from unittest.mock import patch
        denied = self.deny_writes(self.tmp / "sandbox")
        os.environ[appdirs.ENV_VAR] = str(denied)

        def never(*_args, **_kwargs):                   # neither read nor written
            raise AssertionError("an explicit data directory looked elsewhere")

        with patch.object(appdirs, "fallback_roots", never), \
             patch.object(appdirs, "_platform_data_root", never):
            with self.assertRaises(appdirs.DataDirUnusable) as caught:
                appdirs.usable_data_dir()
        self.assertEqual([denied], caught.exception.paths)
        self.assertEqual([], self.tree(denied))

    def test_without_an_explicit_setting_an_unwritable_default_still_moves_aside(self):
        """The ordinary launch keeps its fallback: a read-only stick or a
        policy-locked application data folder must still start."""
        from unittest.mock import patch
        denied = self.plain_launch(self.deny_writes(self.tmp / "appdata"))
        spare = self.tmp / "spare"
        with patch.object(appdirs, "fallback_roots", lambda: [spare]):
            root, refused = appdirs.usable_data_dir()
        self.assertEqual(denied, refused)
        self.assertTrue(root.is_relative_to(spare))
        self.assertTrue(root.is_dir())
        self.assertEqual([], self.tree(denied))

    def test_the_fallbacks_are_findable_places_not_a_scratch_folder(self):
        """Data must never land where its owner cannot go and look at it."""
        roots = self.real_fallback_roots()           # the scaffolding redirects these
        self.assertIn(appdirs._platform_data_root(), roots)
        self.assertTrue(all(root.is_absolute() for root in roots))

    def test_a_leftover_probe_does_not_make_a_writable_directory_unusable(self):
        """One file left by a kill between create and unlink, or by a scanner
        holding it open, used to brick every later launch handed the same pid:
        the probe name held nothing but that pid and was opened ``O_EXCL``."""
        root = self.tmp / "library"
        root.mkdir()
        leftover = root / f"{appdirs.PROBE_PREFIX}{os.getpid()}"
        leftover.touch()
        self.assertTrue(appdirs.is_usable(root))
        self.assertFalse(leftover.exists())             # and swept up on the way

    def test_a_directory_that_refuses_a_subdirectory_is_not_called_usable(self):
        """Everything the program stores lives in one, and on NTFS adding a
        file and adding a sub-directory are separate rights. Such a folder
        passed the old file-only probe and then took the start-up down with an
        untranslated ``Access is denied`` from ``logsetup.configure``."""
        root = self.deny_subdirectories(self.tmp / "no-subdirs")
        probe = root / "write-probe"                    # a file is still allowed
        probe.touch()
        probe.unlink()
        self.assertFalse(appdirs.is_usable(root))
        self.assertEqual([], self.tree(root))           # and nothing is left behind
        self.assertIsNone(logsetup.configure(True, 3, directory=root / "logs"))

    def test_a_directory_that_refuses_to_let_the_probe_go_is_not_called_usable(self):
        """An un-deletable probe is information about the directory, not noise:
        sqlite cannot keep a journal where files may not be deleted."""
        from unittest.mock import patch
        root = self.tmp / "no-deletes"
        root.mkdir()
        with patch("os.unlink", side_effect=PermissionError(13, "denied")):
            self.assertFalse(appdirs.is_usable(root))

    def test_a_busy_moment_is_retried_rather_than_read_as_a_refusal(self):
        """A share still mounting or a scanner on the probe must not fork the
        library: relocating on the first refusal strands the user's work."""
        from unittest.mock import patch
        answers = [False, False, True]
        with patch.object(appdirs, "is_usable", lambda _root: answers.pop(0)):
            with patch.object(appdirs, "time") as clock:
                root, refused = appdirs.usable_data_dir()
        self.assertEqual(self.data, root)
        self.assertIsNone(refused)
        self.assertEqual(2, clock.sleep.call_count)

    def test_a_fallback_gets_an_area_of_its_own_and_says_where_it_came_from(self):
        """A read-only portable copy must not start writing into the installed
        copy's history, journal and snapshot store."""
        from unittest.mock import patch
        denied = self.plain_launch(self.deny_writes(self.tmp / "stick"))
        spare = self.tmp / "appdata"
        with patch.object(appdirs, "fallback_roots", lambda: [spare]):
            root, refused = appdirs.usable_data_dir()
            other = appdirs._relocated(spare, self.tmp / "another stick")
            self.assertNotEqual(other, root)            # one area per refused place
            self.assertEqual(str(denied),
                             (root / appdirs.ORIGIN_MARKER).read_text(
                                 encoding="utf-8").strip())
            # The directory works again: the earlier run's data is named once.
            self.write(root / "settings.json", b"{}")
            self.assertIsNone(appdirs.stranded_data(self.data))
            self.assertEqual(root, appdirs.stranded_data(denied))
            self.assertIsNone(appdirs.stranded_data(denied))   # told once, not nagged

    def test_the_temporary_directory_is_marked_as_one_the_system_may_empty(self):
        import tempfile
        self.assertTrue(appdirs.is_volatile(Path(tempfile.gettempdir()) / "qingjian-data"))
        self.assertFalse(appdirs.is_volatile(appdirs._platform_data_root()))


class LoggingTests(TempCase):
    def test_a_log_file_is_written(self):
        path = logsetup.configure(True, 3, directory=self.data / "logs")
        logsetup.get_logger("t").warning("hello %s", "world")
        self.assertIn("hello world", path.read_text(encoding="utf-8"))

    def test_logging_can_be_switched_off(self):
        self.assertIsNone(logsetup.configure(False, directory=self.data / "logs"))
        logsetup.get_logger("t").warning("nothing should be written")

    def test_the_bundle_carries_logs_and_environment_only(self):
        logsetup.configure(True, 3, directory=self.data / "logs")
        logsetup.get_logger("t").info("something")
        (self.data / "store").mkdir(exist_ok=True)
        (self.data / "store" / "journal.json").write_text("{}", encoding="utf-8")
        media = self.data / "holiday.JPG"
        media.write_bytes(b"a photograph")
        bundle = logsetup.diagnostic_bundle(self.tmp / "bundle.zip", self.data)
        names = zipfile.ZipFile(bundle).namelist()
        self.assertIn("environment.json", names)
        self.assertIn("store/journal.json", names)
        self.assertFalse(any(name.endswith(".JPG") for name in names))

    def test_the_environment_report_names_the_optional_modules(self):
        report = logsetup.environment_report()
        self.assertIn("PySide6", report["modules"])
        self.assertIn("app_version", report)


class DiagnosticBundleTests(TempCase):
    """The bundle is meant to be attachable to a public bug report.

    The settings page promises in words what the zip carries, so the words and
    the zip are driven by one manifest and checked against each other here.
    """

    ACCOUNT = "rj-privacy-probe"
    # Spaces on purpose: a folder named after a person is exactly what must not
    # travel, and a path that holds spaces is the one a masker gets wrong.
    LIBRARY = "Wedding Chen 2024"
    TARGET = "Keep Grandmother"

    def _populate(self):
        """Build a data directory that carries every kind of leak at once."""
        library = self.tmp / "Users" / self.ACCOUNT / "Pictures" / self.LIBRARY
        target = library / self.TARGET
        photo = library / "IMG 0001.JPG"
        self.write(photo, b"a photograph")
        target.mkdir(parents=True, exist_ok=True)

        settings = config.Settings()
        settings.source_folder = str(library)
        settings.last_path = str(photo)
        bindings = settings.bindings
        bindings[0].folder = str(target)
        settings.set_bindings(bindings)
        config.save(settings, self.data / "settings.json")

        from qingjian.core import state
        store = state.StateStore(self.data / "state" / "state.db")
        self.addCleanup(store.close)
        store.apply({
            "records_add": [{"id": "r1", "action": "move", "original": str(photo),
                             "destination": str(target / photo.name),
                             "root": str(library), "time_epoch": 1.0, "bytes": 12}],
            "reviews_add": [(str(library), str(photo))],
            "tags_set": [(str(photo), 4, "green")],
        })

        (self.data / "store").mkdir(exist_ok=True)
        (self.data / "store" / "journal.json").write_text(json.dumps(
            {"stage_id": "s1", "label": "move",
             "steps": [{"kind": "move", "src": str(photo),
                        "dst": str(target / photo.name)}]}), encoding="utf-8")
        logsetup.configure(True, 3, directory=self.data / "logs")
        # INFO rather than WARNING only to keep the configured stderr handler
        # from printing the very paths this test is about into the test output.
        log = logsetup.get_logger("probe")
        log.info("opened %s", library)
        log.info("could not move %s into %s: denied", photo, target)
        return library, photo, target

    @staticmethod
    def _blob(bundle) -> str:
        """Every byte the zip carries, member names included."""
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            return "\n".join(names + [archive.read(name).decode("utf-8", "replace")
                                      for name in names])

    def test_the_bundle_carries_no_library_path_and_no_account_name(self):
        import getpass
        library, photo, target = self._populate()
        bundle = logsetup.diagnostic_bundle(self.tmp / "bundle.zip", self.data)
        blob = self._blob(bundle)
        secrets = [self.ACCOUNT, self.LIBRARY, self.TARGET, photo.name,
                   str(library), str(photo), str(target), str(self.data),
                   # No fragment of a name may survive either, which is how a
                   # masker that cuts a path at the first space gives one away.
                   "Wedding", "Chen", "Grandmother", "0001.JPG"]
        account = getpass.getuser()
        if len(account) >= 4:
            secrets.append(account)
        for secret in secrets:
            # assertNotIn would print the whole zip; the name alone is the point.
            self.assertFalse(secret in blob, f"{secret!r} survived into the bundle")

    def test_the_bundle_still_answers_what_support_has_to_ask(self):
        from qingjian import STATE_SCHEMA, __version__
        self._populate()
        bundle = logsetup.diagnostic_bundle(self.tmp / "bundle.zip", self.data)
        with zipfile.ZipFile(bundle) as archive:
            environment = json.loads(archive.read("environment.json"))
            settings = json.loads(archive.read("settings.json"))
            summary = json.loads(archive.read("state-summary.json"))
            journal = json.loads(archive.read("store/journal.json"))
            log_text = archive.read("logs/qingjian.log").decode("utf-8")

        self.assertEqual(__version__, environment["app_version"])
        self.assertIn("PySide6", environment["modules"])
        self.assertTrue(environment["platform"])
        self.assertTrue(environment["python"])

        # Settings that are not paths survive untouched; paths keep their shape.
        self.assertEqual(4, settings["workers"])
        self.assertEqual("soft", settings["recycle_mode"])
        self.assertRegex(settings["source_folder"], r"depth=\d+")
        bindings = settings["profiles"][settings["current_profile"]]
        self.assertEqual("move", bindings[0]["action"])
        self.assertRegex(bindings[0]["folder"], r"depth=\d+")

        # The database is described rather than shipped: shape and row counts.
        self.assertEqual(str(STATE_SCHEMA), summary["schema"])
        self.assertEqual(1, summary["rows"]["records"])
        self.assertEqual(1, summary["rows"]["reviews"])
        self.assertEqual(1, summary["rows"]["tags"])
        self.assertIn("original", summary["columns"]["records"])

        self.assertEqual("s1", journal["stage_id"])
        self.assertEqual("move", journal["steps"][0]["kind"])

        self.assertIn("could not move", log_text)
        self.assertIn("denied", log_text)
        self.assertIn("INFO    qingjian.probe", log_text)

        # One path keeps one tag across the bundle, so a report can still be
        # followed from a log line to the setting that points at the same
        # folder -- which is what is left once the name itself is gone.
        tag = re.search(r"id=[0-9a-f]+", settings["source_folder"])
        self.assertIsNotNone(tag)
        self.assertIn("opened <path drive=", log_text)
        self.assertIn(tag.group(0), log_text)

        # Two paths in one sentence stay two masks rather than merging into one.
        self.assertEqual(3, log_text.count("<path "))

    def test_the_settings_page_lists_exactly_what_the_zip_contains(self):
        self._populate()
        bundle = logsetup.diagnostic_bundle(self.tmp / "bundle.zip", self.data)
        names = set(zipfile.ZipFile(bundle).namelist())
        previous = i18n.get_language()
        self.addCleanup(i18n.set_language, previous)
        for language in i18n.LANGUAGE_CODES:
            i18n.set_language(language)
            description = logsetup.bundle_description()
            for entry, key in logsetup.BUNDLE_MANIFEST:
                with self.subTest(language=language, entry=entry):
                    if entry.endswith("/"):
                        self.assertTrue(any(name.startswith(entry) for name in names),
                                        f"{entry} is listed but the zip has nothing in it")
                    else:
                        self.assertIn(entry, names)
                    self.assertIn(entry, description)
                    self.assertIn(i18n.tr(key), description)
            for key in logsetup.BUNDLE_OMITS:
                with self.subTest(language=language, key=key):
                    self.assertIn(i18n.tr(key), description)
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertTrue(
                    any(name == entry or (entry.endswith("/") and name.startswith(entry))
                        for entry, _ in logsetup.BUNDLE_MANIFEST),
                    f"{name} is in the zip but not in the list the user is shown")

    def test_masking_keeps_the_sentence_and_drops_the_path(self):
        import getpass
        account = getpass.getuser()
        masked = logsetup.redact_text(
            f"could not move C:\\Users\\{account}\\Pictures\\Trip\\a b.jpg into "
            f"\\\\nas\\share\\Keep: denied for {account}")
        self.assertIn("could not move", masked)
        self.assertIn("denied", masked)
        secrets = ["Pictures", "Trip", "a b.jpg", "nas", "share", "Keep"]
        if len(account) >= 4:
            secrets.append(account)
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, masked)
        # Two paths in one sentence are two masks, not one.
        self.assertEqual(2, masked.count("<path "))
        self.assertIn("drive=UNC", masked)

    def test_an_account_named_after_a_word_in_the_mask_does_not_eat_it(self):
        """A real account can be called "pat", which is inside "<path ...>"."""
        from unittest.mock import patch
        with patch.object(logsetup, "_account_names", return_value=("pat",)):
            masked = logsetup.redact_text(r"moved C:\Users\pat\a.jpg: denied for pat")
        self.assertNotIn("pat", re.sub(r"<path [^>]*>", "", masked))
        self.assertEqual(1, masked.count("<path drive=C: depth=3 ext=.jpg"))
        self.assertTrue(masked.endswith(f"denied for {logsetup.ACCOUNT_MASK}"), masked)

    def test_two_paths_in_one_line_are_two_masks_whatever_joins_them(self):
        """A segment may hold spaces, so the first match used to run on past
        the end of path one, swallow path two's drive letter and leave the rest
        of it -- folder names, file name and all -- written out in full."""
        first, second = r"C:\pics\Wedding Chen", r"D:\Keep Nana\x.jpg"
        for glue in ("a=%s b=%s", "'%s' and '%s'", "'%s' to '%s'", "'%s', None, '%s'",
                     "(%s)(%s)", "%s -> %s", "from %s into %s: denied"):
            with self.subTest(glue=glue):
                masked = logsetup.redact_text(glue % (first, second))
                for secret in ("Wedding", "Chen", "Keep", "Nana", "x.jpg", ":\\"):
                    self.assertFalse(secret in masked,
                                     f"{secret!r} survived {glue!r}: {masked!r}")
                self.assertEqual(2, masked.count("<path "), masked)

    def test_a_path_with_no_drive_letter_of_its_own_is_masked_too(self):
        """Four spellings the pattern used to walk straight past. The last two
        the folder editor accepts as absolute, so they really do get stored."""
        slash = "\\"
        cases = {
            "~/Pictures/Chen Jianguo Private": "drive=~",
            slash * 2 + "NAS-CHEN-JIANGUO is offline": "drive=UNC",
            slash + "Users" + slash + "pat" + slash + "Chen Wedding" + slash + "a.jpg":
                "drive=/",
            slash * 2 + "?" + slash + "UNC" + slash + "FILESRV" + slash + "Photos"
                + slash + "Chen Wedding" + slash + "a.jpg": "drive=UNC",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                masked = logsetup.redact_text(text)
                self.assertIn(expected, masked)
                for secret in ("Chen", "NAS", "Pictures", "Photos", "FILESRV"):
                    self.assertNotIn(secret, masked)

    def test_a_web_address_is_not_read_as_a_drive(self):
        """``https://host/page/two`` has a letter before a colon before two
        slashes, which is what a drive and a share look like."""
        for text in ("see https://example.com/page/two for help",
                     "store/journal.json missing", r"regex a\bcd matched"):
            with self.subTest(text=text):
                self.assertEqual(text, logsetup.redact_text(text))

    def test_a_template_keeps_its_shape_and_loses_every_literal(self):
        """``path_template`` is a folder the program then creates and
        ``name_template`` a file it then writes, both typed by hand into a
        plain text box, and neither looks remotely like an absolute path."""
        masked = logsetup.redact_settings({
            "current_profile": "Chen Jianguo wedding 2024",
            "profiles": {"Chen Jianguo wedding 2024": [
                {"key": "1", "action": "move",
                 "path_template": "Chen Jianguo Family/{YYYY}/{MM}",
                 "name_template": "{name}-chen-jianguo-{seq:4}"}],
                "Nana": [{"key": "2", "path_template": "{camera|Grandmother}"}]}})
        blob = json.dumps(masked, ensure_ascii=False)
        for secret in ("Chen", "Jianguo", "chen", "jianguo", "Nana", "Grandmother"):
            self.assertFalse(secret in blob, f"{secret!r} survived: {blob}")
        # What a template report needs survives: levels, tokens, padding.
        bindings = masked["profiles"]["preset 1"]
        self.assertEqual("<text len=19>/{YYYY}/{MM}", bindings[0]["path_template"])
        self.assertEqual("{name}<text len=14>{seq:4}", bindings[0]["name_template"])
        self.assertEqual("preset 1", masked["current_profile"])
        self.assertEqual("{camera|<text len=11>}",
                         masked["profiles"]["preset 2"][0]["path_template"])
        self.assertEqual("{?}", logsetup._mask_template("{yyyy}"))   # not a token

    def test_a_journal_carries_no_fingerprint_of_the_file_contents(self):
        """A journal exists only when a transaction failed, which is exactly
        when a bundle is exported. A SHA-256 of the bytes lets anyone holding
        the zip test whether the user has one particular file."""
        digest = "0ef6743fb09cbbe47ff1d560ffe8324ee71e531da799797b7750d8ca5e96f860"
        masked = logsetup.redact_journal({"steps": [
            {"kind": "move", "src_id": {"size": 41, "mtime_ns": 1791267856088198400,
                                        "hash": digest},
             "result": {"size": 41, "mtime_ns": 1791267856088198400, "hash": digest}}]})
        blob = json.dumps(masked)
        self.assertNotIn(digest, blob)
        self.assertNotIn("1791267856088198400", blob)
        step = masked["steps"][0]
        self.assertEqual(41, step["src_id"]["size"])            # kept: it is diagnosis
        self.assertEqual(1791267856, step["src_id"]["mtime_s"])  # the second, not the ns
        # One file keeps one tag, so two steps can still be matched up.
        self.assertEqual(step["src_id"]["hash"], step["result"]["hash"])
        self.assertRegex(step["src_id"]["hash"], r"^<content id=[0-9a-f]+>$")

    def test_a_file_that_cannot_be_read_is_not_named_in_the_bundle(self):
        """The one member a failure writes used to be the one that gave the
        game away: ``str(OSError)`` ends in the file's own absolute path, which
        on a normal install begins ``C:\\Users\\<account>``."""
        from unittest.mock import patch
        self._populate()
        # With the filename, exactly as the operating system raises it.
        denied = PermissionError(13, "Permission denied",
                                 str(self.data / "settings.json"))
        with patch("pathlib.Path.read_text", side_effect=denied):
            bundle = logsetup.diagnostic_bundle(self.tmp / "denied.zip", self.data)
        blob = self._blob(bundle)
        for secret in (str(self.data), str(self.tmp), self.ACCOUNT, os.sep + "Users"):
            self.assertFalse(secret in blob, f"{secret!r} survived into the bundle")
        with zipfile.ZipFile(bundle) as archive:
            settings = json.loads(archive.read("settings.json"))
            index = json.loads(archive.read("logs/index.json"))
        self.assertEqual("Permission denied", settings["unreadable"])
        self.assertEqual(13, settings["errno"])
        self.assertEqual("Permission denied", index[0]["unreadable"])
        self.assertEqual("qingjian.log", index[0]["name"])       # still says which
        self.assertNotIn("settings.json", settings["unreadable"])


class PlatformTests(TempCase):
    def test_recycle_policy_percent_can_be_stricter_than_volume_limit(self):
        import ctypes
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        kernel, shell = Mock(), Mock()
        kernel.GetDriveTypeW.return_value = 3
        def space(_root, free, total, _unused):
            free._obj.value = 100_000_000
            total._obj.value = 100_000_000
            return 1
        def bin_usage(_root, info):
            info._obj.i64Size = 900_000
            return 0
        kernel.GetDiskFreeSpaceExW.side_effect = space
        shell.SHQueryRecycleBinW.side_effect = bin_usage
        with patch.object(platform_, "IS_WINDOWS", True), \
             patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel, shell32=shell)), \
             patch.object(platform_, "_volume_recycle_limit", return_value=50_000_000), \
             patch.object(platform_, "_recycle_policy", return_value=(1, True)) as policy:
            self.assertFalse(platform_.can_recycle(self.tmp / "photo.jpg", 200_000))
            policy.return_value = (None, False)
            self.assertFalse(platform_.can_recycle(self.tmp / "photo.jpg", 1))
            policy.return_value = None
            self.assertFalse(platform_.can_recycle(self.tmp / "photo.jpg", 1))

    def test_recycle_policy_registry_states_fail_closed(self):
        import winreg
        from unittest.mock import Mock, patch
        key = Mock()
        key.__enter__ = Mock(return_value=key)
        key.__exit__ = Mock(return_value=False)
        with patch.object(winreg, "OpenKey", return_value=key), \
             patch.object(winreg, "QueryValueEx", side_effect=FileNotFoundError):
            self.assertEqual((None, True), platform_._recycle_policy())
        with patch.object(winreg, "OpenKey", side_effect=PermissionError):
            self.assertIsNone(platform_._recycle_policy())
        with patch.object(winreg, "OpenKey", return_value=key), \
             patch.object(winreg, "QueryValueEx", side_effect=lambda _key, name: (
                 ("bad", winreg.REG_SZ) if name == "RecycleBinSize" else
                 (0, winreg.REG_DWORD))):
            self.assertIsNone(platform_._recycle_policy())
        with patch.object(winreg, "OpenKey", return_value=key), \
             patch.object(winreg, "QueryValueEx", side_effect=lambda _key, name: (
                 (1, winreg.REG_DWORD) if name == "NoRecycleFiles" else
                 (5, winreg.REG_DWORD))):
            self.assertEqual((5, False), platform_._recycle_policy())

    def test_can_recycle_checks_fixed_volume_query_and_actual_capacity(self):
        import ctypes
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        kernel, shell = Mock(), Mock()
        kernel.GetDriveTypeW.return_value = 3
        def free_space(_root, free, total, _unused):
            free._obj.value = 2_000_000
            total._obj.value = 100_000_000
            return 1
        def bin_query(_root, info):
            info._obj.i64Size = 500_000
            return 0
        kernel.GetDiskFreeSpaceExW.side_effect = free_space
        shell.SHQueryRecycleBinW.side_effect = bin_query
        with patch.object(platform_, "IS_WINDOWS", True), \
             patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel, shell32=shell)), \
             patch.object(platform_, "_volume_recycle_limit", return_value=1_000_000), \
             patch.object(platform_, "_recycle_policy", return_value=(None, True)):
            path = self.tmp / "photo.jpg"
            self.assertTrue(platform_.can_recycle(path, 400_000))
            self.assertFalse(platform_.can_recycle(path, 600_000))
            kernel.GetDriveTypeW.return_value = 4
            self.assertFalse(platform_.can_recycle(path, 1))
            kernel.GetDriveTypeW.return_value = 3
            shell.SHQueryRecycleBinW.return_value = 1
            shell.SHQueryRecycleBinW.side_effect = None
            self.assertFalse(platform_.can_recycle(path, 1))
        with patch.object(platform_, "IS_WINDOWS", True), \
             patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel, shell32=shell)), \
             patch.object(platform_, "_volume_recycle_limit", return_value=None), \
             patch.object(platform_, "_recycle_policy", return_value=(None, True)):
            shell.SHQueryRecycleBinW.return_value = 0
            self.assertFalse(platform_.can_recycle(self.tmp / "photo.jpg", 1))

    def test_recycle_limit_reads_current_user_volume_setting(self):
        import ctypes
        import winreg
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        kernel = Mock()
        def volume_name(_root, buffer, _length):
            buffer.value = "\\\\?\\Volume{12345678-1234-1234-1234-123456789abc}\\"
            return 1
        kernel.GetVolumeNameForVolumeMountPointW.side_effect = volume_name
        key = Mock()
        key.__enter__ = Mock(return_value=key)
        key.__exit__ = Mock(return_value=False)
        with patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel)), \
             patch.object(winreg, "OpenKey", return_value=key) as opened, \
             patch.object(winreg, "QueryValueEx", side_effect=[
                 (10, winreg.REG_DWORD), (0, winreg.REG_DWORD)]):
            self.assertEqual(10 * 1024 * 1024, platform_._volume_recycle_limit("E:\\"))
        self.assertIn("{12345678-1234-1234-1234-123456789abc}", opened.call_args.args[1])

    def test_system_recycle_rejects_unsupported_platform_and_unc(self):
        from unittest.mock import patch
        with patch.object(platform_, "IS_WINDOWS", False):
            self.assertFalse(platform_.can_recycle(self.tmp / "photo.jpg"))
        with patch.object(platform_, "IS_WINDOWS", True):
            self.assertFalse(platform_.can_recycle(r"\\server\share\photo.jpg"))

    def test_reserved_names(self):
        self.assertTrue(platform_.is_reserved_name("CON"))
        self.assertTrue(platform_.is_reserved_name("com1.txt"))
        self.assertFalse(platform_.is_reserved_name("common.txt"))

    def test_volume_identity(self):
        self.assertIsNotNone(platform_.volume_id(self.tmp))
        self.assertTrue(platform_.same_volume(self.tmp, self.tmp / "does" / "not" / "exist"))

    def test_free_space_is_reported(self):
        self.assertIsInstance(platform_.free_space(self.tmp), int)

    def test_nearest_existing_walks_up(self):
        self.assertEqual(self.tmp.resolve(),
                         platform_.nearest_existing(self.tmp / "a" / "b" / "c").resolve())

    def test_trash_reports_absence_instead_of_failing_quietly(self):
        if platform_.trash_available():
            self.skipTest("send2trash present")
        with self.assertRaises(platform_.TrashUnavailable):
            platform_.move_to_trash(self.write(self.tmp / "x.bin"))

    SHELL = r"Software\Classes\Directory\shell\Qingjian"
    BACKGROUND = r"Software\Classes\Directory\Background\shell\Qingjian"

    def test_the_folder_menu_opens_the_folder_that_was_clicked(self):
        registry = FakeRegistry()
        platform_.set_folder_menu(True, "Open with Qingjian", [r"C:\Apps\MediaSorter.exe"],
                                  registry=registry)
        self.assertEqual("Open with Qingjian", registry.keys[self.SHELL][""])
        self.assertEqual('"C:\\Apps\\MediaSorter.exe" "%1"',
                         registry.keys[self.SHELL + r"\command"][""])
        self.assertEqual('"C:\\Apps\\MediaSorter.exe" "%V"',
                         registry.keys[self.BACKGROUND + r"\command"][""])
        self.assertTrue(platform_.folder_menu_installed(registry=registry))

    def test_removing_the_folder_menu_leaves_nothing_behind(self):
        registry = FakeRegistry()
        platform_.set_folder_menu(True, "Open with Qingjian", [r"C:\Apps\MediaSorter.exe"],
                                  registry=registry)
        platform_.set_folder_menu(False, registry=registry)
        self.assertEqual({}, registry.keys)
        self.assertFalse(platform_.folder_menu_installed(registry=registry))
        platform_.set_folder_menu(False, registry=registry)     # a second removal is harmless

    def test_a_source_checkout_launches_without_a_console_window(self):
        folder = self.tmp / "python"
        self.write(folder / "python.exe", b"")
        self.write(folder / "pythonw.exe", b"")
        script = str(self.tmp / "main.py")
        self.assertEqual([str(folder / "pythonw.exe"), script],
                         platform_.launch_command(False, str(folder / "python.exe"), script))
        self.assertEqual([r"C:\Apps\MediaSorter.exe"],
                         platform_.launch_command(True, r"C:\Apps\MediaSorter.exe", script))


class FakeRegistry:
    """The part of winreg the folder menu touches.

    Includes the rule that matters: a key that still has subkeys cannot be
    deleted, so removal has to go leaf first.
    """

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def CreateKey(self, root, path):            # noqa: N802 - winreg naming
        self.keys.setdefault(path, {})
        return path

    def OpenKey(self, root, path):              # noqa: N802 - winreg naming
        if path not in self.keys:
            raise FileNotFoundError(path)
        return path

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802 - winreg naming
        self.keys[key][name] = value

    def CloseKey(self, key):                    # noqa: N802 - winreg naming
        pass

    def DeleteKey(self, root, path):            # noqa: N802 - winreg naming
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(other.startswith(path + "\\") for other in self.keys):
            raise PermissionError("a key with subkeys cannot be deleted")
        del self.keys[path]


if __name__ == "__main__":
    unittest.main()
