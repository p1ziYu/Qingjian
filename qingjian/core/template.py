r"""Target-path and filename templates.

A fixed destination folder per key is not how people file photographs; they
want ``Keepers/2026/2026-08/``. This renders that from tokens, and refuses a
template that would escape the target root or produce a name Windows rejects.

Syntax
------
``{token}``            substitute
``{seq:4}``            zero-pad a number to 4 digits
``{camera|Unknown}``   fall back to this text when the token is empty
``/`` or ``\``         path separator (path templates only)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

from .naming import NameError_, sanitize_component, validate_filename
from .mediatypes import MEDIA_EXTENSIONS
from .platform_ import is_reserved_name

_TOKEN = re.compile(r"\{([^{}]*)\}")
_SEPARATORS = re.compile(r"[\\/]+")


@dataclass
class TemplateContext:
    """Everything a template may refer to about one file."""

    source: Path
    when: datetime
    when_is_fallback: bool = False
    camera: str = ""
    lens: str = ""
    iso: str = ""
    aperture: str = ""
    shutter: str = ""
    focal: str = ""
    rating: int = 0
    label: str = ""
    sequence: int = 1
    source_root: Path | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def stem(self) -> str:
        return self.source.stem

    @property
    def suffix(self) -> str:
        return self.source.suffix

    def relative_folder(self) -> str:
        if self.source_root is None:
            return ""
        try:
            rel = self.source.parent.relative_to(self.source_root)
        except ValueError:
            return ""
        return "" if str(rel) == "." else str(rel).replace("\\", "/")


def _date(fmt: str) -> Callable[[TemplateContext], str]:
    return lambda ctx: ctx.when.strftime(fmt)


#: token name -> renderer. Case-sensitive on purpose: {MM} is the month and
#: {mm} the minute, exactly as in every other date-format language.
TOKENS: dict[str, Callable[[TemplateContext], str]] = {
    "YYYY": _date("%Y"),
    "YY": _date("%y"),
    "MM": _date("%m"),
    "DD": _date("%d"),
    "HH": _date("%H"),
    "mm": _date("%M"),
    "ss": _date("%S"),
    "YYYY-MM": _date("%Y-%m"),
    "YYYY-MM-DD": _date("%Y-%m-%d"),
    "YYYYMMDD": _date("%Y%m%d"),
    "YYYY_MM": _date("%Y_%m"),
    "HH-mm-ss": _date("%H-%M-%S"),
    "HHmmss": _date("%H%M%S"),
    "MonthName": _date("%B"),
    "WeekDay": _date("%A"),
    "name": lambda ctx: ctx.stem,
    "ext": lambda ctx: ctx.suffix.lstrip("."),
    "folder": lambda ctx: ctx.source.parent.name,
    "relpath": lambda ctx: ctx.relative_folder(),
    "camera": lambda ctx: ctx.camera,
    "lens": lambda ctx: ctx.lens,
    "iso": lambda ctx: ctx.iso,
    "ISO": lambda ctx: ctx.iso,
    "aperture": lambda ctx: ctx.aperture,
    "shutter": lambda ctx: ctx.shutter,
    "focal": lambda ctx: ctx.focal,
    "rating": lambda ctx: str(ctx.rating) if ctx.rating else "",
    "label": lambda ctx: ctx.label,
    "seq": lambda ctx: str(ctx.sequence),
}

#: Chinese spellings accepted for the semantic tokens, so the same template
#: text reads naturally in either interface language.
ALIASES: dict[str, str] = {
    "原名": "name",
    "扩展名": "ext",
    "原文件夹": "folder",
    "相对路径": "relpath",
    "相机": "camera",
    "镜头": "lens",
    "感光度": "iso",
    "光圈": "aperture",
    "快门": "shutter",
    "焦距": "focal",
    "评分": "rating",
    "色标": "label",
    "序号": "seq",
    "年": "YYYY",
    "月": "MM",
    "日": "DD",
    "时": "HH",
    "分": "mm",
    "秒": "ss",
}

#: Tokens offered as clickable chips, in the order the dialog shows them.
SUGGESTED_PATH_TOKENS = (
    "YYYY", "YYYY-MM", "YYYY-MM-DD", "MM", "DD",
    "camera", "lens", "ISO", "folder", "relpath", "rating", "label",
)
SUGGESTED_NAME_TOKENS = (
    "YYYY-MM-DD", "YYYYMMDD", "HHmmss", "seq", "name", "ext", "camera", "rating",
)

#: Numeric tokens where ``:N`` means zero-padding rather than a default.
_PADDABLE = frozenset({"seq", "rating", "iso", "ISO"})


def _resolve_one(body: str, ctx: TemplateContext, strict: bool) -> str:
    name = body
    default = ""
    pad = 0
    if "|" in name:
        name, default = name.split("|", 1)
    if ":" in name:
        name, arg = name.split(":", 1)
        name = name.strip()
        arg = arg.strip()
        if name in _PADDABLE or ALIASES.get(name) in _PADDABLE:
            try:
                pad = int(arg)
            except ValueError:
                if strict:
                    raise NameError_("tpl.unknown_token", token="{" + body + "}") from None
        else:
            default = arg
    name = name.strip()
    key = ALIASES.get(name, name)
    renderer = TOKENS.get(key)
    if renderer is None:
        if key in ctx.extra:
            value = str(ctx.extra[key])
        elif strict:
            raise NameError_("tpl.unknown_token", token="{" + body + "}")
        else:
            return ""
    else:
        value = renderer(ctx)
    value = "" if value is None else str(value)
    if pad and value.isdigit():
        value = value.zfill(pad)
    if not value:
        value = default
    return value


def _substitute(template: str, ctx: TemplateContext, strict: bool,
                protected: bool = False) -> str:
    if template.count("{") != template.count("}"):
        raise NameError_("tpl.unbalanced")
    def replace(match):
        value = _resolve_one(match.group(1), ctx, strict)
        return f"\uf000{value}\uf001" if protected and value else value
    return _TOKEN.sub(replace, template)


def _tidy(component: str) -> str:
    """Clean up what an empty token leaves behind: ``a__b`` -> ``a_b``."""
    if "\uf000" not in component:
        text = re.sub(r"(?:_{2,}|-{2,}|\s{2,})", lambda m: m.group(0)[0], component)
        return text.strip("_- ")
    parts = re.split(r"(\uf000.*?\uf001)", component)
    for index, part in enumerate(parts):
        if part.startswith("\uf000"):
            parts[index] = part[1:-1]
        else:
            parts[index] = re.sub(r"(?:_{2,}|-{2,}|\s{2,})", lambda m: m.group(0)[0], part)
    if parts:
        parts[0] = parts[0].lstrip("_- ")
        parts[-1] = parts[-1].rstrip("_- ")
    return "".join(parts)


#: The one token whose value is allowed to introduce further folder levels,
#: because reproducing a source subfolder tree is the whole point of it.
_MULTI_LEVEL_TOKENS = frozenset({"relpath", "相对路径"})


def _is_bare_multilevel(part: str) -> bool:
    stripped = part.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return False
    body = stripped[1:-1].split("|", 1)[0].split(":", 1)[0].strip()
    return body in _MULTI_LEVEL_TOKENS


def render_path(template: str, ctx: TemplateContext, strict: bool = True) -> tuple[str, ...]:
    """Render a *relative* folder path as sanitized components.

    The template is split into components *before* substitution, so a value
    that happens to contain a slash — a camera reported as ``Weird/Brand``, a
    lens name with a backslash — becomes one folder called ``Weird_Brand``
    rather than silently adding a directory level. ``{relpath}`` is the single
    exception, since reproducing a subfolder tree is what it is for.

    Empty components (an absent camera, say) are dropped rather than left as
    an empty directory level.
    """
    text = (template or "").strip()
    if not text:
        return ()
    if PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute():
        raise NameError_("tpl.absolute_not_allowed")
    if text.count("{") != text.count("}"):
        raise NameError_("tpl.unbalanced")

    parts: list[str] = []
    for raw_part in _SEPARATORS.split(text):
        if not raw_part.strip():
            continue
        multilevel = _is_bare_multilevel(raw_part)
        rendered = _substitute(raw_part, ctx, strict, protected=not multilevel)
        pieces = _SEPARATORS.split(rendered) if multilevel else [rendered]
        for piece in pieces:
            piece = piece if multilevel else _tidy(piece)
            if not piece or piece == ".":
                continue
            if piece == "..":
                raise NameError_("tpl.escapes_root")
            parts.append(sanitize_component(piece, collapse_spaces=False, strip_outer=False))
    return tuple(parts)


def render_name(template: str, ctx: TemplateContext, strict: bool = True) -> str:
    """Render one filename. The source extension is appended when absent."""
    text = (template or "").strip()
    if not text:
        return ctx.source.name
    if _SEPARATORS.search(text):
        raise NameError_("error.name_invalid")
    rendered = _tidy(_substitute(text, ctx, strict, protected=True))
    rendered = sanitize_component(rendered, collapse_spaces=False,
                                  strip_outer=False) if rendered else ctx.stem
    tail = text.rsplit("}", 1)[-1] if "}" in text else text
    last_token = list(_TOKEN.finditer(text))[-1] if _TOKEN.search(text) else None
    ends_with_ext = bool(last_token and last_token.end() == len(text) and
                         ALIASES.get(last_token.group(1), last_token.group(1)) == "ext")
    literal_ext = any(tail.casefold().endswith(ext) for ext in MEDIA_EXTENSIONS | {".xmp"})
    if not (ends_with_ext or literal_ext) and ctx.suffix:
        rendered += ctx.suffix
    validate_filename(rendered)
    return rendered


def render_destination(folder: Path, path_template: str, name_template: str,
                       ctx: TemplateContext, strict: bool = True) -> Path:
    """Full target path: base folder + rendered subfolders + rendered name."""
    target = Path(folder)
    for part in render_path(path_template, ctx, strict):
        target = target / part
    return target / render_name(name_template, ctx, strict)


def validate(path_template: str, name_template: str) -> None:
    """Raise :class:`NameError_` if either template cannot be rendered."""
    probe = TemplateContext(
        source=Path("Sample/IMG_0001.JPG"),
        when=datetime(2026, 8, 14, 19, 42, 8),
        camera="Sony ILCE-7M4",
        lens="FE 24mm F2.8 G",
        iso="400",
        rating=3,
        label="green",
        sequence=1,
        source_root=Path("Sample"),
    )
    render_path(path_template, probe, strict=True)
    render_name(name_template, probe, strict=True)
    # Rendering repairs an illegal component so that sorting never stalls on
    # one odd camera name. At validation time the data is a clean probe, so any
    # repair means the template itself is at fault and the user should hear it.
    for raw_part in _SEPARATORS.split((path_template or "").strip()):
        if not raw_part.strip():
            continue
        rendered = _substitute(raw_part, probe, True)
        pieces = _SEPARATORS.split(rendered) if _is_bare_multilevel(raw_part) else [rendered]
        for piece in pieces:
            piece = _tidy(piece)
            if piece and piece != "." and sanitize_component(piece) != piece:
                raise NameError_("error.name_invalid")
    raw_name = _tidy(_substitute((name_template or "").strip(), probe, True))
    if raw_name and sanitize_component(raw_name) != raw_name:
        if is_reserved_name(raw_name):
            raise NameError_("error.name_reserved", name=raw_name)
        raise NameError_("error.name_invalid")
