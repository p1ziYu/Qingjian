"""Files that belong to the same shot and must travel together.

Losing this is the single most damaging thing a sorter can do: move
``IMG_0001.JPG`` and leave ``IMG_0001.CR2`` behind, and the library is now
wrong in a way the user will not notice for months.
"""
from __future__ import annotations

import os

from dataclasses import dataclass, field, replace
from pathlib import Path

from . import mediatypes

KIND_MASTER = "master"
KIND_RAW = "raw"
KIND_METADATA = "metadata"
KIND_LIVE = "live"
KIND_OTHER = "other"

PROMPT_EACH = "each"
PROMPT_ONCE = "once"
PROMPT_ALWAYS = "always"
PROMPT_NEVER = "never"
PROMPT_MODES = (PROMPT_EACH, PROMPT_ONCE, PROMPT_ALWAYS, PROMPT_NEVER)

DEFAULT_METADATA = frozenset({".xmp", ".aae", ".thm", ".pp3", ".dop", ".on1", ".xml", ".lrv"})
DEFAULT_LIVE = frozenset({".mov", ".mp4"})


@dataclass(frozen=True)
class SidecarRules:
    enabled: bool = True
    prompt: str = PROMPT_ONCE
    raw_extensions: frozenset[str] = mediatypes.RAW_EXTENSIONS
    metadata_extensions: frozenset[str] = DEFAULT_METADATA
    live_extensions: frozenset[str] = DEFAULT_LIVE
    extra_extensions: frozenset[str] = frozenset()
    #: Whether two ordinary media files sharing a stem (IMG_1.JPG / IMG_1.PNG)
    #: count as one shot. On for the RAW+JPG case that motivates the feature.
    link_same_stem_media: bool = True
    #: Show one row per group in the sorting queue instead of one per file.
    hide_from_queue: bool = True

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "prompt": self.prompt,
            "raw_extensions": sorted(self.raw_extensions),
            "metadata_extensions": sorted(self.metadata_extensions),
            "live_extensions": sorted(self.live_extensions),
            "extra_extensions": sorted(self.extra_extensions),
            "link_same_stem_media": self.link_same_stem_media,
            "hide_from_queue": self.hide_from_queue,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "SidecarRules":
        if not isinstance(data, dict):
            return cls()
        def norm(key: str, default: frozenset[str]) -> frozenset[str]:
            value = data.get(key)
            if not isinstance(value, (list, tuple, set, frozenset)):
                return default
            cleaned = {
                ("." + str(x).lstrip(".")).lower()
                for x in value
                if str(x).strip(". ")
            }
            return frozenset(cleaned)
        prompt = str(data.get("prompt", PROMPT_ONCE))
        return cls(
            enabled=bool(data.get("enabled", True)),
            prompt=prompt if prompt in PROMPT_MODES else PROMPT_ONCE,
            raw_extensions=norm("raw_extensions", mediatypes.RAW_EXTENSIONS),
            metadata_extensions=norm("metadata_extensions", DEFAULT_METADATA),
            live_extensions=norm("live_extensions", DEFAULT_LIVE),
            extra_extensions=norm("extra_extensions", frozenset()),
            link_same_stem_media=bool(data.get("link_same_stem_media", True)),
            hide_from_queue=bool(data.get("hide_from_queue", True)),
        )

    def with_prompt(self, prompt: str) -> "SidecarRules":
        return replace(self, prompt=prompt if prompt in PROMPT_MODES else self.prompt)

    def classify(self, path: Path, master: Path) -> str | None:
        """Which kind of companion *path* is to *master*, or None if unrelated."""
        ext = path.suffix.lower()
        if ext in self.raw_extensions:
            return KIND_RAW
        if ext in self.metadata_extensions:
            return KIND_METADATA
        if ext in self.live_extensions:
            # A .mov beside a .heic is a Live Photo; a .mov beside another .mov
            # is just another video that happens to share a name.
            if mediatypes.is_video(master):
                return KIND_OTHER if self.link_same_stem_media else None
            return KIND_LIVE
        if ext in self.extra_extensions:
            return KIND_OTHER
        if self.link_same_stem_media and mediatypes.is_media(path):
            return KIND_OTHER
        return None


@dataclass
class SidecarMember:
    path: Path
    kind: str
    size: int = 0


@dataclass
class SidecarGroup:
    master: Path
    members: list[SidecarMember] = field(default_factory=list)

    @property
    def sidecars(self) -> list[SidecarMember]:
        return [m for m in self.members if m.kind != KIND_MASTER]

    @property
    def paths(self) -> list[Path]:
        return [m.path for m in self.members]

    @property
    def count(self) -> int:
        return len(self.members)

    def __bool__(self) -> bool:
        return bool(self.sidecars)

    def total_bytes(self) -> int:
        return sum(m.size for m in self.members)


