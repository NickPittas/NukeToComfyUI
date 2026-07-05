"""PNG <-> ComfyUI tensor helpers.

ComfyUI IMAGE:  BHWC float32 in [0,1].
ComfyUI MASK:    BHW float32 (no channel), convention: 0 = keep, 1 = transparent.
Nuke carrier alpha (per PROTOCOL.md): 1 = keep, 0 = transparent.
"""

from __future__ import annotations

import io
from typing import Tuple

import numpy as np
import torch
from PIL import Image


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
    # Take first frame of the batch (Phase 1: single image return).
    arr = image[0].clamp(0.0, 1.0).cpu().numpy()
    arr8 = (arr * 255.0).round().astype(np.uint8)
    if arr8.shape[-1] == 4:
        arr8 = arr8[:, :, :3]
    img = Image.fromarray(arr8, mode="RGB" if arr8.shape[-1] == 3 else "L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
