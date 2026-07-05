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
            read = nuke.nodes.Read("file", file=path)
            try:
                if colorspace == "raw":
                    read.knob("colorspace").setValue("raw")
                else:
                    read.knob("colorspace").setValue("sRGB")
            except Exception:
                pass

        napi.call(_add_read)

    if bridge_node is not None:
        napi.set_knob_value(bridge_node, "last_result", path)

    return path
