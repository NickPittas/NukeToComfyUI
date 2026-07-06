"""ComfyUI custom nodes: FromNuke and ToNuke."""

from __future__ import annotations

import urllib.parse
import uuid
import json
import os
import tempfile
from typing import Any, Dict, Tuple

import requests
import torch

from . import image_io
from . import video_io


def _base_url(host: str, port: int) -> str:
    return f"http://{(host or '127.0.0.1').strip()}:{int(port or 8765)}"


def _bridge_path_id(bridge_id: str) -> str:
    return (bridge_id or "_active").strip() or "_active"


def _format_value(value: Any) -> str:
    """Normalize format, tolerating old workflows where timeout shifted here."""
    fmt = str(value or "png8").strip().lower()
    return fmt if fmt in ("exr16", "png8") else "png8"


def _colorspace_value(value: Any) -> str:
    """Normalize colorspace; old workflows may shift timeout=30 into this slot."""
    cs = str(value or "").strip()
    try:
        float(cs)
        return ""
    except ValueError:
        return cs


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
                "format": (["png8", "exr16"], {"default": "png8"}),
                "timeout": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 600.0}),
                "colorspace": ("STRING", {"default": "", "multiline": False}),
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
        colorspace: str,
        timeout: float,
    ) -> Tuple[torch.Tensor, torch.Tensor, str, int, int]:
        url = f"{_base_url(host, port)}/bridge/{_bridge_path_id(bridge_id)}/frame"
        resp = requests.post(
            url,
            json={
                "frame": int(frame),
                "format": _format_value(format),
                "colorspace": _colorspace_value(colorspace),
            },
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
        actual_fmt = resp.headers.get("X-NukeBridge-Format", _format_value(format))
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
                "format": (["png8", "exr16"], {"default": "png8"}),
                "timeout": ("FLOAT", {"default": 30.0, "min": 1.0, "max": 600.0}),
                "colorspace": ("STRING", {"default": "", "multiline": False}),
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
        colorspace: str,
        timeout: float,
    ) -> Tuple[torch.Tensor]:
        body, content_type, fmt_tag = image_io.encode_image_bytes(image, _format_value(format))
        url = f"{_base_url(host, port)}/bridge/{_bridge_path_id(bridge_id)}/result"
        headers = {
            "Content-Type": content_type,
            "X-NukeBridge-Filename-Prefix": filename_prefix or "comfy_result",
            "X-NukeBridge-Frame": "-1",
            "X-NukeBridge-Format": fmt_tag,
        }
        cs = _colorspace_value(colorspace)
        if cs:
            headers["X-NukeBridge-Colorspace"] = cs
        resp = requests.post(url, data=body, headers=headers, timeout=float(timeout))
        if resp.status_code != 200:
            raise RuntimeError(
                f"ToNuke: Nuke /result returned {resp.status_code}: {resp.text[:200]}"
            )
        return (image,)


