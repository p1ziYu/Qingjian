"""The application, minus its window.

Everything the interface can ask for lives here: opening a folder, building the
queue, classifying, undoing, finding duplicates, managing backups. The Qt layer
holds no logic of its own, which is what keeps that layer thin enough to review
by eye and this layer testable without a display.
"""
from __future__ import annotations

import csv
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .. import __version__
from . import (appdirs, config, dedupe, hashcache, logsetup, mediatypes, metadata,
               ops, platform_, scanner, sidecar as sidecar_mod, work)
from .viewport import clamp_index
from .i18n import tr
from .logsetup import get_logger
from .opqueue import Job, OperationQueue
from .safestore import (SafeStore, TransactionError,
                        reclaim_candidates)
from .state import STACK_HISTORY, STACK_REDO, Record, StateStore

log = get_logger("engine")


def _noop(message: str, percent: int) -> None:
    """Progress sink for callers that do not report."""


def _never() -> bool:
    return False


@dataclass
class _FolderIndex:
    """One folder's files, grouped by the stem that identifies a shot.

    ``mine`` records that the entry came from a fresh listing after our change.
    """

    buckets: dict[str, list[Path]]
    stamp: tuple
    listed_at: float
    mine: bool = False


@dataclass
class SessionStats:
    started: float = field(default_factory=time.time)
    handled: dict[str, int] = field(default_factory=dict)
    undos: int = 0
    redos: int = 0
    bytes_moved: int = 0
    per_folder: dict[str, int] = field(default_factory=dict)
    ids: set[str] = field(default_factory=set)

    def total(self) -> int:
        return sum(self.handled.values())


