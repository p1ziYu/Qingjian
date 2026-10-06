"""Shared test scaffolding."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))


class TempCase(unittest.TestCase):
    """A test with its own data directory, so nothing touches a real library."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self._previous = os.environ.get("QINGJIAN_DATA_DIR")
        os.environ["QINGJIAN_DATA_DIR"] = str(self.data)
        from qingjian.core import appdirs, metadata
        metadata.clear_cache()
        # ``QINGJIAN_DATA_DIR`` is not enough on its own: a data directory the
        # test makes unwritable sends the application to its fallback, and the
        # first fallback is the real per-user application data directory, which
        # on this machine holds a real library. Pointed inside the test's own
        # temporary directory so that cannot happen whatever a test stages.
        self.fallback = self.tmp / "fallback"
        self.real_fallback_roots = appdirs.fallback_roots
        appdirs.fallback_roots = lambda: [self.fallback]
        self.addCleanup(setattr, appdirs, "fallback_roots", self.real_fallback_roots)
        # Fault-injection tests deliberately provoke warnings; keep them out of
        # the test output so a real failure is easy to see.
        logging.getLogger("qingjian").setLevel(logging.CRITICAL)

    def tearDown(self) -> None:
        # unittest normally runs registered cleanups after tearDown. On
        # Windows that is too late for TemporaryDirectory: SQLite connections
        # registered with addCleanup still hold their files open here.
        self.doCleanups()
        if self._previous is None:
            os.environ.pop("QINGJIAN_DATA_DIR", None)
        else:
            os.environ["QINGJIAN_DATA_DIR"] = self._previous
        self._tmp.cleanup()

    def plain_launch(self, default: Path) -> Path:
        """Stage an ordinary launch: no ``QINGJIAN_DATA_DIR``, *default* used.

        ``QINGJIAN_DATA_DIR`` is explicit intent and is now honoured
        absolutely -- a run that sets it never falls back somewhere else -- so
        the fallback can only be exercised the way a double-clicking user meets
        it: through the platform's per-user directory, which is stood in for
        here by a path inside the test's own temporary directory.
        """
        from unittest.mock import patch
        from qingjian.core import appdirs
        os.environ.pop("QINGJIAN_DATA_DIR", None)
        for name, answer in (("_platform_data_root", lambda: Path(default)),
                             ("portable_dir", lambda: None)):
            patcher = patch.object(appdirs, name, answer)
            patcher.start()
            self.addCleanup(patcher.stop)
        return Path(default)

    def count_hashes(self) -> list[str]:
        """Record the name of every file hashed in full, until the test ends."""
        from qingjian.core import safestore
        seen: list[str] = []
        real = safestore.fingerprint

        def counting(path, *args, **kwargs):
            seen.append(Path(path).name)
            return real(path, *args, **kwargs)

        safestore.fingerprint = counting
        self.addCleanup(setattr, safestore, "fingerprint", real)
        return seen

    def write(self, path: Path, content: bytes = b"data") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def deny_writes(self, path: Path) -> Path:
        """Make *path* refuse to be written to, and undo it afterwards.

        For a directory this is what a standard user sees in
        ``C:\\Program Files``, on a read-only stick or share, and in an
        application-data folder a company policy has locked down: it is there,
        ``os.access`` still answers yes, and creating a file in it is refused.
        For a file it is a leftover lock file carrying an ACL that followed it
        off a share. A deny entry for this very account reproduces both
        without touching anything outside the test's own temporary directory;
        elsewhere the mode bits do the same job. Skips the test where the
        account can write anyway, since there the failure cannot be staged.
        """
        import getpass
        import subprocess
        a_file = path.is_file()
        if not a_file:
            path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            account = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}"
            self.addCleanup(subprocess.run,
                            ["icacls", str(path), "/remove:d", account],
                            capture_output=True, text=True)
            subprocess.run(["icacls", str(path), "/deny",
                            f"{account}:{'(W,D)' if a_file else '(WD,AD)'}"],
                           capture_output=True, text=True)
        else:
            mode = path.stat().st_mode
            self.addCleanup(os.chmod, path, mode)
            os.chmod(path, 0o444 if a_file else 0o555)
        try:
            if a_file:
                with open(path, "ab"):
                    pass
            else:
                probe = path / "write-probe"
                probe.touch()
                probe.unlink()
        except OSError:
            return path
        self.skipTest(f"this account can still write in {path}")
        return path                                     # pragma: no cover

    def deny_subdirectories(self, path: Path) -> Path:
        """Let *path* take files but refuse sub-directories, and undo it after.

        On NTFS add-file and add-subdirectory are separate rights, so this is a
        real folder shape, not a contrivance: it passes a write probe that only
        creates a file and then refuses ``store/``. Windows only -- nothing
        else separates the two rights, so there the test is skipped.
        """
        import getpass
        import subprocess
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.skipTest("only NTFS separates add-file from add-subdirectory")
        account = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}"
        self.addCleanup(subprocess.run,
                        ["icacls", str(path), "/remove:d", account],
                        capture_output=True, text=True)
        subprocess.run(["icacls", str(path), "/deny", f"{account}:(AD)"],
                       capture_output=True, text=True)
        try:
            (path / "subdir-probe").mkdir()
        except OSError:
            return path
        (path / "subdir-probe").rmdir()
        self.skipTest(f"this account can still create folders in {path}")
        return path                                     # pragma: no cover

    def tree(self, root: Path) -> list[str]:
        if not root.exists():
            return []
        return sorted(str(p.relative_to(root)).replace("\\", "/")
                      for p in root.rglob("*") if p.is_file())
