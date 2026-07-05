"""GUI menu registration for NukeToComfyUI.

Nuke runs menu.py only in interactive sessions. Keep this file to UI commands:
the user should only need `nuke.pluginAddPath('/path/to/repo/nuke')` in their
~/.nuke/init.py.
"""

from __future__ import annotations

import nuke

from comfyui_bridge import node, server


def _create_bridge_node():
    server.autostart_if_in_nuke()
    return node.create_bridge_node()


def _restart_bridge_server():
    server.start_server()


_nodes = nuke.menu("Nodes")
_nodes.addCommand("ComfyUI/ComfyUIBridge", _create_bridge_node)

_nuke_menu = nuke.menu("Nuke")
_bridge_menu = _nuke_menu.addMenu("ComfyUI Bridge")
_bridge_menu.addCommand("Restart Local Server", _restart_bridge_server)

# Start once in GUI sessions so ComfyUI can pull from existing bridge nodes.
server.autostart_if_in_nuke()
