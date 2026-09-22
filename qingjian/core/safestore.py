"""Durable, content-checked file transactions.

Model
-----
A :class:`Plan` is an ordered list of steps plus the ready-made inverse list
that undoes them. The journal is written, fsynced and closed *before* the first
user file is touched, so an interrupted run always leaves enough on disk to
finish or to reverse.

Every step is idempotent: it first asks "is this already the case?" and returns
if so. That is what makes crash recovery a simple replay rather than a guess.

Why there is a fast path
------------------------
The previous engine expressed a move as "snapshot both ends, copy the content
to the target, verify it, delete the source". Moving a 5 GB video therefore
read and wrote roughly 15 GB and left a 5 GB restore copy behind. When source
and target sit on one volume a move is a directory-entry change: ``os.replace``
is atomic, needs no copy, and is undone by replacing back. Hashing exists to
prove a *copy* is faithful; a rename has no copy to prove, so the fast path
verifies identity by size and mtime and is still safe.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .logsetup import get_logger
from .platform_ import IS_WINDOWS, free_space, hide, same_volume, volume_id

log = get_logger("safestore")

Progress = Callable[[str, int], None]
Cancel = Callable[[], bool]

BLOCK = 1024 * 1024

#: Where a recycled file waits: a hidden folder beside the one it came from.
#: Getting there is a rename on the same disk -- instant, undone by renaming
#: back -- where a restore copy in the application's own folder meant copying
#: every recycled video onto the system drive.
TRASH_DIR = ".qingjian-trash"

# ``os.replace`` is atomic for readers, but Windows can briefly reject two
# concurrent replacements of the same destination with ``PermissionError``.
# Journal writes are tiny; serialize them in-process while retaining unique
# temporary names so a failed writer can only clean up its own file.
_ATOMIC_JSON_LOCK = threading.RLock()

VERIFY_FULL = "full"
VERIFY_FAST = "fast"

# step kinds
MOVE = "move"
COPY = "copy"
WRITE = "write"
UNLINK = "unlink"


def _retry_sharing(operation, *args):
    """Retry only transient Windows sharing conflicts for at most 0.6 seconds."""
    deadline = time.monotonic() + 0.6
    delay = 0.04
    while True:
        try:
            return operation(*args)
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (32, 33):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.15)

def _NOOP_PROGRESS(message: str, percent: int) -> None:
    """Progress sink used when a caller does not care."""


def _NEVER() -> bool:
    """Cancellation probe that never cancels."""
    return False


class Cancelled(Exception):
    """The user stopped the operation before any file was changed."""


class TransactionError(OSError):
    """Carries an i18n key in :attr:`key` alongside the plain message."""

    def __init__(self, key: str, message: str = "", **fields: object) -> None:
        super().__init__(message or key)
        self.key = key
        self.fields = fields


# ---------------------------------------------------------------- utilities


def atomic_json(path: str | Path, value: object) -> None:
    """Write JSON so that a crash leaves either the old file or the new one.

    The temporary carries a unique suffix. A shared one meant two threads
    writing the same journal raced: the first ``os.replace`` moved the file the
    second was still writing, and that second write then failed with
    FileNotFoundError, leaving a transaction running with no journal at all.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}-{uuid.uuid4().hex}.writing")
    with _ATOMIC_JSON_LOCK:
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def read_json(path: str | Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    # A damaged file is never silently replaced with an empty one; the caller
    # decides, because throwing history away is worse than refusing to start.
    return json.loads(path.read_text(encoding="utf-8"))


def _path_key(path: str | Path) -> str:
    """Compare paths the way the filesystem does, without touching the disk."""
    return os.path.normcase(os.path.abspath(str(path))).casefold()


def _extended_path(path: Path) -> Path:
    r"""The \\?\ form of *path*, which the 259-character limit does not apply to.

    The staged copy carries a 48-character prefix, so a destination that passed
    `check_path_length` can still be too long for the temporary beside it. Open,
    replace and unlink all accept this form, whatever the machine's long-path
    setting says.
    """
    if not IS_WINDOWS:
        return path
    text = os.path.abspath(str(path))
    if text.startswith("\\\\?\\"):
        return Path(text)
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC" + text[1:])
    return Path("\\\\?\\" + text)


def _unlink_source(src: Path, landed: Path | None = None) -> None:
    """Remove the source of a finished move, even when the camera protected it.

    Clearing the flag is what a same-volume rename does implicitly: Windows
    renames a read-only file happily and only refuses to delete it. The copy at
    the far end gets the protection back, so nothing silently loses it.
    """
    try:
        _retry_sharing(src.unlink)
    except PermissionError as error:
        if getattr(error, "winerror", None) in (32, 33):
            raise
        mode = src.stat().st_mode
        if mode & stat.S_IWRITE:
            raise                           # locked by another program, not protected
        os.chmod(src, mode | stat.S_IWRITE)
        _retry_sharing(src.unlink)
        if landed is not None and landed.exists():
            os.chmod(landed, stat.S_IREAD)


def fingerprint(path: str | Path, progress: Progress = _NOOP_PROGRESS,
                cancel: Cancel = _NEVER) -> str | None:
    """SHA-256 of *path*, or None when it does not exist."""
    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise TransactionError("error.not_regular_file", str(path), path=str(path))
    digest = hashlib.sha256()
    size = path.stat().st_size
    done = 0
    with path.open("rb") as stream:
        while block := stream.read(BLOCK):
            if cancel():
                raise Cancelled("cancelled")
            digest.update(block)
            done += len(block)
            progress(f"{path.name}", int(done * 100 / max(1, size)))
    return digest.hexdigest()


#: How much of each end of a file the cheap prefilter reads.
SAMPLE = 64 * 1024


def sample_digest(path: str | Path) -> str | None:
    """A digest of the size plus the first and last :data:`SAMPLE` bytes.

    Two byte-identical files always agree on this, so it never hides a
    duplicate; files that merely share a size almost never agree, so the
    duplicate scan can skip reading them in full. On a library of raw files or
    video that is the difference between reading a few megabytes and reading
    every byte on the disk.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        digest = hashlib.sha256(str(size).encode("ascii"))
        with path.open("rb") as stream:
            digest.update(stream.read(SAMPLE))
            if size > 2 * SAMPLE:
                stream.seek(-SAMPLE, os.SEEK_END)
                digest.update(stream.read(SAMPLE))
    except OSError:
        return None
    return digest.hexdigest()


def _same_content(src: Path, dst: Path, result: dict | None) -> bool:
    """Prove the target holds the source's bytes, before the source is deleted."""
    want = (result or {}).get("hash")
    if want:
        return fingerprint(dst) == want
    if sample_digest(src) != sample_digest(dst):
        return False
    return fingerprint(src) == fingerprint(dst)


def identity(path: str | Path, verify: str = VERIFY_FULL,
             progress: Progress = _NOOP_PROGRESS, cancel: Cancel = _NEVER) -> dict | None:
    """Describe the current content of *path*, or None when absent."""
    path = Path(path)
    try:
        stat = path.stat()
    except (OSError, ValueError):
        return None
    if path.is_symlink() or not path.is_file():
        raise TransactionError("error.symlink" if path.is_symlink() else "error.not_regular_file",
                               str(path), path=str(path))
    record = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if verify == VERIFY_FULL:
        record["hash"] = fingerprint(path, progress, cancel)
    return record


#: What `Path.exists()` itself treats as "not there". Everything else -- a
#: refused folder, a card pulled out mid-operation, a path too long -- is an
#: answer we did not get, and must not be read as "no file here".
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})
_ABSENT_WINERRORS = frozenset({21, 123, 1921})   # not ready / bad name / cannot resolve


