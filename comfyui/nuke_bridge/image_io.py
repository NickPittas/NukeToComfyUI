"""Image <-> ComfyUI tensor helpers (PNG + EXR).

ComfyUI IMAGE:  BHWC float32 in [0,1] (clamped for diffusion models).
ComfyUI MASK:    BHW float32 (no channel), convention: 0 = keep, 1 = transparent.
Nuke carrier alpha (per PROTOCOL.md): 1 = keep, 0 = transparent.

PNG path uses PIL only. EXR path prefers OpenImageIO, falls back to OpenCV for
decode (read-only); EXR encode requires OpenImageIO. All optional imports are
lazy so PNG keeps working when neither is installed.
"""

from __future__ import annotations

import io
import os
import tempfile
from typing import Optional, Tuple

import numpy as np
import torch
from PIL import Image


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------

def png_bytes_to_tensors(png_bytes: bytes) -> Tuple[torch.Tensor, torch.Tensor, int, int]:
    """Decode PNG RGBA into (IMAGE, MASK, width, height).

    MASK follows ComfyUI convention: mask = 1 - carrier_alpha.
    """
    img = Image.open(io.BytesIO(png_bytes))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    arr = np.asarray(img).astype(np.float32) / 255.0  # HWC
    height, width = arr.shape[0], arr.shape[1]

    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3]

    image = torch.from_numpy(rgb).unsqueeze(0).contiguous()  # 1,H,W,3
    mask = torch.from_numpy(1.0 - alpha).unsqueeze(0).contiguous()  # 1,H,W
    return image, mask, width, height


def tensor_to_png_bytes(image: torch.Tensor) -> bytes:
    """Encode an IMAGE tensor (BHWC float32 [0,1]) to PNG RGB bytes."""
    if image.dim() == 3:
        image = image.unsqueeze(0)
    # Take first frame of the batch.
    arr = image[0].clamp(0.0, 1.0).cpu().numpy()
    arr8 = (arr * 255.0).round().astype(np.uint8)
    if arr8.shape[-1] == 4:
        arr8 = arr8[:, :, :3]
    img = Image.fromarray(arr8, mode="RGB" if arr8.shape[-1] == 3 else "L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# EXR (OpenImageIO preferred; OpenCV read-only fallback)
# --------------------------------------------------------------------------

def _have_oiio() -> bool:
    try:
        import OpenImageIO  # noqa: F401
        return True
    except Exception:
        return False


def _have_cv2_exr() -> bool:
    try:
        import cv2  # noqa: F401
        # cv2 must be built with OpenEXR support; IMREAD_EXR_* flags exist
        # when the codec is compiled in.
        return hasattr(cv2, "IMREAD_UNCHANGED")
    except Exception:
        return False


def exr_bytes_to_tensors(exr_bytes: bytes) -> Tuple[torch.Tensor, torch.Tensor, int, int]:
    """Decode half/float EXR RGBA into (IMAGE, MASK, width, height).

    RGB is clamped to [0,1] for diffusion-model consumption (documented
    limitation: raw HDR values are not preserved through this layer). Alpha
    carrier follows PROTOCOL.md (mask = 1 - alpha).
    """
    arr = _exr_bytes_to_array(exr_bytes)  # HWC float32
    if arr.ndim != 3:
        raise RuntimeError(f"EXR decoded to unexpected shape {arr.shape}")
    height, width, channels = arr.shape

    rgb = arr[:, :, :3] if channels >= 3 else np.repeat(arr[:, :, :1], 3, axis=2)
    rgb = np.clip(rgb, 0.0, 1.0)

    if channels >= 4:
        alpha = np.clip(arr[:, :, 3], 0.0, 1.0)
    else:
        alpha = np.ones((height, width), dtype=np.float32)

    image = torch.from_numpy(rgb.astype(np.float32)).unsqueeze(0).contiguous()
    mask = torch.from_numpy((1.0 - alpha).astype(np.float32)).unsqueeze(0).contiguous()
    return image, mask, width, height


def _exr_bytes_to_array(exr_bytes: bytes) -> np.ndarray:
    """Decode EXR bytes into a float32 HWC numpy array via OIIO or OpenCV."""
    # OIIO path (preferred; works for encode too).
    if _have_oiio():
        import OpenImageIO as oiio

        with tempfile.NamedTemporaryFile(prefix="nuke_bridge_", suffix=".exr", delete=False) as fh:
            tmp = fh.name
        try:
            with open(tmp, "wb") as out:
                out.write(exr_bytes)
            inp = oiio.ImageInput.open(tmp)
            if inp is None:
                raise RuntimeError("OpenImageIO could not open EXR")
            try:
                spec = inp.spec()
                arr = inp.read_image()
                channelnames = list(getattr(spec, "channelnames", []) or [])
            finally:
                inp.close()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        if arr is None:
            raise RuntimeError("OpenImageIO read_image returned None")
        arr = np.asarray(arr)
        if arr.ndim == 3 and channelnames:
            names = [str(n).lower() for n in channelnames]

            def _find(candidates: tuple[str, ...]) -> Optional[int]:
                for candidate in candidates:
                    for idx, name in enumerate(names):
                        tail = name.split(".")[-1]
                        if name == candidate or tail == candidate:
                            return idx
                return None

            r = _find(("r", "red"))
            g = _find(("g", "green"))
            b = _find(("b", "blue"))
            a = _find(("a", "alpha"))
            if r is not None and g is not None and b is not None:
                order = [r, g, b]
                if a is not None:
                    order.append(a)
                arr = arr[:, :, order]
        # OIIO returns HWC for most configs; normalize dtype to float32.
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32)
        return arr

    # OpenCV fallback (read-only).
    if _have_cv2_exr():
        import cv2

        with tempfile.NamedTemporaryFile(prefix="nuke_bridge_", suffix=".exr", delete=False) as fh:
            tmp = fh.name
        try:
            with open(tmp, "wb") as out:
                out.write(exr_bytes)
            arr = cv2.imread(tmp, cv2.IMREAD_UNCHANGED)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if arr is None:
            raise RuntimeError("OpenCV could not read EXR (built without OpenEXR support?)")
        arr = np.asarray(arr).astype(np.float32)
        # OpenCV loads as BGR(A); swap to RGB(A).
        if arr.ndim == 3 and arr.shape[2] >= 3:
            channels = list(range(arr.shape[2]))
            channels[0], channels[2] = 2, 0
            arr = arr[:, :, channels]
        return arr

    raise RuntimeError(
        "EXR decode needs OpenImageIO (preferred) or OpenCV built with OpenEXR; "
        "neither is importable. Install OpenImageIO or switch FromNuke format to png8."
    )


