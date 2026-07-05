"""On-demand frame rendering for /frame requests.

Phase 1: PNG8 RGBA only. Mask modes `source alpha` / `invert source alpha` are
implemented by Nuke's own alpha handling. `mask input` modes require composing
the mask input's luma/alpha into the carrier alpha — that path is left as a
NotImplementedError with a TODO so we don't ship an untested Nuke tree.

All Nuke API access is on the main thread via `napi`.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Tuple

from . import napi


def _resolve_frame(requested: int) -> int:
    if requested is None or requested < 0:
        return napi.root_frame()
    return int(requested)


def render_frame_png(
    bridge_node: Any,
    frame: int,
    mask_source: str,
    colorspace: str,
) -> Tuple[bytes, int, int]:
    """Render input 0 of `bridge_node` at `frame` to PNG RGBA bytes.

    Returns (png_bytes, width, height).
    """
    nuke: Any = napi._nuke
    tmp_path = tempfile.NamedTemporaryFile(
        prefix="comfyui_bridge_", suffix=".png", delete=False
    ).name

    def _render() -> Tuple[int, int]:
        # Build a temp Write tree off the bridge's input 0.
        src = bridge_node.input(0)
        if src is None:
            raise napi.NukeError("ComfyUIBridge input 0 is not connected")

        tree_input = src
        temp_nodes = []

        if mask_source in ("mask input", "invert mask input"):
            raise NotImplementedError(
                "mask input modes are not implemented in Phase 1; "
                "use 'source alpha' or 'invert source alpha'. "
                "TODO: compose mask input luma into carrier alpha."
            )

        # ponytail: source alpha / invert source alpha handled by a Shuffle +
        # Invert subtree; keep minimal.
        chain = tree_input
        if mask_source == "invert source alpha":
            inv = nuke.nodes.Invert(inputs=[chain], channels="alpha")
            temp_nodes.append(inv)
            chain = inv

        if colorspace == "sRGB":
            # ponytail: explicit sRGB write via a Colorspace node if available,
            # else rely on Write colorspace knob; minimal.
            cs = nuke.nodes.Colorspace(inputs=[chain])
            temp_nodes.append(cs)
            try:
                cs.knob("colorspace_in").setValue("raw")
                cs.knob("colorspace_out").setValue("sRGB")
            except Exception:
                pass
            chain = cs

        write = nuke.nodes.Write(inputs=[chain])
        temp_nodes.append(write)
        try:
            try:
                write.knob("file_type").setValue("png")
            except Exception:
                pass
            write.knob("file").setValue(tmp_path)
            try:
                write.knob("channels").setValue("rgba")
            except Exception:
                pass
            nuke.execute(write, frame, frame)

            # Width/height from the source's format.
            fmt = src.format()
            return int(fmt.width()), int(fmt.height())
        finally:
            for node in reversed(temp_nodes):
                try:
                    nuke.delete(node)
                except Exception:
                    pass

    try:
        width, height = napi.call(_render)
        with open(tmp_path, "rb") as fh:
            data = fh.read()
        return data, width, height
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