def _stat_or_none(path: str | Path):
    """The stat of *path*, or None when it is not there.

    One syscall, never two: `Path.exists()` is itself a stat, so asking it and
    then asking for the stat pays twice for the same answer. This is on the
    path of every key press.

    Anything other than "not there" is raised, exactly as `Path.exists()`
    raises it. Reading "I cannot tell" as "nothing is there" let a precheck
    wave through a plan that could not run, which is the half-moved group
    F-008 exists to prevent.
    """
    try:
        return os.stat(path)
    except ValueError:
        return None
    except OSError as error:
        if (error.errno in _ABSENT_ERRNOS
                or getattr(error, "winerror", None) in _ABSENT_WINERRORS):
            return None
        raise


def _fast_matches(info, expected: dict | None) -> bool:
    """Size and mtime only, from a stat already taken (*info* is None when gone)."""
    if expected is None:
        return info is None
    if info is None:
        return False
    if expected.get("size") is not None and info.st_size != expected["size"]:
        return False
    want_mtime = expected.get("mtime_ns")
    if want_mtime is not None:
        return info.st_mtime_ns == want_mtime
    return True


def identity_matches(path: str | Path, expected: dict | None, verify: str = VERIFY_FULL,
                     progress: Progress = _NOOP_PROGRESS, cancel: Cancel = _NEVER) -> bool:
    """Does *path* currently hold the content described by *expected*?"""
    path = Path(path)
    info = _stat_or_none(path)
    want_hash = (expected or {}).get("hash")
    if info is not None and want_hash and verify == VERIFY_FULL:
        if expected.get("size") is not None and info.st_size != expected["size"]:
            return False
        return fingerprint(path, progress, cancel) == want_hash
    return _fast_matches(info, expected)


def copy_verified(source: str | Path, target: str | Path, progress: Progress = _NOOP_PROGRESS,
                  cancel: Cancel = _NEVER, verify: str = VERIFY_FULL) -> dict:
    """Copy *source* to a fresh *target* and prove the copy is faithful."""
    source, target = Path(source), Path(target)
    before = source.stat()
    total = before.st_size
    digest = hashlib.sha256()
    done = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as inp, target.open("xb") as out:
        while block := inp.read(BLOCK):
            if cancel():
                raise Cancelled("cancelled")
            out.write(block)
            digest.update(block)
            done += len(block)
            progress(source.name, int(done * 100 / max(1, total)))
        out.flush()
        os.fsync(out.fileno())
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise TransactionError("error.source_changed", str(source), path=str(source))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    value = digest.hexdigest()
    if verify == VERIFY_FULL and fingerprint(target, progress, cancel) != value:
        raise TransactionError("error.copy_verify", str(target), path=str(target))
    stat = target.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "hash": value}


