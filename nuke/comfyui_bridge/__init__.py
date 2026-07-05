"""Nuke <-> ComfyUI bridge plugin.

All Nuke API access is isolated in `napi` (see node.py) so this package imports
cleanly outside Nuke for syntax checks.
"""

__version__ = "0.1.0"

from .settings import (  # noqa: F401
    SETTINGS_PATH,
    DEFAULT_SETTINGS,
    load_settings,
    save_settings,
)

__all__ = [
    "SETTINGS_PATH",
    "DEFAULT_SETTINGS",
    "load_settings",
    "save_settings",
    "__version__",
]
