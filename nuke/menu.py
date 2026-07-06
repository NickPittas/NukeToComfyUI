"""GUI menu registration for NukeToComfyUI.

Nuke runs menu.py only in interactive sessions. The user only needs
`nuke.pluginAddPath('/path/to/repo/nuke')` in their ~/.nuke/init.py; the gizmo
under `nodes/ComfyUIBridge.gizmo` is auto-discovered as a node class.

The Tab-menu/toolbar command calls native `nuke.createNode('ComfyUIBridge')`,
so Nuke handles input attachment and graph placement — no Python-side
setInput/xpos/ypos. The onCreate callback (registered in init.py) fills
dynamic defaults.
"""

from __future__ import annotations

import nuke
import os

from comfyui_bridge import callbacks, node as bridge_node, server


def _create_bridge_node():
    # Best-effort: make sure the local bridge HTTP server is up before the first
    # frame pull arrives. Then create the real gizmo class. Do not fall back to
    # a Python-created Group here: if the gizmo is not discoverable, we want the
    # error to be visible instead of silently getting old placement behavior.
    server.autostart_if_in_nuke()
    callbacks.register()
    created = nuke.createNode("ComfyUIBridge")
    bridge_node.refresh_colorspace_choices(created)
    return created


def _restart_bridge_server():
    server.start_server()


# Defensive path setup: user pluginAddPath('/path/to/repo/nuke') should run our
# init.py, but menu.py also ensures the gizmo directory is discoverable before
# registering the command.
_nodes_dir = os.path.join(os.path.dirname(__file__), "nodes")
if os.path.isdir(_nodes_dir) and _nodes_dir not in nuke.pluginPath():
    nuke.pluginAddPath(_nodes_dir, addToSysPath=False)

_nodes = nuke.menu("Nodes")
_nodes.addCommand(
    "ComfyUI/ComfyUIBridge",
    _create_bridge_node,
    icon="ComfyUIBridge.png",
)

_nuke_menu = nuke.menu("Nuke")
_bridge_menu = _nuke_menu.addMenu("ComfyUI Bridge")
_bridge_menu.addCommand("Restart Local Server", _restart_bridge_server)

# Start once in GUI sessions so ComfyUI can pull from existing bridge nodes.
server.autostart_if_in_nuke()
