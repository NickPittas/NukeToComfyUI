"""On-demand frame rendering for /frame requests.

Phase 1 PNG8 RGBA: all four mask modes implemented; PIL composes mask-input
alpha into the carrier.

Phase 2 EXR16: half-float RGBA EXR via a temporary Nuke Write node.
  - source alpha / invert source alpha: native EXR write of a tiny in-Nuke
    tree, with the same carrier direction as PNG.
  - mask input / invert mask input: EXR-side channel compose would need Nuke
    Copy/Shuffle knob names we can't verify here, so this path degrades to the
    PNG compose + returns actual_format='png8'. Robust and minimal; documented
    in README.

All Nuke API access is on the main thread via `napi`.
"""

from __future__ import annotations

import io
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
    """PyScript_Knob entrypoint: clear cached frame exports."""
    clear_cache()
    if bridge_node is not None:
        try:
            napi.set_knob_value(bridge_node, "status", "frame cache cleared")
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
    if not colorspace:
        return
    write.knob("colorspace").setValue(str(colorspace))


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


def _pil_replace_alpha(source_png: bytes, mask_png: Optional[bytes], invert: bool) -> bytes:
    """Return `source_png` with its alpha replaced by the mask carrier alpha.

    `mask_png=None` means all-keep (carrier alpha = 1 everywhere).
    Otherwise the mask is taken from the mask PNG's alpha channel if it has
    meaningful variation, else from its luma (RGB average).
    """
    from PIL import Image  # local import; only needed on this path
    import numpy as np

    base = Image.open(io.BytesIO(source_png)).convert("RGBA")
    arr = np.asarray(base).astype(np.float32)
    h, w = arr.shape[0], arr.shape[1]

    if mask_png is None:
        carrier = np.ones((h, w), dtype=np.float32)
    else:
        mimg = Image.open(io.BytesIO(mask_png))
        if mimg.mode not in ("RGBA", "RGB", "L"):
            mimg = mimg.convert("RGBA")
        marr = np.asarray(mimg).astype(np.float32) / 255.0
        mh, mw = marr.shape[0], marr.shape[1]
        if (mh, mw) != (h, w):
            resample = getattr(Image, "BILINEAR", 2)
            mimg = mimg.resize((w, h), resample)
            marr = np.asarray(mimg).astype(np.float32) / 255.0
            mh, mw = h, w

        # ponytail: "alpha if present" => mask PNG has an alpha channel that
        # actually varies across the image; otherwise treat as RGB mask and
        # use luma. A uniformly-opaque alpha means the source was effectively
        # RGB and luma is the meaningful signal.
        has_alpha = (
            marr.ndim == 3
            and marr.shape[2] == 4
            and float(marr[..., 3].max() - marr[..., 3].min()) > 1.0 / 255.0
        )
        if has_alpha:
            carrier = marr[..., 3]
        elif marr.ndim == 3:
            carrier = marr[..., :3].mean(axis=2)
        else:
            carrier = marr

    if invert:
        carrier = 1.0 - carrier

    arr[..., 3] = carrier * 255.0
    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGBA")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


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
    is_mask_mode = mask_source in ("mask input", "invert mask input")
    key = _cache_key(bridge_node, frame, mask_source, colorspace, "png8")
    cached = _cache_get(key)
    if cached is not None:
        try:
            napi.set_knob_value(bridge_node, "status", f"cache hit: frame {frame}")
        except Exception:
            pass
        return cached

    src_path = tempfile.NamedTemporaryFile(
        prefix="comfyui_bridge_src_", suffix=".png", delete=False
    ).name

    try:
        def _render() -> Tuple[bytes, Optional[bytes], int, int]:
            src = bridge_node.input(0)
            if src is None:
                raise napi.NukeError("ComfyUIBridge input 0 is not connected")

            temp_nodes: list = []
            try:
                chain = src

                # ComfyUI's MASK socket is inverted from Nuke alpha. To make
                # the user-facing mode names match Nuke's visible alpha/mask
                # meaning, source alpha needs an inverted carrier here; the
                # FromNuke node then does mask = 1 - carrier.
                if mask_source == "source alpha":
                    inv = nuke.nodes.Invert(inputs=[chain], channels="alpha")
                    temp_nodes.append(inv)
                    chain = inv

                _write_png(nuke, chain, src_path, frame, temp_nodes, colorspace)
                fmt = src.format()
                width, height = int(fmt.width()), int(fmt.height())

                mask_png: Optional[bytes] = None
                if is_mask_mode:
                    mask_input = bridge_node.input(1)
                    if mask_input is not None:
                        mask_path = tempfile.NamedTemporaryFile(
                            prefix="comfyui_bridge_mask_", suffix=".png", delete=False
                        ).name
                        try:
                            _write_png(nuke, mask_input, mask_path, frame, temp_nodes, colorspace)
                            with open(mask_path, "rb") as fh:
                                mask_png = fh.read()
                        finally:
                            try:
                                os.remove(mask_path)
                            except OSError:
                                pass
                    # mask_png stays None for the all-keep disconnected case.

                with open(src_path, "rb") as fh:
                    src_png = fh.read()

                return src_png, mask_png, width, height
            finally:
                for node in reversed(temp_nodes):
                    try:
                        nuke.delete(node)
                    except Exception:
                        pass

        src_png, mask_png, width, height = napi.call(_render)

        if is_mask_mode:
            data = _pil_replace_alpha(src_png, mask_png, invert=mask_source == "invert mask input")
        else:
            data = src_png

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
        prefix="comfyui_bridge_src_", suffix=".exr", delete=False
    ).name

    try:
        def _render() -> Tuple[bytes, int, int]:
            src = bridge_node.input(0)
            if src is None:
                raise napi.NukeError("ComfyUIBridge input 0 is not connected")

            temp_nodes: list = []
            try:
                chain = src
                # Match PNG semantics: user-facing source alpha means the
                # visible Nuke alpha/mask is used, but ComfyUI masks are
                # inverted later (mask = 1 - carrier), so the carrier is
                # inverted here. Invert source alpha therefore uses raw alpha.
                if mask_source == "source alpha":
                    inv = nuke.nodes.Invert(inputs=[chain], channels="alpha")
                    temp_nodes.append(inv)
                    chain = inv

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
    is_mask_mode = mask_source in ("mask input", "invert mask input")

    if fmt == "exr16" and not is_mask_mode:
        data, w, h = render_frame_exr(bridge_node, frame, mask_source, colorspace)
        return data, w, h, "exr16"

    # Default / fallback: PNG path (also handles exr16 + mask-input modes).
    data, w, h = render_frame_png(bridge_node, frame, mask_source, colorspace)
    actual = "png8" if (fmt != "exr16" or is_mask_mode) else fmt
    if fmt == "exr16" and is_mask_mode:
        try:
            napi.set_knob_value(
                bridge_node, "status",
                "exr16 + mask input: PNG fallback (no in-tree compose)",
            )
        except Exception:
            pass
    return data, w, h, actual
