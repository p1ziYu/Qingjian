"""Key bindings, presets and application settings."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from . import appdirs
from .logsetup import get_logger
from .safestore import QuotaPolicy, VERIFY_FAST, VERIFY_FULL, atomic_json
from .sidecar import SidecarRules

log = get_logger("config")

DEFAULT_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")
BINDING_COUNT = 10

# action -> (label key, description key, needs a target folder)
ACTIONS: dict[str, tuple[str, str, bool]] = {
    "move": ("action.move", "action.move.desc", True),
    "copy": ("action.copy", "action.copy.desc", True),
    "favorite": ("action.favorite", "action.favorite.desc", True),
    "skip": ("action.skip", "action.skip.desc", False),
    "rename": ("action.rename", "action.rename.desc", False),
    "trash": ("action.trash", "action.trash.desc", False),
    "reveal": ("action.reveal", "action.reveal.desc", False),
    "tag": ("action.tag", "action.tag.desc", False),
}
FOLDER_ACTIONS = tuple(name for name, spec in ACTIONS.items() if spec[2])

RECYCLE_SOFT = "soft"
RECYCLE_SYSTEM = "system"

VIEW_SINGLE = "single"
VIEW_GRID = "grid"

DENSITIES = ("compact", "standard", "roomy")


@dataclass
class Binding:
    key: str
    action: str = "move"
    folder: str = ""
    path_template: str = ""
    name_template: str = ""
    sequence_start: int = 1

    def needs_folder(self) -> bool:
        return ACTIONS.get(self.action, ("", "", False))[2]

    def is_configured(self) -> bool:
        return not self.needs_folder() or bool(self.folder)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "Binding":
        data = data if isinstance(data, dict) else {}
        action = str(data.get("action") or "move")
        if action not in ACTIONS:
            action = "move"
        try:
            start = max(0, int(data.get("sequence_start", 1)))
        except (TypeError, ValueError):
            start = 1
        return cls(key=str(data.get("key") or DEFAULT_KEYS[index % BINDING_COUNT]),
                   action=action,
                   folder=str(data.get("folder") or ""),
                   path_template=str(data.get("path_template") or ""),
                   name_template=str(data.get("name_template") or ""),
                   sequence_start=start)


def default_bindings() -> list[Binding]:
    return [Binding(key=key) for key in DEFAULT_KEYS]


def reserved_conflicts(bindings, reserved) -> list[str]:
    """Binding keys the window already answers to, in binding order.

    Qt treats two shortcuts on one key as ambiguous and fires neither, so a
    folder bound to G -- which is grid view -- silently did nothing at all.
    """
    taken = {str(key).casefold() for key in reserved if key}
    return [binding.key for binding in bindings
            if binding.key and binding.key.casefold() in taken]


@dataclass
class Settings:
    # interface
    language: str = "system"
    density: str = "standard"
    default_view: str = VIEW_SINGLE
    restore_position: bool = True
    # scanning
    recursive: bool = False
    filter_mode: str = "all"
    sort_mode: str = "name"
    sort_reverse: bool = False
    short_video_seconds: int = 60
    # safety
    verification: str = VERIFY_FULL
    fast_path: bool = True
    recycle_mode: str = RECYCLE_SOFT
    quota: QuotaPolicy = field(default_factory=QuotaPolicy)
    logging_enabled: bool = True
    log_days: int = 7
    # performance
    background_queue: bool = True
    workers: int = 4
    #: A 168 px RGBA tile is about 110 KB, so this holds several thousand.
    thumb_cache_mb: int = 512
    hash_cache: bool = True
    # duplicates
    similar_threshold: float = 0.92
    burst_gap_seconds: float = 2.0
    burst_minimum: int = 3
    # sidecars
    sidecar: SidecarRules = field(default_factory=SidecarRules)
    # session
    source_folder: str = ""
    last_path: str = ""
    current_profile: str = ""
    profiles: dict[str, list[Binding]] = field(default_factory=dict)
    window_geometry: str = ""

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "language": self.language, "density": self.density,
            "default_view": self.default_view, "restore_position": self.restore_position,
            "recursive": self.recursive, "filter_mode": self.filter_mode,
            "sort_mode": self.sort_mode, "sort_reverse": self.sort_reverse,
            "short_video_seconds": self.short_video_seconds,
            "verification": self.verification, "fast_path": self.fast_path,
            "recycle_mode": self.recycle_mode, "quota": self.quota.to_dict(),
            "logging_enabled": self.logging_enabled, "log_days": self.log_days,
            "background_queue": self.background_queue, "workers": self.workers,
            "thumb_cache_mb": self.thumb_cache_mb, "hash_cache": self.hash_cache,
            "similar_threshold": self.similar_threshold,
            "burst_gap_seconds": self.burst_gap_seconds, "burst_minimum": self.burst_minimum,
            "sidecar": self.sidecar.to_dict(),
            "source_folder": self.source_folder, "last_path": self.last_path,
            "current_profile": self.current_profile,
            "profiles": {name: [b.to_dict() for b in bindings]
                         for name, bindings in self.profiles.items()},
            "window_geometry": self.window_geometry,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Settings":
        settings = cls()
        if not isinstance(data, dict):
            settings.ensure_profile()
            return settings

        def text(key: str, allowed=None, default=""):
            value = str(data.get(key, default) or default)
            return value if (allowed is None or value in allowed) else default

        def flag(key: str, default: bool) -> bool:
            value = data.get(key, default)
            return bool(value) if isinstance(value, (bool, int)) else default

        def number(key: str, default, low=None, high=None):
            try:
                value = type(default)(data.get(key, default))
            except (TypeError, ValueError):
                return default
            if low is not None:
                value = max(low, value)
            if high is not None:
                value = min(high, value)
            return value

        settings.language = text("language", default="system")
        settings.density = text("density", DENSITIES, "standard")
        settings.default_view = text("default_view", (VIEW_SINGLE, VIEW_GRID), VIEW_SINGLE)
        settings.restore_position = flag("restore_position", True)
        settings.recursive = flag("recursive", False)
        settings.filter_mode = text("filter_mode", default="all")
        settings.sort_mode = text("sort_mode", default="name")
        settings.sort_reverse = flag("sort_reverse", False)
        settings.short_video_seconds = number("short_video_seconds", 60, 1, 86400)
        settings.verification = text("verification", (VERIFY_FULL, VERIFY_FAST), VERIFY_FULL)
        settings.fast_path = flag("fast_path", True)
        settings.recycle_mode = text("recycle_mode", (RECYCLE_SOFT, RECYCLE_SYSTEM), RECYCLE_SOFT)
        settings.quota = QuotaPolicy.from_dict(data.get("quota"))
        settings.logging_enabled = flag("logging_enabled", True)
        settings.log_days = number("log_days", 7, 1, 365)
        settings.background_queue = flag("background_queue", True)
        settings.workers = number("workers", 4, 1, 32)
        settings.thumb_cache_mb = number("thumb_cache_mb", 512, 64, 65536)
        settings.hash_cache = flag("hash_cache", True)
        settings.similar_threshold = number("similar_threshold", 0.92, 0.89, 1.0)
        settings.burst_gap_seconds = number("burst_gap_seconds", 2.0, 0.1, 60.0)
        settings.burst_minimum = number("burst_minimum", 3, 2, 100)
        settings.sidecar = SidecarRules.from_dict(data.get("sidecar"))
        settings.source_folder = str(data.get("source_folder") or "")
        settings.last_path = str(data.get("last_path") or "")
        settings.window_geometry = str(data.get("window_geometry") or "")

        profiles = data.get("profiles")
        if isinstance(profiles, dict):
            for name, items in profiles.items():
                if not isinstance(items, list):
                    continue
                bindings = [Binding.from_dict(item, index)
                            for index, item in enumerate(items[:BINDING_COUNT])]
                while len(bindings) < BINDING_COUNT:
                    bindings.append(Binding(key=DEFAULT_KEYS[len(bindings)]))
                settings.profiles[str(name)] = bindings
        settings.current_profile = str(data.get("current_profile") or "")
        settings.ensure_profile()
        return settings

    # -- profiles ------------------------------------------------------
    def ensure_profile(self, default_name: str = "Default") -> None:
        if not self.profiles:
            self.profiles[default_name] = default_bindings()
        if self.current_profile not in self.profiles:
            self.current_profile = next(iter(self.profiles))

    @property
    def bindings(self) -> list[Binding]:
        self.ensure_profile()
        return self.profiles[self.current_profile]

    def set_bindings(self, bindings: list[Binding]) -> None:
        self.ensure_profile()
        self.profiles[self.current_profile] = list(bindings)

    def target_folders(self) -> list[Path]:
        """Every configured destination across every preset.

        Used to keep a recursive scan from re-reading files it just filed.
        """
        folders: list[Path] = []
        for bindings in self.profiles.values():
            for binding in bindings:
                if binding.needs_folder() and binding.folder:
                    try:
                        folder = Path(binding.folder).expanduser()
                        if folder.is_absolute():
                            folders.append(folder.resolve())
                    except (OSError, RuntimeError):
                        continue
        return folders

    def duplicate_keys(self) -> list[str]:
        seen: dict[str, int] = {}
        for binding in self.bindings:
            seen[binding.key] = seen.get(binding.key, 0) + 1
        return [key for key, count in seen.items() if count > 1 and key]

    def with_language(self, language: str) -> "Settings":
        return replace(self, language=language)


def load(path: str | Path | None = None) -> Settings:
    target = Path(path) if path else appdirs.settings_path()
    if not target.exists():
        settings = Settings()
        settings.ensure_profile()
        return settings
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        log.warning("settings unreadable (%s); keeping the file and starting fresh", error)
        try:
            target.replace(target.with_suffix(".broken.json"))
        except OSError:
            pass
        settings = Settings()
        settings.ensure_profile()
        return settings
    return Settings.from_dict(data)


def save(settings: Settings, path: str | Path | None = None) -> None:
    target = Path(path) if path else appdirs.settings_path()
    atomic_json(target, settings.to_dict())
