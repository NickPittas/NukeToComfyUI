"""ComfyUI custom nodes: FromNuke and ToNuke."""

from __future__ import annotations

import urllib.parse
import uuid
from typing import Any, Dict, Tuple

import requests
import torch

from . import image_io


def _base_url(host: str, port: int) -> str:
    return f"http://{(host or '127.0.0.1').strip()}:{int(port or 8765)}"


class FromNuke:
    """Pull a frame from a Nuke ComfyUIBridge node."""

    @staticmethod
    def INPUT_TYPES(cls_dict: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {
            "required": {
                "bridge_id": ("STRING", {"default": "", "multiline": False}),
                "host": ("STRING", {"default": "127.0.0.1"}),
                "port": ("INT", {"default": 8765, "min": 1, "max": 65535}),
                "frame": ("INT", {"default": -1, "min": -1, "max": 2**31 - 1}),
                "timeout": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 600.0}),
            },
            "optional": {},
        }

    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "INT", "INT")
    RETURN_NAMES = ("image", "mask", "prompt", "width", "height")
    FUNCTION = "pull"
    CATEGORY = "NukeBridge"

    def pull(
        self,
        bridge_id: str,
        host: str,
        port: int,
        frame: int,
        timeout: float,
    ) -> Tuple[torch.Tensor, torch.Tensor, str, int, int]:
        if not bridge_id:
            raise RuntimeError("FromNuke: bridge_id is empty")

        url = f"{_base_url(host, port)}/bridge/{bridge_id.strip()}/frame"
        resp = requests.post(
            url, json={"frame": int(frame)}, timeout=float(timeout)
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"FromNuke: Nuke /frame returned {resp.status_code}: {resp.text[:200]}"
            )

        png = resp.content
        if not png:
            raise RuntimeError("FromNuke: empty body from Nuke /frame")

        image, mask, width, height = image_io.png_bytes_to_tensors(png)

        prompt_raw = resp.headers.get("X-NukeBridge-Prompt", "")
        try:
            prompt = urllib.parse.unquote(prompt_raw)
        except Exception:
            prompt = prompt_raw

        return (image, mask, prompt, width, height)

    @staticmethod
    def IS_CHANGED(**kwargs: Any) -> float:
        # ponytail: force re-pull on every execution; if a stable hash of the
        # upstream Nuke image is needed later, replace this with that hash.
        return float(uuid.uuid4().int)


class ToNuke:
    """Send an IMAGE back to a Nuke ComfyUIBridge node's /result."""

    @staticmethod
    def INPUT_TYPES(cls_dict: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
                "bridge_id": ("STRING", {"default": "", "multiline": False}),
                "host": ("STRING", {"default": "127.0.0.1"}),
                "port": ("INT", {"default": 8765, "min": 1, "max": 65535}),
                "filename_prefix": ("STRING", {"default": "comfy_result"}),
                "timeout": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 600.0}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "push"
    CATEGORY = "NukeBridge"
    OUTPUT_NODE = True

    def push(
        self,
        image: torch.Tensor,
        bridge_id: str,
        host: str,
        port: int,
        filename_prefix: str,
        timeout: float,
    ) -> Tuple[torch.Tensor]:
        if not bridge_id:
            raise RuntimeError("ToNuke: bridge_id is empty")

        png = image_io.tensor_to_png_bytes(image)
        url = f"{_base_url(host, port)}/bridge/{bridge_id.strip()}/result"
        headers = {
            "Content-Type": "image/png",
            "X-NukeBridge-Filename-Prefix": filename_prefix or "comfy_result",
            "X-NukeBridge-Frame": "-1",
            "X-NukeBridge-Format": "png8",
            "X-NukeBridge-Colorspace": "sRGB",
        }
        resp = requests.post(url, data=png, headers=headers, timeout=float(timeout))
        if resp.status_code != 200:
            raise RuntimeError(
                f"ToNuke: Nuke /result returned {resp.status_code}: {resp.text[:200]}"
            )
        return (image,)
