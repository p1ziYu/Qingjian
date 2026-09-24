"""Application bootstrap: one instance, one style, one window."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .. import __app_name__, __display_name__, __organization__, __version__
from ..core import appdirs, config
from ..core.engine import Engine
from ..core.i18n import set_language, tr
from ..core.logsetup import get_logger
from . import icons, theme
from .prompts import critical, warning

log = get_logger("app")


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


class Doorbell(QObject):
    """Listens for later launches, so a folder opened from Explorer reaches this window.

    Without it the second copy could only say that one was already running.
    """

    arrived = Signal(str)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        QLocalServer.removeServer(name)            # a name left behind by a crash
        self._server.listen(name)
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
    options = {"data_dir": None, "language": "", "folder": "", "selfcheck": False,
               "version": False}
    rest = list(argv[1:])
    while rest:
        item = rest.pop(0)
        if item in ("--version", "-V"):
            options["version"] = True
        elif item == "--selfcheck":
            options["selfcheck"] = True
        elif item == "--data-dir" and rest:
            options["data_dir"] = Path(rest.pop(0))
        elif item == "--lang" and rest:
            options["language"] = rest.pop(0)
        elif not item.startswith("-"):
            options["folder"] = item
    return options


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    options = _parse(argv)
    if options["version"]:
        print(f"{__display_name__} (Qingjian) {__version__}")
        return 0
    if options["selfcheck"]:
        from ..selfcheck import run as run_selfcheck
        return run_selfcheck(argv[argv.index("--selfcheck") + 1:])

    if options["data_dir"]:
        os.environ[appdirs.ENV_VAR] = str(options["data_dir"])

    app = create_app(argv)

    # One instance per data directory: two copies sorting the same library
    # would race on the journal.
    lock = QLockFile(str(appdirs.lock_path()))
    lock.setStaleLockTime(0)
    doorbell = doorbell_name(appdirs.data_dir())
    if not lock.tryLock(50):
        # Explorer's "Open with Qingjian" starts a second copy; hand the folder
        # to the first one and bow out quietly.
        if hand_over(doorbell, str(options["folder"] or "")):
            return 0
        warning(None, __display_name__, tr("error.single_instance"))
        return 1

    settings = config.load()
    if options["language"]:
        settings.language = options["language"]
    set_language(settings.language)
    theme.apply(app, settings.density)

    try:
        engine = Engine(appdirs.data_dir(), settings)
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
