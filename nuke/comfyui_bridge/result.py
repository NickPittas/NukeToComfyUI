"""Handle /result: save bytes to the output dir, optionally create a Read node."""

from __future__ import annotations

import os
import time
from typing import Any

from . import napi


def _unique_path(output_directory: str, prefix: str, ext: str) -> str:
    os.makedirs(output_directory, exist_ok=True)
    # ponytail: ms-precision stamp + pid for uniqueness; collision-resistant.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"{prefix}_{stamp}_{os.getpid()}"
    candidate = os.path.join(output_directory, f"{base}.{ext}")
    i = 0
    while os.path.exists(candidate):
        i += 1
        candidate = os.path.join(output_directory, f"{base}_{i}.{ext}")
    return candidate


def save_result(
    body: bytes,
    output_directory: str,
    filename_prefix: str,
    colorspace: str,
    bridge_node: Any = None,
    create_read: bool = False,
    ext: str = "png",
) -> str:
    """Write `body` to a deterministic unique path; optionally add a Read node."""
    path = _unique_path(output_directory, filename_prefix or "comfy_result", ext)
    with open(path, "wb") as fh:
        fh.write(body)

    if create_read and bridge_node is not None and napi.has_nuke():
        nuke: Any = napi._nuke

        def _add_read() -> None:
            read = nuke.nodes.Read(file=path)
            if colorspace:
                k = read.knob("colorspace")
                cs = str(colorspace or "").strip()
                if k is not None and cs:
                    try:
                        values = [str(v) for v in list(k.values()) if str(v)]
                    except Exception:
                        values = []
                    if not values or cs in values:
                        try:
                            k.setValue(cs)
                        except Exception:
                            pass
            # Place the new Read visually next to the bridge node (to its
            # right with a small offset). Best effort — silently skip if any
            # xpos/ypos access is unavailable.
            try:
                def _g(node: Any, name: str) -> Any:
                    k = node.knob(name)
                    return k.value() if k is not None else None

                bx = _g(bridge_node, "xpos")
                by = _g(bridge_node, "ypos")
                # ponytail: prefer method API if present (some Nuke versions).
                for getter_name in ("xpos", "ypos"):
                    if hasattr(bridge_node, getter_name):
                        try:
                            val = getattr(bridge_node, getter_name)()
                            if getter_name == "xpos":
                                bx = val
                            else:
                                by = val
                        except Exception:
                            pass

                offset_x = 200
                offset_y = 0
                if isinstance(bx, (int, float)) and isinstance(by, (int, float)):
                    rk_x = read.knob("xpos")
                    rk_y = read.knob("ypos")
                    if rk_x is not None:
                        rk_x.setValue(int(bx) + offset_x)
                    if rk_y is not None:
                        rk_y.setValue(int(by) + offset_y)
            except Exception:
                pass

        napi.call(_add_read)

    if bridge_node is not None:
        napi.set_knob_value(bridge_node, "last_result", path)

    return path
