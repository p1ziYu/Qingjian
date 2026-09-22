"""Turning "the user pressed 1" into a transaction.

This is where sidecar groups, path templates, name clashes and the undo record
are decided. It produces plans; it never touches a file itself, which is what
lets every branch here be tested without a filesystem race.
"""
from __future__ import annotations

import uuid
import os
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Callable, Sequence

from . import sidecar as sidecar_mod
from . import metadata, naming, template
from .config import Binding, Settings
from .logsetup import get_logger
from .naming import NameError_
from .safestore import (VERIFY_FAST, Plan, SafeStore, identity, step_copy,
                        step_move, step_unlink)
from .state import STACK_REDO, Record, StateStore, empty_delta

log = get_logger("ops")

CONFLICT_REPLACE = "replace"
CONFLICT_SEQUENCE = "sequence"
CONFLICT_SKIP = "skip"
CONFLICT_CANCEL = "cancel"

#: (source, target) -> one of the CONFLICT_* constants.
Resolver = Callable[[Path, Path], str]


def always_sequence(source: Path, target: Path) -> str:
    """Default clash policy: never destroy an existing file without being told."""
    return CONFLICT_SEQUENCE


@dataclass
class Outcome:
    """What a planner call decided. ``plan`` is None when nothing must be written."""

    plan: Plan | None = None
    record: Record | None = None
    delta: dict = field(default_factory=empty_delta)
    cancelled: bool = False
    skipped: bool = False
    message_key: str = ""
    fields: dict = field(default_factory=dict)

    def is_noop(self) -> bool:
        return self.plan is None and not any(self.delta.values())


def sidecar_target_name(new_master_name: str, base_stem: str, member_name: str) -> str:
    """Rename a companion in step with its master.

    ``IMG_4821.JPG`` -> ``2026-08-14_0001_IMG_4821.JPG`` turns
    ``IMG_4821.JPG.xmp`` into ``2026-08-14_0001_IMG_4821.JPG.xmp`` and
    ``IMG_4821.CR2`` into ``2026-08-14_0001_IMG_4821.CR2``.
    """
    new_stem = sidecar_mod.base_stem(new_master_name)
    if member_name[:len(base_stem)].casefold() == base_stem.casefold():
        tail = member_name[len(base_stem):]
    else:
        tail = Path(member_name).suffix
    return new_stem + tail


class SequenceCounter:
    """Per-target running number for the ``{seq}`` token, kept across sessions."""

    def __init__(self, state: StateStore) -> None:
        self.state = state
        self._pending: dict[str, int] = {}

    @staticmethod
    def _key(binding: Binding) -> str:
        return "seq:" + str(Path(binding.folder or "").as_posix()).casefold()

    def peek(self, binding: Binding) -> int:
        key = self._key(binding)
        if key in self._pending:
            return max(self._pending[key], binding.sequence_start, 1)
        stored = self.state.meta(key, "")
        try:
            return max(int(stored), binding.sequence_start, 1) if stored else max(1, binding.sequence_start)
        except ValueError:
            return max(1, binding.sequence_start)

    def take(self, binding: Binding, count: int = 1) -> int:
        value = self.peek(binding)
        self._pending[self._key(binding)] = value + count
        return value

    def commit(self) -> None:
        for key, value in self._pending.items():
            self.state.set_meta(key, str(value))
        self._pending.clear()

    def rollback(self) -> None:
        self._pending.clear()


