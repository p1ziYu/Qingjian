"""Three kinds of "these are the same picture".

exact
    Byte-identical. Size pre-filter, then SHA-256. This is what the previous
    version could do, and it is the rarest of the three in a real library.
similar
    Visually the same frame: a web export, a re-compress, a light crop. Found
    with a perceptual hash and a banded index so the search is not quadratic.
burst
    Frames shot within a couple of seconds of each other that also look alike.
    The useful action here is "keep the sharpest and review the rest".
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass, field
from math import floor
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import imaging, metadata
from .work import mapped as _mapped
from .safestore import Cancelled, fingerprint, sample_digest
from .logsetup import get_logger

log = get_logger("dedupe")

Progress = Callable[[str, int], None]
Cancel = Callable[[], bool]

#: Files smaller than this are icons, thumbnails and web junk. Including them
#: makes the scan longer and the results less useful.
MIN_SIZE = 8 * 1024


def _noop(message: str, percent: int) -> None:
    """Progress sink for callers that do not report."""


def _never() -> bool:
    return False


def _sized(paths: Sequence[Path], minimum: int = MIN_SIZE) -> list[tuple[Path, int]]:
    """Files worth comparing, keeping one name for each physical file."""
    out: list[tuple[Path, int]] = []
    seen: set[tuple[int, int]] = set()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size < minimum:
            continue
        identity = (stat.st_dev, stat.st_ino)
        if identity[1]:
            if identity in seen:
                continue
            seen.add(identity)
        out.append((path, stat.st_size))
    return out


MODE_EXACT = "exact"
MODE_SIMILAR = "similar"
MODE_BURST = "burst"

#: 8 bands of 8 bits. Two hashes within Hamming distance 7 must agree exactly
#: on at least one band, so the banded index cannot miss a pair at or above
#: similarity 1 - 7/64.
_BANDS = 8
_BAND_BITS = 64 // _BANDS
_BAND_MASK = (1 << _BAND_BITS) - 1
MAX_INDEXED_DISTANCE = _BANDS - 1


@dataclass
class Candidate:
    path: Path
    size: int = 0
    width: int = 0
    height: int = 0
    sharpness: float = 0.0
    captured: float = 0.0
    similarity: float = 1.0
    digest: str = ""
    phash: int | None = None
    defect: str = ""

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class Group:
    mode: str
    members: list[Candidate] = field(default_factory=list)
    keeper: int = 0

    @property
    def extras(self) -> list[Candidate]:
        return [m for index, m in enumerate(self.members) if index != self.keeper]

    def reclaimable_bytes(self) -> int:
        return sum(m.size for m in self.extras)

    def keep(self) -> Candidate:
        return self.members[self.keeper]


def identity_key(path: str | Path, digest: str) -> str:
    """Stable identity for the ignore list: the path plus what it contained.

    Editing a file changes its digest, which puts it back into consideration.
    """
    return f"{Path(path).resolve()}\n{digest}"


# ------------------------------------------------------------------ exact


def find_exact(paths: Sequence[Path], cache=None, ignored: set[str] | None = None,
               progress: Progress = _noop, cancel: Cancel = _never) -> list[Group]:
    """Byte-identical files, found without reading the library end to end.

    Three narrowing passes. Sizes come from the directory entry. Files that
    share a size are then compared on a digest of their first and last sixty-four
    kilobytes, which two identical files always agree on and two merely
    same-sized files almost never do. Only what survives both is read in full.
    A folder of raw files used to mean hashing every byte on the disk.
    """
    ignored = ignored or set()
    if cache is not None:
        cache.preload(paths)

    by_size: dict[int, list[Path]] = defaultdict(list)
    for path, size in _sized(paths):
        if cancel():
            raise Cancelled("cancelled")
        by_size[size].append(path)
    contested = [p for bucket in by_size.values() if len(bucket) > 1 for p in bucket]
    if not contested:
        return []

    sizes: dict[Path, int] = {}
    for size, bucket in by_size.items():
        for path in bucket:
            sizes[path] = size
    by_sample: dict[tuple, list[Path]] = defaultdict(list)
    for path, digest in _mapped(sample_digest, contested, progress, cancel, first=0, span=35):
        # The size comes from the pass that already stat'd it; stat'ing again
        # here meant one file vanishing mid-scan threw the whole scan away.
        if digest is not None:
            by_sample[(sizes[path], digest)].append(path)
    suspects = [p for bucket in by_sample.values() if len(bucket) > 1 for p in bucket]
    if not suspects:
        return []

    def full(path: Path) -> str | None:
        cached = cache.get(path, "sha256") if cache is not None else None
        if cached:
            return str(cached)
        try:
            # Cancel reaches inside the read: one video can be several
            # gigabytes, and a Stop that waits for it is not a Stop.
            return fingerprint(path, cancel=cancel)
        except OSError:
            return None

    by_digest: dict[str, list[Path]] = defaultdict(list)
    with (cache.batch() if cache is not None else nullcontext()):
        for path, digest in _mapped(full, suspects, progress, cancel, first=35, span=55):
            if not digest:
                continue
            if cache is not None:
                cache.put(path, sha256=digest)
            if identity_key(path, digest) in ignored:
                continue
            by_digest[digest].append(path)

    groups = []
    # Only the files that turned out to have a twin are worth a header read.
    for digest, members in by_digest.items():
        if len(members) < 2:
            continue
        if cancel():
            raise Cancelled("cancelled")
        candidates = []
        for path in members:
            info = metadata.read(path)
            candidates.append(Candidate(
                path=path, size=info.size, width=info.width, height=info.height,
                captured=info.timestamp(), digest=digest, similarity=1.0))
        candidates.sort(key=lambda c: (len(str(c.path)), str(c.path)))
        groups.append(Group(mode=MODE_EXACT, members=candidates, keeper=0))
    progress("", 100)
    groups.sort(key=lambda g: -g.reclaimable_bytes())
    return groups


# ---------------------------------------------------------------- similar


class _Union:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[b] = a


def banded_pairs(hashes: Sequence[int], max_distance: int) -> set[tuple[int, int]]:
    """Index-pairs whose hashes are within *max_distance* bits.

    Bucketing by 8-bit bands turns an all-pairs comparison into a handful of
    small ones; the pigeonhole principle guarantees no pair is missed while
    ``max_distance`` stays below the band count.
    """
    buckets: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(_BANDS)]
    for index, value in enumerate(hashes):
        for band in range(_BANDS):
            buckets[band][(value >> (band * _BAND_BITS)) & _BAND_MASK].append(index)
    pairs: set[tuple[int, int]] = set()
    for band in buckets:
        for members in band.values():
            if len(members) < 2 or len(members) > 4096:
                continue
            for i, left in enumerate(members):
                for right in members[i + 1:]:
                    if imaging.hamming(hashes[left], hashes[right]) <= max_distance:
                        pairs.add((left, right) if left < right else (right, left))
    return pairs


def find_similar(paths: Sequence[Path], threshold: float = 0.92, cache=None,
                 ignored: set[str] | None = None, progress: Progress = _noop,
                 cancel: Cancel = _never, score_quality: bool = True) -> list[Group]:
    """Group images whose perceptual hashes are within *threshold*."""
    ignored = ignored or set()
    from . import mediatypes

    stills = [p for p, _size in _sized(paths) if mediatypes.is_image(p)]
    if cache is not None:
        cache.preload(stills)

    def hash_of(path: Path) -> int | None:
        cached = cache.get(path, "phash") if cache is not None else None
        if cached is not None:
            return int(cached)
        return imaging.phash(path)

    usable: list[Path] = []
    hashes: list[int] = []
    with (cache.batch() if cache is not None else nullcontext()):
        for path, value in _mapped(hash_of, stills, progress, cancel, first=0, span=85):
            if value is None:
                continue
            if cache is not None:
                cache.put(path, phash=int(value))
            usable.append(path)
            hashes.append(int(value))

    distance = max(0, min(MAX_INDEXED_DISTANCE,
                          floor((1.0 - threshold) * imaging.HASH_BITS)))
    pairs = banded_pairs(hashes, distance)
    if not pairs:
        progress("", 100)
        return []

    union = _Union(len(usable))
    for left, right in pairs:
        union.union(left, right)
    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(len(usable)):
        clusters[union.find(index)].append(index)

    groups: list[Group] = []
    with (cache.batch() if cache is not None else nullcontext()):
        for members in clusters.values():
            if len(members) < 2:
                continue
            if cancel():
                raise Cancelled("cancelled")
            candidates = _build_candidates(
                [usable[i] for i in members], [hashes[i] for i in members], cache,
                score_quality, ignored, progress, cancel)
            candidates = [c for c in candidates
                          if identity_key(c.path, c.digest or "") not in ignored]
            if len(candidates) < 2:
                continue
            groups.append(_ranked_group(MODE_SIMILAR, candidates))
    progress("", 100)
    groups.sort(key=lambda g: -g.reclaimable_bytes())
    return groups


def _digest_for_ignore(path: Path, cache, ignored: set[str], cancel: Cancel) -> str:
    digest = str(cache.get(path, "sha256") or "") if cache is not None else ""
    prefix = f"{path.resolve()}\n"
    if prefix in ignored:
        return ""
    if not digest and any(key.startswith(prefix) for key in ignored):
        if cancel():
            raise Cancelled("cancelled")
        try:
            digest = fingerprint(path, cancel=cancel)
        except OSError:
            return ""
        if cache is not None:
            cache.put(path, sha256=digest)
    return digest


def _build_candidates(paths: list[Path], hashes: list[int], cache, score_quality: bool,
                      ignored: set[str] | None = None, progress: Progress = _noop,
                      cancel: Cancel = _never) -> list[Candidate]:
    ignored = ignored or set()

    def build(item: tuple[int, Path, int]):
        _index, path, value = item
        if cancel():
            raise Cancelled("cancelled")
        info = metadata.read(path)
        digest = _digest_for_ignore(path, cache, ignored, cancel)
        sharp = 0.0
        defect = ""
        if score_quality:
            cached = ({field: cache.get(path, field)
                       for field in ("sharpness", "blown", "crushed")}
                      if cache is not None else {})
            if not cached or any(value is None for value in cached.values()):
                if cancel():
                    raise Cancelled("cancelled")
                score = imaging.quality_score(path)
                sharp = score["sharpness"]
                if cache is not None:
                    cache.put(path, sharpness=sharp, blown=score.get("blown", 0.0),
                              crushed=score.get("crushed", 0.0))
            else:
                score = cached
                sharp = float(cached["sharpness"])
            defect = imaging.looks_unusable(score)
        return Candidate(path=path, size=info.size, width=info.width, height=info.height,
                         sharpness=sharp, captured=info.timestamp(),
                         digest=digest, phash=value, defect=defect)

    items = [(index, path, value) for index, (path, value) in
             enumerate(zip(paths, hashes, strict=True))]
    found = _mapped(build, items, progress, cancel, workers=2, first=85, span=15)
    return [candidate for _item, candidate in sorted(found, key=lambda pair: pair[0][0])
            if candidate is not None]


def _best_index(candidates: list[Candidate]) -> int:
    """Which frame to keep: most pixels, then sharpest, then largest file."""
    best = max(range(len(candidates)),
               key=lambda i: (candidates[i].pixels, candidates[i].sharpness,
                              candidates[i].size, -len(str(candidates[i].path))))
    return best


def _ranked_group(mode: str, candidates: list[Candidate]) -> Group:
    """Order a group for display and mark the frame worth keeping.

    A burst reads as a sequence, so it stays in shooting order with the keeper
    flagged in place; the other modes put the keeper first because there is no
    meaningful order to preserve.
    """
    reference = candidates[_best_index(candidates)]
    for candidate in candidates:
        candidate.similarity = imaging.similarity(reference.phash, candidate.phash)
    if mode == MODE_BURST:
        ordered = sorted(candidates, key=lambda c: (c.captured, str(c.path)))
    else:
        ordered = sorted(candidates, key=lambda c: (-c.pixels, -c.sharpness, -c.size, str(c.path)))
    return Group(mode=mode, members=ordered, keeper=_best_index(ordered))


# ------------------------------------------------------------------ burst


def find_bursts(paths: Sequence[Path], gap_seconds: float = 2.0, minimum: int = 3,
                similarity_floor: float = 0.85, cache=None, ignored: set[str] | None = None,
                progress: Progress = _noop, cancel: Cancel = _never) -> list[Group]:
    """Runs of frames shot in quick succession that also look alike."""
    ignored = ignored or set()
    from . import mediatypes

    stills = [p for p, _size in _sized(paths) if mediatypes.is_image(p)]
    if cache is not None:
        cache.preload(stills)

    def timed(path: Path):
        info = metadata.read(path)
        if info.captured_is_fallback:
            # Without a real capture time, "shot within two seconds" means
            # nothing: a bulk copy gives every file the same mtime.
            return None
        cached = cache.get(path, "phash") if cache is not None else None
        value = int(cached) if cached is not None else imaging.phash(path)
        if value is None:
            return None
        return (info.timestamp(), int(value))

    entries = []
    with (cache.batch() if cache is not None else nullcontext()):
        for path, found in _mapped(timed, stills, progress, cancel, first=0, span=85):
            if found is None:
                continue
            when, value = found
            if cache is not None:
                cache.put(path, phash=value)
            entries.append((when, path, value))

    entries.sort(key=lambda item: item[0])
    groups: list[Group] = []
    run: list[tuple[float, Path, int]] = []

    def flush() -> None:
        if cancel():
            raise Cancelled("cancelled")
        if len(run) >= minimum:
            candidates = _build_candidates([r[1] for r in run], [r[2] for r in run],
                                           cache, True, ignored, progress, cancel)
            # Ignoring a burst has to stick, or the group the user dismissed
            # comes straight back on the next scan.
            candidates = [c for c in candidates
                          if identity_key(c.path, c.digest or "") not in ignored]
            if len(candidates) >= minimum:
                groups.append(_ranked_group(MODE_BURST, candidates))
        run.clear()

    with (cache.batch() if cache is not None else nullcontext()):
        for entry in entries:
            if cancel():
                raise Cancelled("cancelled")
            if not run:
                run.append(entry)
                continue
            previous = run[-1]
            close_in_time = entry[0] - previous[0] <= gap_seconds
            looks_alike = imaging.similarity(previous[2], entry[2]) >= similarity_floor
            if close_in_time and looks_alike:
                run.append(entry)
            else:
                flush()
                run.append(entry)
        flush()
    progress("", 100)
    groups.sort(key=lambda g: -len(g.members))
    return groups


def summarise(groups: Iterable[Group]) -> dict:
    groups = list(groups)
    return {
        "groups": len(groups),
        "files": sum(len(g.members) for g in groups),
        "reclaimable": sum(g.reclaimable_bytes() for g in groups),
    }