class FromNukeVideo:
    """Pull a video bundle from a Nuke ComfyUIBridge node."""

    @staticmethod
    def INPUT_TYPES(cls_dict: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {"required": {
            "bridge_id": ("STRING", {"default": "", "multiline": False}),
            "host": ("STRING", {"default": "127.0.0.1"}),
            "port": ("INT", {"default": 8765, "min": 1, "max": 65535}),
            "frame_start": ("INT", {"default": -1, "min": -1, "max": 2**31 - 1}),
            "frame_end": ("INT", {"default": -1, "min": -1, "max": 2**31 - 1}),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0}),
            "format": (["mp4", "mov"], {"default": "mp4"}),
            "mov_codec": (["prores_422hq", "prores_4444"], {"default": "prores_422hq"}),
            "colorspace": ("STRING", {"default": "", "multiline": False}),
            "timeout": ("FLOAT", {"default": 120.0, "min": 1.0, "max": 3600.0}),
        }}

    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "INT", "INT", "INT", "FLOAT")
    RETURN_NAMES = ("image", "mask", "video_meta_json", "width", "height", "frame_count", "fps")
    FUNCTION = "pull"
    CATEGORY = "NukeBridge"

    def pull(self, bridge_id: str, host: str, port: int, frame_start: int, frame_end: int, fps: float, format: str, mov_codec: str, colorspace: str, timeout: float):
        url = f"{_base_url(host, port)}/bridge/{_bridge_path_id(bridge_id)}/video"
        req = {
            "frame_start": int(frame_start), "frame_end": int(frame_end), "fps": float(fps),
            "format": format, "mov_codec": mov_codec, "colorspace": _colorspace_value(colorspace),
        }
        resp = requests.post(url, json=req, timeout=float(timeout))
        if resp.status_code != 200:
            raise RuntimeError(f"FromNukeVideo: Nuke /video returned {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        meta = data.get("metadata") or {}
        with tempfile.TemporaryDirectory(prefix="nuke_bridge_video_") as tmp:
            main_path = os.path.join(tmp, "main.mov" if meta.get("format") == "mov" else "main.mp4")
            mask_path = os.path.join(tmp, "mask.mp4")
            for url_key, path in (("main_url", main_path), ("mask_url", mask_path)):
                r = requests.get(data[url_key], timeout=float(timeout))
                if r.status_code != 200:
                    raise RuntimeError(f"FromNukeVideo: download failed {r.status_code}: {data[url_key]}")
                with open(path, "wb") as fh:
                    fh.write(r.content)
            image, mask, width, height, frame_count = video_io.decode_video(main_path, mask_path)
        return image, mask, json.dumps(meta), width, height, frame_count, float(meta.get("fps") or fps)


class ToNukeVideo:
    """Send an IMAGE batch back to Nuke as mp4/mov."""

    @staticmethod
    def INPUT_TYPES(cls_dict: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {"required": {
            "image": ("IMAGE",),
            "bridge_id": ("STRING", {"default": "", "multiline": False}),
            "host": ("STRING", {"default": "127.0.0.1"}),
            "port": ("INT", {"default": 8765, "min": 1, "max": 65535}),
            "video_meta_json": ("STRING", {"default": "{}", "multiline": True}),
            "filename_prefix": ("STRING", {"default": "comfy_video_result"}),
            "format_override": (["auto", "mp4", "mov"], {"default": "auto"}),
            "mov_codec_override": (["auto", "prores_422hq", "prores_4444"], {"default": "auto"}),
            "colorspace": ("STRING", {"default": "", "multiline": False}),
            "timeout": ("FLOAT", {"default": 120.0, "min": 1.0, "max": 3600.0}),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "push"
    CATEGORY = "NukeBridge"
    OUTPUT_NODE = True

    def push(self, image: torch.Tensor, bridge_id: str, host: str, port: int, video_meta_json: str, filename_prefix: str, format_override: str, mov_codec_override: str, colorspace: str, timeout: float):
        try:
            meta = json.loads(video_meta_json or "{}")
        except Exception:
            meta = {}
        fmt = meta.get("format") or "mp4"
        if format_override != "auto":
            fmt = format_override
        mov_codec = meta.get("mov_codec") or "prores_422hq"
        if mov_codec_override != "auto":
            mov_codec = mov_codec_override
        fps = float(meta.get("fps") or 24.0)
        first = int(meta.get("frame_start") or 1)
        last = first + int(image.shape[0]) - 1
        with tempfile.TemporaryDirectory(prefix="nuke_bridge_video_result_") as tmp:
            path = os.path.join(tmp, "result.mov" if fmt == "mov" else "result.mp4")
            video_io.encode_video(image, path, fmt, mov_codec, fps)
            with open(path, "rb") as fh:
                body = fh.read()
        headers = {
            "Content-Type": "video/quicktime" if fmt == "mov" else "video/mp4",
            "X-NukeBridge-Filename-Prefix": filename_prefix or "comfy_video_result",
            "X-NukeBridge-Format": str(fmt),
            "X-NukeBridge-Mov-Codec": str(mov_codec),
            "X-NukeBridge-FPS": str(fps),
            "X-NukeBridge-Frame-Start": str(first),
            "X-NukeBridge-Frame-End": str(last),
        }
        cs = _colorspace_value(colorspace)
        if cs:
            headers["X-NukeBridge-Colorspace"] = cs
        resp = requests.post(f"{_base_url(host, port)}/bridge/{_bridge_path_id(bridge_id)}/video_result", data=body, headers=headers, timeout=float(timeout))
        if resp.status_code != 200:
            raise RuntimeError(f"ToNukeVideo: Nuke /video_result returned {resp.status_code}: {resp.text[:300]}")
        return (image,)
