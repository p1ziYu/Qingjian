"""Which rows a lazily-loaded list should actually fetch.

The arithmetic lives here rather than in the widget so that the off-by-one
risks in it are covered by tests. Qt supplies the first and last row it is
currently showing; everything else is decided by these functions.
"""
from __future__ import annotations


def band(first: int, last: int, total: int, overscan: int = 12) -> tuple[int, int]:
    """Inclusive row range to fetch for a viewport showing *first*..*last*.

    A little overscan either side means a slow scroll always meets tiles that
    are already there. An empty list yields an empty band.
    """
    if total <= 0:
        return (0, -1)
    if last < first:
        last = first
    low = max(0, min(first, total - 1) - max(0, overscan))
    high = min(total - 1, max(0, last) + max(0, overscan))
    return (low, high)


def window(index: int, total: int, radius: int) -> tuple[int, int]:
    """Half-open range of rows to keep loaded around *index*.

    Used by the filmstrip, which is a way to glance at neighbours rather than
    a way to scrub through fifty thousand items.
    """
    if total <= 0:
        return (0, 0)
    index = max(0, min(index, total - 1))
    radius = max(0, radius)
    if total <= 2 * radius + 1:
        # It all fits: load it once and stop thinking about windows.
        return (0, total)
    return (max(0, index - radius), min(total, index + radius + 1))


def needs_rebuild(index: int, loaded: tuple[int, int], margin: int = 8,
                  total: int | None = None) -> bool:
    """Is *index* close enough to the edge of *loaded* to want a new window?

    ``loaded`` is half-open, as :func:`window` returns it. When *total* is given
    and the window already spans the whole list there is nothing left to load,
    so a short folder is built once and then left alone.
    """
    low, high = loaded
    if high <= low:
        return True
    if total is not None and low <= 0 and high >= total:
        return False
    if index - margin < low and low > 0:
        return True
    if total is None:
        return index + margin >= high
    # Sitting against the end of the queue is not a reason to rebuild:
    # the window already holds everything there is on that side.
    return index + margin >= high and high < total


def clamp_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(index, total - 1))
