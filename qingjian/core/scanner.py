"""Finding, filtering and ordering the files to sort."""
from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from . import mediatypes, metadata, sidecar as sidecar_mod
from .safestore import Cancelled

Progress = Callable[[str, int], None]
Cancel = Callable[[], bool]


def _noop(message: str, percent: int) -> None:
    """Progress sink for callers that do not report."""


def _never() -> bool:
    return False


FILTERS = ("all", "images", "videos", "raw", "landscape", "portrait", "square",
           "short", "animated", "rated", "unrated", "labelled")
SORTS = ("name", "date", "modified", "size", "rating", "random")

_NUMBER = re.compile(r"(\d+)")


def natural_key(path: Path) -> list:
    return [int(part) if part.isdecimal() else part.casefold()
            for part in _NUMBER.split(path.name)]


def pruned_targets(root: str | Path, targets: Sequence[str | Path]) -> list[Path]:
    """Resolved destinations strictly below the source root, once per scan."""
    try:
        base = Path(root).resolve()
    except (OSError, RuntimeError):
        return []
    kept: list[Path] = []
    for item in targets:
        try:
            resolved = Path(item).resolve()
        except (OSError, RuntimeError):
            continue
        if resolved != base and resolved.is_relative_to(base):
            kept.append(resolved)
    return kept


def scan(root: str | Path, recursive: bool = False, excluded: Sequence[Path] = (),
         progress: Progress = _noop, cancel: Cancel = _never) -> list[Path]:
    """Every media file under *root*, skipping targets, symlinks and junctions.

    The directory listing already says whether each entry is a file, a folder
    or a link. Asking every ``Path`` again is a system call per file, and on
    Windows each one opens the file: five seconds for a folder of twenty
    thousand photographs, against a twentieth of a second for the listing.
    """
    root = Path(root)
    blocked = pruned_targets(root, excluded)
    found: list[Path] = []

    def is_blocked(folder: Path) -> bool:
        try:
            resolved = folder.resolve()
        except (OSError, RuntimeError):
            return True
        return any(resolved == item or resolved.is_relative_to(item) for item in blocked)

    # Depth first, a folder's own files before its subfolders: the order
    # os.walk produced, which a queue sorted with ties still falls back on.
    pending = [root]
    while pending:
        here = pending.pop()
        subfolders: list[Path] = []
        try:
            with os.scandir(here) as entries:
                for entry in entries:
                    if cancel():
                        raise Cancelled("cancelled")
                    name = entry.name
                    try:
                        if entry.is_symlink() or entry.is_junction():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if recursive and not name.startswith(".qingjian") \
                                    and not is_blocked(here / name):
                                subfolders.append(here / name)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if name.startswith(".qingjian-") and name.endswith(".part"):
                        continue
                    if os.path.splitext(name)[1].lower() not in mediatypes.MEDIA_EXTENSIONS:
                        continue
                    found.append(here / name)
        except OSError:
            continue                    # an unreadable folder is skipped, as os.walk did
        progress(str(here), 0)
        pending.extend(reversed(subfolders))
    return found


#: Below this many paths each file is asked directly. A single restored file
#: must not pay for listing a folder of twenty thousand.
_LIST_FOLDERS_FROM = 32


def _presence(paths: Sequence[Path]) -> Callable[[Path], bool]:
    """A test for "is this still a regular file", as cheap as *paths* allows.

    One listing per folder answers it for every file in that folder. Asking
    each file instead cost two and a half seconds for twenty thousand of them,
    on every change of filter or sort.
    """
    if len(paths) < _LIST_FOLDERS_FROM:
        def asked(path: Path) -> bool:
            try:
                return path.is_file()
            except OSError:
                return False
        return asked

    listed: dict[str, set[str]] = {}
    for path in paths:
        folder = os.path.dirname(os.fspath(path))
        if folder in listed:
            continue
        names: set[str] = set()
        try:
            with os.scandir(folder or ".") as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            names.add(entry.name)
                    except OSError:
                        continue
        except OSError:
            pass
        listed[folder] = names

    def looked_up(path: Path) -> bool:
        folder, name = os.path.split(os.fspath(path))
        return name in listed.get(folder, ())
    return looked_up


@dataclass
class FilterSpec:
    """Everything the queue can be narrowed by."""

    mode: str = "all"
    short_video_seconds: int = 60
    min_rating: int = 0
    label: str = ""
    name_pattern: str = ""
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_pixels: int = 0
    max_pixels: int = 0
    #: Paths already handled (copied, tagged) that should not reappear.
    exclude: set[str] = field(default_factory=set)
    ratings: dict[str, tuple[int, str]] = field(default_factory=dict)

    def narrows_nothing(self) -> bool:
        """True when this filter would keep every file it is shown.

        The common case by far. Recognising it lets the queue rebuild skip the
        per-file checks entirely and look only at the exclusion set.
        """
        return (self.mode == "all" and not self.name_pattern and not self.min_rating
                and not self.label and self.date_from is None and self.date_to is None
                and not self.min_pixels and not self.max_pixels)

    def compiled_pattern(self):
        if not self.name_pattern:
            return None
        try:
            return re.compile(self.name_pattern, re.IGNORECASE)
        except re.error:
            # An unfinished regex typed into a search box must not raise.
            return re.compile(re.escape(self.name_pattern), re.IGNORECASE)


