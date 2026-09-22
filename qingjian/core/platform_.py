"""Thin wrappers over the few genuinely platform-specific operations.

Keeping these in one place is what lets the rest of the core run (and be
tested) on a machine that is not Windows. Every function degrades to a clear
failure rather than a traceback from three layers down.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

#: Names Windows refuses regardless of extension, in any letter case.
WINDOWS_RESERVED: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
    # Windows 11 also rejects the superscript digit forms.
    | {f"com{c}" for c in "¹²³"}
    | {f"lpt{c}" for c in "¹²³"}
)

#: Characters Windows forbids in a path component.
ILLEGAL_NAME_CHARS = '<>:"/\\|?*'

#: Conservative cap. Long-path support exists but is opt-in per machine.
MAX_PATH_LENGTH = 255


class TrashUnavailable(RuntimeError):
    """The system recycle bin could not be reached."""


def _send2trash():
    try:
        from send2trash import send2trash  # type: ignore
    except Exception:  # pragma: no cover - depends on the install
        return None
    return send2trash


def trash_available() -> bool:
    return _send2trash() is not None


def _volume_recycle_limit(root: str) -> int | None:
    """Read this user's actual per-volume bin limit, in bytes; unknown is unsafe."""
    import ctypes
    import winreg

    volume = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(
            ctypes.c_wchar_p(root), volume, len(volume)):
        return None
    name = volume.value
    if "{" not in name or "}" not in name:
        return None
    guid = "{" + name.split("{", 1)[1].split("}", 1)[0] + "}"
    key_name = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                "\\BitBucket\\Volume\\" + guid)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            cap, cap_type = winreg.QueryValueEx(key, "MaxCapacity")
            nuke, nuke_type = winreg.QueryValueEx(key, "NukeOnDelete")
    except OSError:
        return None
    if (cap_type != winreg.REG_DWORD or nuke_type != winreg.REG_DWORD
            or not isinstance(cap, int) or cap <= 0 or nuke != 0):
        return None
    return cap * 1024 * 1024


def _recycle_policy() -> tuple[int | None, bool] | None:
    """Return (all-volume size percentage, recycle allowed); None if unreadable."""
    import winreg

    key_name = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name)
    except FileNotFoundError:
        return None, True  # Neither user policy is configured.
    except OSError:
        return None
    try:
        with key:
            def setting(name: str) -> tuple[object, int] | None:
                try:
                    return winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    return None
            size = setting("RecycleBinSize")
            no_recycle = setting("NoRecycleFiles")
    except OSError:
        return None
    for entry in (size, no_recycle):
        if entry is not None and (entry[1] != winreg.REG_DWORD
                                  or type(entry[0]) is not int):
            return None
    if size is not None and not 1 <= size[0] <= 100:
        return None
    if no_recycle is not None and no_recycle[0] not in (0, 1):
        return None
    return (size[0] if size is not None else None,
            no_recycle is None or no_recycle[0] == 0)