class Planner:
    def __init__(self, store: SafeStore, state: StateStore, settings: Settings) -> None:
        self.store = store
        self.state = state
        self.settings = settings
        self.sequence = SequenceCounter(state)

    # -- helpers -------------------------------------------------------
    @property
    def verify(self) -> str:
        return self.settings.verification

    def group_for(self, path: str | Path, listing: list[Path] | None = None) -> sidecar_mod.SidecarGroup:
        return sidecar_mod.find_group(path, self.settings.sidecar, listing)

    @staticmethod
    def _absolute_folder(folder: str) -> bool:
        path = Path(folder).expanduser()
        return path.is_absolute() and (os.name != "nt" or bool(PureWindowsPath(str(path)).anchor.rstrip("\\")))

    @staticmethod
    def _same_path(source: Path, target: Path, target_exists: bool | None = None) -> bool:
        if target_exists is None:
            target_exists = target.exists()
        if target_exists:
            try:
                return os.path.samefile(source, target)
            except OSError:
                pass
        return os.path.normcase(str(source.resolve())) == os.path.normcase(str(target.resolve()))

    @staticmethod
    def _group_pairs(group: sidecar_mod.SidecarGroup, folder: Path,
                     master_name: str) -> list[tuple[Path, Path]]:
        stem = sidecar_mod.base_stem(group.master)
        return [(member.path, folder / (master_name if member.path == group.master else
                 sidecar_target_name(master_name, stem, member.path.name)))
                for member in group.members]

    def _resolve_pairs(self, group: sidecar_mod.SidecarGroup, folder: Path,
                       name: str, resolver: Resolver) -> tuple[list[tuple[Path, Path]], str, list[bool]] | Outcome:
        pairs = self._group_pairs(group, folder, name)
        sources = [source for source, _ in pairs]
        exists = [target.exists() for _, target in pairs]
        keys = [str(target).casefold() for _, target in pairs]
        if len(keys) != len(set(keys)):
            raise NameError_("error.group_name_collision", path=str(folder / name))
        for (source, target), target_exists in zip(pairs, exists, strict=True):
            if any(self._same_path(other, target, target_exists) for other in sources):
                if source == group.master and self._same_path(source, target, target_exists):
                    return Outcome(skipped=True, message_key="error.target_is_source")
                raise NameError_("error.group_name_collision", path=str(target))
        conflicts = [pair for pair, target_exists in zip(pairs, exists, strict=True)
                     if target_exists]
        if not conflicts:
            return pairs, "", exists
        decision = resolver(*conflicts[0])
        if decision == CONFLICT_CANCEL:
            return Outcome(cancelled=True)
        if decision == CONFLICT_SKIP:
            return Outcome(skipped=True)
        if decision == CONFLICT_REPLACE:
            return pairs, CONFLICT_REPLACE, exists
        stem, suffix = naming.split_name(name)
        base_stem = naming.strip_sequence(stem)
        for number in range(2, naming.MAX_SEQUENCE):
            candidate = f"{base_stem} ({number}){suffix}"
            proposed = self._group_pairs(group, folder, candidate)
            destinations = [target for _, target in proposed]
            if len({str(path).casefold() for path in destinations}) != len(destinations):
                raise NameError_("error.group_name_collision", path=str(folder / candidate))
            proposed_exists = [path.exists() for path in destinations]
            if all(not target_exists and not any(self._same_path(source, path, False)
                                                  for source in sources)
                   for path, target_exists in zip(destinations, proposed_exists, strict=True)):
                return proposed, CONFLICT_SEQUENCE, proposed_exists
        raise NameError_("error.no_unique_name")

    def _build_steps(self, pairs: list[tuple[Path, Path]], action: str,
                     exists: list[bool]) -> tuple[list[dict], list[str]]:
        forward: list[dict] = []
        snapshots: list[str] = []
        try:
            for (source, destination), target_exists in zip(pairs, exists, strict=True):
                source_id = identity(source, VERIFY_FAST)
                if source_id is None:
                    continue
                naming.check_path_length(destination)
                if target_exists:
                    existing = identity(destination, VERIFY_FAST)
                    snapshot = self.store.snapshot(destination)
                    if snapshot:
                        snapshots.append(snapshot["file"])
                    unlink = step_unlink(destination, existing)
                    unlink["snapshot"] = snapshot
                    forward.append(unlink)
                forward.append(step_move(source, destination, source_id) if action in ("move", "rename")
                               else step_copy(source, destination, source_id))
        except Exception:
            self.sequence.rollback()
            self.store.discard_snapshots(snapshots)
            raise
        return forward, snapshots

    def context_for(self, path: Path, binding: Binding, source_root: Path | None,
                    sequence: int) -> template.TemplateContext:
        info = metadata.read(path)
        rating, label = self.state.tag(str(path))
        return template.TemplateContext(
            source=path, when=info.when(), when_is_fallback=info.captured_is_fallback,
            camera=info.camera, lens=info.lens, iso=info.iso, aperture=info.aperture,
            shutter=info.shutter, focal=info.focal, rating=rating, label=label,
            sequence=sequence, source_root=source_root)

    def preview_target(self, group: sidecar_mod.SidecarGroup, binding: Binding,
                       source_root: Path | None = None) -> Path | None:
        """Where this shot would land, without reserving a sequence number.

        The window uses it to notice a name clash *before* queueing the work,
        so the question can be asked on the interface thread rather than from
        inside a background transaction.
        """
        if not binding.folder or not self._absolute_folder(binding.folder):
            return None
        base = Path(binding.folder).expanduser()
        ctx = self.context_for(group.master, binding, source_root,
                               self.sequence.peek(binding))
        try:
            folder = base
            for part in template.render_path(binding.path_template, ctx, strict=False):
                folder = folder / part
            return folder / template.render_name(binding.name_template, ctx, strict=False)
        except NameError_:
            return None

    # -- move / copy / favorite ---------------------------------------
    def plan_folder_action(self, action: str, group: sidecar_mod.SidecarGroup, binding: Binding,
                           source_root: Path | None = None,
                           resolver: Resolver = always_sequence) -> Outcome:
        """Move, copy or favourite one shot (master plus every companion)."""
        if not binding.folder or not self._absolute_folder(binding.folder):
            return Outcome(cancelled=True, message_key="bind.target_folder")
        base = Path(binding.folder).expanduser().resolve()
        master = group.master
        uses_sequence = "{seq" in binding.path_template or "{seq" in binding.name_template
        seq = self.sequence.take(binding) if uses_sequence else self.sequence.peek(binding)
        ctx = self.context_for(master, binding, source_root, seq)
        try:
            folder = base
            for part in template.render_path(binding.path_template, ctx, strict=False):
                folder = folder / part
            master_name = template.render_name(binding.name_template, ctx, strict=False)
        except NameError_:
            self.sequence.rollback()
            raise

        try:
            resolved = self._resolve_pairs(group, folder, master_name, resolver)
        except Exception:
            self.sequence.rollback()
            raise
        if isinstance(resolved, Outcome):
            self.sequence.rollback()
            return resolved
        pairs, conflict, exists = resolved
        target = pairs[0][1]

        forward, snapshots = self._build_steps(pairs, action, exists)

        if not forward:
            self.sequence.rollback()
            return Outcome(skipped=True)

        plan = Plan(forward=forward, verify=self.verify, label=action, snapshots=snapshots)
        plan.inverse = SafeStore.invert(forward)

        record = Record(
            id=uuid.uuid4().hex, action=action, original=str(master), destination=str(target),
            root=str(source_root or master.parent), conflict=conflict,
            bytes=sum(m.size for m in group.members),
            payload={"forward": forward, "inverse": plan.inverse, "verify": self.verify,
                     "snapshots": snapshots, "paths": [str(p) for p, _ in pairs],
                     "targets": [str(t) for _, t in pairs]})
        plan.state = self._delta_for(record, group, action)
        return Outcome(plan=plan, record=record, delta=plan.state)

    # -- rename --------------------------------------------------------
    def plan_rename(self, group: sidecar_mod.SidecarGroup, new_name: str,
                    source_root: Path | None = None,
                    resolver: Resolver = always_sequence) -> Outcome:
        master = group.master
        new_name = naming.ensure_suffix(str(new_name).strip(), master.suffix)
        naming.validate_filename(new_name)
        if str(master.with_name(new_name)).casefold() == str(master).casefold() \
                and new_name != master.name:
            raise NameError_("error.name_case_only")
        if new_name == master.name:
            return Outcome(skipped=True)

        folder = master.parent
        resolved = self._resolve_pairs(group, folder, new_name, resolver)
        if isinstance(resolved, Outcome):
            return resolved
        pairs, conflict, exists = resolved
        target = pairs[0][1]

        forward, snapshots = self._build_steps(pairs, "rename", exists)

        plan = Plan(forward=forward, verify=self.verify, label="rename", snapshots=snapshots)
        plan.inverse = SafeStore.invert(forward)
        record = Record(id=uuid.uuid4().hex, action="rename", original=str(master),
                        destination=str(target), root=str(source_root or folder),
                        conflict=conflict, bytes=sum(m.size for m in group.members),
                        payload={"forward": forward, "inverse": plan.inverse,
                                 "verify": self.verify, "snapshots": snapshots,
                                 "paths": [str(p) for p, _ in pairs],
                                 "targets": [str(t) for _, t in pairs]})
        plan.state = self._delta_for(record, group, "rename")
        return Outcome(plan=plan, record=record, delta=plan.state)

    # -- recycle -------------------------------------------------------
    def plan_trash(self, group: sidecar_mod.SidecarGroup,
                   source_root: Path | None = None) -> Outcome:
        forward: list[dict] = []
        snapshots: list[str] = []
        for member in group.members:
            current = identity(member.path, VERIFY_FAST)
            if current is None:
                continue
            slot = self.store.trash_slot(member.path)
            if slot is not None:
                # A rename into the hidden folder beside the file: instant, on
                # the same disk, undone by renaming it back.
                forward.append(step_move(member.path, slot, current))
                snapshots.append(str(slot))
                continue
            # No folder could be made there, so keep a restore copy instead.
            snapshot = self.store.snapshot(member.path)
            if snapshot:
                snapshots.append(snapshot["file"])
            step = step_unlink(member.path, current)
            step["snapshot"] = snapshot
            forward.append(step)
        if not forward:
            return Outcome(skipped=True)
        plan = Plan(forward=forward, verify=self.verify, label="trash", snapshots=snapshots)
        plan.inverse = SafeStore.invert(forward)
        record = Record(id=uuid.uuid4().hex, action="trash", original=str(group.master),
                        root=str(source_root or group.master.parent),
                        bytes=sum(m.size for m in group.members),
                        payload={"forward": forward, "inverse": plan.inverse,
                                 "verify": self.verify, "snapshots": snapshots,
                                 "paths": [str(m.path) for m in group.members]})
        plan.state = self._delta_for(record, group, "trash")
        return Outcome(plan=plan, record=record, delta=plan.state)

    # -- state-only actions --------------------------------------------
    def plan_skip(self, group: sidecar_mod.SidecarGroup, source_root: Path) -> Outcome:
        delta = empty_delta()
        delta["reviews_add"].append([str(source_root), str(group.master)])
        record = Record(id=uuid.uuid4().hex, action="skip", original=str(group.master),
                        root=str(source_root),
                        payload={"forward": [], "inverse": [], "paths": [str(group.master)]})
        delta["records_add"].append(record.to_dict())
        return Outcome(record=record, delta=delta)

    def plan_unskip(self, path: Path, source_root: Path) -> Outcome:
        delta = empty_delta()
        delta["reviews_remove"].append([str(source_root), str(path)])
        return Outcome(delta=delta)

    def plan_tag(self, paths: Sequence[Path], rating: int | None = None,
                 label: str | None = None) -> Outcome:
        delta = empty_delta()
        for path in paths:
            current_rating, current_label = self.state.tag(str(path))
            delta["tags_set"].append([
                str(path),
                current_rating if rating is None else max(0, min(5, int(rating))),
                current_label if label is None else str(label or ""),
            ])
        return Outcome(delta=delta)

    def plan_ignore_duplicates(self, root: Path, keys: Sequence[str]) -> Outcome:
        delta = empty_delta()
        for key in keys:
            delta["ignored_add"].append([str(root), str(key)])
        return Outcome(delta=delta)

    def plan_restore_ignored(self, root: Path) -> Outcome:
        delta = empty_delta()
        delta["ignored_clear_root"].append(str(root))
        return Outcome(delta=delta)

    # -- undo / redo ----------------------------------------------------
    def plan_undo(self, record: Record) -> Outcome:
        steps = list(record.payload.get("inverse") or [])
        delta = empty_delta()
        delta["records_move"].append({"id": record.id, "to": "redo"})
        self._reverse_side_effects(record, delta, undo=True)
        if not steps:
            return Outcome(record=record, delta=delta)
        plan = Plan(forward=steps, verify=record.payload.get("verify") or self.verify,
                    label="undo", state=delta)
        plan.inverse = list(record.payload.get("forward") or [])
        return Outcome(plan=plan, record=record, delta=delta)

    def plan_redo(self, record: Record) -> Outcome:
        steps = list(record.payload.get("forward") or [])
        delta = empty_delta()
        delta["records_move"].append({"id": record.id, "to": "history"})
        self._reverse_side_effects(record, delta, undo=False)
        if not steps:
            return Outcome(record=record, delta=delta)
        plan = Plan(forward=steps, verify=record.payload.get("verify") or self.verify,
                    label="redo", state=delta)
        plan.inverse = list(record.payload.get("inverse") or [])
        return Outcome(plan=plan, record=record, delta=delta)

    # -- deltas ---------------------------------------------------------
    def _delta_for(self, record: Record, group: sidecar_mod.SidecarGroup, action: str) -> dict:
        delta = empty_delta()
        # A new operation ends the redo branch, the way every editor's undo
        # stack does. Only the paths that push a real record come through here,
        # so tagging or ignoring a duplicate leaves redo untouched.
        delta["records_clear_stack"].append(STACK_REDO)
        delta["records_add"].append(record.to_dict())
        root = record.root
        master = str(group.master)
        # Any decision about a file takes it out of the review queue.
        delta["reviews_remove"].append([root, master])
        if action in ("copy", "favorite"):
            # The original stays put, so remember it was dealt with.
            delta["done_add"].append(master)
        return delta

    def _reverse_side_effects(self, record: Record, delta: dict, undo: bool) -> None:
        root, master = record.root, record.original
        if record.action == "skip":
            if undo:
                delta["reviews_remove"].append([root, master])
            else:
                delta["reviews_add"].append([root, master])
            return
        if record.from_review:
            if undo:
                delta["reviews_add"].append([root, master])
            else:
                delta["reviews_remove"].append([root, master])
        if record.action in ("copy", "favorite"):
            if undo:
                delta["done_remove"].append(master)
            else:
                delta["done_add"].append(master)
