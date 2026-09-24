"""Qt-free command dispatch shared by source and module entry points."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--selfcheck" in argv:
        from .selfcheck import run
        return run(argv[argv.index("--selfcheck") + 1:])
    from .ui.app import main as run_app
    return run_app(argv)
