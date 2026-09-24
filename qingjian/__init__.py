"""轻拣 / Qingjian — local media sorter.

The package is split so that everything decidable without a GUI lives in
``qingjian.core`` and imports no Qt. ``qingjian.ui`` is a thin Qt layer on
top of it.
"""

__version__ = "2.0.5"
__app_name__ = "Qingjian"
__display_name__ = "轻拣"
__organization__ = "LocalMediaTools"

# Bumped whenever the on-disk state layout changes in a way that older
# builds cannot read. Written into state files so a future version can
# migrate instead of guessing.
STATE_SCHEMA = 3

__all__ = ["__version__", "__app_name__", "__display_name__", "__organization__", "STATE_SCHEMA"]