class Engine:
    """Owns the stores and turns intents into transactions."""

    def __init__(self, data_dir: Path | None = None,
                 settings: config.Settings | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else appdirs.data_dir()
        self.settings = settings if settings is not None else config.load()
        logsetup.configure(self.settings.logging_enabled, self.settings.log_days,
                           directory=self.data_dir / "logs")
        self.store = SafeStore(self.data_dir / "store", quota=self.settings.quota,
                               verify=self.settings.verification,
                               fast_path=self.settings.fast_path)
        self.state = StateStore(self.data_dir / "state" / "state.db")
        self.cache = hashcache.HashCache(
            (self.data_dir / "cache" / "hashes.db") if self.settings.hash_cache else None)
        self.planner = ops.Planner(self.store, self.state, self.settings)
        self.queue = OperationQueue(on_event=self._on_queue_event)
        self.queue_listeners: list[Callable[[str, Job], None]] = []

        self.source_root: Path | None = None
        self.all_files: list[Path] = []
        self.queue_paths: list[Path] = []
        self.index = 0
        self.review_mode = False
        self.stats = SessionStats()
        # Guards every mutation. See `exclusive`.
        self._mutex = threading.RLock()
        self._mutating: int | None = None
        # folder -> base stem -> the files of that shot. Finding companions by
        # listing the folder each time turned every keystroke into a directory
        # walk, which on a folder of twenty thousand files is most of a tenth
        # of a second before anything else happens.
        self._stem_index: dict[str, _FolderIndex] = {}
        log.info("engine ready · version %s · data %s", __version__, self.data_dir)

    # ------------------------------------------------------------ setup
    def close(self) -> None:
        self.queue.stop()
        self.cache.close()
        self.state.close()

    def save_settings(self) -> None:
        config.save(self.settings, self.data_dir / "settings.json")

    def apply_settings(self, settings: config.Settings) -> None:
        """Adopt edited settings, re-tuning the pieces that depend on them."""
        self.settings = settings
        self.planner.settings = settings
        self.store.verify = settings.verification
        self.store.fast_path = settings.fast_path
        self.store.quota = settings.quota
        logsetup.configure(settings.logging_enabled, settings.log_days,
                           directory=self.data_dir / "logs")
        self.save_settings()

    # -------------------------------------------------------- pendings
    def has_pending(self) -> bool:
        return self.store.has_pending()

    def recover(self, progress=_noop) -> bool:
        done = self.store.recover(progress, save_state=self.state.apply)
        if self.source_root:
            self.store.sweep_partials(self._folders_in_play())
        return done

    def abandon_pending(self, rollback: bool, progress=_noop) -> bool:
        """The way out of a journal that can never be replayed (see SafeStore.abandon)."""
        done = self.store.abandon(rollback, progress, save_state=self.state.apply)
        if self.source_root:
            self.store.sweep_partials(self._folders_in_play())
        # Files moved back or left where they are: the per-folder name index is stale.
        self._stem_index.clear()
        return done

    def _folders_in_play(self) -> list[Path]:
        folders = set(self.settings.target_folders())
        if self.source_root:
            folders.add(self.source_root)
        return sorted(folders)

    # --------------------------------------------------------- opening
    def open_folder(self, folder: str | Path, progress=_noop, cancel=_never) -> int:
        root = Path(folder).expanduser()
        if not root.is_dir():
            raise TransactionError("error.folder_missing", str(root))
        previous = (self.source_root, self.settings.source_folder, self.review_mode,
                    self.index, self.all_files, self.queue_paths)
        self.source_root = root.resolve()
        self.settings.source_folder = str(self.source_root)
        self.review_mode = False
        self.index = 0
        self._stem_index.clear()
        try:
            return self.rescan(progress, cancel)
        except BaseException:
            (self.source_root, self.settings.source_folder, self.review_mode,
             self.index, self.all_files, self.queue_paths) = previous
            self._stem_index.clear()
            raise

    def rescan(self, progress=_noop, cancel=_never) -> int:
        if not self.source_root:
            return 0
        self._stem_index.clear()
        metadata.clear_cache()
        self.all_files = scanner.scan(self.source_root, self.settings.recursive,
                                      self.settings.target_folders(), progress, cancel)
        self.rebuild_queue(progress, cancel)
        return len(self.all_files)

    def capture_time(self, path: Path) -> float:
        """Capture time for sorting, remembered between runs.

        Reading it means parsing the file, so the value is kept in the same
        cache as the content hashes and only recomputed when the file changes.
        """
        cached = self.cache.get(path, "captured")
        if cached is not None:
            return float(cached)
        value = metadata.capture_time(path)
        self.cache.put(path, captured=float(value))
        return value

    def warm_capture_times(self, paths: Sequence[Path], progress=_noop,
                           cancel=_never) -> dict[Path, float]:
        """Fill the capture-time cache for *paths* in one pass.

        Sorting by date asks for a capture time per file. One at a time that was
        a cache query, a header parse and a separate disk flush each -- eleven
        seconds for twenty thousand photographs, on the thread that paints the
        window. Here the cached rows come back in one query, only the misses are
        parsed, they are parsed on several threads, and they are written in a
        single transaction.
        """
        paths = list(paths)
        if not paths:
            return {}
        self.cache.preload(paths)
        times: dict[Path, float] = {}
        missing: list[Path] = []
        for path in paths:
            cached = self.cache.get(path, "captured")
            if cached is None:
                missing.append(path)
            else:
                times[path] = float(cached)
        if not missing:
            return times

        def timestamp(path: Path) -> float:
            quick = metadata.capture_only(path)
            return quick if quick is not None else metadata.capture_time(path)

        # One thread on purpose. Parsing EXIF is mostly Python holding the
        # interpreter lock, so a pool made this measurably slower, not faster.
        with self.cache.batch():
            for path, value in work.mapped(timestamp, missing, progress, cancel,
                                           workers=1):
                if value is None:
                    continue
                times[path] = float(value)
                self.cache.put(path, captured=float(value))
        return times

    def rebuild_queue(self, progress=_noop, cancel=_never) -> int:
        """Re-apply filter, sidecar collapsing and sort, keeping the current item."""
        if not self.source_root:
            self.queue_paths = []
            return 0
        current = self.current_path()
        root = str(self.source_root)
        ratings = self.state.tags_for([str(p) for p in self.all_files])
        if self.review_mode:
            candidates = [Path(p) for p in self.state.review_queue(root)]
            exclude: set[str] = set()
        else:
            candidates = list(self.all_files)
            exclude = set(self.state.review_queue(root)) | self.state.done_paths()
        spec = scanner.FilterSpec(
            mode=self.settings.filter_mode,
            short_video_seconds=self.settings.short_video_seconds,
            exclude=exclude, ratings=ratings)
        reader = self.capture_time
        if self.settings.sort_mode == "date":
            # Hand the sort a plain dictionary. Asking the cache per file meant
            # a `stat` per file to build its key, twenty thousand syscalls for
            # values this pass has already loaded.
            times = self.warm_capture_times(candidates, progress, cancel)

            def reader(path: Path) -> float:
                # `or` would treat 1970-01-01 as a miss and pay a stat for it.
                found = times.get(path)
                return found if found is not None else self.capture_time(path)
        self.queue_paths = scanner.build_queue(
            candidates, spec, self.settings.sidecar, self.settings.sort_mode,
            self.settings.sort_reverse, ratings, progress, cancel,
            capture_time=reader)
        if current in self.queue_paths:
            self.index = self.queue_paths.index(current)
        else:
            self.index = min(self.index, max(0, len(self.queue_paths) - 1))
        return len(self.queue_paths)

    def set_review_mode(self, enabled: bool) -> None:
        self.review_mode = bool(enabled)
        self.index = 0
        self.rebuild_queue()

    # ---------------------------------------------------------- cursor
    def current_path(self) -> Path | None:
        if 0 <= self.index < len(self.queue_paths):
            return self.queue_paths[self.index]
        return None

    def step(self, offset: int) -> Path | None:
        if not self.queue_paths:
            return None
        self.index = (self.index + offset) % len(self.queue_paths)
        return self.current_path()

    def go_to(self, path: str | Path) -> bool:
        target = Path(path)
        if target in self.queue_paths:
            self.index = self.queue_paths.index(target)
            return True
        return False

    #: A directory whose own timestamp is this recent is treated as still being
    #: written to. FAT and exFAT round timestamps to whole seconds, so a card
    #: importer can add a file without the stamp changing; re-reading during
    #: that window costs one listing and closes the hole.
    INDEX_GRACE_SECONDS = 2.0
    #: However long the stamp says nothing changed, the folder is read afresh
    #: this often. It bounds how stale the grouping can be on a filesystem
    #: whose timestamps are too coarse to notice a change at all.
    INDEX_MAX_AGE_SECONDS = 30.0

    @staticmethod
    def _folder_stamp(folder: Path) -> tuple[int, float]:
        try:
            stat = folder.stat()
        except OSError:
            return (0, 0.0)
        return (stat.st_mtime_ns, stat.st_mtime)

    def _index_for(self, folder: Path) -> dict[str, list[Path]]:
        """Files of *folder*, bucketed by the stem that identifies one shot.

        The directory's own modification time is checked on every lookup — one
        stat, not a listing. Without that check a raw file written by a card
        importer *after* the folder was scanned would be invisible to the
        grouping, and its photograph would move without it: the exact failure
        this whole feature exists to prevent.

        A change this program made itself is already reflected in the index, so
        it does not trigger a re-read; only a stamp we cannot account for does.
        """
        key = str(folder)
        entry = self._stem_index.get(key)
        stamp = self._folder_stamp(folder)
        now = time.time()
        if entry is not None and stamp == entry.stamp:
            settled = entry.mine or (now - stamp[1]) > self.INDEX_GRACE_SECONDS
            if settled and (now - entry.listed_at) < self.INDEX_MAX_AGE_SECONDS:
                return entry.buckets
        buckets = self._bucket_folder(folder)
        if buckets is None:
            self._stem_index.pop(key, None)
            return {}
        self._stem_index[key] = _FolderIndex(buckets, stamp, now, False)
        return buckets

    @staticmethod
    def _bucket_folder(folder: Path) -> dict[str, list[Path]] | None:
        buckets: dict[str, list[Path]] = {}
        try:
            for item in folder.iterdir():
                buckets.setdefault(sidecar_mod.base_stem(item).casefold(), []).append(item)
        except OSError:
            return None
        return buckets

    def _index_entry(self, folder: Path):
        entry = self._stem_index.get(str(folder))
        return entry.buckets if entry else None

    def _refresh_index(self, folder: Path) -> None:
        """Re-list an indexed folder after our work; include outside writes."""
        key = str(folder)
        if key not in self._stem_index:
            return
        stamp = self._folder_stamp(folder)
        buckets = self._bucket_folder(folder)
        if buckets is None:
            del self._stem_index[key]
            return
        self._stem_index[key] = _FolderIndex(buckets, stamp, time.time(), True)

    def _index_forget(self, path: Path) -> None:
        index = self._index_entry(path.parent)
        if not index:
            return
        bucket = index.get(sidecar_mod.base_stem(path).casefold())
        if bucket and path in bucket:
            bucket.remove(path)

    def _index_note(self, path: Path) -> None:
        index = self._index_entry(path.parent)
        if index is None:
            return                      # that folder is not indexed yet
        bucket = index.setdefault(sidecar_mod.base_stem(path).casefold(), [])
        if path not in bucket:
            bucket.append(path)

    def listing_for(self, folder: Path) -> list[Path]:
        """Every indexed entry of *folder*, for callers that want them all."""
        return [item for bucket in self._index_for(Path(folder)).values() for item in bucket]

    def siblings_of(self, path: Path) -> list[Path]:
        """Only the entries that could belong to the same shot as *path*."""
        return list(self._index_for(path.parent).get(
            sidecar_mod.base_stem(path).casefold(), []))

    def group_for(self, path: str | Path) -> sidecar_mod.SidecarGroup:
        target = Path(path)
        return self.planner.group_for(target, self.siblings_of(target))

    def _operation_group(self, group: sidecar_mod.SidecarGroup) -> sidecar_mod.SidecarGroup:
        rules = self.settings.sidecar
        if rules.enabled and rules.prompt == sidecar_mod.PROMPT_NEVER:
            return sidecar_mod.SidecarGroup(group.master,
                [member for member in group.members if member.path == group.master])
        return group

    # ------------------------------------------------------ operations
    @contextmanager
    def exclusive(self):
        """One mutation at a time, from whichever thread asks.

        Sorting runs on the queue thread; undo, redo, rename and tagging used
        to run straight off the interface thread. Nothing kept the two apart,
        so a read of the history stack could land between another thread's file
        moves and its state commit. Planning has to sit inside the same guard
        as the run: a plan records what it expects each file to contain, and a
        plan built against a filesystem that a second mutation is changing is
        already wrong by the time it executes.

        Re-entry from the same thread is refused rather than allowed through.
        ``processEvents`` inside a progress callback can dispatch a queued key
        press, and a plain reentrant lock would let that second operation start
        in the middle of the first.
        """
        current = threading.get_ident()
        if self._mutating == current:
            raise TransactionError("error.busy")
        with self._mutex:
            self._mutating = current
            try:
                yield
            finally:
                self._mutating = None

    def _commit(self, outcome: ops.Outcome, progress=_noop, cancel=_never) -> ops.Outcome:
        """Run a planned outcome: files first, then state, in one transaction."""
        if outcome.cancelled or outcome.skipped:
            return outcome
        if outcome.plan is not None:
            had_pending = self.store.has_pending()
            try:
                self.store.run(outcome.plan, progress, cancel, save_state=self.state.apply)
            except Exception:
                if had_pending or not self.store.has_pending():
                    self.planner.sequence.rollback()
                    self.store.discard_snapshots(outcome.plan.snapshots)
                raise
            self.planner.sequence.commit()
        elif outcome.delta:
            self.state.apply(outcome.delta)
        if outcome.record is not None:
            self._note(outcome.record)
        self._discard_orphans()
        self._reindex(outcome)
        self._maybe_reclaim()
        return outcome

    def _discard_orphans(self) -> None:
        """Delete restore copies that the last state commit left unreferenced."""
        refs = self.state.take_orphans()
        if refs:
            freed = self.store.discard_snapshots(refs)
            log.info("released %d bytes of restore copies from %d dropped records",
                     freed, len(refs))

    def _reindex(self, outcome: ops.Outcome) -> None:
        """Keep the stem index current instead of throwing it away.

        Dropping it meant the next preview re-listed the whole source folder.
        """
        record = outcome.record
        if record is None:
            return
        payload = record.payload or {}
        touched: dict[str, Path] = {}
        for moved in payload.get("paths") or []:
            candidate = Path(moved)
            touched.setdefault(str(candidate.parent), candidate.parent)
            if not candidate.exists():
                self._index_forget(candidate)
        for created in payload.get("targets") or []:
            candidate = Path(created)
            touched.setdefault(str(candidate.parent), candidate.parent)
            if candidate.exists():
                self._index_note(candidate)
        for folder in touched.values():
            self._refresh_index(folder)

    def _note(self, record: Record) -> None:
        self.stats.ids.add(record.id)
        self.stats.handled[record.action] = self.stats.handled.get(record.action, 0) + 1
        self.stats.bytes_moved += record.bytes
        if record.destination:
            folder = str(Path(record.destination).parent)
            self.stats.per_folder[folder] = self.stats.per_folder.get(folder, 0) + 1

    def preview_target(self, binding: config.Binding, path: str | Path | None = None,
                       group: sidecar_mod.SidecarGroup | None = None) -> Path | None:
        target = Path(path) if path else self.current_path()
        if target is None:
            return None
        return self.planner.preview_target(group or self.group_for(target), binding,
                                           self.source_root)

    def classify(self, binding: config.Binding, path: str | Path | None = None,
                 resolver: ops.Resolver = ops.always_sequence,
                 progress=_noop, cancel=_never,
                 group: sidecar_mod.SidecarGroup | None = None,
                 allow_system: bool = True) -> ops.Outcome:
        target = Path(path) if path else self.current_path()
        if target is None:
            return ops.Outcome(skipped=True)
        if binding.action == "reveal":
            platform_.reveal(target)
            return ops.Outcome(skipped=True, message_key="status.revealed")
        with self.exclusive():
            group = self._operation_group(group if group is not None else self.group_for(target))
            action = binding.action
            if action in ("move", "copy", "favorite"):
                outcome = self.planner.plan_folder_action(action, group, binding,
                                                          self.source_root, resolver)
            elif action == "skip":
                outcome = self.planner.plan_skip(group, self.source_root or target.parent)
            elif action == "trash":
                return self._commit_trash(group, progress, cancel, allow_system)
            else:
                return ops.Outcome(skipped=True)
            return self._commit(outcome, progress, cancel)

    def skip(self, path: str | Path | None = None) -> ops.Outcome:
        target = Path(path) if path else self.current_path()
        if target is None or not self.source_root:
            return ops.Outcome(skipped=True)
        if self.review_mode:
            return ops.Outcome(skipped=True)
        with self.exclusive():
            return self._commit(
                self.planner.plan_skip(self.group_for(target), self.source_root))

    def unskip(self, path: str | Path) -> ops.Outcome:
        if not self.source_root:
            return ops.Outcome(skipped=True)
        with self.exclusive():
            return self._commit(self.planner.plan_unskip(Path(path), self.source_root))

    def rename(self, new_name: str, path: str | Path | None = None,
               resolver: ops.Resolver = ops.always_sequence,
               progress=_noop, cancel=_never) -> ops.Outcome:
        target = Path(path) if path else self.current_path()
        if target is None:
            return ops.Outcome(skipped=True)
        with self.exclusive():
            outcome = self.planner.plan_rename(self.group_for(target), new_name,
                                               self.source_root, resolver)
            result = self._commit(outcome, progress, cancel)
            if result.record and result.record.destination:
                self.state.rename_tag(str(target), result.record.destination)
            return result

    def trash(self, path: str | Path | None = None, progress=_noop, cancel=_never,
              allow_system: bool = True) -> ops.Outcome:
        target = Path(path) if path else self.current_path()
        if target is None:
            return ops.Outcome(skipped=True)
        with self.exclusive():
            group = self._operation_group(self.group_for(target))
            return self._commit_trash(group, progress, cancel, allow_system)

    def _commit_trash(self, group: sidecar_mod.SidecarGroup,
                      progress, cancel, allow_system: bool = True) -> ops.Outcome:
        """Recycle, honouring the single-copy setting.

        The previous version always kept an application snapshot *and* pushed a
        second full copy into the Windows bin, so a recycled 5 GB video briefly
        cost 10 GB and what the bin restored was a renamed duplicate. Sending
        the file itself is now the default when the user asks for the system
        bin at all.
        """
        system_requested = self.settings.recycle_mode == config.RECYCLE_SYSTEM
        if (system_requested and allow_system and platform_.trash_available()
                and all(platform_.can_recycle(m.path, group.total_bytes())
                        for m in group.members)):
            paths = [m.path for m in group.members]
            if not paths:
                return ops.Outcome(skipped=True)
            for path in paths:
                if not path.is_file() or path.is_symlink():
                    raise OSError(f"Cannot recycle non-regular file: {path}")
            sent: list[Path] = []
            def commit_sent() -> ops.Outcome:
                sent_group = sidecar_mod.SidecarGroup(group.master,
                    [m for m in group.members if m.path in sent])
                record = Record(id=uuid.uuid4().hex, action="trash", original=str(group.master),
                                root=str(self.source_root or group.master.parent),
                                undoable=False, bytes=sent_group.total_bytes(),
                                payload={"forward": [], "inverse": [],
                                         "paths": [str(p) for p in sent],
                                         "system_recycle": True})
                delta = self.planner._delta_for(record, sent_group, "trash")
                if group.master not in sent:
                    delta["reviews_remove"] = []
                self.state.apply(delta)
                self._note(record)
                self._discard_orphans()
                for path in sent:
                    self._index_forget(path)
                return ops.Outcome(record=record, delta=delta)
            try:
                for path in paths:
                    platform_.move_to_trash(path)
                    if path.exists():
                        raise OSError(f"Recycle did not remove source: {path}")
                    sent.append(path)
            except OSError as error:
                if not path.exists() and path not in sent:
                    sent.append(path)
                if sent:
                    commit_sent()
                remaining = ", ".join(str(p) for p in paths if p not in sent)
                raise OSError(tr("status.partial_recycle", paths=remaining,
                                 error=str(error))) from error
            return commit_sent()
        outcome = self.planner.plan_trash(group, self.source_root)
        result = self._commit(outcome, progress, cancel)
        if system_requested and result.record:
            result.message_key = "status.soft_recycle_fallback"
        return result

    def tag(self, paths: Sequence[Path], rating: int | None = None,
            label: str | None = None) -> ops.Outcome:
        with self.exclusive():
            return self._commit(self.planner.plan_tag(list(paths), rating, label))

    # ---------------------------------------------------- undo and redo
    def can_undo(self) -> bool:
        return self.state.top(STACK_HISTORY) is not None

    def can_redo(self) -> bool:
        top = self.state.top(STACK_REDO, undoable_only=False)
        return top is not None and top.undoable

    def undo(self, progress=_noop, cancel=_never) -> ops.Outcome:
        with self.exclusive():
            record = self.state.top(STACK_HISTORY)
            if record is None:
                return ops.Outcome(skipped=True)
            outcome = self.planner.plan_undo(record)
            result = self._commit_transition(outcome, progress, cancel)
            self.stats.undos += 1
            self.stats.handled[record.action] = max(
                0, self.stats.handled.get(record.action, 0) - 1)
            if record.destination:
                folder = str(Path(record.destination).parent)
                self.stats.per_folder[folder] = max(
                    0, self.stats.per_folder.get(folder, 0) - 1)
            return result

    def redo(self, progress=_noop, cancel=_never) -> ops.Outcome:
        with self.exclusive():
            record = self.state.top(STACK_REDO, undoable_only=False)
            if record is None:
                return ops.Outcome(skipped=True)
            if not record.undoable:
                from .state import empty_delta
                delta = empty_delta()
                delta["records_clear_stack"].append(STACK_REDO)
                self.state.apply(delta)
                self._discard_orphans()
                return ops.Outcome(skipped=True)
            outcome = self.planner.plan_redo(record)
            result = self._commit_transition(outcome, progress, cancel)
            self.stats.redos += 1
            self._note(record)
            return result

    def _commit_transition(self, outcome: ops.Outcome, progress, cancel) -> ops.Outcome:
        if outcome.plan is not None:
            self.store.run(outcome.plan, progress, cancel, save_state=self.state.apply)
        elif outcome.delta:
            self.state.apply(outcome.delta)
        self._stem_index.clear()
        self._discard_orphans()
        return outcome

    # -- absorbing a change without re-reading the folder ---------------
    def touched_paths(self, record: Record | None) -> list[Path]:
        """Every file a record's steps could have created or removed."""
        if record is None:
            return []
        payload = record.payload or {}
        seen: dict[str, Path] = {}
        for key in ("paths", "targets"):
            for item in payload.get(key) or []:
                seen.setdefault(str(item), Path(item))
        for key in ("forward", "inverse"):
            for step in payload.get(key) or []:
                for side in ("src", "dst"):
                    value = step.get(side)
                    if value:
                        seen.setdefault(str(value), Path(value))
        return list(seen.values())

    def absorb(self, record: Record | None) -> dict | None:
        """Bring the file lists in line with what *record* changed.

        Undo and redo used to call :meth:`rescan`, which walks the whole source
        tree, throws away every parsed header and sorts the library again --
        half a second of work on a folder of twenty thousand files, for a change
        that touched one shot. Here the record already names the files involved,
        so the lists are patched in place.

        Membership is decided by the queue and by the filter, never by
        ``all_files``: that list keeps an entry for a file the queue has already
        let go, so trusting it meant an undone move put the file back on disk
        and nowhere on screen. Undoing a skip is the same shape -- the file
        never moved, only its exclusion did -- so every touched file that still
        exists is re-tested against the filter rather than assumed to be fine.

        Returns None when the lists cannot be patched confidently and the
        caller should rebuild the queue instead; otherwise the queue rows that
        were added and removed, so the views can be patched the same way.
        """
        touched = self.touched_paths(record)
        if not touched or self.source_root is None:
            return None
        if self.review_mode:
            # That queue is a database query, not a view of these lists.
            return None
        key = self._sort_key(touched)
        if key is None:                     # random order has no insert point
            return None

        added: list[tuple[int, Path]] = []
        removed: list[Path] = []
        metadata.forget(touched)
        root = self.source_root
        excluded = scanner.pruned_targets(root, self.settings.target_folders())
        spec = self._filter_spec(touched)
        queued = set(self.queue_paths)
        known = set(self.all_files)

        for path in touched:
            if path.exists():
                if not self._belongs(path, root, excluded):
                    continue            # a copy that landed in a target folder
                if path not in known:
                    self.all_files.append(path)
                    known.add(path)
                    self._index_note(path)
                # Whether it should be listed is re-decided every time, in both
                # directions. Copying or marking a file leaves it exactly where
                # it was and only changes the handled list, so a check that
                # looked at the file rather than the filter left it on screen --
                # and holding the key down copied it again and again.
                wanted = bool(scanner.apply_filter([path], spec))
                if wanted and path not in queued:
                    where = self._insert_into_queue(path, key)
                    if where >= 0:
                        added.append((where, path))
                        queued.add(path)
                elif not wanted and path in queued:
                    if self._drop_from_queue(path):
                        removed.append(path)
                    queued.discard(path)
                continue
            if path in known:
                self.all_files.remove(path)
                known.discard(path)
            if self._drop_from_queue(path):
                removed.append(path)
                queued.discard(path)
            self._index_forget(path)

        self.index = clamp_index(self.index, len(self.queue_paths))
        return {"added": added, "removed": removed}

    def exclude_targets(self) -> dict | None:
        """Drop files that now sit inside a destination folder.

        A recursive scan skips the folders the user files into, so pointing a
        key at a folder underneath the source root has to take that subtree out
        of the queue. Doing it by re-walking the whole source tree cost as much
        as reopening the folder; the files are already in a list here, so this
        filters that list instead.

        Returns the same shape as :meth:`absorb`, or None when nothing was
        affected -- which is the usual case, because a destination folder is
        normally somewhere else entirely.
        """
        if self.source_root is None or not self.settings.recursive:
            return None
        blocked = scanner.pruned_targets(self.source_root, self.settings.target_folders())
        if not blocked:
            return None

        # One `resolve` per folder, not per file: it is a syscall, and a library
        # of twenty thousand photographs lives in a handful of directories.
        verdicts: dict[Path, bool] = {}

        def folder_is_blocked(folder: Path) -> bool:
            known = verdicts.get(folder)
            if known is not None:
                return known
            try:
                resolved = folder.resolve()
                answer = any(resolved == item or resolved.is_relative_to(item)
                             for item in blocked)
            except (OSError, RuntimeError):
                answer = False
            verdicts[folder] = answer
            return answer

        doomed = {path for path in self.all_files if folder_is_blocked(path.parent)}
        if not doomed:
            return None
        # Rebuild both lists in one pass. Removing several hundred entries from a
        # list of twenty thousand one `remove` at a time is quadratic.
        current = self.current_path()
        self.all_files = [path for path in self.all_files if path not in doomed]
        removed = [path for path in self.queue_paths if path in doomed]
        if removed:
            self.queue_paths = [path for path in self.queue_paths if path not in doomed]
        for path in doomed:
            self._index_forget(path)
        if current is not None and current not in doomed:
            try:
                self.index = self.queue_paths.index(current)
            except ValueError:
                pass
        self.index = clamp_index(self.index, len(self.queue_paths))
        log.info("excluded %d files now inside a destination folder", len(doomed))
        return {"added": [], "removed": removed}

    def drop_subfolders(self) -> dict | None:
        """Keep only files sitting directly in the source folder.

        Turning off "include subfolders" can only ever shorten the list, and the
        list is already here, so there is nothing to re-read from the disk.
        """
        if self.source_root is None:
            return None
        root = self.source_root
        doomed = {path for path in self.all_files if path.parent != root}
        if not doomed:
            return {"added": [], "removed": []}
        current = self.current_path()
        self.all_files = [path for path in self.all_files if path not in doomed]
        removed = [path for path in self.queue_paths if path in doomed]
        if removed:
            self.queue_paths = [path for path in self.queue_paths if path not in doomed]
        for path in doomed:
            self._index_forget(path)
        if current is not None and current not in doomed:
            try:
                self.index = self.queue_paths.index(current)
            except ValueError:
                pass
        self.index = clamp_index(self.index, len(self.queue_paths))
        return {"added": [], "removed": removed}

    def _sort_key(self, touched: Sequence[Path]):
        """The key the queue is ordered by, valid for the whole queue.

        Sorting by rating scores a file from the tag table, so a key built from
        only the touched files' ratings would read every existing row as
        unrated and bisect into a list that is not sorted under it.
        """
        mode = self.settings.sort_mode
        wanted = list(touched)
        if mode == "rating":
            wanted += list(self.queue_paths)
        return scanner.sort_key(mode, self.state.tags_for([str(p) for p in wanted]),
                                self.capture_time)

    def _belongs(self, path: Path, root: Path, excluded: Sequence[Path]) -> bool:
        """Would `scan` return *path*? *excluded* is resolved and pruned."""
        try:
            if not path.is_file() or path.is_symlink():
                return False
            if path.suffix.lower() not in mediatypes.MEDIA_EXTENSIONS:
                return False
            parent = path.parent.resolve()
            if parent != path.parent:
                return False
            if parent != root and not (self.settings.recursive
                                       and parent.is_relative_to(root)):
                return False
            # The recycle folder and anything else `scan` steps over.
            if any(part.startswith(".qingjian") for part in parent.relative_to(root).parts):
                return False
            if any(parent == folder or parent.is_relative_to(folder) for folder in excluded):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _filter_spec(self, paths: Sequence[Path]) -> scanner.FilterSpec:
        """The current filter, with only the ratings *paths* needs.

        Asking for every rating in the library turned a one-file change into a
        query over the whole folder.
        """
        root = str(self.source_root or "")
        exclude = set(self.state.review_queue(root)) | self.state.done_paths()
        return scanner.FilterSpec(
            mode=self.settings.filter_mode,
            short_video_seconds=self.settings.short_video_seconds,
            exclude=exclude,
            ratings=self.state.tags_for([str(p) for p in paths]))

    def _drop_from_queue(self, path: Path) -> bool:
        try:
            position = self.queue_paths.index(path)
        except ValueError:
            return False
        self.queue_paths.pop(position)
        if position < self.index:
            self.index -= 1
        if self.index >= len(self.queue_paths):
            self.index = max(0, len(self.queue_paths) - 1)
        return True

    def _insert_into_queue(self, path: Path, key) -> int:
        """Place *path* where a full sort would have put it; -1 if not added."""
        if path in self.queue_paths:
            return -1
        if self.settings.sidecar.enabled:
            # One row per shot: a restored RAW joins the JPEG already listed.
            stem = sidecar_mod.base_stem(path).casefold()
            folder = path.parent
            for existing in self.queue_paths:
                if existing.parent == folder and \
                        sidecar_mod.base_stem(existing).casefold() == stem:
                    return -1
        try:
            value = key(path)
        except OSError:
            self.queue_paths.append(path)
            return len(self.queue_paths) - 1
        low, high = 0, len(self.queue_paths)
        while low < high:
            middle = (low + high) // 2
            try:
                other = key(self.queue_paths[middle])
            except OSError:
                other = value
            ahead = other < value if not self.settings.sort_reverse else value < other
            if ahead:
                low = middle + 1
            else:
                high = middle
        self.queue_paths.insert(low, path)
        if low <= self.index:
            self.index += 1
        return low

    # ------------------------------------------------------ background
    def enqueue(self, label: str, work: Callable, context: dict | None = None) -> Job:
        job = Job(run=work, label=label, context=dict(context or {}))
        return self.queue.submit(job)

    def _on_queue_event(self, event: str, job: Job) -> None:
        for listener in list(self.queue_listeners):
            try:
                listener(event, job)
            except Exception:  # pragma: no cover - a listener must not break the worker
                log.exception("queue listener failed")

    # ------------------------------------------------------ duplicates
    def find_duplicates(self, mode: str = dedupe.MODE_EXACT, progress=_noop,
                        cancel=_never) -> list[dedupe.Group]:
        paths = list(self.all_files)
        root = str(self.source_root or "")
        ignored = self.state.ignored_keys(root)
        if mode == dedupe.MODE_SIMILAR:
            return dedupe.find_similar(paths, self.settings.similar_threshold, self.cache,
                                       ignored, progress, cancel)
        if mode == dedupe.MODE_BURST:
            return dedupe.find_bursts(paths, self.settings.burst_gap_seconds,
                                      self.settings.burst_minimum, cache=self.cache,
                                      ignored=ignored, progress=progress, cancel=cancel)
        return dedupe.find_exact(paths, self.cache, ignored, progress, cancel)

    def ignore_duplicates(self, candidates: Sequence[dedupe.Candidate]) -> ops.Outcome:
        root = self.source_root or Path(".")
        keys = []
        for candidate in candidates:
            digest = candidate.digest or self.cache.get(candidate.path, "sha256") or ""
            keys.append(dedupe.identity_key(candidate.path, str(digest)))
        with self.exclusive():
            return self._commit(self.planner.plan_ignore_duplicates(root, keys))

    def restore_ignored(self) -> ops.Outcome:
        root = self.source_root or Path(".")
        with self.exclusive():
            return self._commit(self.planner.plan_restore_ignored(root))

    def hidden_handled(self) -> int:
        """Files in this folder that copying or favouriting took out of the queue."""
        if self.source_root is None or self.review_mode:
            return 0
        return self.state.done_under(str(self.source_root), self.settings.recursive)

    def reveal_handled(self, progress=_noop, cancel=_never) -> int:
        """Put those files back in the queue. They never moved; they were only marked."""
        if self.source_root is None:
            return 0
        with self.exclusive():
            cleared = self.state.clear_done_under(str(self.source_root), self.settings.recursive)
        self.rebuild_queue(progress, cancel)
        return cleared

    def send_to_review(self, paths: Sequence[Path]) -> None:
        if not self.source_root:
            return
        from .state import empty_delta
        delta = empty_delta()
        for path in paths:
            delta["reviews_add"].append([str(self.source_root), str(path)])
        self.state.apply(delta)

    # --------------------------------------------------------- backups
    def backup_usage(self) -> int:
        return self.store.usage()

    def reclaim(self, force: bool = False) -> tuple[int, int]:
        """Retire the oldest restore copies until the quota is satisfied."""
        policy = self.settings.quota
        if not force and not policy.automatic:
            return (0, 0)
        records = self.state.oldest_undoable(limit=5000)
        rows = [{"time_epoch": r.time_epoch, "snapshots": r.snapshots,
                 "snapshot_bytes": r.payload.get("snapshot_bytes", None)} for r in records]
        for row in rows:
            if row["snapshot_bytes"] is None:
                del row["snapshot_bytes"]
        drop = reclaim_candidates(rows, policy, keep_newest=not force)
        if not drop:
            return (0, 0)
        refs: list[str] = []
        ids: list[str] = []
        for index in drop:
            refs.extend(records[index].snapshots)
            ids.append(records[index].id)
        freed = self.store.discard_snapshots(refs)
        self.state.retire(ids)
        log.info("reclaimed %d bytes from %d records", freed, len(ids))
        return (freed, len(ids))

    def _maybe_reclaim(self) -> None:
        policy = self.settings.quota
        if not policy.automatic:
            return
        count, oldest = self.state.retention_gate()
        over_count = policy.max_operations > 0 and count > policy.max_operations
        over_age = (policy.max_days > 0 and oldest is not None
                    and oldest < time.time() - policy.max_days * 86400)
        over_bytes = policy.max_bytes > 0 and self.store.usage() > policy.max_bytes
        if not (over_count or over_age or over_bytes):
            return
        try:
            self.reclaim()
        except OSError as error:  # pragma: no cover - defensive
            log.warning("reclaim failed: %s", error)

    def clear_backups(self) -> int:
        refs = self.state.clear_records()
        freed = self.store.discard_snapshots(refs)
        self.stats.ids.clear()
        return freed

    # ----------------------------------------------------------- info
    def info_rows(self, path: str | Path) -> list[tuple[str, str]]:
        target = Path(path)
        info = metadata.read(target)
        rating, label = self.state.tag(str(target))
        group = self.group_for(target)
        rows: list[tuple[str, str]] = [
            (tr("info.filename"), target.name),
            (tr("info.path"), str(target)),
            (tr("info.size"), human_size(info.size)),
            (tr("info.modified"), datetime.fromtimestamp(info.mtime).strftime("%Y-%m-%d %H:%M:%S")),
            (tr("info.captured"),
             (info.captured.strftime("%Y-%m-%d %H:%M:%S") if info.captured else "")
             + (" *" if info.captured_is_fallback else "")),
        ]
        if info.width and info.height:
            rows.append((tr("info.dimensions"), f"{info.width} × {info.height}"))
        for label_key, value in (
            ("info.format", info.fmt), ("info.codec", info.codec),
            ("info.framerate", info.framerate),
            ("info.duration", metadata.format_duration(info.duration)),
            ("info.camera", info.camera), ("info.lens", info.lens),
            ("info.iso", info.iso), ("info.aperture", info.aperture),
            ("info.shutter", info.shutter), ("info.focal", info.focal),
        ):
            if value:
                rows.append((tr(label_key), str(value)))
        if rating or label:
            rows.append((tr("info.rating"), "★" * rating if rating else "—"))
            rows.append((tr("info.label"), label or "—"))
        if group.sidecars:
            rows.append((tr("info.sidecars"),
                         ", ".join(m.path.name for m in group.sidecars)))
        if info.error:
            rows.append(("!", info.error))
        return rows

    def statistics(self) -> list[tuple[str, str]]:
        counts = self.state.counts()
        rows = [
            (tr("stats.net_handled"), str(self.stats.total())),
            (tr("stats.remaining"), str(len(self.queue_paths))),
            (tr("stats.in_review"), str(counts["reviews"])),
            (tr("stats.bytes_moved"), human_size(self.stats.bytes_moved)),
        ]
        for action, count in sorted(self.stats.handled.items()):
            if count:
                rows.append((tr(config.ACTIONS.get(action, ("", "", False))[0] or action), str(count)))
        rows.append((tr("stats.undo_count"), str(self.stats.undos)))
        rows.append((tr("stats.redo_count"), str(self.stats.redos)))
        for folder, count in sorted(self.stats.per_folder.items()):
            if count:
                rows.append((tr("stats.target_prefix", folder=folder), str(count)))
        return rows

    def folder_counts(self) -> dict[str, int]:
        return dict(self.stats.per_folder)

    def export_csv(self, destination: str | Path) -> Path:
        out = Path(destination)
        records = self.state.records(limit=100000)
        with out.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([tr("history.time"), tr("history.action"), tr("history.original"),
                             tr("history.destination"), tr("history.conflict"),
                             tr("history.state")])
            for record in records:
                writer.writerow([
                    _csv_safe(datetime.fromtimestamp(record.time_epoch)
                              .strftime("%Y-%m-%d %H:%M:%S")),
                    _csv_safe(tr(config.ACTIONS.get(record.action, (record.action,))[0])),
                    _csv_safe(record.original), _csv_safe(record.destination),
                    _csv_safe(tr(f"conflict.mode.{record.conflict}") if record.conflict
                              else tr("conflict.mode.none")),
                    _csv_safe(tr("history.state.done") if record.stack == STACK_HISTORY
                              else tr("history.state.undone")),
                ])
        return out

    def diagnostic_bundle(self, destination: str | Path) -> Path:
        return logsetup.diagnostic_bundle(destination, self.data_dir)


def _csv_safe(value) -> str:
    """Stop a spreadsheet treating a filename as a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
