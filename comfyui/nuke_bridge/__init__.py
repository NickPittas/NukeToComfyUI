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

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "FromNuke", "ToNuke"]
