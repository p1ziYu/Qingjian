"""Rotating run log plus a diagnostic bundle.

The previous version had no log at all: when something failed the only record
was a message box the user had already dismissed.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import re
import secrets
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from .. import __version__
from . import appdirs, i18n

LOGGER_NAME = "qingjian"
_configured = False


class _ClosingTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Flush and release the Windows file handle after every record.

    Keeping the handle open prevents temporary data directories, portable
    installs and application-data migrations from being removed on Windows.
    ``delay=True`` makes the next record reopen the same file transparently.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        finally:
            self.close()


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def configure(enabled: bool = True, keep_days: int = 7, level: int = logging.INFO,
              directory: Path | None = None) -> Path | None:
    """Attach a rotating file handler. Safe to call more than once."""
    global _configured
    root = get_logger()
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass
    _configured = False
    if not enabled:
        root.addHandler(logging.NullHandler())
        return None

    folder = directory or appdirs.log_dir()
    try:
        # On NTFS, creating a file and creating a sub-directory are separate
        # rights, so a folder that accepted the write probe can still refuse
        # this one. Raising here would take the whole start-up down with an
        # untranslated OSError in a box; no log is the lesser failure.
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        root.addHandler(logging.NullHandler())
        return None
    path = folder / "qingjian.log"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    try:
        handler = _ClosingTimedRotatingFileHandler(
            path, when="midnight", backupCount=max(1, keep_days), encoding="utf-8", delay=True
        )
    except OSError:
        return None
    handler.setFormatter(formatter)
    root.addHandler(handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.WARNING)
        stream.setFormatter(formatter)
        root.addHandler(stream)
    _configured = True
    root.info("logging started · version %s · %s", __version__, platform.platform())
    return path


def is_configured() -> bool:
    return _configured


def environment_report() -> dict:
    modules = {}
    for name in ("PySide6", "PIL", "numpy", "cv2", "av", "send2trash", "hachoir"):
        try:
            module = __import__(name)
            modules[name] = str(getattr(module, "__version__", "present"))
        except Exception:
            modules[name] = "missing"
    return {
        "app_version": __version__,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "data_dir": str(appdirs.data_dir(create=False)),
        "modules": modules,
    }


# ---------------------------------------------------------------------------
# Redaction
#
# A bundle exists to be sent to someone else, so the paths inside it are the
# problem: the records carry the absolute path of every file the user ever
# sorted, and every one of those paths starts with the Windows account name.
# Folder and file names are what people name after other people. So a path is
# never written out; only its shape is.
# ---------------------------------------------------------------------------

#: Stands in for the signed-in account wherever its name survives outside a path.
ACCOUNT_MASK = "[account]"

#: No character of a path segment may begin a drive prefix. Without this a
#: segment may hold spaces -- ``C:\\Program Files`` is a real path -- so in
#: "source=C:\\pics\\Wedding Chen last=D:\\Keep Nana\\x.jpg" the first match ran
#: on through ``last=D``, the scan resumed at the colon, and the second path's
#: folder and file names were written out whole. Stopping the segment at the
#: boundary is what keeps two paths in one line two paths.
_NOT_DRIVE = r"(?![A-Za-z]:[\\/])"
#: One path segment. A segment may not *end* in a space either, so a match
#: stops at the end of the path instead of running on into the sentence.
_SEGMENT = (r"[\\/](?:(?:" + _NOT_DRIVE + r"[^\\/:*?\"<>|\r\n])*"
            + _NOT_DRIVE + r"[^\s\\/:*?\"<>|])?")
#: ``\\\\?\\C:\\x`` and ``\\\\?\\UNC\\server\\share\\x``: the extended-length
#: spellings, whose ``?`` an ordinary segment may not contain. Matched first,
#: so the prefix is never mistaken for a share whose server is named ``?``.
_EXTENDED_PATH = re.compile(r"\\\\[?.]\\(?:UNC|[A-Za-z]:)(?:" + _SEGMENT + r")*")
#: A share -- which may name nothing but the server, as ``\\\\NAS is offline``
#: does, and a server is often named after the family or the company -- or a
#: drive, or a rooted path with no drive at all, which is what a Windows API
#: hands back and what the folder editor accepts. The drive letter may not
#: follow a word character, or ``https://host/page`` would be read as drive
#: ``s:``; the rooted form needs two segments and the same lookbehind, so that
#: ``a\\b`` in prose is left alone.
_WINDOWS_PATH = re.compile(
    r"(?:\\\\[^\s\\/:*?\"<>|]+(?:" + _SEGMENT + r")*"
    r"|(?<!\w)[A-Za-z]:(?:" + _SEGMENT + r")+"
    r"|(?<![\w.~:\\/])(?=[\\/])(?:" + _SEGMENT + r"){2,})")
#: ``~/Pictures/...``: home-relative, which the folder editor accepts because
#: ``Path.expanduser().is_absolute()`` is true for it.
_HOME_PATH = re.compile(r"(?<![\w.~:/])~(?:" + _SEGMENT + r")+")
#: The lookbehind keeps ``https://host/page`` and ``store/journal.json`` whole:
#: only a slash that starts a path is one.
_POSIX_PATH = re.compile(r"(?<![\w.~:/])(?:/(?:[^/\x00\r\n]*[^\s/\x00])?){2,}")
_PATTERNS = (_EXTENDED_PATH, _WINDOWS_PATH, _HOME_PATH, _POSIX_PATH)
_SEPARATORS = "\\/"

#: Inside one bundle the same path gets the same tag, so a report can still be
#: followed from a log line to the setting that points at the same file. The key
#: is per process, so the tag cannot be matched against a guessed path either.
_PATH_KEY = secrets.token_bytes(16)

#: A member larger than this is listed but not copied.
_MAX_MEMBER_BYTES = 32 * 1024 * 1024


def _account_names() -> tuple[str, ...]:
    """Every spelling of the signed-in account worth striking out.

    Paths are already gone by the time this runs; this catches the name where
    it appears as a word of its own, as an access-denied message spells it.
    Longest first, so a name that contains another is replaced before it.
    """
    names = {os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    for source in (getpass.getuser, lambda: Path.home().name):
        try:
            names.add(source())
        except (OSError, RuntimeError, KeyError):   # pragma: no cover - needs a bare environment
            continue
    # A two-letter account name would strike out half the words in a log line,
    # so only a name long enough to be distinctive is replaced.
    return tuple(sorted((name.strip() for name in names if len(name.strip()) >= 3),
                        key=len, reverse=True))


def _trim_path(text: str, following: str) -> str:
    """Hand back a trailing drive letter, and the punctuation after a path.

    :data:`_NOT_DRIVE` is what keeps a match from running into the next path,
    so this is no longer what separates two paths in one line; it stays for the
    one shape that lookahead cannot see, a drive letter named on its own in
    front of a colon that no separator follows ("drive C: is full"). Every
    other trailing word is kept, because "Wedding Chen 2024" and "IMG
    0001.JPG" are names, not sentences -- giving those back is how a masker
    hands over the very names it was written to hide.
    """
    text = text.rstrip()
    head, space, tail = text.rpartition(" ")
    if following.startswith(":") and space and len(tail) == 1 and tail.isascii() \
            and tail.isalpha():
        text = head.rstrip()
    # ``]`` is not stripped: by this point a path may end in ACCOUNT_MASK.
    return text.rstrip(".,;!?)'\"")


def _short_tag(text: str) -> str:
    """A stand-in that is the same for the same input inside one bundle only.

    Keyed, so two members of one zip can still be matched against each other
    while the tag says nothing to anyone who guesses the input.
    """
    return hashlib.blake2s(text.encode("utf-8", "replace"),
                           key=_PATH_KEY, digest_size=4).hexdigest()


def _mask_path(text: str) -> str:
    """Describe a path by its shape rather than naming any part of it."""
    unified = text.replace("\\", "/")
    if unified[:4] in ("//?/", "//./"):               # the extended-length spelling
        unified = unified[4:]
        if unified[:4].upper() == "UNC/":
            unified = "//" + unified[4:]
    if unified.startswith("//"):                      # a share: the server names a company
        drive, rest = "UNC", unified[2:].split("/")[1:]
    elif re.match(r"^[A-Za-z]:", unified):
        drive, rest = unified[:2].upper(), unified[2:].split("/")
    elif unified.startswith("~"):                     # home-relative, and a home is a name
        drive, rest = "~", unified[1:].split("/")
    else:
        drive, rest = "/", unified.split("/")
    segments = [part for part in rest if part]
    suffix = ""
    if segments:
        name = segments[-1]
        dot = name.rfind(".")
        if 0 < dot < len(name) - 1 and name[dot + 1:].isalnum() and len(name) - dot <= 9:
            suffix = f" ext={name[dot:].lower()}"
    return (f"<path drive={drive} depth={len(segments)}{suffix} "
            f"id={_short_tag(unified.lower())}>")


def _mask_matches(pattern: re.Pattern[str], text: str) -> str:
    """Replace every path *pattern* finds, resuming where the path really ended.

    ``re.sub`` would resume after the untrimmed match and so skip a second path
    in the same sentence, leaving its folder names in place.
    """
    pieces: list[str] = []
    index = 0
    while (match := pattern.search(text, index)) is not None:
        found = _trim_path(match.group(0), text[match.end():match.end() + 1])
        if len(found) < 3 or not any(sep in found for sep in _SEPARATORS):
            index = match.end()
            continue
        pieces.append(text[index:match.start()])
        pieces.append(_mask_path(found))
        index = match.start() + len(found)
    pieces.append(text[index:])
    return "".join(pieces)


def redact_text(text: str) -> str:
    """Mask every absolute path and the account name, keeping the words around them.

    A log line keeps its level, its logger and its sentence; the path inside it
    keeps its drive, its depth and its extension. Where the pattern cannot tell
    where a path ends it takes one word too many, which loses a word of the
    sentence rather than a folder name.

    The account name goes first, so that an account named ``pat`` or ``dri``
    cannot afterwards eat its way through the ``<path drive=...>`` masks this
    very function writes.
    """
    for name in _account_names():
        text = re.sub(re.escape(name), ACCOUNT_MASK, text, flags=re.IGNORECASE)
    for pattern in _PATTERNS:
        text = _mask_matches(pattern, text)
    return text


def redact_json(value):
    """Mask every string in a decoded JSON document, keys included."""
    if isinstance(value, dict):
        return {redact_text(key) if isinstance(key, str) else key: redact_json(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _why_unreadable(error: OSError) -> dict:
    """Why a file could not be read, without naming it.

    ``str(OSError)`` ends in the file's own path, so the one member a failure
    writes used to be the member that named the data directory -- which on a
    normal install is ``C:\\Users\\<account>\\AppData\\...`` -- and with it the
    account name the rest of the bundle takes such care to hide. The reason and
    the errno are what a report needs; the name adds nothing, because the
    member is already filed under it.
    """
    return {"unreadable": error.strerror or type(error).__name__,
            "errno": error.errno}


# ---------------------------------------------------------------------------
# Field-aware masking
#
# ``redact_text`` can only mask what looks like an absolute path. Four of the
# strings in settings.json are folder and file names that are nothing of the
# kind: a preset name has no separator at all, and a path or name template is
# relative by design -- ``template`` exists precisely so that someone can type
# ``Keepers/2026`` -- so both travelled verbatim while the ``folder`` beside
# them was masked. These are the fields people fill with a family name, so each
# is replaced by its shape rather than guessed at.
# ---------------------------------------------------------------------------

#: Settings fields known to hold a path. Masked by shape directly: a folder the
#: user typed may be relative, driveless or home-relative, and a field that is
#: known to be a path should never depend on a pattern recognising it.
_PATH_FIELDS = ("source_folder", "last_path", "folder")
_TEMPLATE_FIELDS = ("path_template", "name_template")
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]*)\}")
_TOKEN_NAMES: frozenset[str] | None = None


def _token_names() -> frozenset[str]:
    """Token names the renderer knows, so an unknown one is not echoed back.

    Imported on first use rather than at the top: every module imports this one
    for its logger, and nothing else here needs templates.
    """
    global _TOKEN_NAMES
    if _TOKEN_NAMES is None:
        try:
            from . import template
            _TOKEN_NAMES = frozenset(template.TOKENS) | frozenset(template.ALIASES)
        except Exception:                           # pragma: no cover - import order
            _TOKEN_NAMES = frozenset()
    return _TOKEN_NAMES


def _mask_token(body: str) -> str:
    """One ``{token}`` with everything the user typed into it taken out.

    The name stays only when the renderer knows it, because an unknown name is
    as likely to be a word someone typed as a token they meant; the fallback in
    ``{camera|Grandmother}`` is free text, so it goes. What is left answers the
    questions a template report actually raises: which tokens, padded to what.
    """
    name, bar, fallback = body.partition("|")
    name, colon, spec = name.partition(":")
    if name not in _token_names():
        return "{?}"
    if colon:
        name += ":" + (spec if spec.isdigit() else "?")
    if bar:
        name += f"|<text len={len(fallback)}>"
    return "{" + name + "}"


def _mask_template(text: str) -> str:
    """Keep a template's levels and tokens; drop every literal it holds.

    ``Chen Jianguo Family/{YYYY}/{MM}`` becomes ``<text len=19>/{YYYY}/{MM}``:
    how many levels, which tokens, and whether a level was written out by hand
    is the whole of what a template bug report needs. The literal is the one
    part that must not travel, because that is where a family name goes.
    """
    pieces = re.split(r"([\\/]+)", text)
    out = []
    for index, piece in enumerate(pieces):
        if index % 2:                               # a separator run, as typed
            out.append(piece)
            continue
        parts = _TEMPLATE_TOKEN.split(piece)
        for position, part in enumerate(parts):
            if position % 2:
                out.append(_mask_token(part))
            elif part:
                out.append(f"<text len={len(part)}>")
    return "".join(out)


def _mask_binding(value):
    if not isinstance(value, dict):
        return redact_json(value)
    masked = {}
    for key, item in value.items():
        if key in _TEMPLATE_FIELDS and isinstance(item, str):
            masked[key] = _mask_template(item)
        elif key in _PATH_FIELDS and isinstance(item, str):
            masked[key] = _mask_path(item) if item else item
        else:
            masked[key] = redact_json(item)
    return masked


def _mask_profile(bindings):
    if not isinstance(bindings, list):
        return redact_json(bindings)
    return [_mask_binding(entry) for entry in bindings]


def redact_settings(data):
    """Mask the settings document field by field rather than by pattern.

    Preset names are replaced by the order they are stored in, which is all a
    report needs (how many presets, which one is current, what each one binds)
    and which keeps the two places a name appears -- the key and
    ``current_profile`` -- pointing at each other.
    """
    if not isinstance(data, dict):
        return redact_json(data)
    profiles = data.get("profiles")
    order = {}
    if isinstance(profiles, dict):
        order = {name: f"preset {number}"
                 for number, name in enumerate(profiles, 1) if isinstance(name, str)}
    masked = {}
    for key, item in data.items():
        if key == "profiles" and isinstance(profiles, dict):
            masked[key] = {order.get(name, "preset ?"): _mask_profile(bindings)
                           for name, bindings in profiles.items()}
        elif key == "current_profile" and isinstance(item, str):
            masked[key] = order.get(item, "preset ?") if item else item
        elif key in _PATH_FIELDS and isinstance(item, str):
            masked[key] = _mask_path(item) if item else item
        elif key in _TEMPLATE_FIELDS and isinstance(item, str):
            masked[key] = _mask_template(item)
        else:
            masked[key] = redact_json(item)
    return masked


def redact_journal(value):
    """Mask the journal: paths by shape, content fingerprints by tag.

    A journal exists only when a transaction did not finish, which is exactly
    when a bundle gets exported, and every step of it carries the SHA-256 of
    the user's file *contents* plus a nanosecond modification time. Neither is
    a name, so neither contradicted the words on the About page, but a content
    hash lets anyone holding the zip test "does this person have this exact
    file" against any list of hashes they like, and a nanosecond timestamp is a
    near-unique fingerprint of one file. The tag keeps steps matchable against
    each other inside the one zip; the time is kept only to the second, which
    is enough to see that a file changed under the transaction.
    """
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            name = redact_text(key) if isinstance(key, str) else key
            if key == "hash" and isinstance(item, str) and item:
                masked[name] = f"<content id={_short_tag(item)}>"
            elif key == "mtime_ns" and isinstance(item, int) and not isinstance(item, bool):
                masked["mtime_s"] = item // 1_000_000_000
            else:
                masked[name] = redact_journal(item)
        return masked
    if isinstance(value, list):
        return [redact_journal(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def state_summary(path: str | Path) -> dict:
    """Describe the record database instead of shipping it.

    It holds the absolute path of every file that was ever sorted, every target
    folder and the review queue -- the one thing a bundle must not carry. Its
    shape and its row counts answer what a bug report actually raises (which
    schema, how much history, is the review queue empty) and name no file.
    """
    target = Path(path)
    summary: dict = {"present": target.is_file(), "bytes": 0,
                     "schema": "", "rows": {}, "columns": {}}
    if not summary["present"]:
        return summary
    try:
        summary["bytes"] = target.stat().st_size
        connection = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True)
    except OSError as error:
        summary.update(_why_unreadable(error))
        return summary
    except (ValueError, sqlite3.Error) as error:
        summary["unreadable"] = redact_text(str(error))
        return summary
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            summary["columns"][table] = [row[1] for row in
                                         connection.execute(f'PRAGMA table_info("{table}")')]
            summary["rows"][table] = int(connection.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        if "meta" in tables:
            # The only key ever written there is the schema number.
            row = connection.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            summary["schema"] = str(row[0]) if row and row[0] is not None else ""
    except sqlite3.Error as error:
        summary["unreadable"] = redact_text(str(error))
    finally:
        connection.close()
    return summary


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------

#: Every member a bundle carries, in the order the settings page lists them:
#: (name inside the zip, catalogue key for the line the user reads). A name
#: ending in ``/`` is a folder with at least one member. The zip and the words
#: on screen are both built from this tuple, so they cannot drift apart -- the
#: previous version listed a ``store/quota.json`` nothing ever writes and
#: shipped a ``state/state.db`` the screen never mentioned.
BUNDLE_MANIFEST: tuple[tuple[str, str], ...] = (
    ("environment.json", "bundle.item.environment"),
    ("settings.json", "bundle.item.settings"),
    ("state-summary.json", "bundle.item.state"),
    ("store/journal.json", "bundle.item.journal"),
    ("snapshots-index.json", "bundle.item.snapshots"),
    ("logs/", "bundle.item.logs"),
)

#: What a bundle leaves out, listed on screen beside what it carries.
BUNDLE_OMITS: tuple[str, ...] = ("bundle.omit.media", "bundle.omit.paths",
                                 "bundle.omit.account")


def bundle_description() -> str:
    """The item-by-item list the settings page shows, in the current language."""
    lines = [i18n.tr("bundle.contents")]
    lines += [f"· {name} — {i18n.tr(key)}" for name, key in BUNDLE_MANIFEST]
    lines.append(i18n.tr("bundle.omits"))
    lines += [f"· {i18n.tr(key)}" for key in BUNDLE_OMITS]
    return "\n".join(lines)


def _dump(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _redacted_json_file(path: Path, masker=redact_json):
    """Read one of the application's own JSON files, masked. Never raises.

    *masker* is the one gate every byte of that file passes through, so a
    member can never be added without choosing how it is masked.
    """
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        return _why_unreadable(error)
    try:
        return masker(json.loads(raw))
    except ValueError:
        # Shape, not content: the bytes themselves are what cannot be read.
        return {"unparsable": True, "bytes": len(raw.encode("utf-8", "replace"))}


def _snapshot_index(root: Path) -> list[dict]:
    """The restore copies, by their generated ids -- never by what they hold."""
    folder = root / "store" / "snapshots"
    listing: list[dict] = []
    if not folder.is_dir():
        return listing
    for item in sorted(folder.iterdir()):
        try:
            listing.append({"name": item.name, "bytes": item.stat().st_size})
        except OSError:
            continue
    return listing


def _log_members(folder: Path) -> list[tuple[str, str]]:
    """Every log file, redacted, behind an index that is written even when empty."""
    index: list[dict] = []
    members: list[tuple[str, str]] = []
    for item in sorted(folder.glob("*")) if folder.is_dir() else []:
        if not item.is_file():
            continue
        try:
            size = item.stat().st_size
            oversized = size > _MAX_MEMBER_BYTES
            text = "" if oversized else redact_text(
                item.read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            index.append({"name": item.name, "bytes": 0, "included": False,
                          **_why_unreadable(error)})
            continue
        entry = {"name": item.name, "bytes": size, "included": not oversized}
        if not oversized:
            entry["lines"] = text.count("\n") + 1
            members.append((item.name, text))
        index.append(entry)
    return [("index.json", _dump(index))] + members


def diagnostic_bundle(destination: str | Path, data_root: Path | None = None) -> Path:
    """Write the zip the settings page describes, masked as it promises.

    Every member of :data:`BUNDLE_MANIFEST` is written even when the file
    behind it is missing, so what the user is shown is what the zip holds.
    Media files were never in it; now neither are the paths of the files that
    were sorted. The record database is summarised rather than copied; the logs
    go in with every path and account name replaced by its shape; and the two
    documents whose fields are known -- the settings and the journal -- are
    masked field by field rather than by pattern, because the names people type
    into a template or a preset look nothing like a path.
    """
    root = data_root or appdirs.data_dir(create=False)
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("environment.json", _dump(redact_json(environment_report())))
        bundle.writestr("settings.json",
                        _dump(_redacted_json_file(root / "settings.json", redact_settings)))
        bundle.writestr("state-summary.json",
                        _dump(redact_json(state_summary(root / "state" / "state.db"))))
        bundle.writestr("store/journal.json",
                        _dump(_redacted_json_file(root / "store" / "journal.json",
                                                  redact_journal)))
        bundle.writestr("snapshots-index.json", _dump(_snapshot_index(root)))
        for name, text in _log_members(root / "logs"):
            bundle.writestr(f"logs/{name}", text)
    return out
