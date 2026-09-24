"""``python -m qingjian``."""
from __future__ import annotations

def main() -> int:
    from .entry import main as dispatch
    return dispatch()


if __name__ == "__main__":
    raise SystemExit(main())
