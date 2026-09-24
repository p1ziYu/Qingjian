"""Persistent cache for content hashes, perceptual hashes and sharpness.

Without it, every duplicate scan re-reads the whole library from disk. Keyed by
path plus size plus mtime, so an edited file is recomputed and a merely moved
one costs a single row.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .logsetup import get_logger

log = get_logger("hashcache")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    sha256    TEXT,
    phash     TEXT,
    sharpness REAL,
    blown     REAL,
    crushed   REAL,
    captured  REAL,
    updated   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_size ON entries(size);
CREATE TABLE IF NOT EXISTS cache_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

FIELDS = ("sha256", "phash", "sharpness", "blown", "crushed", "captured")


class HashCache:
    """Thread-safe key/value store. Losing it costs time, never correctness."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._memory: dict[tuple, dict] = {}
        self._db: sqlite3.Connection | None = None
        #: Depth of the current `batch`. Above zero, writes are not committed
        #: one at a time: a duplicate scan stores a row per file, and a commit
        #: each is a synchronous disk flush per photograph.
        self._batching = 0
        self._dirty = False
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(str(self.path), check_same_thread=False)
                self._db.executescript(SCHEMA)
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.commit()
                self._migrate()
            except sqlite3.Error as error:
                log.warning("hash cache unavailable, running in memory: %s", error)
                self._db = None

    def _migrate(self) -> None:
        """Add columns an older cache file lacks, and drop stale hashes."""
        if self._db is None:
            return
        try:
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(entries)")}
            if "captured" not in columns:
                self._db.execute("ALTER TABLE entries ADD COLUMN captured REAL")
            if "blown" not in columns:
                self._db.execute("ALTER TABLE entries ADD COLUMN blown REAL")
            if "crushed" not in columns:
                self._db.execute("ALTER TABLE entries ADD COLUMN crushed REAL")
            row = self._db.execute(
                "SELECT value FROM cache_meta WHERE key='algorithm'").fetchone()
            from .imaging import ALGORITHM_VERSION
            stored = str(row[0]) if row else ""
            if stored != str(ALGORITHM_VERSION):
                # Perceptual hashes from a different decode path are not
                # comparable with fresh ones; clear rather than mix them.
                self._db.execute("UPDATE entries SET phash=NULL, sharpness=NULL")
                self._db.execute(
                    "INSERT INTO cache_meta(key,value) VALUES('algorithm',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(ALGORITHM_VERSION),))
            captured_row = self._db.execute(
                "SELECT value FROM cache_meta WHERE key='captured'").fetchone()
            if not captured_row or captured_row[0] != "2":
                self._db.execute("UPDATE entries SET captured=NULL")
                self._db.execute(
                    "INSERT INTO cache_meta(key,value) VALUES('captured','2') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            self._db.commit()
        except sqlite3.Error as error:
            log.warning("hash cache migration skipped: %s", error)

    # -- batching ------------------------------------------------------
    @contextmanager
    def batch(self):
        """Defer commits until the block ends.

        Twenty thousand perceptual hashes meant twenty thousand commits, each
        one a flush to disk. The rows are the same either way; only the number
        of flushes changes, and a lost batch costs recomputation, never
        correctness.
        """
        with self._lock:
            self._batching += 1
        try:
            yield self
        finally:
            with self._lock:
                self._batching -= 1
                if self._batching <= 0:
                    self._batching = 0
                    self._commit()

    def _commit(self) -> None:
        if self._db is None or not self._dirty:
            return
        try:
            self._db.commit()
            self._dirty = False
        except sqlite3.Error as error:
            log.debug("hash cache commit failed: %s", error)

    def preload(self, paths) -> int:
        """Read every cached row for *paths* in one query instead of one each."""
        if self._db is None:
            return 0
        wanted = [str(p) for p in paths]
        if not wanted:
            return 0
        found = 0
        with self._lock:
            for start in range(0, len(wanted), 400):
                batch = wanted[start:start + 400]
                marks = ",".join("?" * len(batch))
                try:
                    rows = self._db.execute(
                        "SELECT path, size, mtime_ns, sha256, phash, sharpness, "
                        f"blown, crushed, captured FROM entries WHERE path IN ({marks})",
                        batch).fetchall()
                except sqlite3.Error:
                    return found
                for path, size, mtime_ns, sha, ph, sharp, blown, crushed, captured in rows:
                    self._memory[(path, size, mtime_ns)] = {
                        "sha256": sha, "phash": _from_text(ph),
                        "sharpness": sharp, "blown": blown, "crushed": crushed,
                        "captured": captured}
                    found += 1
        return found

    # -- keys ----------------------------------------------------------
    @staticmethod
    def key(path: str | Path) -> tuple | None:
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        return (str(path), stat.st_size, stat.st_mtime_ns)

    # -- access --------------------------------------------------------
    def get(self, path: str | Path, field: str) -> object | None:
        key = self.key(path)
        if key is None:
            return None
        return self.get_by_key(key, field)

    def get_by_key(self, key: tuple, field: str) -> object | None:
        with self._lock:
            row = self._memory.get(key)
            if row is None and self._db is not None:
                row = self._load(key)
            if not row:
                return None
            value = row.get(field)
            return value if value not in ("", None) else None

    def get_many(self, key: tuple, fields) -> dict[str, object | None]:
        with self._lock:
            row = self._memory.get(key)
            if row is None and self._db is not None:
                row = self._load(key)
            return {field: (row or {}).get(field) for field in fields}

    def put(self, path: str | Path, **values) -> None:
        key = self.key(path)
        if key is None:
            return
        self.put_by_key(key, **values)

    def put_by_key(self, key: tuple, **values) -> None:
        clean = {k: v for k, v in values.items() if k in FIELDS and v is not None}
        if not clean:
            return
        with self._lock:
            row = dict(self._memory.get(key) or {})
            row.update(clean)
            self._memory[key] = row
            if self._db is None:
                return
            try:
                self._db.execute(
                    "INSERT INTO entries(path,size,mtime_ns,sha256,phash,sharpness,"
                    "blown,crushed,captured,updated) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET"
                    " sha256=CASE WHEN entries.size=excluded.size AND entries.mtime_ns=excluded.mtime_ns"
                    " THEN COALESCE(excluded.sha256,entries.sha256) ELSE excluded.sha256 END,"
                    " phash=CASE WHEN entries.size=excluded.size AND entries.mtime_ns=excluded.mtime_ns"
                    " THEN COALESCE(excluded.phash,entries.phash) ELSE excluded.phash END,"
                    " sharpness=CASE WHEN entries.size=excluded.size AND entries.mtime_ns=excluded.mtime_ns"
                    " THEN COALESCE(excluded.sharpness,entries.sharpness) ELSE excluded.sharpness END,"
                    " blown=CASE WHEN entries.size=excluded.size AND entries.mtime_ns=excluded.mtime_ns"
                    " THEN COALESCE(excluded.blown,entries.blown) ELSE excluded.blown END,"
                    " crushed=CASE WHEN entries.size=excluded.size AND entries.mtime_ns=excluded.mtime_ns"
                    " THEN COALESCE(excluded.crushed,entries.crushed) ELSE excluded.crushed END,"
                    " captured=CASE WHEN entries.size=excluded.size AND entries.mtime_ns=excluded.mtime_ns"
                    " THEN COALESCE(excluded.captured,entries.captured) ELSE excluded.captured END,"
                    " size=excluded.size, mtime_ns=excluded.mtime_ns, updated=excluded.updated",
                    (key[0], key[1], key[2], row.get("sha256"),
                     _to_text(row.get("phash")),
                     row.get("sharpness"), row.get("blown"), row.get("crushed"),
                     row.get("captured"), time.time()),
                )
                self._dirty = True
                if not self._batching:
                    self._commit()
            except sqlite3.Error as error:
                log.debug("hash cache write failed: %s", error)

    def _load(self, key: tuple) -> dict | None:
        if self._db is None:
            return None
        try:
            cursor = self._db.execute(
                "SELECT size, mtime_ns, sha256, phash, sharpness, blown, crushed, captured "
                "FROM entries WHERE path=?", (key[0],),
            )
            row = cursor.fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        size, mtime_ns, sha, ph, sharp, blown, crushed, captured = row
        if size != key[1] or mtime_ns != key[2]:
            return None                     # the file changed; recompute
        value = {"sha256": sha, "phash": _from_text(ph),
                 "sharpness": sharp, "blown": blown, "crushed": crushed,
                 "captured": captured}
        self._memory[key] = value
        return value

    def prune(self, older_than_days: int = 90) -> int:
        if self._db is None:
            return 0
        cutoff = time.time() - older_than_days * 86400
        try:
            cursor = self._db.execute("DELETE FROM entries WHERE updated < ?", (cutoff,))
            self._db.commit()
            return cursor.rowcount or 0
        except sqlite3.Error:
            return 0

    def count(self) -> int:
        if self._db is None:
            return len(self._memory)
        try:
            return int(self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        except sqlite3.Error:
            return 0

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._batching = 0
                self._commit()
                try:
                    self._db.close()
                except sqlite3.Error:
                    pass
                self._db = None


def _to_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return format(value, "016x")
    return str(value)


def _from_text(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None
