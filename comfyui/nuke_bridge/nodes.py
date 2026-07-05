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


def _bridge_path_id(bridge_id: str) -> str:
    return (bridge_id or "_active").strip() or "_active"


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
                "format": (["exr16", "png8"],),
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
        format: str,
        timeout: float,
    ) -> Tuple[torch.Tensor, torch.Tensor, str, int, int]:
        url = f"{_base_url(host, port)}/bridge/{_bridge_path_id(bridge_id)}/frame"
        resp = requests.post(
            url,
            json={"frame": int(frame), "format": format},
            timeout=float(timeout),
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"FromNuke: Nuke /frame returned {resp.status_code}: {resp.text[:200]}"
            )

        body = resp.content
        if not body:
            raise RuntimeError("FromNuke: empty body from Nuke /frame")

        # Trust the server's actual format (may differ from requested, e.g.
        # exr16 + mask-input mode degrades to png8 server-side).
        actual_fmt = resp.headers.get("X-NukeBridge-Format", format)
        image, mask, width, height = image_io.decode_image_bytes(body, actual_fmt)

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
                "format": (["exr16", "png8"],),
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
        format: str,
        timeout: float,
    ) -> Tuple[torch.Tensor]:
        body, content_type, fmt_tag = image_io.encode_image_bytes(image, format)
        url = f"{_base_url(host, port)}/bridge/{_bridge_path_id(bridge_id)}/result"
        headers = {
            "Content-Type": content_type,
            "X-NukeBridge-Filename-Prefix": filename_prefix or "comfy_result",
            "X-NukeBridge-Frame": "-1",
            "X-NukeBridge-Format": fmt_tag,
            "X-NukeBridge-Colorspace": "raw" if fmt_tag == "exr16" else "sRGB",
        }
        resp = requests.post(url, data=body, headers=headers, timeout=float(timeout))
        if resp.status_code != 200:
            raise RuntimeError(
                f"ToNuke: Nuke /result returned {resp.status_code}: {resp.text[:200]}"
            )
        return (image,)
