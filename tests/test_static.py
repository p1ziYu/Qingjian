"""Static checks over the whole source tree.

The interface layer cannot be exercised on a machine without Qt, so these
checks stand in for the parts a running window would have caught: a mistyped
translation key, an attribute that is read but never set, and the layering rule
that keeps the core testable.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from base import ROOT, unittest
from qingjian.core.i18n import CATALOG

PACKAGE = ROOT / "qingjian"
CORE = PACKAGE / "core"


def python_files(folder: Path):
    return sorted(p for p in folder.rglob("*.py"))


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class LayeringTests(unittest.TestCase):
    def test_the_core_never_imports_qt(self):
        """If the core needs Qt, it stops being testable without a display."""
        offenders = []
        for path in python_files(CORE):
            for node in ast.walk(parse(path)):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(name.startswith(("PySide", "PyQt", "shiboken")) for name in names):
                    offenders.append(f"{path.name}: {names}")
        self.assertEqual([], offenders)

    def test_the_core_does_not_import_the_ui(self):
        offenders = []
        for path in python_files(CORE):
            text = path.read_text(encoding="utf-8")
            if re.search(r"from\s+\.\.ui|import\s+qingjian\.ui", text):
                offenders.append(path.name)
        self.assertEqual([], offenders)

    def test_every_module_compiles(self):
        for path in python_files(PACKAGE):
            with self.subTest(module=path.name):
                parse(path)


class TranslationTests(unittest.TestCase):
    def _keys_in(self, path: Path) -> list[tuple[int, str]]:
        found = []
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name != "tr" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((first.lineno, first.value))
        return found

    def test_every_translated_key_exists(self):
        missing = []
        for path in python_files(PACKAGE):
            for line, key in self._keys_in(path):
                if key not in CATALOG:
                    missing.append(f"{path.name}:{line} {key!r}")
        self.assertEqual([], missing)

    def test_every_catalogue_key_is_used_or_an_explicit_pending_prompt(self):
        literals = set()
        for path in python_files(PACKAGE):
            if path.name == "i18n.py":
                continue
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.add(node.value)
        dynamic = {
            *(f"filter.{name}" for name in
              ("all", "images", "videos", "raw", "landscape", "portrait", "square",
               "short", "animated", "rated", "unrated", "labelled")),
            *(f"sort.{name}" for name in
              ("name", "date", "modified", "size", "rating", "random")),
            "conflict.mode.replace", "conflict.mode.sequence",
            "dup.defect.blurry", "dup.defect.overexposed", "dup.defect.black",
        }
        pending = {"error.permission", "dup.none_found", "dup.none_similar"}
        self.assertEqual(pending, set(CATALOG) - literals - dynamic)

    def test_translated_calls_supply_every_placeholder(self):
        """A missing field would print a raw {brace} to the user."""
        problems = []
        placeholder = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
        for path in python_files(PACKAGE):
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if name != "tr" or not node.args:
                    continue
                first = node.args[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    continue
                entry = CATALOG.get(first.value)
                if entry is None:
                    continue
                needed = set(placeholder.findall(entry[0]))
                given = {kw.arg for kw in node.keywords if kw.arg}
                if node.keywords and any(kw.arg is None for kw in node.keywords):
                    continue                        # **kwargs, cannot tell
                if needed - given:
                    problems.append(
                        f"{path.name}:{first.lineno} {first.value!r} missing "
                        f"{sorted(needed - given)}")
        self.assertEqual([], problems)

    #: Chinese literals that are not user-facing copy: template placeholder
    #: names, which are part of the template syntax, and the language names,
    #: which read the same whichever language the interface is in.
    ALLOWED_CJK = frozenset({
        "\u8f7b\u62e3", "\u4e2d", "\u4e2d\u6587",
        "\u539f\u540d", "\u6269\u5c55\u540d", "\u539f\u6587\u4ef6\u5939",
        "\u76f8\u5bf9\u8def\u5f84", "\u76f8\u673a", "\u955c\u5934",
        "\u611f\u5149\u5ea6", "\u5149\u5708", "\u5feb\u95e8", "\u7126\u8ddd",
        "\u8bc4\u5206", "\u8272\u6807", "\u5e8f\u53f7",
        "\u5e74", "\u6708", "\u65e5", "\u65f6", "\u5206", "\u79d2",
    })

    def test_no_user_facing_literal_slipped_through(self):
        """Chinese text outside the catalogue would never switch to English."""
        offenders = []
        cjk = re.compile(r"[\u4e00-\u9fff]")
        for path in python_files(PACKAGE):
            if path.name == "i18n.py":
                continue
            tree = parse(path)
            # Docstrings are for whoever reads the source, not for the window.
            docstrings = {id(node.value) for node in ast.walk(tree)
                          if isinstance(node, ast.Expr)
                          and isinstance(node.value, ast.Constant)
                          and isinstance(node.value.value, str)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if id(node) in docstrings:
                    continue
                text = node.value.strip()
                if cjk.search(text) and text not in self.ALLOWED_CJK:
                    offenders.append(f"{path.name}:{node.lineno} {text[:30]!r}")
        self.assertEqual([], offenders)


class AttributeTests(unittest.TestCase):
    """Catch ``self.thing`` read but never assigned.

    Only snake_case names are considered: Qt's own methods are camelCase, so
    restricting to snake_case keeps the check free of false positives without
    needing Qt present to introspect.
    """

    SNAKE = re.compile(r"^_?[a-z][a-z0-9]*(_[a-z0-9]+)+$")
    KNOWN_QT = {
        "set_value", "set_paths", "set_edge", "show_path", "set_current",
        "path_selected", "path_activated", "selection_changed", "set_options",
        "set_show_names", "select_all_paths", "invert_selection", "selected_paths",
        "set_decoration", "clear_media", "rotate_by", "set_pixmap", "zoom_percent",
        "show_empty", "toggle_play", "seek_relative", "player_seek", "toggle_mute",
        "step_frame", "is_video_page", "update_binding", "add_row", "result_bindings",
        "result_settings", "remembered", "chosen", "peek", "request", "clear",
    }

    def _classes(self, tree: ast.Module):
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                yield node

    @staticmethod
    def _members(klass: ast.ClassDef) -> tuple[set, set]:
        """(assigned attributes, defined methods) for one class body."""
        assigned = set()
        methods = {item.name for item in klass.body
                   if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for item in klass.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                assigned.add(item.target.id)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
        for node in ast.walk(klass):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "self" and isinstance(node.ctx, ast.Store):
                assigned.add(node.attr)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute):
                target = node.target
                if isinstance(target.value, ast.Name) and target.value.id == "self":
                    assigned.add(target.attr)
        return assigned, methods

    def _package_classes(self) -> dict:
        """Every class in the package, so base classes can be resolved by name."""
        found = {}
        for path in python_files(PACKAGE):
            for klass in self._classes(parse(path)):
                assigned, methods = self._members(klass)
                bases = [base.id for base in klass.bases if isinstance(base, ast.Name)]
                found[klass.name] = {"assigned": assigned, "methods": methods, "bases": bases}
        return found

    def _inherited(self, name: str, table: dict, seen=None) -> set:
        """Names a class gets from base classes defined in this package."""
        seen = seen or set()
        if name in seen or name not in table:
            return set()
        seen.add(name)
        entry = table[name]
        out = set(entry["assigned"]) | set(entry["methods"])
        for base in entry["bases"]:
            out |= self._inherited(base, table, seen)
        return out

    def test_attributes_are_assigned_before_use(self):
        table = self._package_classes()
        problems = []
        for path in python_files(PACKAGE):
            tree = parse(path)
            for klass in self._classes(tree):
                assigned, methods = self._members(klass)
                # A subclass legitimately uses what its base defines.
                for base in klass.bases:
                    if isinstance(base, ast.Name):
                        assigned |= self._inherited(base.id, table)
                for node in ast.walk(klass):
                    if not (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id == "self"
                            and isinstance(node.ctx, ast.Load)):
                        continue
                    name = node.attr
                    if name in assigned or name in methods or name in self.KNOWN_QT:
                        continue
                    if not self.SNAKE.match(name):
                        continue
                    problems.append(f"{path.name}:{node.lineno} {klass.name}.{name}")
        self.assertEqual([], problems)


class SignalTests(unittest.TestCase):
    def test_signals_referenced_in_connects_are_declared(self):
        """A misspelled signal name only fails when that code path runs."""
        problems = []
        for path in python_files(PACKAGE / "ui"):
            tree = parse(path)
            for klass in self._classes_of(tree):
                declared = set()
                for item in klass.body:
                    if isinstance(item, ast.Assign) and isinstance(item.value, ast.Call):
                        func = item.value.func
                        if getattr(func, "id", "") == "Signal":
                            for target in item.targets:
                                if isinstance(target, ast.Name):
                                    declared.add(target.id)
                for node in ast.walk(klass):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                            and node.func.attr in ("emit",):
                        owner = node.func.value
                        if isinstance(owner, ast.Attribute) and \
                                isinstance(owner.value, ast.Name) and owner.value.id == "self":
                            if owner.attr not in declared and "_" not in owner.attr:
                                problems.append(f"{path.name}:{node.lineno} {owner.attr}")
        self.assertEqual([], problems)

    @staticmethod
    def _classes_of(tree: ast.Module):
        return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]



class CrossLayerTests(unittest.TestCase):
    """The interface calls into the core by name; check those names exist.

    Without a display these calls are never executed here, so a renamed method
    would otherwise surface only when the user clicked the button.
    """

    def _calls_on(self, receiver: str) -> list[tuple[str, int, str]]:
        found = []
        for path in python_files(PACKAGE / "ui"):
            for node in ast.walk(parse(path)):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                owner = node.func.value
                if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) \
                        and owner.value.id == "self" and owner.attr == receiver:
                    found.append((path.name, node.lineno, node.func.attr))
        return found

    def _reads_on(self, receiver: str) -> list[tuple[str, int, str]]:
        found = []
        for path in python_files(PACKAGE / "ui"):
            for node in ast.walk(parse(path)):
                if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)):
                    continue
                owner = node.value
                if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) \
                        and owner.value.id == "self" and owner.attr == receiver:
                    found.append((path.name, node.lineno, node.attr))
        return found

    def test_engine_methods_called_by_the_window_exist(self):
        from qingjian.core.engine import Engine
        missing = [f"{name}:{line} engine.{attr}"
                   for name, line, attr in self._calls_on("engine")
                   if not hasattr(Engine, attr)]
        self.assertEqual([], missing)

    def test_engine_attributes_read_by_the_window_exist(self):
        from qingjian.core.engine import Engine
        known = set(dir(Engine)) | {
            "source_root", "all_files", "queue_paths", "index", "review_mode", "stats",
            "settings", "store", "state", "cache", "planner", "queue", "queue_listeners",
            "data_dir",
        }
        missing = [f"{name}:{line} engine.{attr}"
                   for name, line, attr in self._reads_on("engine")
                   if attr not in known]
        self.assertEqual([], missing)

    def test_settings_fields_used_by_the_window_exist(self):
        from qingjian.core.config import Settings
        probe = Settings()
        probe.ensure_profile()
        known = set(dir(probe)) | set(probe.to_dict())
        missing = []
        for name, line, attr in self._reads_on("settings") + self._calls_on("settings"):
            if attr not in known:
                missing.append(f"{name}:{line} settings.{attr}")
        self.assertEqual([], missing)

    def test_action_names_used_by_the_window_are_registered(self):
        from qingjian.core.config import ACTIONS
        names = set(ACTIONS)
        offenders = []
        for path in python_files(PACKAGE / "ui"):
            tree = parse(path)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute)):
                    continue
                if node.left.attr != "action":
                    continue
                for comparator in node.comparators:
                    values = ([comparator] if isinstance(comparator, ast.Constant)
                              else getattr(comparator, "elts", []))
                    for value in values:
                        if isinstance(value, ast.Constant) and isinstance(value.value, str) \
                                and value.value not in names:
                            offenders.append(f"{path.name}:{node.lineno} {value.value!r}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
