"""Building filenames that the filesystem will actually accept."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .platform_ import ILLEGAL_NAME_CHARS, MAX_PATH_LENGTH, is_reserved_name
from .mediatypes import MEDIA_EXTENSIONS

_ILLEGAL = re.compile("[" + re.escape(ILLEGAL_NAME_CHARS) + "]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_COLLAPSE = re.compile(r"\s{2,}")

#: Sequence suffix this module appends, and the pattern that recognises one it
#: has appended before, so repeated runs do not stack " (2) (2)".
_SEQUENCE = re.compile(r"^(?P<stem>.*?)(?: \((?P<number>\d+)\))$")

MAX_SEQUENCE = 100_000


class NameError_(ValueError):
    """Raised with an i18n key in :attr:`key` plus formatting fields."""

    def __init__(self, key: str, **fields: object) -> None:
        super().__init__(key)
        self.key = key
        self.fields = fields


def sanitize_component(name: str, replacement: str = "_",
                       collapse_spaces: bool = True, strip_outer: bool = True) -> str:
    """Make *name* usable as one path component on every supported platform."""
    text = unicodedata.normalize("NFC", str(name))
    text = _CONTROL.sub("", text)
    text = _ILLEGAL.sub(replacement, text)
    text = _COLLAPSE.sub(" ", text) if collapse_spaces else text
    if strip_outer:
        text = text.strip()
    # Windows silently drops trailing dots and spaces, which turns "a." into
    # "a" and makes a later existence check disagree with what was written.
    text = text.rstrip(". ")
    if not text:
        text = replacement
    if is_reserved_name(text):
        text = "_" + text
    return text


def validate_filename(name: str) -> None:
    """Raise :class:`NameError_` when *name* cannot be used as a filename."""
    text = str(name)
    if not text.strip() or text.strip() in (".", ".."):
        raise NameError_("error.name_invalid")
    if _CONTROL.search(text) or _ILLEGAL.search(text):
        raise NameError_("error.name_invalid")
    if text != text.rstrip(". "):
        raise NameError_("error.name_invalid")
    if is_reserved_name(text):
        raise NameError_("error.name_reserved", name=text)
    if len(text) > MAX_PATH_LENGTH:
        raise NameError_("error.name_too_long", length=len(text))


def check_path_length(path: str | Path) -> None:
    text = str(path)
    if len(text) > 259:  # classic MAX_PATH minus the NUL
        raise NameError_("error.name_too_long", length=len(text))


def split_name(filename: str) -> tuple[str, str]:
    """Split into (stem, suffix) treating ``a.jpg.xmp`` as ('a.jpg', '.xmp')."""
    path = Path(filename)
    return path.stem, path.suffix


def strip_sequence(stem: str) -> str:
    """``photo (3)`` -> ``photo``; anything else unchanged."""
    match = _SEQUENCE.match(stem)
    return match.group("stem") if match else stem


def unique_destination(folder: Path, filename: str, taken: set | None = None,
                       case_insensitive: bool = True) -> Path:
    """A path in *folder* named *filename*, or the next free ``name (n)``.

    ``taken`` lets a batch reserve names that are planned but not written yet,
    which is what stops two files in one operation racing for the same slot.
    """
    folder = Path(folder)
    reserved = set()
    if taken:
        reserved = {str(p).casefold() if case_insensitive else str(p) for p in taken}

    def is_free(candidate: Path) -> bool:
        key = str(candidate).casefold() if case_insensitive else str(candidate)
        return key not in reserved and not candidate.exists()

    first = folder / filename
    if is_free(first):
        return first
    stem, suffix = split_name(filename)
    base = strip_sequence(stem)
    for number in range(2, MAX_SEQUENCE):
        candidate = folder / f"{base} ({number}){suffix}"
        if is_free(candidate):
            return candidate
    raise NameError_("error.no_unique_name")


def ensure_suffix(name: str, fallback_suffix: str) -> str:
    """Give *name* an extension if the user typed one without."""
    return name if Path(name).suffix.casefold() in MEDIA_EXTENSIONS | {".xmp"} else name + fallback_suffix