# ------------------------------------------------------------------- plans


def step_move(src: str | Path, dst: str | Path, src_id: dict, result: dict | None = None) -> dict:
    return {"kind": MOVE, "src": str(src), "dst": str(dst), "src_id": src_id,
            "result": result or dict(src_id)}


def step_copy(src: str | Path, dst: str | Path, src_id: dict) -> dict:
    return {"kind": COPY, "src": str(src), "dst": str(dst), "src_id": src_id, "result": None}


def step_write(dst: str | Path, snapshot: dict, expect: dict | None) -> dict:
    return {"kind": WRITE, "dst": str(dst), "snapshot": snapshot, "expect": expect,
            "result": {"size": snapshot.get("size"), "hash": snapshot.get("hash")}}


def step_unlink(path: str | Path, expect: dict | None) -> dict:
    return {"kind": UNLINK, "dst": str(path), "expect": expect, "result": None}


@dataclass
class Plan:
    """A forward step list, its inverse, and the app state to commit with it."""

    forward: list[dict] = field(default_factory=list)
    inverse: list[dict] = field(default_factory=list)
    state: dict | None = None
    verify: str = VERIFY_FULL
    label: str = ""
    #: Snapshots this plan created, so a rollback can delete them again.
    snapshots: list[str] = field(default_factory=list)

    def bytes_written(self) -> int:
        total = 0
        for step in self.forward:
            if step["kind"] == COPY:
                total += int((step.get("src_id") or {}).get("size") or 0)
            elif step["kind"] == WRITE:
                total += int((step.get("snapshot") or {}).get("size") or 0)
            elif step["kind"] == MOVE:
                # Counted only when the two ends sit on different volumes.
                if not same_volume(step["src"], Path(step["dst"]).parent):
                    total += int((step.get("src_id") or {}).get("size") or 0)
        return total

    def targets(self) -> list[str]:
        return [step["dst"] for step in self.forward]

    def to_dict(self) -> dict:
        return {"forward": self.forward, "inverse": self.inverse, "state": self.state,
                "verify": self.verify, "label": self.label, "snapshots": self.snapshots}

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(forward=list(data.get("forward") or []),
                   inverse=list(data.get("inverse") or []),
                   state=data.get("state"),
                   verify=str(data.get("verify") or VERIFY_FULL),
                   label=str(data.get("label") or ""),
                   snapshots=list(data.get("snapshots") or []))