def _matches(path: Path, spec: FilterSpec, pattern) -> bool:
    # One conversion, not three: this runs once per file on every rebuild.
    text = str(path)
    if text in spec.exclude:
        return False
    if pattern and not pattern.search(path.name):
        return False

    rating, label = spec.ratings.get(text, (0, ""))
    mode = spec.mode

    if mode == "rated" and rating <= 0:
        return False
    if mode == "unrated" and rating > 0:
        return False
    if mode == "labelled" and not label:
        return False
    if spec.min_rating and rating < spec.min_rating:
        return False
    if spec.label and label != spec.label:
        return False

    is_video = mediatypes.is_video(path)
    if mode == "images" and is_video:
        return False
    if mode == "videos" and not is_video:
        return False
    if mode == "raw" and not mediatypes.is_raw(path):
        return False
    if mode == "animated" and not mediatypes.may_animate(path):
        return False

    needs_info = (
        mode in ("landscape", "portrait", "square", "short")
        or spec.date_from or spec.date_to or spec.min_pixels or spec.max_pixels
    )
    if not needs_info:
        return True

    # Reading the header is enough for dimensions and dates. The previous
    # version fully decoded every image just to compare width with height.
    info = metadata.read(path)
    if mode == "landscape" and not info.is_landscape:
        return False
    if mode == "portrait" and not info.is_portrait:
        return False
    if mode == "square" and not info.is_square:
        return False
    if mode == "short":
        if not is_video or info.duration is None or info.duration > spec.short_video_seconds:
            return False
    if spec.min_pixels and info.width * info.height < spec.min_pixels:
        return False
    if spec.max_pixels and info.width * info.height > spec.max_pixels:
        return False
    if spec.date_from or spec.date_to:
        when = info.when()
        if spec.date_from and when < spec.date_from:
            return False
        if spec.date_to and when > spec.date_to:
            return False
    return True


def apply_filter(paths: Sequence[Path], spec: FilterSpec, progress: Progress = _noop,
                 cancel: Cancel = _never) -> list[Path]:
    total = max(1, len(paths))
    out: list[Path] = []
    present = _presence(paths)
    if spec.narrows_nothing():
        # Nothing to test but "is it still there" and "has it been handled".
        exclude = spec.exclude
        for index, path in enumerate(paths):
            if cancel():
                raise Cancelled("cancelled")
            if index % 256 == 0:
                progress(path.name, int(index * 100 / total))
            if str(path) not in exclude and present(path):
                out.append(path)
        return out

    pattern = spec.compiled_pattern()
    for index, path in enumerate(paths):
        if cancel():
            raise Cancelled("cancelled")
        if index % 64 == 0:
            progress(path.name, int(index * 100 / total))
        if present(path) and _matches(path, spec, pattern):
            out.append(path)
    return out


def _stat_field(path: Path, name: str) -> float:
    try:
        return getattr(path.stat(), name)
    except OSError:
        return 0


def sort_key(mode: str = "name", ratings: dict[str, tuple[int, str]] | None = None,
             capture_time: Callable[[Path], float] | None = None):
    """The key `sort_paths` orders by, or None when the order has no key.

    Exposed so a caller that has to place one restored file can bisect into the
    list it already holds instead of sorting the whole library again. Every key
    ends in the natural name: a burst shot in one second or a card copied in one
    go ties on time and size, and with ties a bisect and a full sort disagree
    about where a file belongs.
    """
    ratings = ratings or {}
    if mode == "name":
        return natural_key
    if mode == "date":
        reader = capture_time or metadata.capture_time
        return lambda p: (reader(p), natural_key(p))
    if mode == "modified":
        return lambda p: (_stat_field(p, "st_mtime"), natural_key(p))
    if mode == "size":
        return lambda p: (_stat_field(p, "st_size"), natural_key(p))
    if mode == "rating":
        return lambda p: (-ratings.get(str(p), (0, ""))[0], natural_key(p))
    return None


def sort_paths(paths: Sequence[Path], mode: str = "name", reverse: bool = False,
               ratings: dict[str, tuple[int, str]] | None = None,
               seed: int | None = None,
               capture_time: Callable[[Path], float] | None = None) -> list[Path]:
    """Order *paths*.

    ``capture_time`` lets the caller supply a cached reader. Sorting by date
    otherwise opens and parses every file in the folder, which on a library of
    twenty thousand photographs is seconds of work repeated on every change of
    filter.
    """
    items = list(paths)
    if mode == "random":
        random.Random(seed).shuffle(items)
        return items
    key = sort_key(mode, ratings, capture_time)
    if key is not None:
        items.sort(key=key)
    if reverse:
        items.reverse()
    return items


def build_queue(paths: Sequence[Path], spec: FilterSpec, rules: sidecar_mod.SidecarRules,
                sort_mode: str = "name", reverse: bool = False,
                ratings: dict[str, tuple[int, str]] | None = None,
                progress: Progress = _noop, cancel: Cancel = _never,
                capture_time: Callable[[Path], float] | None = None) -> list[Path]:
    """Filter, collapse sidecar groups to one row each, then order."""
    kept = apply_filter(paths, spec, progress, cancel)
    collapsed = sidecar_mod.collapse_groups(kept, rules)
    return sort_paths(collapsed, sort_mode, reverse, ratings, capture_time=capture_time)
