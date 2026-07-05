"""Top-level Nuke init entry point.

Copy this file (or symlink it) to ~/.nuke/menu.py, or append:
    import sys; sys.path.insert(0, "/abs/path/to/inpaint/nuke")
    import comfyui_bridge  # noqa: F401
    from comfyui_bridge import node as _cb_node
    _cb_node.register_node()
    from comfyui_bridge import server as _cb_server
    _cb_server.autostart_if_in_nuke()
"""

from __future__ import annotations

import sys


def _bootstrap() -> None:
    try:
        import nuke as _nuke  # noqa: F401
        # Avoid mistaking our own nuke/ directory (namespace package) for Nuke.
        if not hasattr(_nuke, "executeInMainThreadWithResult"):
            raise ImportError("not the real Nuke module")
    except Exception:
        # Not in Nuke; nothing to do.
        return

    # Make sure our package is importable when menu.py is loaded directly.
    here = __file__
    if here:
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(here))))

    try:
        from comfyui_bridge import node as _cb_node  # noqa: WPS433
        from comfyui_bridge import server as _cb_server  # noqa: WPS433
        _cb_node.register_node()
        _cb_server.autostart_if_in_nuke()
    except Exception as exc:  # pragma: no cover - Nuke console only
        import sys as _sys
        _sys.stderr.write(f"[comfyui_bridge] bootstrap failed: {exc!r}\n")


# Run only when interpreted by Nuke's python (nuke importable above).
_bootstrap()
