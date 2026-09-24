"""A headless self-test, so the interface layer can be verified on a real machine.

Run it with::

    MediaSorter.exe --selfcheck
    python -m qingjian --selfcheck

It builds a throwaway library in a temporary folder, exercises the core, then
constructs the real window offscreen and drives it: switches language, changes
view, opens each dialog, classifies a file, undoes it. Nothing outside the
temporary folder is touched.
"""
from __future__ import annotations

import faulthandler
import functools
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []


def _reject_active_modal(errors: list[str], active=None) -> None:
    """Close an unexpected modal dialog so an unattended self-check can finish."""
    if active is None:
        from PySide6.QtWidgets import QApplication
        active = QApplication.activeModalWidget
    dialog = active()
    if dialog is None:
        return
    title = dialog.windowTitle() if hasattr(dialog, "windowTitle") else ""
    text = dialog.text() if hasattr(dialog, "text") else ""
    description = f"unexpected modal {type(dialog).__name__}: {title!r} {text!r}"
    if description not in errors:
        errors.append(description)
    if hasattr(dialog, "reject"):
        dialog.reject()
    else:
        dialog.close()


class _CallbackExceptionMonitor:
    """Collect exceptions escaping Python callbacks in the UI package."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self._old_hook = None
        self._old_unraisable = None
        self._old_trace = None
        self._tool_id = None
        self._wrapped: list[tuple[type, str, object]] = []

    def _add_error(self, error: BaseException) -> None:
        detail = f"{type(error).__name__}: {error}"
        if detail not in self.errors:
            self.errors.append(detail)

    def watch_class(self, cls: type) -> None:
        """Wrap Python Qt callbacks so binding-layer exception handling cannot hide them."""
        for name, original in list(cls.__dict__.items()):
            if not callable(original):
                continue
            if not (name.endswith("Event") or name in ("event", "eventFilter")):
                continue

            def guarded(instance, *args, __original=original, **kwargs):
                try:
                    return __original(instance, *args, **kwargs)
                except BaseException as error:
                    self._add_error(error)
                    raise

            self._wrapped.append((cls, name, original))
            setattr(cls, name, guarded)

    def _trace(self, frame, event, arg):
        if event == "exception":
            name = frame.f_code.co_name
            if name.endswith("Event") or name in ("event", "eventFilter"):
                _kind, value, _tb = arg
                self._add_error(value)
        return self._trace

    def record(self, exc_info) -> None:
        _kind, value, _tb = exc_info
        self._add_error(value)

    def __enter__(self):
        self._old_hook = sys.excepthook

        def hook(kind, value, tb):
            self.record((kind, value, tb))

        sys.excepthook = hook
        self._old_unraisable = sys.unraisablehook

        def unraisable(details):
            self.record((details.exc_type, details.exc_value, details.exc_traceback))

        sys.unraisablehook = unraisable
        self._old_trace = sys.gettrace()
        sys.settrace(self._trace)
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is not None:
            tool_id = monitoring.DEBUGGER_ID
            try:
                monitoring.use_tool_id(tool_id, "qingjian-selfcheck")

                def unwind(code, _offset, exception):
                    callback = (code.co_name.endswith("Event") or
                                code.co_name in ("event", "eventFilter"))
                    if callback:
                        self._add_error(exception)

                monitoring.register_callback(tool_id, monitoring.events.PY_UNWIND, unwind)
                monitoring.set_events(tool_id, monitoring.events.PY_UNWIND)
                self._tool_id = tool_id
            except (ValueError, RuntimeError):
                self._tool_id = None
        return self

    def __exit__(self, _kind, _value, _tb) -> None:
        for cls, name, original in reversed(self._wrapped):
            setattr(cls, name, original)
        self._wrapped.clear()
        sys.settrace(self._old_trace)
        sys.unraisablehook = self._old_unraisable
        sys.excepthook = self._old_hook
        if self._tool_id is not None:
            monitoring = sys.monitoring
            monitoring.set_events(self._tool_id, 0)
            monitoring.register_callback(self._tool_id, monitoring.events.PY_UNWIND, None)
            monitoring.free_tool_id(self._tool_id)


def check(name: str):
    def wrapper(function):
        @functools.wraps(function)
        def run(*args, **kwargs):
            started = time.perf_counter()
            try:
                detail = function(*args, **kwargs) or ""
                RESULTS.append((name, True, f"{detail} ({time.perf_counter() - started:.2f}s)"))
                return True
            except Exception as error:
                RESULTS.append((name, False, f"{type(error).__name__}: {error}"))
                if os.environ.get("QINGJIAN_SELFCHECK_TRACE"):
                    traceback.print_exc()
                return False
        return run
    return wrapper


def _make_library(root: Path) -> dict:
    """A tiny library, built without the test fixtures so a frozen build works."""
    from PIL import Image
    from datetime import datetime
    day = root / "Day1"
    day.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    exif[0x010F] = "Selfcheck"
    exif[0x0110] = "Camera"
    exif[0x8769] = {0x9003: datetime(2026, 8, 14, 19, 42, 8).strftime("%Y:%m:%d %H:%M:%S"),
                    0x8827: 200}
    blob = exif.tobytes()
    made = []
    for index in range(6):
        image = Image.new("RGB", (640 + index * 8, 480), (30 + index * 20, 60, 120))
        for x in range(0, 640, 24):
            for y in range(0, 480, 24):
                image.putpixel((min(x + index, 639), y), (240, 220, 180))
        path = day / f"IMG_{index + 1:04d}.JPG"
        image.save(path, exif=blob, quality=92)
        made.append(path)
    (day / "IMG_0001.CR2").write_bytes(b"raw-placeholder" * 200)
    (day / "IMG_0001.XMP").write_text("<x:xmpmeta/>", encoding="utf-8")
    (root / "Backup").mkdir(exist_ok=True)
    (root / "Backup" / "IMG_0002.JPG").write_bytes((day / "IMG_0002.JPG").read_bytes())
    return {"root": root, "files": made}


def run(argv: list[str] | None = None) -> int:
    argv = argv or []
    keep = "--keep" in argv
    RESULTS.clear()
    workspace = Path(tempfile.mkdtemp(prefix="qingjian-selfcheck-"))
    data = workspace / "data"
    data.mkdir()
    os.environ["QINGJIAN_DATA_DIR"] = str(data)
    library = workspace / "library"
    print(f"workspace: {workspace}")

    checks = (
        (_check_imports, ()),
        (_check_catalogue, ()),
        (_check_core, (library, data)),
        (_check_scale, (workspace,)),
        (_check_history, (workspace,)),
        (_check_duplicates, (workspace,)),
        (_check_ui, (library, data)),
    )
    faulthandler.dump_traceback_later(240, exit=True)
    try:
        for function, args in checks:
            function(*args)
            name, passed, detail = RESULTS[-1]
            print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}", flush=True)
    finally:
        faulthandler.cancel_dump_traceback_later()

    print()
    width = max(len(name) for name, _, _ in RESULTS) + 2
    for name, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name.ljust(width)} {detail}")
    failures = [name for name, passed, _ in RESULTS if not passed]
    print()
    print(f"{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
    if not keep:
        import shutil
        shutil.rmtree(workspace, ignore_errors=True)
    else:
        print(f"workspace kept at {workspace}")
    return 0 if not failures else 2


@check("imports")
def _check_imports() -> str:
    import importlib
    names = [
        "qingjian.core.i18n", "qingjian.core.mediatypes", "qingjian.core.appdirs",
        "qingjian.core.logsetup", "qingjian.core.platform_", "qingjian.core.naming",
        "qingjian.core.sidecar", "qingjian.core.template", "qingjian.core.exifread",
        "qingjian.core.isobmff", "qingjian.core.metadata", "qingjian.core.imaging",
        "qingjian.core.hashcache", "qingjian.core.dedupe", "qingjian.core.safestore",
        "qingjian.core.state", "qingjian.core.ops", "qingjian.core.scanner",
        "qingjian.core.opqueue", "qingjian.core.config", "qingjian.core.engine",
        "qingjian.core.video",
    ]
    for name in names:
        importlib.import_module(name)
    optional = {}
    for name in ("PySide6", "PIL", "numpy", "av", "send2trash"):
        try:
            module = importlib.import_module(name)
            optional[name] = getattr(module, "__version__", "present")
        except Exception:
            optional[name] = "MISSING"
    if getattr(sys, "frozen", False):
        missing = [name for name in ("av", "send2trash") if optional[name] == "MISSING"]
        from qingjian.core import video
        if not video.available() and "av" not in missing:
            missing.append("PyAV runtime")
        if sys.platform == "win32":
            from PySide6.QtCore import QLibraryInfo
            plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
            if not (plugins / "platforms" / "qwindows.dll").is_file():
                missing.append("platforms/qwindows.dll")
            multimedia = plugins / "multimedia"
            if not multimedia.is_dir() or not any(multimedia.iterdir()):
                missing.append("multimedia plugin")
        if missing:
            raise AssertionError("frozen bundle missing: " + ", ".join(missing))
    return " ".join(f"{k}={v}" for k, v in optional.items())


@check("string catalogue")
def _check_catalogue() -> str:
    from qingjian.core import i18n
    problems = i18n.catalog_problems()
    if problems:
        raise AssertionError("; ".join(problems[:5]))
    for code in i18n.LANGUAGE_CODES:
        i18n.set_language(code)
        i18n.tr("app.tagline")
    i18n.set_language("zh")
    return f"{len(i18n.CATALOG)} keys x {len(i18n.LANGUAGE_CODES)} languages"


@check("core operations")
def _check_core(library: Path, data: Path) -> str:
    from qingjian.core import config, dedupe
    from qingjian.core.engine import Engine
    made = _make_library(library)
    settings = config.Settings()
    settings.recursive = True
    keep = library.parent / "Keepers"
    settings.bindings[0].folder = str(keep)
    settings.bindings[0].path_template = "{YYYY}/{YYYY-MM}"
    settings.bindings[0].name_template = "{YYYY-MM-DD}_{seq:4}_{name}"
    engine = Engine(data, settings)
    try:
        engine.open_folder(library)
        assert engine.queue_paths, "no media found"
        engine.go_to(made["files"][0])
        outcome = engine.classify(settings.bindings[0])
        assert outcome.record is not None, "classify produced no record"
        moved = sorted(p.name for p in keep.rglob("*") if p.is_file())
        assert len(moved) == 3, f"sidecars did not travel: {moved}"
        engine.undo()
        restored = library / "Day1"
        for suffix in ("JPG", "CR2", "XMP"):
            assert (restored / f"IMG_0001.{suffix}").exists(), f"undo lost IMG_0001.{suffix}"
        assert not any(path.is_file() for path in keep.rglob("*")), "undo left files behind"
        exact = engine.find_duplicates(dedupe.MODE_EXACT)
        assert len(exact) == 1, f"expected one duplicate group, got {len(exact)}"
        engine.find_duplicates(dedupe.MODE_SIMILAR)
        engine.tag([made["files"][1]], rating=4, label="green")
        assert engine.state.tag(str(made["files"][1]))[0] == 4
        engine.export_csv(data / "records.csv")
        engine.diagnostic_bundle(data / "bundle.zip")
        return f"{len(engine.all_files)} files, {len(engine.queue_paths)} queue rows"
    finally:
        engine.close()


@check("large folder")
def _check_scale(workspace: Path) -> str:
    """Prove the folder-sized costs are gone, on this machine's disk.

    Opening a folder used to queue a thumbnail decode for every file in it, and
    every keystroke re-listed the directory. Both are measured here rather than
    asserted, so the numbers can be compared after any later change.
    """
    from PIL import Image
    from qingjian.core import config
    from qingjian.core.engine import Engine

    library = workspace / "scale"
    library.mkdir(parents=True, exist_ok=True)
    seed = workspace / "seed.jpg"
    Image.new("RGB", (1600, 1200), (30, 60, 100)).save(seed, quality=60)
    blob = seed.read_bytes()
    count = 1500
    for index in range(count):
        (library / f"IMG_{index:05d}.JPG").write_bytes(blob)
    for index in range(40):
        (library / f"IMG_{index:05d}.CR2").write_bytes(b"raw" * 300)

    # The folder was made a moment ago; a real library was not. The grouping
    # index deliberately re-reads a directory that looks like it is still being
    # written to, so age it before measuring the steady state.
    stamp = time.time() - 3600
    os.utime(library, (stamp, stamp))

    settings = config.Settings()
    settings.bindings[0].folder = str(workspace / "scale-out")
    settings.bindings[0].name_template = "{name}"
    engine = Engine(workspace / "scale-data", settings)
    try:
        start = time.perf_counter()
        engine.open_folder(library)
        assert len(engine.queue_paths) == count, (
            f"expected {count} queue rows, got {len(engine.queue_paths)}")
        opened = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(60):
            current = engine.current_path()
            if current is None:
                break
            engine.group_for(current)
            engine.step(1)
        browse = (time.perf_counter() - start) / 60 * 1000

        start = time.perf_counter()
        for _ in range(15):
            current = engine.current_path()
            if current is None:
                break
            outcome = engine.classify(settings.bindings[0], current)
            assert outcome.record is not None, "large-folder classify produced no record"
            assert engine.absorb(outcome.record) is not None, (
                "large-folder classify could not patch the queue")
        sort_ms = (time.perf_counter() - start) / 15 * 1000

        # Pointing a key at a folder used to re-read the whole library.
        engine.settings.bindings[1].folder = str(workspace / "scale-elsewhere")
        start = time.perf_counter()
        engine.exclude_targets()
        assign = (time.perf_counter() - start) * 1000

        # Switching the sort used to parse every file's full metadata, one at a
        # time, each with its own disk flush.
        engine.settings.sort_mode = "date"
        start = time.perf_counter()
        engine.rebuild_queue()
        date_cold = time.perf_counter() - start
        start = time.perf_counter()
        engine.rebuild_queue()
        date_warm = time.perf_counter() - start
        engine.settings.sort_mode = "name"

        if opened > 20:
            raise AssertionError(f"opening {count} files took {opened:.1f}s")
        if browse > 60:
            raise AssertionError(f"browsing costs {browse:.1f} ms per item")
        if assign > 200:
            raise AssertionError(f"assigning a folder costs {assign:.0f} ms")
        if date_warm > max(1.0, date_cold):
            raise AssertionError(
                f"a second date sort cost {date_warm:.2f}s against {date_cold:.2f}s")
        return (f"{count} files: open {opened * 1000:.0f} ms · "
                f"browse {browse:.1f} ms/item · file {sort_ms:.0f} ms/item · "
                f"assign folder {assign:.1f} ms · date sort {date_cold * 1000:.0f} ms "
                f"then {date_warm * 1000:.0f} ms")
    finally:
        engine.close()


@check("history")
def _check_history(workspace: Path) -> str:
    """Undo, redo and the branch rule, against this machine's filesystem.

    The failure this guards is not a slow one: sorting a file, undoing, sorting
    it again and pressing redo used to replay a move that no longer applied.
    """
    import hashlib
    from qingjian.core import config
    from qingjian.core.engine import Engine

    library = workspace / "history"
    library.mkdir(parents=True, exist_ok=True)
    for index in range(6):
        (library / f"IMG_{index:03d}.JPG").write_bytes(bytes([65 + index]) * 4096)

    settings = config.Settings()
    settings.source_folder = str(library)
    binding = config.Binding(key="1", action="move", folder=str(workspace / "history-out"))
    engine = Engine(workspace / "history-data", settings)
    try:
        engine.open_folder(library)
        target = library / "IMG_000.JPG"
        engine.classify(binding, target)
        engine.undo()
        if not engine.can_redo():
            raise AssertionError("undo did not leave anything to redo")
        engine.classify(binding, target)
        if engine.can_redo():
            raise AssertionError("a new operation left the redo branch in place")

        start = time.perf_counter()
        outcome = engine.undo()
        undone = time.perf_counter() - start
        start = time.perf_counter()
        if engine.absorb(outcome.record) is None:
            raise AssertionError("undo could not patch the queue and fell back")
        absorbed = time.perf_counter() - start
        if target not in engine.queue_paths:
            raise AssertionError("the restored file is missing from the queue")

        seen: dict[str, list[str]] = {}
        for folder in (library, workspace / "history-out"):
            for path in sorted(folder.rglob("*.JPG")) if folder.exists() else []:
                key = hashlib.sha256(path.read_bytes()).hexdigest()
                seen.setdefault(key, []).append(str(path.relative_to(workspace)))
        doubled = [names for names in seen.values() if len(names) > 1]
        if doubled:
            raise AssertionError(f"the same picture exists twice: {doubled[:4]}")
        return (f"branch truncated · undo {undone * 1000:.0f} ms · "
                f"refresh {absorbed * 1000:.1f} ms (no rescan)")
    finally:
        engine.close()


@check("duplicates")
def _check_duplicates(workspace: Path) -> str:
    """The scan must narrow before it reads, and remember what it read."""
    from PIL import Image
    from qingjian.core import dedupe, safestore
    from qingjian.core.hashcache import HashCache

    import hashlib
    import shutil
    import numpy as np

    library = workspace / "dupes"
    library.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    for index in range(60):
        # Structured, not flat: two flat colours can encode to the same bytes,
        # which would make this stage report a duplicate nobody created.
        base = rng.integers(0, 255, (50, 70, 3), dtype=np.uint8)
        Image.fromarray(base).resize((1400, 1000), Image.BILINEAR).save(
            library / f"D_{index:03d}.JPG", quality=70)
    for index in range(0, 60, 20):
        shutil.copy2(library / f"D_{index:03d}.JPG", library / f"D_{index:03d}_copy.JPG")
    paths = sorted(library.iterdir())
    on_disk = sum(p.stat().st_size for p in paths)
    truth: dict[str, int] = {}
    for path in paths:
        key = hashlib.sha256(path.read_bytes()).hexdigest()
        truth[key] = truth.get(key, 0) + 1
    expected = sum(1 for count in truth.values() if count > 1)

    read = {"bytes": 0}
    real_full, real_sample = dedupe.fingerprint, dedupe.sample_digest

    def full(path, *args, **kwargs):
        read["bytes"] += Path(path).stat().st_size
        return real_full(path, *args, **kwargs)

    def sample(path):
        read["bytes"] += min(2 * safestore.SAMPLE, Path(path).stat().st_size)
        return real_sample(path)

    cache = HashCache(workspace / "dupe-cache.db")
    try:
        dedupe.fingerprint, dedupe.sample_digest = full, sample
        start = time.perf_counter()
        exact = dedupe.find_exact(paths, cache)
        exact_ms = (time.perf_counter() - start) * 1000
        dedupe.fingerprint, dedupe.sample_digest = real_full, real_sample
        if len(exact) != expected:
            raise AssertionError(
                f"expected {expected} identical sets, found {len(exact)}")
        if read["bytes"] > on_disk / 2:
            raise AssertionError(
                f"read {read['bytes']} of {on_disk} bytes to compare them")

        start = time.perf_counter()
        similar = dedupe.find_similar(paths, cache=cache)
        if len(similar) < expected:
            raise AssertionError(
                f"expected at least {expected} similar sets, found {len(similar)}")
        cold = time.perf_counter() - start
        start = time.perf_counter()
        again = dedupe.find_similar(paths, cache=cache)
        warm = time.perf_counter() - start
        if len(again) != len(similar):
            raise AssertionError("the cached scan disagreed with the first one")
        if warm > max(0.5, cold / 4):
            raise AssertionError(f"a second scan cost {warm:.2f}s against {cold:.2f}s")
        return (f"{len(paths)} files · exact {exact_ms:.0f} ms reading "
                f"{read['bytes'] * 100 // on_disk}% of the folder · "
                f"similar {cold:.2f}s cold, {warm:.2f}s cached")
    finally:
        dedupe.fingerprint, dedupe.sample_digest = real_full, real_sample
        cache.close()


@check("interface")
def _check_ui(library: Path, data: Path) -> str:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from qingjian.core import config, i18n
    from qingjian.core.sidecar import PROMPT_ALWAYS
    from qingjian.core.engine import Engine
    from qingjian.ui.app import create_app
    from qingjian.ui.mainwindow import MainWindow

    app = create_app([sys.argv[0]])
    settings = config.Settings()
    settings.recursive = True
    settings.background_queue = False
    settings.restore_position = False
    settings.sidecar = settings.sidecar.with_prompt(PROMPT_ALWAYS)
    settings.bindings[0].folder = str(library.parent / "Keepers2")
    settings.bindings[0].name_template = "{name}"
    engine = Engine(data, settings)
    notes = []
    modal_errors: list[str] = []
    i18n.translator().missing.clear()
    from PySide6.QtCore import QTimer
    modal_guard = QTimer()
    modal_guard.timeout.connect(lambda: _reject_active_modal(modal_errors))
    modal_guard.start(100)
    callback_monitor = _CallbackExceptionMonitor()
    callback_monitor.watch_class(MainWindow)
    try:
        with callback_monitor:
            window = MainWindow(engine)
            window.resize(1540, 940)
            window.show()
            app.processEvents()
            assert not window.undo_button.isEnabled(), "undo enabled on a fresh window"
            assert not window.redo_button.isEnabled(), "redo enabled on a fresh window"
            notes.append("fresh undo/redo disabled")
            window.open_folder(library)
            engine.go_to(library / "Day1" / "IMG_0002.JPG")
            app.processEvents()
            notes.append(f"queue={len(engine.queue_paths)}")

            for code in ("en", "zh"):
                window._change_language(code)
                app.processEvents()
            notes.append("language ok")

            for mode in (config.VIEW_GRID, config.VIEW_SINGLE):
                window._set_view(mode)
                app.processEvents()
            notes.append("views ok")

            for index in range(window.filter_combo.count()):
                window.filter_combo.setCurrentIndex(index)
                app.processEvents()
            window.filter_combo.setCurrentIndex(0)
            for index in range(window.sort_combo.count()):
                window.sort_combo.setCurrentIndex(index)
                app.processEvents()
            window.sort_combo.setCurrentIndex(0)
            notes.append("filters ok")

            window.step(1)
            window.step(-1)
            window._rate_current(3)
            window._label_by_index(2)
            app.processEvents()
            notes.append("tagging ok")

            # Dialogs: build each one and close it without user input.
            from qingjian.ui.dialogs import BackupDialog, SidecarDialog, TableDialog
            from qingjian.ui.duplicates import DuplicatesDialog
            from qingjian.ui.editors import BindingsDialog, SettingsDialog, TemplateEditor
            current = engine.current_path()
            group = engine.group_for(current)
            built = []
            for name, factory in (
            ("settings", lambda: SettingsDialog(settings, engine, window)),
            ("bindings", lambda: BindingsDialog(settings.bindings,
                                                window._template_samples(), window)),
            ("template", lambda: TemplateEditor(settings.bindings[0],
                                                window._template_samples(), engine, window)),
            ("backups", lambda: BackupDialog(engine.backup_usage(), settings.quota, window)),
            ("sidecar", lambda: SidecarDialog(group, current.name, str(current.parent),
                                              window)),
            ("duplicates", lambda: DuplicatesDialog(engine, window)),
            ("table", lambda: TableDialog("t", ["a", "b"], [["1", "2"]], parent=window)),
            ):
                dialog = factory()
                app.processEvents()
                dialog.close()
                dialog.deleteLater()
                built.append(name)
            notes.append("dialogs: " + ",".join(built))

            original = engine.current_path()
            window.classify_index(0)
            app.processEvents()
            keepers = library.parent / "Keepers2"
            assert any(keepers.rglob("*")), "classify wrote nothing"
            window.undo()
            app.processEvents()
            assert original.exists(), "interface undo lost the original"
            assert not any(path.is_file() for path in keepers.rglob("*")), (
                "interface undo left files behind")
            notes.append("classify+undo ok")

            shot = data / "selfcheck-window.png"
            window.grab().save(str(shot))
            notes.append(f"screenshot {shot.stat().st_size} bytes")

            window.close()
            app.processEvents()
        if modal_errors:
            raise AssertionError("; ".join(modal_errors))
        if callback_monitor.errors:
            raise AssertionError("UI callback exceptions: " + "; ".join(callback_monitor.errors))
        if i18n.translator().missing:
            raise AssertionError("missing translations: " +
                                 ", ".join(sorted(i18n.translator().missing)))
    finally:
        modal_guard.stop()
        try:
            engine.close()
        except Exception:
            pass
    return " · ".join(notes)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(run(sys.argv[1:]))