def _stem_of(name: str) -> str:
    """``Path(name).stem``, without building a Path.

    A trailing dot is the one place ``os.path.splitext`` and pathlib disagree:
    splitext calls it an extension, pathlib does not. Following pathlib keeps
    the folder index and the sidecar grouping keyed on the same strings they
    were before.
    """
    stem, extension = os.path.splitext(name)
    return name if extension == "." else stem


def _split_stem(name: str) -> str:
    """The stem, with one inner media extension removed."""
    stem = _stem_of(name)
    inner, extension = os.path.splitext(stem)
    if extension and extension != "." and extension.lower() in mediatypes.MEDIA_EXTENSIONS:
        return inner
    return stem


def base_stem(path: str | Path) -> str:
    """The stem with an inner media extension removed.

    ``IMG_0001.JPG.xmp`` -> ``IMG_0001``, so an Adobe-style sidecar reduces to
    the same identity as the photo it describes no matter which file of the
    shot the caller happens to be holding.

    Done with string operations rather than ``Path``. This runs once per file
    every time the queue is rebuilt, and building three Path objects per call
    was most of the cost of collapsing a folder of twenty thousand files.
    """
    return _split_stem(os.path.basename(os.fspath(path)))


def group_key(path: str | Path) -> tuple[str, str]:
    """Identity shared by every file of one shot: folder plus base stem."""
    folder, name = os.path.split(os.fspath(path))
    # `Path("x.jpg").parent` is ".", not "": a bare relative name has to key the
    # same way whichever helper produced it.  Normalise separators as well:
    # Windows accepts both slash styles, and mixing them must not split a JPEG
    # from its RAW or metadata companion into different groups.
    return (os.path.normpath(folder or ".").casefold(), _split_stem(name).casefold())


def _stem_matches(candidate: Path, master: Path) -> bool:
    # Comparing base stems keeps the relation symmetric: acting on the CR2
    # finds IMG_0001.JPG.xmp just as acting on the JPG does.
    return base_stem(candidate).casefold() == base_stem(master).casefold()


def find_group(master: str | Path, rules: SidecarRules | None = None,
               siblings: list[Path] | None = None) -> SidecarGroup:
    """Collect every file in the master's folder that belongs to the same shot.

    ``siblings`` lets a caller pass a directory listing it already has, which
    keeps a batch of a thousand items from doing a thousand ``iterdir`` calls.
    """
    rules = rules or SidecarRules()
    path = Path(master)
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        size = 0
    group = SidecarGroup(master=path, members=[SidecarMember(path, KIND_MASTER, size)])
    if not rules.enabled:
        return group

    if siblings is None:
        try:
            siblings = [p for p in path.parent.iterdir()]
        except OSError:
            return group

    for candidate in sorted(siblings, key=lambda p: (p.suffix.lower(), p.name.casefold())):
        if candidate == path:
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
        except OSError:
            continue
        if not _stem_matches(candidate, path):
            continue
        kind = rules.classify(candidate, path)
        if kind is None:
            continue
        try:
            member_size = candidate.stat().st_size
        except OSError:
            member_size = 0
        group.members.append(SidecarMember(candidate, kind, member_size))
    return group


#: Order used to pick which file of a group stands in for it in the queue.
#: An ordinary image previews fastest, so it wins over a raw of the same shot.
_REPRESENTATIVE_RANK = {
    mediatypes.KIND_IMAGE: 0,
    mediatypes.KIND_VIDEO: 1,
    mediatypes.KIND_RAW: 2,
    mediatypes.KIND_OTHER: 3,
}


def representative(paths: list[Path]) -> Path:
    """The single file that stands for a group in the sorting queue."""
    return min(paths, key=lambda p: (_REPRESENTATIVE_RANK.get(mediatypes.kind(p), 9), p.name.casefold()))


def collapse_groups(paths: list[Path], rules: SidecarRules | None = None) -> list[Path]:
    """Reduce a file list to one representative per shot, preserving order."""
    rules = rules or SidecarRules()
    if not (rules.enabled and rules.hide_from_queue):
        return list(paths)
    buckets: dict[tuple[str, str], list[Path]] = {}
    order: list[tuple[str, str]] = []
    for path in paths:
        key = group_key(path)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(path)
    result = []
    for key in order:
        members = buckets[key]
        master = representative(members)
        linked = [path for path in members if path == master or
                  rules.classify(path, master) is not None]
        result.append(master)
        result.extend(path for path in members if path not in linked)
    return result
