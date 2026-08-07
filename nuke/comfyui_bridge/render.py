"""On-demand frame rendering for /frame requests.

All four mask modes are composed in-tree with Nuke nodes (Copy, Invert,
Expression) — no PIL, no numpy, no Python pixel manipulation. EXR16 and
PNG8 both use the same node-tree approach; mask-input modes no longer
degrade to PNG.

All Nuke API access is on the main thread via `napi`.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from typing import Any, Optional, Tuple

from . import napi


_CACHE_LOCK = threading.RLock()
_CACHE: dict[tuple, tuple[float, bytes, int, int]] = {}
_CACHE_TTL_SECONDS = 300.0
_CACHE_MAX_ENTRIES = 24


def clear_cache() -> None:
    """Clear all cached frame exports."""
    with _CACHE_LOCK:
        _CACHE.clear()


def clear_cache_from_node(bridge_node: Any = None) -> None:
    """PyScript_Knob entrypoint: clear cached frame/video exports and session files.

    Session cleanup deletes only files tracked by this process (exact paths),
    never a directory scan.
    """
    clear_cache()
    try:
        from . import video
        video.clear_cache()
    except Exception:
        pass
    removed, failed = 0, 0
    try:
        from . import session_files
        removed, failed = session_files.clear()
    except Exception:
        pass
    if bridge_node is not None:
        try:
            parts = ["frame cache cleared"]
            if removed:
                parts.append(f"removed {removed} file(s)")
            if failed:
                parts.append(f"{failed} failed, kept for retry")
            napi.set_knob_value(bridge_node, "status", "; ".join(parts))
        except Exception:
            pass


def _cache_get(key: tuple) -> Optional[Tuple[bytes, int, int]]:
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        ts, data, width, height = entry
        if now - ts > _CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        return data, width, height


def _cache_set(key: tuple, data: bytes, width: int, height: int) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), data, width, height)
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)


def _node_name(node: Any) -> str:
    if node is None:
        return ""
    for name in ("fullName", "name"):
        fn = getattr(node, name, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:
                pass
    return str(node)


def _cache_key(bridge_node: Any, frame: int, mask_source: str, colorspace: str, fmt: str = "png8") -> tuple:
    """Build a cheap cache key on Nuke's main thread."""
    def _build() -> tuple:
        bridge_id = ""
        k = bridge_node.knob("bridge_id")
        if k is not None:
            bridge_id = str(k.value())
        src = bridge_node.input(0)
        mask = bridge_node.input(1)
        return (
            bridge_id,
            int(frame),
            str(mask_source),
            str(colorspace),
            str(fmt),
            _node_name(src),
            _node_name(mask),
        )
    return napi.call(_build)


def _resolve_frame(requested: int) -> int:
    if requested is None or requested < 0:
        return napi.root_frame()
    return int(requested)


def _set_write_colorspace(write: Any, colorspace: str) -> None:
    cs = str(colorspace or "").strip()
    if not cs:
        return
    knob = write.knob("colorspace")
    if knob is None:
        return
    try:
        values = [str(v) for v in list(knob.values()) if str(v)]
    except Exception:
        values = []
    if values and cs not in values:
        return
    try:
        knob.setValue(cs)
    except Exception:
        return


def _write_png(nuke: Any, src_node: Any, tmp_path: str, frame: int, temp_nodes: list, colorspace: str = "") -> None:
    """Render `src_node` to PNG (RGBA when available) at `frame`."""
    write = nuke.nodes.Write(inputs=[src_node])
    temp_nodes.append(write)
    try:
        write.knob("file_type").setValue("png")
    except Exception:
        pass
    write.knob("file").setValue(tmp_path)
    try:
        write.knob("channels").setValue("rgba")
    except Exception:
        pass
    _set_write_colorspace(write, colorspace)
    nuke.execute(write, frame, frame)


def _write_exr(nuke: Any, src_node: Any, tmp_path: str, frame: int, temp_nodes: list, colorspace: str = "") -> None:
    """Render `src_node` to half-float RGBA EXR at `frame`.

    Nuke Write knob names vary slightly across versions; we try the common
    ones and rely on sensible defaults otherwise. The important one is
    `datatype=half` for 16-bit float; if it can't be set, Nuke's default EXR
    type still produces a valid file (just possibly full float).
    """
    write = nuke.nodes.Write(inputs=[src_node])
    temp_nodes.append(write)
    try:
        write.knob("file_type").setValue("exr")
    except Exception:
        pass
    write.knob("file").setValue(tmp_path)
    # Half-float (16-bit). Older Nuke uses "datatype"; some use "datatype" with
    # value "half". Best effort — wrap each in try.
    for knob_name in ("datatype", "pixel_type"):
        try:
            write.knob(knob_name).setValue("half")
        except Exception:
            pass
    try:
        write.knob("channels").setValue("rgba")
    except Exception:
        pass
    _set_write_colorspace(write, colorspace)
    nuke.execute(write, frame, frame)