@dataclass(frozen=True)
class QuotaPolicy:
    """Limits past which the oldest restore copies are reclaimed."""

    max_operations: int = 200
    max_bytes: int = 20 * 1024 ** 3
    max_days: int = 30
    #: Reclaim without asking once a limit is passed.
    automatic: bool = True

    def to_dict(self) -> dict:
        return {"max_operations": self.max_operations, "max_bytes": self.max_bytes,
                "max_days": self.max_days, "automatic": self.automatic}

    @classmethod
    def from_dict(cls, data: dict | None) -> "QuotaPolicy":
        if not isinstance(data, dict):
            return cls()
        def positive(key: str, default: int) -> int:
            try:
                value = int(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(0, value)
        return cls(positive("max_operations", 200),
                   positive("max_bytes", 20 * 1024 ** 3),
                   positive("max_days", 30),
                   bool(data.get("automatic", True)))


# ------------------------------------------------------------------- store


class SafeStore:
    """Owns the journal, the snapshot area and the transaction executor."""

    def __init__(self, root: str | Path, quota: QuotaPolicy | None = None,
                 verify: str = VERIFY_FULL, fast_path: bool = True) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.root / "journal.json"
        self.snapshot_root = self.root / "snapshots"
        self.snapshot_root.mkdir(exist_ok=True)
        self.quota = quota or QuotaPolicy()
        self.verify = verify
        self.fast_path = fast_path
        # Walking the restore area is a stat per snapshot; the status bar asks
        # for it after every operation, so the answer is remembered until a
        # snapshot is actually written or reclaimed.
        self._usage: int | None = None
        # There is one journal file, so there can be one transaction. Sorting
        # runs on the queue thread while undo and redo ran straight off the
        # interface thread, and the two then fought over that file: whoever
        # renamed it first left the other writing into nothing.
        self._lock = threading.RLock()
        # Set the moment a step makes a change that cannot be taken back by
        # itself. A journal read back from a crash cannot know how far the run
        # got, so only a run that has touched nothing may drop its journal.
        self._touched = False
        # Recycle folders live beside the user's files, so the usage total can
        # only find them again after a restart if it keeps a list of them.
        self.trash_index = self.root / "trash-folders.json"
        self._trash_lock = threading.Lock()
        try:
            self._trash_folders: set[str] = set(read_json(self.trash_index, []) or [])
        except ValueError:
            self._trash_folders = set()
        self._sweep_writing()

    def _sweep_writing(self) -> None:
        """Drop half-written journals left by a kill during atomic_json."""
        try:
            for item in self.root.iterdir():
                if item.name.endswith(".writing"):
                    item.unlink(missing_ok=True)
        except OSError:
            pass

    # -- pending -------------------------------------------------------
    def has_pending(self) -> bool:
        return self.journal_path.exists()

    def pending_plan(self) -> Plan | None:
        if not self.has_pending():
            return None
        return Plan.from_dict(read_json(self.journal_path, {}) or {})

    # -- snapshots -----------------------------------------------------
    def snapshot(self, path: str | Path, progress: Progress = _NOOP_PROGRESS,
                 cancel: Cancel = _NEVER) -> dict | None:
        """Copy *path* into the restore area. None when the file is absent."""
        path = Path(path)
        if not path.exists():
            return None
        target = self.snapshot_root / uuid.uuid4().hex
        try:
            info = copy_verified(path, target, progress, cancel, self.verify)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if self._usage is not None:
            self._usage += int(info["size"])
        return {"file": str(target), "hash": info.get("hash"), "size": info["size"]}

    def trash_slot(self, path: str | Path) -> Path | None:
        """A free name for *path* in the hidden recycle folder beside it.

        None when that folder cannot be made there -- a file in the way, no
        rights -- or the name would be too long; the caller then keeps a restore
        copy instead.
        """
        path = Path(path)
        slot = path.parent / TRASH_DIR / (uuid.uuid4().hex + path.suffix)
        if len(str(slot)) > 259:
            return None
        try:
            self._prepare_trash_folder(slot.parent)
        except OSError:
            return None
        return slot

    def _prepare_trash_folder(self, folder: Path) -> None:
        if not folder.is_dir():
            folder.mkdir(parents=True)
            hide(folder)
        with self._trash_lock:
            if str(folder) in self._trash_folders:
                return
            self._trash_folders.add(str(folder))
            atomic_json(self.trash_index, sorted(self._trash_folders))

    def usage(self, refresh: bool = False) -> int:
        if self._usage is not None and not refresh:
            return self._usage
        with self._trash_lock:
            folders = [self.snapshot_root, *map(Path, self._trash_folders)]
        total = 0
        for folder in folders:
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        self._usage = total
        return total

    def discard_snapshots(self, refs: Iterable[str]) -> int:
        freed = 0
        for ref in refs:
            path = Path(ref)
            if not path.is_file():
                continue
            try:
                freed += path.stat().st_size
                path.unlink()
            except OSError:
                continue
            if path.parent.name == TRASH_DIR:
                self._drop_trash_folder_if_empty(path.parent)
        if self._usage is not None:
            self._usage = max(0, self._usage - freed)
        return freed

    def _drop_trash_folder_if_empty(self, folder: Path) -> None:
        try:
            folder.rmdir()                  # refuses while anything is left in it
        except OSError:
            return
        with self._trash_lock:
            self._trash_folders.discard(str(folder))
            atomic_json(self.trash_index, sorted(self._trash_folders))

    def _count_recycled(self, src: Path, dst: Path, identity_: dict | None) -> None:
        """Keep the usage total right as files go into and out of a recycle folder."""
        if self._usage is None:
            return
        size = int((identity_ or {}).get("size") or 0)
        if dst.parent.name == TRASH_DIR:
            self._usage += size
        elif src.parent.name == TRASH_DIR:
            self._usage = max(0, self._usage - size)

    # -- executing -----------------------------------------------------
    def run(self, plan: Plan, progress: Progress = _NOOP_PROGRESS,
            cancel: Cancel = _NEVER, save_state: Callable[[dict], None] | None = None) -> None:
        """Journal *plan*, apply it, then commit its state and clear the journal."""
        with self._lock:
            if self.has_pending():
                raise TransactionError("error.pending_block_write")
            plan.verify = plan.verify or self.verify
            self._precheck(plan)
            if cancel():
                raise Cancelled("cancelled")
            payload = plan.to_dict()
            payload["stage_id"] = uuid.uuid4().hex
            payload["created"] = time.time()
            # Durable before the first user file changes.
            atomic_json(self.journal_path, payload)
            self._apply(payload, progress, save_state)

    def recover(self, progress: Progress = _NOOP_PROGRESS,
                save_state: Callable[[dict], None] | None = None) -> bool:
        """Finish a journalled transaction left behind by a crash."""
        with self._lock:
            if not self.has_pending():
                return False
            payload = read_json(self.journal_path, None)
            if not payload:
                self.journal_path.unlink(missing_ok=True)
                return False
            log.warning("resuming journalled transaction %s", payload.get("stage_id"))
            # What is on disk may already have been changed: a kill leaves no trace of
            # how far the run got. Only `run` may drop a journal, and only while nothing
            # has been touched yet.
            payload["mutated"] = True
            self._apply(payload, progress, save_state)
            return True

    def abandon(self, rollback: bool, progress: Progress = _NOOP_PROGRESS,
                save_state: Callable[[dict], None] | None = None) -> bool:
        """Leave a journal that can never be replayed: reverse it, or keep what is on disk.

        This is the way out of a permanent failure, not a delete confirmation.
        """
        with self._lock:
            if not self.has_pending():
                return False
            try:
                payload = read_json(self.journal_path, None)
            except ValueError:
                payload, damaged = None, True
            else:
                damaged = payload is not None and not isinstance(payload, dict)
            if damaged:
                if rollback:
                    raise TransactionError("error.journal_damaged")
                # Keep it for diagnosis: it is small, and deleting the only
                # record of an unfinished operation is worse than a stray file.
                os.replace(self.journal_path,
                           self.root / f"journal.damaged-{time.time_ns()}.json")
                return True
            if not payload:
                self.journal_path.unlink(missing_ok=True)
                return True
            if rollback:
                self._rollback(payload, progress)
                self.discard_snapshots(payload.get("snapshots") or [])
            else:
                delta = _keep_record_delta(payload)
                if delta is not None:
                    if save_state is None:
                        raise ValueError("save_state is required to keep snapshot references")
                    # The record goes in first: a snapshot nothing points at is
                    # a restore copy the user can never find or reclaim.
                    save_state(delta)
            log.warning("journal %s abandoned (rollback=%s)", payload.get("stage_id"), rollback)
            self.journal_path.unlink(missing_ok=True)
            return True

    def _rollback(self, payload: dict, progress: Progress) -> None:
        """Undo the steps that already ran. Not journalled: it only ever shrinks the prefix."""
        verify = str(payload.get("verify") or VERIFY_FULL)
        stage = str(payload.get("stage_id") or uuid.uuid4().hex)
        forward = list(payload.get("forward") or [])
        inverse = list(payload.get("inverse") or [])
        done = 0
        for index, step in enumerate(forward):
            if self._step_done(step, verify, forward[index + 1:]):
                done = index + 1
                continue
            if (step["kind"] == MOVE
                    and identity_matches(Path(step["dst"]),
                                         step.get("result") or step.get("src_id"), VERIFY_FAST)
                    and Path(step["src"]).exists()):
                done = index + 1        # a cross-volume move that copied but could not unlink
            break
        steps = (inverse[len(inverse) - done:] if len(inverse) == len(forward)
                 else SafeStore.invert(forward[:done]))
        for position, step in enumerate(steps):
            progress(Path(step.get("dst", "")).name, int(position * 100 / max(1, len(steps))))
            self._run_step(step, verify, stage, progress, steps[position + 1:])

    # -- internals -----------------------------------------------------
    def _precheck(self, plan: Plan) -> None:
        """Refuse a doomed plan before the journal exists, so nothing is touched.

        A group whose second member cannot be moved used to leave the first one
        moved and a journal nobody could replay. Stat only, never a hash: this
        runs in front of every key press.
        """
        # Nothing here changes the disk, so each path is stat'ed once for the
        # whole check and the answer reused. Volumes are asked per folder: a
        # group of photographs shares the folder it comes from and the one it
        # goes to, so a five-member group costs the same as a one-member one.
        seen: dict[str, object] = {}
        volumes: dict[str, object] = {}
        pairs: dict[tuple[str, str], bool] = {}

        def stat_of(path: str | Path):
            key = _path_key(path)
            if key not in seen:
                seen[key] = _stat_or_none(path)
            return seen[key]

        def volume_of(folder: Path):
            key = _path_key(folder)
            if key not in volumes:
                volumes[key] = volume_id(folder)
            return volumes[key]

        def on_same_volume(src: Path, parent: Path) -> bool:
            # Asked per folder pair, not per file: a group shares the folder it
            # comes from and the one it goes to. `same_volume` stays the single
            # source of truth, so this check and the move itself never disagree.
            key = (_path_key(src.parent), _path_key(parent))
            if key not in pairs:
                pairs[key] = same_volume(src.parent, parent)
            return pairs[key]

        # A plan may never delete a file it also reads: the member that comes
        # later would then find its source gone.
        sources = {_path_key(step["src"]) for step in plan.forward
                   if step["kind"] in (MOVE, COPY)}
        for step in plan.forward:
            if step["kind"] == UNLINK and _path_key(step["dst"]) in sources:
                raise TransactionError("error.plan_collision", step["dst"], path=step["dst"])

        made: set[str] = set()              # paths earlier steps put a file at
        gone: set[str] = set()              # paths earlier steps emptied
        for step in plan.forward:
            kind, dst = step["kind"], Path(step["dst"])
            dkey = _path_key(dst)
            done = self._step_done(step, VERIFY_FAST, stat_of=stat_of)
            if kind in (MOVE, COPY):
                src = Path(step["src"])
                skey = _path_key(src)
                if skey not in made and not done and (
                        skey in gone
                        or not _fast_matches(stat_of(src), step.get("src_id"))):
                    raise TransactionError("error.external_change", str(src), path=str(src))
            if kind in (MOVE, COPY, WRITE):
                if dkey in made:
                    raise TransactionError("error.plan_collision", str(dst), path=str(dst))
                if kind == WRITE and not done:
                    snapshot = Path((step.get("snapshot") or {}).get("file", ""))
                    kept = stat_of(snapshot)
                    if kept is None or not stat.S_ISREG(kept.st_mode):
                        raise TransactionError("error.snapshot_missing", str(snapshot),
                                               path=str(snapshot))
                if not done and dkey not in gone and stat_of(dst) is not None and (
                        kind != WRITE
                        or not _fast_matches(stat_of(dst), step.get("expect"))):
                    raise TransactionError("error.changed_midway", str(dst), path=str(dst))
                made.add(dkey)
                gone.discard(dkey)
            if kind == UNLINK:
                if not done and stat_of(dst) is not None \
                        and not _fast_matches(stat_of(dst), step.get("expect")):
                    raise TransactionError("error.external_change", str(dst), path=str(dst))
                gone.add(dkey)
                made.discard(dkey)
            if kind == MOVE:
                skey = _path_key(step["src"])
                gone.add(skey)
                made.discard(skey)

        # Space, per volume. A same-volume move writes nothing, so a plan made
        # only of those adds no root here and never asks for free space.
        roots: dict[object, tuple[Path, int]] = {}
        for step in plan.forward:
            kind = step["kind"]
            parent = Path(step["dst"]).parent
            if kind in (COPY, WRITE):
                size = int((step.get("src_id") or step.get("snapshot") or {}).get("size") or 0)
            elif kind == MOVE and (not self.fast_path
                                   or not on_same_volume(Path(step["src"]), parent)):
                # A cross-volume move is a copy: the group that half-filled a
                # card is exactly what jammed the journal.
                size = int((step.get("src_id") or {}).get("size") or 0)
            else:
                continue
            key = volume_of(parent)
            key = key if key is not None else str(parent)
            where, total = roots.get(key, (parent, 0))
            roots[key] = (where, total + size)
        for where, size in roots.values():
            available = free_space(where)
            # Leave a small margin: a volume filled to the last byte behaves badly.
            if available is not None and available < size + 16 * 1024 * 1024:
                raise TransactionError("error.disk_full", f"{where}: need {size}, free {available}")

    def _step_done(self, step: dict, verify: str, later: Sequence[dict] = (),
                   stat_of: Callable[[str | Path], object] | None = None) -> bool:
        """Is this step already in effect? Stat only, except WRITE, which uses *verify*.

        *stat_of* lets a caller that checks many steps share one stat per path.
        """
        info = stat_of or _stat_or_none
        kind, dst, result = step["kind"], Path(step["dst"]), step.get("result")
        if kind == MOVE:
            return (_fast_matches(info(dst), result or step.get("src_id"))
                    and info(step["src"]) is None)
        if kind == COPY:
            return _fast_matches(info(dst), result or step.get("src_id"))
        if kind == WRITE:
            return bool(result) and identity_matches(dst, result, verify)
        if kind == UNLINK:
            return info(dst) is None or self._landed_later(dst, later)
        return False

    def _apply(self, payload: dict, progress: Progress, save_state) -> None:
        verify = str(payload.get("verify") or VERIFY_FULL)
        stage = str(payload.get("stage_id") or uuid.uuid4().hex)
        steps = list(payload.get("forward") or [])
        total = max(1, len(steps))
        mutated = bool(payload.get("mutated"))
        try:
            for index, step in enumerate(steps):
                progress(Path(step.get("dst", "")).name, int(index * 100 / total))
                self._touched = False
                if self._run_step(step, verify, stage, progress, steps[index + 1:]):
                    mutated = True
                if step["kind"] in (MOVE, COPY):
                    self._sync_inverse(payload, index, step)
        except BaseException as error:
            mutated = mutated or self._touched
            if not mutated:
                # Nothing on disk changed, so there is nothing to recover from.
                # Leaving a journal here would block every later operation for
                # a failure that already rolled itself back.
                self.journal_path.unlink(missing_ok=True)
                log.warning("transaction refused before any change: %s", error)
                raise
            payload["mutated"] = True
            atomic_json(self.journal_path, payload)
            log.exception("transaction stopped part-way")
            if isinstance(error, Cancelled):
                raise
            raise TransactionError("error.unfinished", str(error), error=str(error)) from error
        state = payload.get("state")
        if state is not None and save_state is not None:
            try:
                save_state(state)
            except BaseException:
                # The files are all in place; only the record is missing. Keeping the
                # journal is what lets "Finish pending operation" commit it later.
                payload["mutated"] = True
                atomic_json(self.journal_path, payload)
                raise
        self.journal_path.unlink(missing_ok=True)

    def _staged_copy(self, source: Path, dst: Path, stage: str, verify: str,
                     progress: Progress) -> dict:
        """Copy into a hidden sibling, then rename into place.

        The temporary carries this transaction's stage id so it is owned by
        exactly one run and is never matched by a wildcard.
        """
        staged = _extended_path(dst.with_name(f".qingjian-{stage}-{dst.name}.part"))
        staged.unlink(missing_ok=True)
        try:
            info = copy_verified(source, staged, progress, _NEVER, verify)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        os.replace(staged, dst)
        return info

    def sweep_partials(self, folders: Iterable[str | Path]) -> int:
        """Remove leftover ``.part`` files from a run that died mid-copy."""
        removed = 0
        for folder in folders:
            try:
                for item in Path(folder).iterdir():
                    if item.name.startswith(".qingjian-") and item.name.endswith(".part"):
                        item.unlink(missing_ok=True)
                        removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def _sync_inverse(payload: dict, index: int, step: dict) -> None:
        """Teach the inverse what the copy actually got.

        A volume with a coarse clock rounds the modification time, so the file at
        the far end is not bit-identical in its metadata to the plan's guess. In
        process the record holds the very same lists; read back from JSON it holds
        copies, so both are written. A record may carry fewer steps than the journal
        does, so each list pair is lined up against its own forward, and only once
        the step at *index* is confirmed to be the step that just ran.
        """
        landed = step.get("result")
        if not landed:
            return
        pairs = [(payload.get("forward") or [], payload.get("inverse") or [])]
        for record in (payload.get("state") or {}).get("records_add") or []:
            body = record.get("payload") or {}
            pairs.append((body.get("forward") or [], body.get("inverse") or []))
        for forward, inverse in pairs:
            if len(forward) <= index:
                continue
            twin = forward[index]
            if any(twin.get(field) != step.get(field) for field in ("kind", "src", "dst")):
                continue                    # this list describes some other step
            if twin is not step:
                twin["result"] = dict(landed)
            if len(inverse) != len(forward):
                continue
            mirror = inverse[len(inverse) - 1 - index]
            if step["kind"] == MOVE and mirror.get("kind") == MOVE:
                mirror["src_id"] = dict(landed)
            elif step["kind"] == COPY and mirror.get("kind") == UNLINK:
                mirror["expect"] = dict(landed)

    @staticmethod
    def _landed_later(dst: Path, later: Sequence[dict]) -> bool:
        """Did a later step already put its own file at *dst*?

        An UNLINK followed by a MOVE onto the same path leaves no trace of itself
        once the move has landed. Looking forward costs nothing; a per-step done
        marker in the journal would cost one fsync on every move.
        """
        key = _path_key(dst)
        for step in later:
            if step["kind"] not in (MOVE, COPY) or _path_key(step["dst"]) != key:
                continue
            if not identity_matches(dst, step.get("result") or step.get("src_id"), VERIFY_FAST):
                return False
            return step["kind"] != MOVE or not Path(step["src"]).exists()
        return False

    def _run_step(self, step: dict, verify: str, stage: str, progress: Progress,
                  later: Sequence[dict] = ()) -> bool:
        """Apply one step. Returns True when the filesystem actually changed."""
        kind = step["kind"]
        dst = Path(step["dst"])
        result = step.get("result")

        if kind == MOVE:
            # Identity here is size and modification time, never content: a
            # rename has no copy to prove, and a cross-volume move proves its
            # copy inside `_staged_copy`. Hashing both ends first read a 300 MB
            # video twice for a rename that takes a millisecond.
            src = Path(step["src"])
            done_at_dst = identity_matches(dst, result, VERIFY_FAST)
            if done_at_dst and not src.exists():
                # An impostor of the same size and mtime is not "already restored".
                if result and result.get("hash") and fingerprint(dst) != result["hash"]:
                    raise TransactionError("error.changed_midway", str(dst), path=str(dst))
                return False                            # already done
            if done_at_dst and src.exists():
                # Crashed between writing the target and removing the source. A rename
                # is atomic, so on one volume this state is impossible and the source
                # must be kept; across volumes the target has to prove it holds the
                # bytes before the source is deleted.
                if (same_volume(src, dst.parent)
                        or not identity_matches(src, step.get("src_id"), VERIFY_FAST)
                        or not _same_content(src, dst, result)):
                    raise TransactionError("error.changed_midway", str(src), path=str(src))
                _unlink_source(src, dst)
                self._touched = True
                return True
            if not identity_matches(src, step.get("src_id"), VERIFY_FAST):
                raise TransactionError("error.external_change", str(src), path=str(src))
            if dst.exists():
                raise TransactionError("error.changed_midway", str(dst), path=str(dst))
            if dst.parent.name == TRASH_DIR:
                # Redo can land in a recycle folder that reclaiming has removed.
                self._prepare_trash_folder(dst.parent)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
            if self.fast_path and same_volume(src, dst.parent):
                _retry_sharing(os.replace, src, dst)
                self._touched = True
            else:
                step["result"] = self._staged_copy(src, dst, stage, verify, progress)
                self._touched = True        # the copy is in place; the source may resist
                _unlink_source(src, dst)
            self._count_recycled(src, dst, step.get("src_id"))
            return True

        if kind == COPY:
            src = Path(step["src"])
            # A copy preserves size, mtime and content, so the source identity
            # also describes a finished copy. Checking both is what lets
            # recovery recognise a copy that completed just before the crash,
            # back when `result` had not been written to the journal yet.
            if identity_matches(dst, result or step.get("src_id"), VERIFY_FAST):
                return False
            if dst.exists():
                raise TransactionError("error.changed_midway", str(dst), path=str(dst))
            if not identity_matches(src, step.get("src_id"), VERIFY_FAST):
                raise TransactionError("error.external_change", str(src), path=str(src))
            dst.parent.mkdir(parents=True, exist_ok=True)
            step["result"] = self._staged_copy(src, dst, stage, verify, progress)
            self._touched = True
            return True

        if kind == WRITE:
            snapshot = step.get("snapshot") or {}
            source = Path(snapshot.get("file", ""))
            if result and identity_matches(dst, result, verify):
                return False
            if not source.is_file():
                raise TransactionError("error.snapshot_missing", str(source), path=str(source))
            if snapshot.get("hash") and fingerprint(source) != snapshot["hash"]:
                raise TransactionError("error.snapshot_missing", str(source), path=str(source))
            if dst.exists() and not identity_matches(dst, step.get("expect"), verify):
                raise TransactionError("error.changed_midway", str(dst), path=str(dst))
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._staged_copy(source, dst, stage, verify, progress)
            self._touched = True
            return True

        if kind == UNLINK:
            if not dst.exists():
                return False
            if not identity_matches(dst, step.get("expect"), verify):
                if self._landed_later(dst, later):
                    return False                    # the step after this one already ran
                raise TransactionError("error.external_change", str(dst), path=str(dst))
            dst.unlink()
            self._touched = True
            return True

        raise TransactionError("error.name_invalid", f"unknown step kind {kind!r}")

    # -- inverses ------------------------------------------------------
    @staticmethod
    def invert(steps: list[dict]) -> list[dict]:
        """Build the step list that undoes *steps*, in reverse order."""
        out: list[dict] = []
        for step in reversed(steps):
            kind = step["kind"]
            if kind == MOVE:
                identity_at_target = step.get("result") or step.get("src_id")
                out.append({"kind": MOVE, "src": step["dst"], "dst": step["src"],
                            "src_id": identity_at_target,
                            "result": step.get("src_id") or identity_at_target})
            elif kind == COPY:
                # The inverse is built before the copy runs, so `result` is not
                # filled in yet. A verified copy has the source's content and
                # mtime, so the source identity describes the finished copy too.
                out.append({"kind": UNLINK, "dst": step["dst"],
                            "expect": step.get("result") or step.get("src_id"),
                            "result": None})
            elif kind == WRITE:
                previous = step.get("expect")
                if previous is None:
                    out.append({"kind": UNLINK, "dst": step["dst"],
                                "expect": step.get("result") or (step.get("snapshot") or None),
                                "result": None})
                else:
                    out.append({"kind": WRITE, "dst": step["dst"],
                                "snapshot": step.get("previous_snapshot") or {},
                                "expect": step.get("result"),
                                "result": previous})
            elif kind == UNLINK:
                snapshot = step.get("snapshot") or {}
                out.append({"kind": WRITE, "dst": step["dst"], "snapshot": snapshot,
                            "expect": None,
                            "result": {"size": snapshot.get("size"), "hash": snapshot.get("hash")}})
        return out


def _keep_record_delta(payload: dict) -> dict | None:
    """One record that still points at the snapshots an abandoned plan made."""
    refs = [str(ref) for ref in payload.get("snapshots") or []]
    if not refs:
        return None                  # undo and redo plans: the old record still holds them
    stage = str(payload.get("stage_id") or "")
    added = (payload.get("state") or {}).get("records_add") or []
    if added:
        record = dict(added[0])
    else:
        first = next((s.get("src") or s.get("dst") for s in payload.get("forward") or []), "")
        record = {"id": stage or uuid.uuid4().hex,
                  "action": str(payload.get("label") or "move"), "original": str(first)}
    body = dict(record.get("payload") or {})
    body["snapshots"] = sorted(set(body.get("snapshots") or []) | set(refs))
    body["abandoned"] = stage
    record["payload"] = body
    record["undoable"] = False
    return {"records_add": [record]}


def snapshot_bytes(record: dict) -> int:
    """Bytes of restore copies a history record is holding open."""
    if "snapshot_bytes" in record:
        return int(record["snapshot_bytes"])
    total = 0
    for ref in record.get("snapshots") or []:
        try:
            total += Path(ref).stat().st_size
        except OSError:
            continue
    return total


def reclaim_candidates(records: list[dict], policy: QuotaPolicy,
                       now: float | None = None, keep_newest: bool = False) -> list[int]:
    """Indices of the oldest records to retire so *policy* is satisfied.

    Records are assumed oldest-first. Retiring a record means deleting its
    restore copies and marking it non-undoable — never touching a user file.
    """
    now = time.time() if now is None else now
    sizes = [snapshot_bytes(r) for r in records]
    total = sum(sizes)
    live = list(range(len(records)))
    drop: list[int] = []
    protected = len(records) - 1 if keep_newest and records else None

    def retire(index: int) -> None:
        nonlocal total
        live.remove(index)
        drop.append(index)
        total -= sizes[index]

    if policy.max_days > 0:
        horizon = now - policy.max_days * 86400
        for index in list(live):
            when = records[index].get("time_epoch")
            if index != protected and isinstance(when, (int, float)) and when < horizon:
                retire(index)
    if policy.max_operations > 0:
        while len(live) > policy.max_operations:
            candidate = next((i for i in live if i != protected), None)
            if candidate is None:
                break
            retire(candidate)
    if policy.max_bytes > 0:
        while live and total > policy.max_bytes:
            candidate = next((i for i in live if i != protected and sizes[i] > 0), None)
            if candidate is None:
                break
            retire(candidate)
    return sorted(drop)
