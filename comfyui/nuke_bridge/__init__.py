"""Nuke bridge ComfyUI custom nodes.

Drop this package (or a symlink) into ComfyUI's `custom_nodes/` directory.
"""

from .nodes import FromNuke, ToNuke

NODE_CLASS_MAPPINGS = {
    "FromNuke": FromNuke,
    "ToNuke": ToNuke,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FromNuke": "Nuke Bridge: From Nuke",
    "ToNuke": "Nuke Bridge: To Nuke",
}

# Serve the frontend extension at custom_nodes/nuke_bridge/web/nuke_bridge.js.
WEB_DIRECTORY = "web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
    "FromNuke",
    "ToNuke",
]


def _register_routes() -> None:
    """Register /nuke_bridge/* routes on ComfyUI's PromptServer, best effort."""
    try:
        from server import PromptServer  # type: ignore
        from . import workflow_registry
        workflow_registry.add_routes(PromptServer.instance)
    except Exception:
        # Outside ComfyUI or routes already registered; non-fatal.
        pass


_register_routes()