def tensor_to_exr_bytes(image: torch.Tensor) -> bytes:
    """Encode an IMAGE tensor (BHWC float32 [0,1]) to half-float RGB EXR bytes.

    Requires OpenImageIO for encoding. Alpha is omitted (Phase 1 ToNuke returns
    RGB-only results; Nuke will treat missing alpha as opaque).
    """
    if not _have_oiio():
        raise RuntimeError(
            "EXR encode requires OpenImageIO; not importable. "
            "Install OpenImageIO or switch ToNuke format to png8."
        )
    import OpenImageIO as oiio

    if image.dim() == 3:
        image = image.unsqueeze(0)
    arr = image[0].clamp(0.0, 1.0).detach().cpu().numpy().astype(np.float32)
    if arr.shape[-1] == 4:
        arr = arr[:, :, :3]
    arr = np.ascontiguousarray(arr)
    height, width, channels = arr.shape

    spec = oiio.ImageSpec(width, height, channels, oiio.HALF)
    spec.channelnames = ["R", "G", "B", "A"][:channels]
    spec.attribute("compression", "zip")
    with tempfile.NamedTemporaryFile(prefix="nuke_bridge_", suffix=".exr", delete=False) as fh:
        tmp = fh.name
    try:
        out = oiio.ImageOutput.create(tmp)
        if out is None:
            raise RuntimeError("OpenImageIO could not create EXR writer")
        ok = out.open(tmp, spec)
        try:
            if not ok:
                raise RuntimeError(f"OpenImageIO failed to open EXR: {out.geterror()}")
            ok = out.write_image(arr)
            if not ok:
                raise RuntimeError(f"OpenImageIO failed to write EXR: {out.geterror()}")
        finally:
            out.close()
        with open(tmp, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Dispatch helpers used by nodes.py
# --------------------------------------------------------------------------

def decode_image_bytes(
    data: bytes, fmt_hint: Optional[str] = None
) -> Tuple[torch.Tensor, torch.Tensor, int, int]:
    """Decode PNG or EXR based on a hint or magic bytes."""
    fmt = (fmt_hint or "").strip().lower()
    if fmt == "exr16" or data[:4] == b"\x76\x2f\x31\x01":
        return exr_bytes_to_tensors(data)
    return png_bytes_to_tensors(data)


def encode_image_bytes(image: torch.Tensor, fmt: str) -> Tuple[bytes, str, str]:
    """Encode an IMAGE tensor; returns (bytes, content_type, format_tag)."""
    fmt = (fmt or "png8").strip().lower()
    if fmt == "exr16":
        return tensor_to_exr_bytes(image), "image/exr", "exr16"
    return tensor_to_png_bytes(image), "image/png", "png8"
