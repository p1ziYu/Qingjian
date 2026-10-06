"""Application bootstrap: one instance, one style, one window."""
from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QSysInfo, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .. import __app_name__, __display_name__, __organization__, __version__
from ..core import appdirs, config, logsetup
from ..core.engine import Engine
from ..core.i18n import set_language, tr
from ..core.logsetup import get_logger
from . import icons, theme
from .prompts import critical, warning

log = get_logger("app")

#: Qt's way of saying the lock file could not be created at all. Read once,
#: here, so that a test standing in for QLockFile cannot change its meaning.
_LOCK_DENIED = (QLockFile.LockError.PermissionError, QLockFile.LockError.UnknownError)

#: Every spelling of this machine's name, folded, because Qt writes the lock's
#: host name in whatever case the system hands it and compares it in another.
_THIS_MACHINE = frozenset(name.casefold() for name in
                          (QSysInfo.machineHostName(), platform.node(),
                           os.environ.get("COMPUTERNAME", "")) if name)


def create_app(argv: list[str] | None = None) -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName(__display_name__)
    app.setOrganizationName(__organization__)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(icons.app_icon())
    _load_fonts(app)
    theme.apply(app)
    return app


def _load_fonts(app: QApplication) -> None:
    """Offscreen Qt has no system font discovery, and some Windows installs
    lack the interface font, so register a CJK-capable face explicitly."""
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [
        fonts / "msyh.ttc",
        fonts / "msyhbd.ttc",
        fonts / "bahnschrift.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    # Register when Qt cannot discover fonts itself (offscreen) or when the
    # interface font may simply be absent (Windows installs vary).
    if app.platformName() != "offscreen" and sys.platform != "win32":
        return
    for path in candidates:
        try:
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        except OSError:
            continue


def doorbell_name(data_dir: Path) -> str:
    """One name per data directory: the same unit the lock file guards."""
    key = str(Path(data_dir).resolve()).casefold().encode("utf-8")
    return "qingjian-" + hashlib.sha1(key).hexdigest()[:16]


def hand_over(name: str, folder: str, timeout_ms: int = 1500) -> bool:
    """Give *folder* to the copy already running. False when nothing answers.

    Waits for that copy to acknowledge before hanging up. A local socket closed
    straight after writing loses its message before the other side reads it,
    and on Windows the other side hanging up is not reliably noticed here.
    """
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(folder.encode("utf-8") + b"\n")
    socket.flush()
    acknowledged = socket.waitForReadyRead(timeout_ms) and bytes(socket.readAll()).startswith(b"ok")
    socket.disconnectFromServer()
    return acknowledged


def _hand_over_with_retry(name: str, folder: str, timeout: float = 4.0,
                          interval: float = 0.15) -> bool:
    """Wait briefly for a first instance that has its lock but not its listener."""
    deadline = time.monotonic() + timeout
    while True:
        if hand_over(name, folder, timeout_ms=max(50, int(interval * 1000))):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _clear_read_only(path: Path) -> bool:
    """Drop a read-only attribute from *path*.

    False when there was none to drop or it would not come off, so that the
    caller does not retry for nothing. A portable folder copied off a CD or a
    read-only share brings its leftover lock file along with the attribute
    set; the process that file names is long gone, so Qt rules the lock stale,
    but it may not delete a read-only file.
    """
    try:
        if not path.exists() or os.access(path, os.W_OK):
            return False
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        return False
    return True


def _process_alive(pid: int) -> bool:
    """Whether a process with this id is running on this machine.

    ``os.kill(pid, 0)`` must not be used on Windows: CPython implements it
    with ``TerminateProcess``, so asking would kill the very copy we are
    asking about. An access-denied answer still means the process exists.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:                     # someone else's: still running
            return True
        except OSError:                             # pragma: no cover - unexpected
            return True
        return True
    import ctypes
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5     # access denied: it is there
        code = ctypes.c_ulong(0)
        read = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return not read or code.value == 259        # STILL_ACTIVE
    except (AttributeError, OSError, ValueError):    # pragma: no cover - no kernel32
        return True


def _lock_is_held(lock: QLockFile, path: Path) -> bool:
    """True when a process that is still running wrote this lock file.

    This is the question Qt's error code cannot answer: ``LockFailedError``
    means both "a live copy holds it" and "it is stale and I could not delete
    it". Permissions cannot answer it either -- deleting a file needs the
    DELETE right, which is not the one ``os.chmod`` asks about, so a folder
    that refuses one may well allow the other, in both directions. The lock
    file itself names its owner, so the owner is asked. Anything that cannot
    be read is treated as held: a lock we do not understand is never cleared.
    """
    if path.exists() and not path.is_file():
        return False                                # not a lock file at all
    try:
        pid, host, _app = lock.getLockInfo()
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - older Qt
        return True
    if not pid:
        return False                                # unreadable or half-written
    if host and host.casefold() not in _THIS_MACHINE:
        return True                                 # another machine on a share
    return _process_alive(int(pid))


def _clear_stale_lock(path: Path) -> bool:
    """Move a lock file whose owner is gone out of the way, and delete it.

    Asks the operation instead of asking about permissions: renaming needs
    exactly the right that deleting needs, so it either works or says so. On
    Windows it also cannot touch a lock a live copy holds -- that file is open
    without delete sharing, so ``os.replace`` fails with a sharing violation.
    """
    if not path.is_file():
        return False
    for leftover in path.parent.glob(f"{path.name}.stale-*"):
        try:                                        # one an earlier run could not delete
            leftover.unlink()
        except OSError:
            continue
    spare = path.with_name(f"{path.name}.stale-{os.getpid()}")
    try:
        os.replace(path, spare)
    except OSError:
        return False
    try:
        spare.unlink()
    except OSError:                                 # out of the way is enough
        pass
    return True


def _take_lock(lock: QLockFile, path: Path) -> bool:
    """Take the single-instance lock, repairing the obstacles we can.

    Neither repair can steal a lock a live copy holds: clearing the read-only
    attribute still leaves the owner alive, and a stale lock is only cleared
    once its owner has been asked for and found gone.
    """
    if lock.tryLock(50):
        return True
    if _clear_read_only(path) and lock.tryLock(50):
        return True
    if not _lock_is_held(lock, path) and _clear_stale_lock(path):
        return lock.tryLock(50)
    return False


def _lock_problem(lock: QLockFile, path: Path) -> str:
    """The message key for a lock that could not be taken, or ``""``.

    Empty means a copy really is running and should be handed the folder.
    Saying "already running" for anything else sends the user looking for a
    window that does not exist, and the condition never clears by itself.
    """
    if path.exists() and not path.is_file():
        # A bad unzip of a backup, a sync-client conflict, a robocopy of the
        # data folder: nothing in the product would ever remove this, and
        # every launch would blame a window that cannot exist.
        return "error.lock_occupied"
    if lock.error() in _LOCK_DENIED:
        return "error.lock_unreachable"
    if _lock_is_held(lock, path):
        return ""
    return "error.lock_unreachable"


class Doorbell(QObject):
    """Listens for later launches, so a folder opened from Explorer reaches this window.

    Without it the second copy could only say that one was already running.
    """

    arrived = Signal(str)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        QLocalServer.removeServer(name)            # a name left behind by a crash
        if not self._server.listen(name):
            # The name itself is never logged. It is an unsalted digest of the
            # data directory's whole path, every part of which is fixed and
            # public except the account name, so one log line carrying it
            # would hand a bundle's reader the one thing every other mask in
            # it takes out -- and it cannot be salted, because both copies
            # have to arrive at the same name. What a failure here needs is
            # Qt's reason, not the name.
            log.error("local hand-over listener failed: %s", self._server.errorString())
        self._server.newConnection.connect(self._answer)

    def _answer(self) -> None:
        while self._server.hasPendingConnections():
            self._listen_to(self._server.nextPendingConnection())

    def _listen_to(self, connection: QLocalSocket) -> None:
        """Read one line, acknowledge it, pass it on. The caller hangs up."""
        received = bytearray()
        finished: list[bool] = []

        def finish() -> None:
            if finished:
                return
            finished.append(True)
            self.arrived.emit(received.decode("utf-8", "replace"))

        def read() -> None:
            if finished:
                return
            received.extend(bytes(connection.readAll()))
            if b"\n" in received:
                del received[received.index(b"\n"):]
                # Before passing it on: opening a large folder takes long enough
                # that the caller would give up waiting.
                connection.write(b"ok\n")
                connection.flush()
                finish()

        connection.readyRead.connect(read)
        connection.disconnected.connect(finish)
        connection.disconnected.connect(connection.deleteLater)
        read()

    def close(self) -> None:
        self._server.close()


def _parse(argv: list[str]) -> dict:
    options = {"data_dir": None, "language": "", "folder": "", "version": False}
    rest = list(argv[1:])
    while rest:
        item = rest.pop(0)
        if item in ("--version", "-V"):
            options["version"] = True
        elif item == "--data-dir" and rest:
            options["data_dir"] = Path(rest.pop(0))
        elif item == "--lang" and rest:
            options["language"] = rest.pop(0)
        elif not item.startswith("-"):
            if item.endswith('"'):
                item = item[:-1] + "\\"
            options["folder"] = item
    return options


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    options = _parse(argv)
    if options["version"]:
        print(f"{__display_name__} (Qingjian) {__version__}")
        return 0
    if options["data_dir"]:
        os.environ[appdirs.ENV_VAR] = str(options["data_dir"])

    if options["folder"]:
        options["folder"] = os.path.abspath(options["folder"])

    app = create_app(argv)

    # The data directory decides where the lock file goes, so it is settled
    # first -- and without raising: the frozen build hides its console, so an
    # OSError escaping here is a double-click that does nothing at all.
    try:
        data_root, refused = appdirs.usable_data_dir()
    except appdirs.DataDirUnusable as error:
        set_language(options["language"])           # before anything the user reads
        critical(None, __display_name__, tr(
            "error.data_dir_unwritable",
            paths="\n".join(str(path) for path in error.paths)))
        return 1
    os.environ[appdirs.ENV_VAR] = str(data_root)

    # Loaded before the lock is taken, because every message below this line
    # has to come out in the user's own language.
    settings = config.load()
    if options["language"]:
        settings.language = options["language"]
    set_language(settings.language)

    # And the log is opened before any of it, because the frozen build hides
    # its console: a start-up event logged before this line reaches nothing but
    # `logging.lastResort`, so the one fact that explains such a session would
    # be missing from the very bundle the user is asked to attach. `configure`
    # is safe to call twice and the engine calls it again below.
    logsetup.configure(settings.logging_enabled, settings.log_days,
                       directory=data_root / "logs")

    # One instance per data directory: two copies sorting the same library
    # would race on the journal.
    lock_file = appdirs.lock_path()
    lock = QLockFile(str(lock_file))
    lock.setStaleLockTime(0)
    doorbell = doorbell_name(data_root)
    if not _take_lock(lock, lock_file):
        problem = _lock_problem(lock, lock_file)
        if problem:
            log.error("%s: %s (Qt error %s)", problem, lock_file, lock.error())
            # One quick ring even so: being told nobody is running while a copy
            # is would cost the user the folder they opened, so the verdict is
            # never the last word.
            if hand_over(doorbell, str(options["folder"] or ""), timeout_ms=300):
                return 0
            critical(None, __display_name__, tr(problem, path=str(lock_file)))
            return 1
        # Explorer's "Open with Qingjian" starts a second copy; hand the folder
        # to the first one and bow out quietly.
        if _hand_over_with_retry(doorbell, str(options["folder"] or "")):
            return 0
        warning(None, __display_name__, tr("error.single_instance"))
        return 1

    if refused is not None:
        # Said out loud, naming both places: data must never end up somewhere
        # its owner cannot find it. Said here rather than earlier so that a
        # second copy handing over a folder stays silent.
        log.warning("data directory %s will not hold data; using %s", refused, data_root)
        note = tr("error.data_dir_volatile") if appdirs.is_volatile(data_root) else ""
        warning(None, __display_name__,
                tr("error.data_dir_moved", requested=str(refused),
                   used=str(data_root), note=("\n" + note if note else "")))
    else:
        # The run before this one may have been refused this very directory and
        # written a whole second library elsewhere. Nothing used to mention it
        # again, so that work was stranded where nobody would look.
        stranded = appdirs.stranded_data(data_root)
        if stranded is not None:
            log.warning("earlier data left behind in %s", stranded)
            warning(None, __display_name__,
                    tr("error.data_dir_stranded", path=str(stranded)))

    theme.apply(app, settings.density)

    try:
        engine = Engine(data_root, settings)
    except OSError:
        # A folder that took the write probe can still refuse a sub-directory
        # or a deletion, and a raw "[WinError 5] Access is denied: ..." in a
        # box is neither translated nor advice.
        log.exception("engine failed to start")
        critical(None, __display_name__,
                 tr("error.data_dir_unwritable", paths=str(data_root)))
        lock.unlock()
        return 1
    except Exception as error:                      # pragma: no cover - startup failure
        log.exception("engine failed to start")
        critical(None, __display_name__, str(error))
        lock.unlock()
        return 1

    from .mainwindow import MainWindow
    window = MainWindow(engine)
    if options["folder"] and Path(options["folder"]).is_dir():
        # Handed to the window rather than opened here: opening now would run a
        # scan (which pumps events) before exec(), letting the queued start-up
        # timer fire re-entrantly in the middle of it.
        window.startup_folder = Path(options["folder"])
    listener = Doorbell(doorbell, window)
    listener.arrived.connect(window.open_requested)
    window.show()
    try:
        return app.exec()
    finally:
        lock.unlock()
