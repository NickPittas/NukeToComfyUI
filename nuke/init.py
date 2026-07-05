"""Nuke plugin-path setup for NukeToComfyUI.

Nuke runs init.py in all modes. Keep this file to path setup only; UI entries
belong in menu.py.
"""

from __future__ import annotations

import os

import nuke

_ROOT = os.path.dirname(__file__)

for _subdir in ("icons", "nodes"):
    _path = os.path.join(_ROOT, _subdir)
    if os.path.isdir(_path) and _path not in nuke.pluginPath():
        nuke.pluginAddPath(_path, addToSysPath=False)
