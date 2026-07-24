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
    if cs.lower() in ("", "default"):
        return ""
    try:
        float(cs)
        return ""
    except ValueError:
        return cs


class _VideoProgress:
    """Print [NukeBridge] <stage> on stage changes; drive comfy ProgressBar to total."""

    def __init__(self, total: int | None):
        self._total = total
        self._stage = None
        self._bar = None
        if total:
            try:
                from comfy.utils import ProgressBar
                self._bar = ProgressBar(total)
            except Exception:
                self._bar = None  # standalone / test context: no server hook

    def __call__(self, completed, total, stage):
        if stage != self._stage:
            print(f"[NukeBridge] {stage}", flush=True)
            self._stage = stage
        if self._bar is not None and completed is not None and total:
            self._bar.update_absolute(min(completed, total), total)


class FromNuke:
    """Pull a frame from a Nuke ComfyUIBridge node."""

    @classmethod
    def VALIDATE_INPUTS(cls, colorspace: str) -> bool:
        return True

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
                "colorspace": (["default", "raw", "sRGB", "rec709"], {"default": "default"}),
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

    @classmethod
    def VALIDATE_INPUTS(cls, colorspace: str) -> bool:
        return True

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
                "colorspace": (["default", "raw", "sRGB", "rec709"], {"default": "default"}),
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

    @classmethod
    def VALIDATE_INPUTS(cls, colorspace: str) -> bool:
        return True

    @staticmethod
    def INPUT_TYPES(cls_dict: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {"required": {
            "bridge_id": ("STRING", {"default": "", "multiline": False}),
            "host": ("STRING", {"default": "127.0.0.1"}),
            "port": ("INT", {"default": 8765, "min": 1, "max": 65535}),
            "frame_start": ("INT", {"default": -1, "min": -1, "max": 2**31 - 1}),
            "frame_end": ("INT", {"default": -1, "min": -1, "max": 2**31 - 1}),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0}),
            "format": (["mov", "mp4"], {"default": "mov"}),
            "mov_codec": (["prores_422hq", "prores_4444"], {"default": "prores_422hq"}),
            "colorspace": (["default", "raw", "sRGB", "rec709"], {"default": "default"}),
            "timeout": ("FLOAT", {"default": 120.0, "min": 1.0, "max": 3600.0}),
        }, "optional": {
            "main_path": ("STRING", {"default": "", "multiline": False, "forceInput": True, "socketless": True}),
            "mask_path": ("STRING", {"default": "", "multiline": False, "forceInput": True, "socketless": True}),
            "metadata_json": ("STRING", {"default": "{}", "multiline": True, "forceInput": True, "socketless": True}),
        }}

    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "INT", "INT", "INT", "FLOAT")
    RETURN_NAMES = ("image", "mask", "video_meta_json", "width", "height", "frame_count", "fps")
    FUNCTION = "pull"
    CATEGORY = "NukeBridge"

    def pull(self, bridge_id: str, host: str, port: int, frame_start: int, frame_end: int, fps: float, format: str, mov_codec: str, colorspace: str, timeout: float, main_path: str = "", mask_path: str = "", metadata_json: str = "{}"):
        if not main_path or not mask_path:
            raise RuntimeError("FromNukeVideo: Nuke did not inject rendered video paths")
        if not os.path.isfile(main_path):
            raise RuntimeError(f"FromNukeVideo: missing main_path {main_path!r}")
        if not os.path.isfile(mask_path):
            raise RuntimeError(f"FromNukeVideo: missing mask_path {mask_path!r}")
        meta = json.loads(metadata_json or "{}")
        expected = int(meta["frame_count"]) if meta.get("frame_count") else None
        report = _VideoProgress(2 * expected + 1 if expected else None)
        image, mask, width, height, frame_count = video_io.decode_video(
            main_path, mask_path, expected_frames=expected, progress_cb=report
        )
        return image, mask, json.dumps(meta), width, height, frame_count, float(meta.get("fps") or fps)


class ToNukeVideo:
    """Send an IMAGE batch back to Nuke as mp4/mov."""

    @classmethod
    def VALIDATE_INPUTS(cls, colorspace: str) -> bool:
        return True

    @staticmethod
    def INPUT_TYPES(cls_dict: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return {"required": {
            "image": ("IMAGE",),
            "bridge_id": ("STRING", {"default": "", "multiline": False}),
            "host": ("STRING", {"default": "127.0.0.1"}),
            "port": ("INT", {"default": 8765, "min": 1, "max": 65535}),
            "video_meta_json": ("STRING", {"default": "{}", "multiline": True, "forceInput": True, "socketless": True}),
            "filename_prefix": ("STRING", {"default": "comfy_video_result"}),
            "format_override": (["auto", "mp4", "mov"], {"default": "auto"}),
            "mov_codec_override": (["auto", "prores_422hq", "prores_4444"], {"default": "auto"}),
            "colorspace": (["default", "raw", "sRGB", "rec709"], {"default": "default"}),
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
        source_meta = video_io.probe_source_meta(str(meta.get("main_path") or ""))
        B = int(image.shape[0])
        report = _VideoProgress(B + 3)
        with tempfile.TemporaryDirectory(prefix="nuke_bridge_video_result_") as tmp:
            path = os.path.join(tmp, "result.mov" if fmt == "mov" else "result.mp4")
            video_io.encode_video(image, path, fmt, mov_codec, fps, source_meta=source_meta, progress_cb=report, extra_steps=2)
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
        report(B + 2, B + 3, f"uploading {len(body) / (1024 * 1024):.1f} MiB to Nuke")
        resp = requests.post(f"{_base_url(host, port)}/bridge/{_bridge_path_id(bridge_id)}/video_result", data=body, headers=headers, timeout=float(timeout))
        if resp.status_code != 200:
            raise RuntimeError(f"ToNukeVideo: Nuke /video_result returned {resp.status_code}: {resp.text[:300]}")
        report(B + 3, B + 3, "complete")
        return (image,)
