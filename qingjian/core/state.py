"""Durable application state: history, queues, tags and ignore lists.

The previous version held all of this in one JSON document, deep-copied it on
every keystroke and rewrote the whole thing to disk. With the 300-record cap
lifted, that turned every classification into an O(history) operation.

SQLite instead: one file, real transactions, and a single-row insert stays a
single-row insert whether the library has fifty records or fifty thousand.
Mutations arrive as a *delta* so that the transaction engine can journal the
intent alongside the file changes and commit both together.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .. import STATE_SCHEMA
from .logsetup import get_logger

log = get_logger("state")

STACK_HISTORY = "history"
STACK_REDO = "redo"
STACK_STUCK = "stuck"

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          TEXT PRIMARY KEY,
    seq         INTEGER,
    stack       TEXT NOT NULL,
    action      TEXT NOT NULL,
    original    TEXT NOT NULL,
    destination TEXT NOT NULL DEFAULT '',
    root        TEXT NOT NULL DEFAULT '',
    conflict    TEXT NOT NULL DEFAULT '',
    time_epoch  REAL NOT NULL,
    from_review INTEGER NOT NULL DEFAULT 0,
    undoable    INTEGER NOT NULL DEFAULT 1,
    bytes       INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS records_stack_seq ON records(stack, seq);
CREATE TABLE IF NOT EXISTS reviews (
    root TEXT NOT NULL, path TEXT NOT NULL, added REAL NOT NULL,
    PRIMARY KEY (root, path)
);
CREATE TABLE IF NOT EXISTS done (path TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ignored (
    root TEXT NOT NULL, key TEXT NOT NULL, added REAL NOT NULL,
    PRIMARY KEY (root, key)
);
CREATE TABLE IF NOT EXISTS tags (
    path TEXT PRIMARY KEY, rating INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '', updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

LABELS = ("red", "yellow", "green", "blue", "purple")


@dataclass
class Record:
    """One undoable operation, as the history list shows it."""

    id: str
    action: str
    original: str
    destination: str = ""
    root: str = ""
    conflict: str = ""
    time_epoch: float = field(default_factory=time.time)
    from_review: bool = False
    undoable: bool = True
    bytes: int = 0
    seq: int = 0
    stack: str = STACK_HISTORY
    payload: dict = field(default_factory=dict)

    @property
    def snapshots(self) -> list[str]:
        return list(self.payload.get("snapshots") or [])

    @property
    def paths(self) -> list[str]:
        """Every file the operation touched, master and sidecars alike."""
        return list(self.payload.get("paths") or [self.original])

    def to_row(self) -> tuple:
        return (self.id, self.seq, self.stack, self.action, self.original, self.destination,
                self.root, self.conflict, self.time_epoch, int(self.from_review),
                int(self.undoable), self.bytes, json.dumps(self.payload, ensure_ascii=False))

    @classmethod
    def from_row(cls, row: Sequence) -> "Record":
        try:
            payload = json.loads(row[12]) if row[12] else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return cls(id=row[0], seq=row[1] or 0, stack=row[2], action=row[3], original=row[4],
                   destination=row[5], root=row[6], conflict=row[7], time_epoch=row[8],
                   from_review=bool(row[9]), undoable=bool(row[10]), bytes=row[11] or 0,
                   payload=payload)

    def to_dict(self) -> dict:
        return {"id": self.id, "action": self.action, "original": self.original,
                "destination": self.destination, "root": self.root, "conflict": self.conflict,
                "time_epoch": self.time_epoch, "from_review": self.from_review,
                "undoable": self.undoable, "bytes": self.bytes, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        return cls(id=str(data.get("id") or ""), action=str(data.get("action") or ""),
                   original=str(data.get("original") or ""),
                   destination=str(data.get("destination") or ""),
                   root=str(data.get("root") or ""), conflict=str(data.get("conflict") or ""),
                   time_epoch=float(data.get("time_epoch") or time.time()),
                   from_review=bool(data.get("from_review")),
                   undoable=bool(data.get("undoable", True)),
                   bytes=int(data.get("bytes") or 0),
                   payload=dict(data.get("payload") or {}))


def empty_delta() -> dict:
    return {"records_add": [], "records_move": [], "records_drop": [],
            "records_clear_stack": [],
            "reviews_add": [], "reviews_remove": [],
            "done_add": [], "done_remove": [],
            "ignored_add": [], "ignored_clear_root": [],
            "tags_set": []}


class StateStore:
    """Every mutation goes through :meth:`apply` so it is one SQLite commit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        #: Snapshot files that `apply` has unlinked from history. The caller
        #: drains them with `take_orphans` and deletes them afterwards.
        self._orphans: list[str] = []
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.commit()
        self._set_meta_if_absent("schema", str(STATE_SCHEMA))

    # -- meta ----------------------------------------------------------
    def _set_meta_if_absent(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (key, value))
            self._db.commit()

    def meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    # -- the one mutation entry point ----------------------------------
    def apply(self, delta: dict) -> None:
        """Apply a delta atomically. Safe to run twice: every step is upsert-ish."""
        if not delta:
            return
        now = time.time()
        orphan_refs: list[str] = []
        with self._lock, self._db:
            cursor = self._db.cursor()
            # Before anything is added: a fresh operation invalidates whatever
            # was waiting to be redone. Leaving those records in place left the
            # redo button live, and replaying one of them against files that had
            # since moved on is how the same picture ended up in a folder twice.
            for stack in delta.get("records_clear_stack") or []:
                rows = cursor.execute(
                    "SELECT payload FROM records WHERE stack=?", (stack,)).fetchall()
                cursor.execute("DELETE FROM records WHERE stack=?", (stack,))
                for (payload,) in rows:
                    try:
                        refs = json.loads(payload or "{}").get("snapshots") or []
                    except (ValueError, json.JSONDecodeError):
                        continue
                    # The files go only after the database says nothing points
                    # at them, so a crash in between leaks disk, never history.
                    orphan_refs.extend(str(ref) for ref in refs)
            for data in delta.get("records_add") or []:
                record = Record.from_dict(data)
                record.seq = self._next_seq(cursor)
                record.stack = data.get("stack") or STACK_HISTORY
                cursor.execute(
                    "INSERT INTO records(id,seq,stack,action,original,destination,root,conflict,"
                    "time_epoch,from_review,undoable,bytes,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO NOTHING", record.to_row())
            for move in delta.get("records_move") or []:
                cursor.execute("UPDATE records SET stack=?, seq=? WHERE id=?",
                               (move["to"], self._next_seq(cursor), move["id"]))
            drops = delta.get("records_drop") or []
            if drops:
                cursor.executemany("DELETE FROM records WHERE id=?", [(i,) for i in drops])
            adds = delta.get("reviews_add") or []
            if adds:
                cursor.executemany("INSERT OR IGNORE INTO reviews(root,path,added) VALUES(?,?,?)",
                                   [(r, p, now) for r, p in adds])
            removes = delta.get("reviews_remove") or []
            if removes:
                cursor.executemany("DELETE FROM reviews WHERE root=? AND path=?",
                                   [(r, p) for r, p in removes])
            done_add = delta.get("done_add") or []
            if done_add:
                cursor.executemany("INSERT OR IGNORE INTO done(path,at) VALUES(?,?)",
                                   [(p, now) for p in done_add])
            done_remove = delta.get("done_remove") or []
            if done_remove:
                cursor.executemany("DELETE FROM done WHERE path=?", [(p,) for p in done_remove])
            ignored = delta.get("ignored_add") or []
            if ignored:
                cursor.executemany("INSERT OR IGNORE INTO ignored(root,key,added) VALUES(?,?,?)",
                                   [(r, k, now) for r, k in ignored])
            for root in delta.get("ignored_clear_root") or []:
                cursor.execute("DELETE FROM ignored WHERE root=?", (root,))
            tags = delta.get("tags_set") or []
            if tags:
                cursor.executemany(
                    "INSERT INTO tags(path,rating,label,updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET rating=excluded.rating, "
                    "label=excluded.label, updated=excluded.updated",
                    [(p, int(r), str(lbl or ""), now) for p, r, lbl in tags])
                empty_paths = [(p,) for p, r, lbl in tags if int(r) == 0 and not lbl]
                if empty_paths:
                    cursor.executemany(
                        "DELETE FROM tags WHERE path=? AND rating=0 AND label=''", empty_paths)
        if orphan_refs:
            with self._lock:
                self._orphans.extend(orphan_refs)

    @staticmethod
    def _next_seq(cursor) -> int:
        row = cursor.execute("SELECT COALESCE(MAX(seq),0)+1 FROM records").fetchone()
        return int(row[0])

    # -- records -------------------------------------------------------
    def records(self, stack: str | None = None, limit: int = 500, offset: int = 0) -> list[Record]:
        query = ("SELECT id,seq,stack,action,original,destination,root,conflict,time_epoch,"
                 "from_review,undoable,bytes,payload FROM records")
        params: list = []
        if stack:
            query += " WHERE stack=?"
            params.append(stack)
        query += " ORDER BY seq DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [Record.from_row(row) for row in rows]

    def top(self, stack: str, undoable_only: bool = True) -> Record | None:
        query = ("SELECT id,seq,stack,action,original,destination,root,conflict,time_epoch,"
                 "from_review,undoable,bytes,payload FROM records WHERE stack=?")
        if undoable_only:
            query += " AND undoable=1"
        query += " ORDER BY seq DESC LIMIT 1"
        with self._lock:
            row = self._db.execute(query, (stack,)).fetchone()
        return Record.from_row(row) if row else None

    def record(self, record_id: str) -> Record | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id,seq,stack,action,original,destination,root,conflict,time_epoch,"
                "from_review,undoable,bytes,payload FROM records WHERE id=?", (record_id,)).fetchone()
        return Record.from_row(row) if row else None

    def oldest_undoable(self, limit: int = 1000) -> list[Record]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,seq,stack,action,original,destination,root,conflict,time_epoch,"
                "from_review,undoable,bytes,payload FROM records WHERE undoable=1 AND stack=? "
                "ORDER BY seq ASC LIMIT ?", (STACK_HISTORY, limit)).fetchall()
        return [Record.from_row(row) for row in rows]

    def oldest_reclaimable(self, limit: int = 1000) -> list[Record]:
        """Undoable history and stuck records that may still own snapshots."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id,seq,stack,action,original,destination,root,conflict,time_epoch,"
                "from_review,undoable,bytes,payload FROM records "
                "WHERE undoable=1 AND stack IN (?,?) ORDER BY seq ASC LIMIT ?",
                (STACK_HISTORY, STACK_STUCK, limit)).fetchall()
        return [Record.from_row(row) for row in rows]

    def retention_gate(self) -> tuple[int, float | None]:
        """Count undoable history and get its oldest timestamp in one query."""
        with self._lock:
            count, oldest = self._db.execute(
                "SELECT COUNT(*), MIN(time_epoch) FROM records "
                "WHERE stack=? AND undoable=1", (STACK_HISTORY,)).fetchone()
        return int(count), oldest

    def retire(self, ids: Iterable[str]) -> None:
        """Mark records non-undoable after their restore copies were reclaimed."""
        ids = list(ids)
        if not ids:
            return
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE records SET undoable=0, payload=json_set(COALESCE(NULLIF(payload,''),'{}'),"
                "'$.snapshots', json('[]')) WHERE id=?", [(i,) for i in ids])

    def take_orphans(self) -> list[str]:
        """Snapshot files that `apply` unlinked from history, ready to delete."""
        with self._lock:
            refs, self._orphans = self._orphans, []
        return refs

    def clear_records(self) -> list[str]:
        """Drop all history, returning the snapshot paths that were referenced."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT payload FROM records").fetchall()
            self._db.execute("DELETE FROM records")
        refs: list[str] = []
        for (payload,) in rows:
            try:
                refs.extend(json.loads(payload or "{}").get("snapshots") or [])
            except (ValueError, json.JSONDecodeError):
                continue
        return refs

    def counts(self) -> dict:
        with self._lock:
            history = self._db.execute(
                "SELECT COUNT(*) FROM records WHERE stack=?", (STACK_HISTORY,)).fetchone()[0]
            redo = self._db.execute(
                "SELECT COUNT(*) FROM records WHERE stack=?", (STACK_REDO,)).fetchone()[0]
            reviews = self._db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            done = self._db.execute("SELECT COUNT(*) FROM done").fetchone()[0]
            tags = self._db.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        return {"history": history, "redo": redo, "reviews": reviews, "done": done, "tags": tags}

    # -- review queue --------------------------------------------------
    def review_queue(self, root: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT path FROM reviews WHERE root=? ORDER BY added", (str(root),)).fetchall()
        return [row[0] for row in rows]

    def in_review(self, root: str) -> set[str]:
        return set(self.review_queue(root))

    def is_queued(self, root: str, path: str) -> bool:
        """Check one review row without materialising the full queue."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM reviews WHERE root=? AND path=? LIMIT 1",
                (str(root), str(path))).fetchone()
        return row is not None

    def review_count(self, root: str) -> int:
        """How many items are queued, without materialising the list.

        The sidebar asks after every navigation step, and a long queue would
        otherwise be pulled out of the database each time.
        """
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM reviews WHERE root=?",
                                   (str(root),)).fetchone()
        return int(row[0]) if row else 0

    # -- done ----------------------------------------------------------
    def done_paths(self) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT path FROM done").fetchall()
        return {row[0] for row in rows}

    def clear_done(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM done")

    @staticmethod
    def _inside(root: str, recursive: bool) -> tuple[str, list]:
        """A WHERE clause selecting paths inside *root*, and its parameters.

        A range on the primary key rather than LIKE, so it stays an index
        lookup: every path under ``root\\`` sorts between that prefix and the
        same prefix with its separator bumped by one code point.
        """
        prefix = os.path.join(str(root), "")
        where = "path >= ? AND path < ?"
        params: list = [prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)]
        if not recursive:
            where += " AND instr(substr(path, ?), ?) = 0"
            params += [len(prefix) + 1, os.sep]
        return where, params

    def done_under(self, root: str, recursive: bool = True) -> int:
        """How many handled files live inside *root*."""
        where, params = self._inside(root, recursive)
        with self._lock:
            row = self._db.execute(f"SELECT COUNT(*) FROM done WHERE {where}", params).fetchone()
        return int(row[0]) if row else 0

    def clear_done_under(self, root: str, recursive: bool = True) -> int:
        """Forget that the files inside *root* were handled. Returns how many."""
        where, params = self._inside(root, recursive)
        with self._lock, self._db:
            return self._db.execute(f"DELETE FROM done WHERE {where}", params).rowcount

    # -- duplicate ignore list -----------------------------------------
    def ignored_keys(self, root: str) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT key FROM ignored WHERE root=?", (str(root),)).fetchall()
        return {row[0] for row in rows}

    # -- tags ----------------------------------------------------------
    def tag(self, path: str) -> tuple[int, str]:
        with self._lock:
            row = self._db.execute("SELECT rating,label FROM tags WHERE path=?",
                                   (str(path),)).fetchone()
        return (int(row[0]), row[1]) if row else (0, "")

    def tags_for(self, paths: Sequence[str]) -> dict[str, tuple[int, str]]:
        if not paths:
            return {}
        out: dict[str, tuple[int, str]] = {}
        chunk = 400
        with self._lock:
            for start in range(0, len(paths), chunk):
                batch = [str(p) for p in paths[start:start + chunk]]
                marks = ",".join("?" * len(batch))
                rows = self._db.execute(
                    f"SELECT path,rating,label FROM tags WHERE path IN ({marks})", batch).fetchall()
                for path, rating, label in rows:
                    out[path] = (int(rating), label)
        return out

    def rename_tag(self, old: str, new: str) -> None:
        """Follow a file so its rating survives a rename."""
        with self._lock, self._db:
            self._db.execute("UPDATE OR REPLACE tags SET path=? WHERE path=?", (str(new), str(old)))

    def rated_paths(self, minimum: int = 1) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT path FROM tags WHERE rating>=?", (minimum,)).fetchall()
        return {row[0] for row in rows}

    def labelled_paths(self) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT path FROM tags WHERE label<>''").fetchall()
        return {row[0] for row in rows}
