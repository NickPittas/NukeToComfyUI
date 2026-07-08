"""Nuke plugin-path setup for NukeToComfyUI.

Nuke runs init.py in all modes (GUI, terminal, render fanout). Keep this file
to path setup + non-UI lifecycle hooks; toolbar/Tab-menu entries belong in
menu.py.
"""

from __future__ import annotations

import os

import nuke

_ROOT = os.path.dirname(__file__)

for _subdir in ("icons", "nodes"):
    _path = os.path.join(_ROOT, _subdir)
    if os.path.isdir(_path) and _path not in nuke.pluginPath():
        nuke.pluginAddPath(_path, addToSysPath=False)

# Register the ComfyUIBridge onCreate callback in all modes so the gizmo gets
# its dynamic defaults (bridge_id uuid + host/port from settings) whether it's
# created interactively, pasted, or loaded from a script. No-op outside Nuke.
try:
    from comfyui_bridge import callbacks as _cb
    _cb.register()
except Exception:
    # Importing the package must never break Nuke startup.
    pass

# Register the OmniPaintRemove onCreate callback (separate node, same lifecycle
# rules). Idempotent; no-op outside Nuke.
try:
    from omnipaint_remove import callbacks as _opr_cb
    _opr_cb.register()
except Exception:
    pass