def can_recycle(path: str | Path, bytes_needed: int = 0) -> bool:
    """Conservatively verify that Windows can recycle this volume and payload."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        value = str(Path(path).resolve())
        if value.startswith("\\\\"):
            return False
        root = Path(value).anchor
        if not root or not root.endswith("\\"):
            return False
        kernel = ctypes.windll.kernel32
        if kernel.GetDriveTypeW(ctypes.c_wchar_p(root)) != 3:  # DRIVE_FIXED
            return False

        class BinInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                        ("i64NumItems", ctypes.c_longlong)]

        info = BinInfo()
        info.cbSize = ctypes.sizeof(info)
        shell = ctypes.windll.shell32
        if shell.SHQueryRecycleBinW(ctypes.c_wchar_p(root), ctypes.byref(info)) != 0:
            return False
        limit = _volume_recycle_limit(root)
        if limit is None:
            return False
        policy = _recycle_policy()
        if policy is None or not policy[1]:
            return False
        free = ctypes.c_ulonglong()
        total = ctypes.c_ulonglong()
        if not kernel.GetDiskFreeSpaceExW(ctypes.c_wchar_p(root), ctypes.byref(free),
                                          ctypes.byref(total), None):
            return False
        if policy[0] is not None:
            limit = min(limit, total.value * policy[0] // 100)
        return (bytes_needed <= free.value
                and bytes_needed + info.i64Size <= limit)
    except (AttributeError, OSError, ValueError):
        return False


def move_to_trash(path: str | Path) -> None:
    """Send *path* to the platform recycle bin.

    Raises :class:`TrashUnavailable` when no backend is installed, so callers
    can fall back to the application's own restore area instead of silently
    doing nothing (the previous version swallowed this).
    """
    fn = _send2trash()
    if fn is None:
        raise TrashUnavailable("send2trash is not installed")
    fn(str(path))


#: Explorer's per-user verbs: a right-clicked folder (%1), and the empty
#: background of an open one (%V). Under HKCU, so no administrator rights.
FOLDER_MENU_KEYS = (
    (r"Software\Classes\Directory\shell\Qingjian", "%1"),
    (r"Software\Classes\Directory\Background\shell\Qingjian", "%V"),
)


def _registry(registry):
    if registry is not None:
        return registry
    import winreg                        # Windows only; callers check IS_WINDOWS
    return winreg


def set_folder_menu(enabled: bool, label: str = "", command=(), registry=None) -> None:
    """Add or remove "Open with Qingjian" on folders in Explorer, for this user."""
    reg = _registry(registry)
    for key, placeholder in FOLDER_MENU_KEYS:
        if not enabled:
            for path in (key + "\\command", key):       # a key with subkeys cannot go first
                try:
                    reg.DeleteKey(reg.HKEY_CURRENT_USER, path)
                except FileNotFoundError:
                    continue
            continue
        values = ((key, {"": label, "Icon": command[0]}),
                  (key + "\\command",
                   {"": " ".join(f'"{part}"' for part in [*command, placeholder])}))
        for path, entries in values:
            handle = reg.CreateKey(reg.HKEY_CURRENT_USER, path)
            try:
                for name, value in entries.items():
                    reg.SetValueEx(handle, name, 0, reg.REG_SZ, value)
            finally:
                reg.CloseKey(handle)


def folder_menu_installed(registry=None) -> bool:
    try:
        reg = _registry(registry)
        reg.CloseKey(reg.OpenKey(reg.HKEY_CURRENT_USER, FOLDER_MENU_KEYS[0][0] + "\\command"))
    except (ImportError, OSError):
        return False
    return True


def launch_command(frozen: bool, executable: str, script: str) -> list[str]:
    """What Explorer should run to open a folder in this program.

    A source checkout runs under pythonw.exe when there is one, so a folder
    opened from the menu does not bring a console window with it.
    """
    if frozen:
        return [executable]
    windowed = Path(executable).with_name("pythonw.exe")
    return [str(windowed if windowed.is_file() else Path(executable)), script]


def reveal(path: str | Path) -> bool:
    """Show *path* in the system file manager. Returns False if it could not."""
    target = Path(path)
    try:
        if IS_WINDOWS:
            # explorer.exe wants the odd "/select," token as one argument and
            # returns a non-zero exit code even on success, so ignore the code.
            subprocess.run(["explorer.exe", f"/select,{target}"], check=False)
            return True
        if IS_MACOS:
            subprocess.run(["open", "-R", str(target)], check=False)
            return True
        opener = shutil.which("xdg-open")
        if not opener:
            return False
        subprocess.run([opener, str(target.parent)], check=False)
        return True
    except (OSError, ValueError):
        return False


def hide(path: str | Path) -> None:
    """Set the hidden attribute on Windows. Elsewhere a leading dot already hides it."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        attributes = kernel32.GetFileAttributesW(str(path))
        if attributes != 0xFFFFFFFF:                    # INVALID_FILE_ATTRIBUTES
            kernel32.SetFileAttributesW(str(path), attributes | 0x2)
    except (OSError, AttributeError):
        pass


def animations_enabled() -> bool:
    """Whether the user left Windows' "Show animations" on. True elsewhere."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        value = wintypes.BOOL(True)
        # SPI_GETCLIENTAREAANIMATION
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0):
            return bool(value.value)
    except (OSError, AttributeError, ValueError):
        pass
    return True


def nearest_existing(path: str | Path) -> Path:
    """Walk up from *path* until something exists. Used before a target is made."""
    current = Path(path).absolute()
    seen = 0
    while not current.exists() and current.parent != current and seen < 64:
        current = current.parent
        seen += 1
    return current


def volume_id(path: str | Path):
    """An opaque identifier for the volume holding *path*, or None.

    On Windows ``st_dev`` is the volume serial number, on POSIX the device id;
    either way two paths with the same value can be renamed between.
    """
    try:
        return nearest_existing(path).stat().st_dev
    except OSError:
        return None


def same_volume(a: str | Path, b: str | Path) -> bool:
    left = volume_id(a)
    right = volume_id(b)
    return left is not None and left == right


def free_space(path: str | Path) -> int | None:
    try:
        return shutil.disk_usage(nearest_existing(path)).free
    except OSError:
        return None


def filesystem_is_case_insensitive(path: str | Path) -> bool:
    """Probe rather than assume: a case-sensitive volume can be mounted on
    Windows, and macOS can be formatted either way."""
    base = nearest_existing(path)
    try:
        probe = base / ".qingjian-case-probe"
        alt = base / ".QINGJIAN-CASE-PROBE"
        probe.touch(exist_ok=True)
        try:
            return alt.exists()
        finally:
            probe.unlink(missing_ok=True)
    except OSError:
        return IS_WINDOWS or IS_MACOS


def path_equal(a: str | Path, b: str | Path, case_insensitive: bool | None = None) -> bool:
    if case_insensitive is None:
        case_insensitive = IS_WINDOWS or IS_MACOS
    left, right = str(a), str(b)
    if case_insensitive:
        return os.path.normcase(left).casefold() == os.path.normcase(right).casefold()
    return left == right


def is_reserved_name(name: str) -> bool:
    stem = name.split(".", 1)[0].strip().casefold()
    return stem in WINDOWS_RESERVED
