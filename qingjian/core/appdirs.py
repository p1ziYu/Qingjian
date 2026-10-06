"""Where the application keeps its own data.

Resolution order, highest first:

1. ``QINGJIAN_DATA_DIR`` (also what ``--data-dir`` sets) — used by the tests
   and by the diagnostic runner so a run can never touch a real library's
   history. Because that is the whole point of it, it is honoured absolutely:
   when the directory it names will not hold data the start-up fails with
   :class:`DataDirUnusable` rather than falling back. Anything else would put
   the fallback -- the per-user application data directory, i.e. the real
   library -- back in reach of exactly the runs that asked not to touch it.
2. A ``qingjian-portable`` marker directory beside the executable, which makes
   a USB-stick install keep its state with it.
3. The platform's per-user application data directory.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

from .. import __app_name__, __organization__

ENV_VAR = "QINGJIAN_DATA_DIR"
PORTABLE_MARKER = "qingjian-portable"


class DataDirUnusable(OSError):
    """Nowhere would hold the data. ``paths`` lists what was tried, in order."""

    def __init__(self, paths: list[Path]) -> None:
        self.paths = list(paths)
        super().__init__("no writable data directory: "
                         + ", ".join(str(path) for path in self.paths))


def _platform_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / __organization__ / __app_name__
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / __organization__ / __app_name__
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / __organization__.lower() / __app_name__.lower()


def executable_dir() -> Path:
    """Directory holding the running program (the bundle dir when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()


def portable_dir() -> Path | None:
    try:
        candidate = executable_dir() / PORTABLE_MARKER
    except OSError:
        return None
    return candidate if candidate.is_dir() else None


def explicit_dir() -> Path | None:
    """The directory ``QINGJIAN_DATA_DIR`` names, or ``None`` when unset.

    Told apart from the places that are merely guessed at, because an explicit
    one is never traded for another: see :func:`usable_data_dir`.
    """
    override = os.environ.get(ENV_VAR)
    return Path(override).expanduser() if override else None


def data_dir(create: bool = True) -> Path:
    override = explicit_dir()
    if override is not None:
        root = override
    else:
        root = portable_dir() or _platform_data_root()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


#: Probe files and folders carry this prefix so a leftover can be recognised
#: and swept up rather than mistaken for data.
PROBE_PREFIX = ".qingjian-write-test-"

#: Left in a fallback area, naming the directory that was refused, so the data
#: written there can be found again rather than stranded.
ORIGIN_MARKER = ".qingjian-relocated-from"


def _sweep_probes(root: Path) -> None:
    """Remove probes an earlier run left behind. Never raises."""
    try:
        leftovers = list(root.glob(PROBE_PREFIX + "*"))
    except OSError:                                 # pragma: no cover - unlistable
        return
    for stale in leftovers:
        try:
            stale.rmdir() if stale.is_dir() else stale.unlink()
        except OSError:
            continue


def is_usable(root: Path) -> bool:
    """True when *root* exists, or can be made, and accepts what we store.

    Nothing cheaper is honest. ``mkdir(parents=True, exist_ok=True)`` does
    nothing at all for a directory that is already there, so it says yes to a
    read-only stick and to ``C:\\Program Files``; ``os.access`` on Windows
    reports only the read-only attribute, so it says yes to a folder an ACL
    denies.

    Four things are asked, because on NTFS they are four separate rights and
    everything this program stores lives in a sub-directory: may a file be
    created, may it be deleted again, may a sub-directory be created, and may
    that come off too. A folder that takes a file but no ``store/`` would
    otherwise pass here and then kill the start-up with an untranslated
    ``Access is denied`` and no fallback. Every probe gets a name of its own,
    so a leftover can never make a perfectly writable directory test as
    unusable -- the previous name held only the pid, and one leftover bricked
    every later launch that happened to be handed the same pid. A probe that
    will not come off again is not ignored either: a folder that refuses
    deletions is one sqlite cannot keep a journal in, which is exactly what
    the caller is asking about.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    _sweep_probes(root)
    for make, remove in ((_make_probe_file, os.unlink), (_make_probe_dir, os.rmdir)):
        probe = make(root)
        if probe is None:
            return False
        try:
            remove(probe)
        except FileNotFoundError:                   # another launch swept it: still fine
            continue
        except OSError:
            return False
    return True


def _probe_path(root: Path) -> Path:
    """A probe name no other launch can be handed.

    ``tempfile.mkstemp`` cannot be used for this: on Windows it reads a
    refusal as "a directory of that name exists" whenever ``os.access`` says
    the folder is writable -- which it always does for an ACL -- and retries
    ten thousand times before giving up.
    """
    return root / f"{PROBE_PREFIX}{os.getpid()}-{os.urandom(4).hex()}"


def _make_probe_file(root: Path) -> str | None:
    for _attempt in range(4):
        probe = _probe_path(root)
        try:
            handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:                     # pragma: no cover - 32 bits of name
            continue
        except OSError:
            return None
        os.close(handle)
        return str(probe)
    return None                                     # pragma: no cover - unreachable


def _make_probe_dir(root: Path) -> str | None:
    """Everything this program stores lives in a sub-directory, and on NTFS
    adding a file and adding a sub-directory are separate rights."""
    for _attempt in range(4):
        probe = _probe_path(root)
        try:
            os.mkdir(probe, 0o700)
        except FileExistsError:                     # pragma: no cover - 32 bits of name
            continue
        except OSError:
            return None
        return str(probe)
    return None                                     # pragma: no cover - unreachable


def is_volatile(root: Path) -> bool:
    """True when the system is allowed to delete *root* behind the user's back.

    The temporary directory is: Storage Sense and every cleanup tool treat it
    as disposable, and the history, journal and undo snapshots kept there may
    be the only copy.
    """
    try:
        return Path(root).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, RuntimeError, ValueError):     # pragma: no cover - unresolvable
        return False


def fallback_roots() -> list[Path]:
    """Where to keep data when the requested directory will not hold it.

    Both are per-user, stable between runs, and somewhere their owner can go
    and look: the ordinary application data directory first, then the
    temporary directory, which stays writable even on a machine whose profile
    is locked down but which the system may empty, so it is named last and
    never used without saying so.
    """
    return [_platform_data_root(),
            Path(tempfile.gettempdir()) / f"{__app_name__.lower()}-data"]


def _relocated(root: Path, requested: Path) -> Path:
    """An area of its own, inside *root*, for data refused by *requested*.

    Without this a portable copy on a read-only stick falls back onto the
    installed copy's own library and starts writing into its history, journal
    and snapshot store. Keyed by the refused location, so two launches that
    were refused the same directory share one area -- and therefore one lock.
    """
    key = hashlib.sha1(str(requested).casefold().encode("utf-8")).hexdigest()[:12]
    return root / "relocated" / key


def _accepts_data(root: Path, attempts: int = 1, pause: float = 0.2) -> bool:
    """``is_usable``, retried, so a busy moment is not read as a refusal.

    A share still mounting, a backup tool holding the folder, an antivirus
    scanner on the probe: relocating on the first refusal turns a few seconds
    into a second library the user never asked for and may never find again.
    """
    for remaining in range(attempts, 0, -1):
        if is_usable(root):
            return True
        if remaining > 1:
            time.sleep(pause)
    return False


def usable_data_dir() -> tuple[Path, Path | None]:
    """The directory to keep data in, and the requested one if it was refused.

    The requested directory may be unusable in a way :func:`data_dir` cannot
    see: a portable install on a read-only stick or share, a copy under
    ``C:\\Program Files`` started by a standard user, an application-data
    folder a company policy denies, or a ``--data-dir`` whose parent does not
    exist. Starting anyway leaves every write to fail later, in whichever
    place happens to write first.

    Raises :class:`DataDirUnusable` when no candidate accepts a file, so the
    caller can say which places were tried instead of dying silently.

    A directory named by ``QINGJIAN_DATA_DIR`` or ``--data-dir`` gets no
    fallback at all: it is a statement about where this run's data belongs,
    usually "not in my library", and the first fallback is the library. A test,
    a self-check or a sandboxed run handed a directory it cannot write would
    otherwise relocate silently into the user's real history -- which is how
    one run left its lock file, logs and sqlite journals there. Refusing to
    start is the lesser harm, and the only outcome the caller can report
    honestly.
    """
    explicit = explicit_dir()
    try:
        requested = data_dir(create=False)
    except (OSError, RuntimeError, ValueError):     # no home directory at all
        requested = None
    if requested is not None and _accepts_data(requested, attempts=3):
        return requested, None
    tried: list[Path] = [] if requested is None else [requested]
    if explicit is not None:
        raise DataDirUnusable(tried)
    for root in fallback_roots():
        candidate = root if requested is None else _relocated(root, requested)
        if candidate in tried or _inside(candidate, requested):
            continue
        tried.append(candidate)
        if is_usable(candidate):
            _leave_origin(candidate, requested)
            return candidate, requested
    raise DataDirUnusable(tried)


def _inside(candidate: Path, requested: Path | None) -> bool:
    """A fallback under the directory that just refused us is no fallback."""
    if requested is None:
        return False
    try:
        return candidate == requested or candidate.is_relative_to(requested)
    except (OSError, ValueError):                   # pragma: no cover - uncomparable
        return False


def _leave_origin(root: Path, requested: Path | None) -> None:
    """Note in *root* which directory it stands in for. Never raises."""
    if requested is None:
        return
    try:
        (root / ORIGIN_MARKER).write_text(f"{requested}\n", encoding="utf-8")
    except OSError:                                 # pragma: no cover - just probed it
        pass


def stranded_data(active: Path) -> Path | None:
    """A fallback area holding data that *active* once overflowed into.

    Called when the requested directory is working again: the run before it
    may have written a whole second library somewhere else, and nothing in the
    product ever mentioned it again. Returns the area once, then marks the note
    as read, so the user is told but not nagged.
    """
    active = Path(active)
    for root in fallback_roots():
        candidate = _relocated(root, active)
        marker = candidate / ORIGIN_MARKER
        try:
            if not marker.is_file() or marker.read_text(encoding="utf-8").strip() \
                    != str(active):
                continue
            if not any((candidate / name).exists()
                       for name in ("settings.json", "state", "store")):
                continue
            marker.replace(candidate / (ORIGIN_MARKER + ".seen"))
        except OSError:
            continue
        return candidate
    return None


def subdir(name: str, create: bool = True) -> Path:
    path = data_dir(create=create) / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def store_dir(create: bool = True) -> Path:
    """Transactions, snapshots and journal."""
    return subdir("store", create)


def state_dir(create: bool = True) -> Path:
    """History shards, review queues, tags, ignore lists."""
    return subdir("state", create)


def cache_dir(create: bool = True) -> Path:
    """Thumbnails and content-hash caches. Safe to delete at any time."""
    return subdir("cache", create)


def log_dir(create: bool = True) -> Path:
    return subdir("logs", create)


def settings_path() -> Path:
    return data_dir() / "settings.json"


def lock_path() -> Path:
    return data_dir() / "qingjian.lock"