def _mask_chain(nuke: Any, bridge_node: Any, mask_source: str, temp_nodes: list) -> Any:
    """Build the in-tree mask composition chain. Returns the output node.

    All four mask modes use Nuke nodes only — no PIL, no numpy.
    """
    src = bridge_node.input(0)
    if src is None:
        raise napi.NukeError("ComfyUIBridge input 0 is not connected")

    chain = src

    if mask_source == "invert source alpha":
        inv = nuke.nodes.Invert(inputs=[chain], channels="alpha")
        temp_nodes.append(inv)
        chain = inv
    elif mask_source in ("mask input", "invert mask input"):
        mask_input = bridge_node.input(1)
        if mask_input is not None:
            copy = nuke.nodes.Copy(inputs=[chain, mask_input])
            copy.knob("from0").setValue("alpha")
            copy.knob("to0").setValue("alpha")
            temp_nodes.append(copy)
            chain = copy
        else:
            # Disconnected mask: all-keep (carrier alpha = 1).
            expr = nuke.nodes.Expression(inputs=[chain])
            expr.knob("expr3").setValue("1")
            temp_nodes.append(expr)
            chain = expr
        if mask_source == "invert mask input":
            inv = nuke.nodes.Invert(inputs=[chain], channels="alpha")
            temp_nodes.append(inv)
            chain = inv

    return chain


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
    key = _cache_key(bridge_node, frame, mask_source, colorspace, "png8")
    cached = _cache_get(key)
    if cached is not None:
        try:
            napi.set_knob_value(bridge_node, "status", f"cache hit: frame {frame}")
        except Exception:
            pass
        return cached

    src_path = tempfile.NamedTemporaryFile(
        prefix=f"{napi.comp_stem()}_source_", suffix=".png", delete=False
    ).name

    try:
        def _render() -> Tuple[bytes, int, int]:
            src = bridge_node.input(0)
            if src is None:
                raise napi.NukeError("ComfyUIBridge input 0 is not connected")

            temp_nodes: list = []
            try:
                chain = _mask_chain(nuke, bridge_node, mask_source, temp_nodes)
                _write_png(nuke, chain, src_path, frame, temp_nodes, colorspace)
                fmt = src.format()
                width, height = int(fmt.width()), int(fmt.height())
                with open(src_path, "rb") as fh:
                    data = fh.read()
                return data, width, height
            finally:
                for node in reversed(temp_nodes):
                    try:
                        nuke.delete(node)
                    except Exception:
                        pass

        data, width, height = napi.call(_render)
        _cache_set(key, data, width, height)
        try:
            napi.set_knob_value(bridge_node, "status", f"exported frame {frame}")
        except Exception:
            pass
        return data, width, height
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


def render_frame_exr(
    bridge_node: Any,
    frame: int,
    mask_source: str,
    colorspace: str,
) -> Tuple[bytes, int, int]:
    """Render input 0 of `bridge_node` at `frame` to half-float RGBA EXR bytes.

    Source-alpha modes are written natively as EXR. Mask-input modes can't be
    composed on the Nuke side without unverified Copy/Shuffle knob names, so
    they degrade to PNG compose (caller treats result as PNG). Returns
    (exr_bytes, width, height).
    """
    nuke: Any = napi._nuke
    key = _cache_key(bridge_node, frame, mask_source, colorspace, "exr16")
    cached = _cache_get(key)
    if cached is not None:
        try:
            napi.set_knob_value(bridge_node, "status", f"cache hit: frame {frame}")
        except Exception:
            pass
        return cached

    src_path = tempfile.NamedTemporaryFile(
        prefix=f"{napi.comp_stem()}_source_", suffix=".exr", delete=False
    ).name

    try:
        def _render() -> Tuple[bytes, int, int]:
            src = bridge_node.input(0)
            if src is None:
                raise napi.NukeError("ComfyUIBridge input 0 is not connected")

            temp_nodes: list = []
            try:
                chain = _mask_chain(nuke, bridge_node, mask_source, temp_nodes)
                _write_exr(nuke, chain, src_path, frame, temp_nodes, colorspace)
                fmt = src.format()
                width, height = int(fmt.width()), int(fmt.height())
                with open(src_path, "rb") as fh:
                    exr = fh.read()
                return exr, width, height
            finally:
                for node in reversed(temp_nodes):
                    try:
                        nuke.delete(node)
                    except Exception:
                        pass

        exr, width, height = napi.call(_render)
        _cache_set(key, exr, width, height)
        try:
            napi.set_knob_value(bridge_node, "status", f"exported EXR frame {frame}")
        except Exception:
            pass
        return exr, width, height
    finally:
        try:
            os.remove(src_path)
        except OSError:
            pass


def render_frame(
    bridge_node: Any,
    frame: int,
    mask_source: str,
    colorspace: str,
    fmt: str = "png8",
) -> Tuple[bytes, int, int, str]:
    """Render one frame in the requested format.

    Returns (data, width, height, actual_format). `actual_format` may differ
    from `fmt` when the requested path can't honor it (e.g. exr16 + mask-input
    mode degrades to png8).
    """
    fmt = (fmt or "png8").strip().lower()

    if fmt == "exr16":
        data, w, h = render_frame_exr(bridge_node, frame, mask_source, colorspace)
        return data, w, h, "exr16"

    data, w, h = render_frame_png(bridge_node, frame, mask_source, colorspace)
    return data, w, h, "png8"
