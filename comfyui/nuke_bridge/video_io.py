"""Comfy-side video helpers for Nuke bridge video nodes."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Tuple

import numpy as np
import torch
from PIL import Image


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout)[-1000:])


def _ffmpeg() -> str:
    return os.environ.get("NUKE_BRIDGE_FFMPEG") or "ffmpeg"


def _frames_to_tensor(pattern_dir: str, mode: str) -> torch.Tensor:
    files = sorted(f for f in os.listdir(pattern_dir) if f.endswith(".png"))
    if not files:
        raise RuntimeError("ffmpeg produced no frames")
    frames = []
    for name in files:
        img = Image.open(os.path.join(pattern_dir, name)).convert(mode)
        arr = np.asarray(img).astype(np.float32) / 255.0
        frames.append(arr)
    return torch.from_numpy(np.stack(frames, axis=0)).contiguous()


def decode_video(main_path: str, mask_path: str) -> Tuple[torch.Tensor, torch.Tensor, int, int, int]:
    tmp = tempfile.mkdtemp(prefix="nuke_bridge_video_decode_")
    try:
        rgb_dir = os.path.join(tmp, "rgb")
        mask_dir = os.path.join(tmp, "mask")
        os.makedirs(rgb_dir)
        os.makedirs(mask_dir)
        _run([_ffmpeg(), "-y", "-i", main_path, os.path.join(rgb_dir, "%06d.png")])
        _run([_ffmpeg(), "-y", "-i", mask_path, os.path.join(mask_dir, "%06d.png")])
        image = _frames_to_tensor(rgb_dir, "RGB")
        mask = _frames_to_tensor(mask_dir, "L")
        if mask.ndim == 4:
            mask = mask[..., 0]
        frame_count, height, width = image.shape[0], image.shape[1], image.shape[2]
        return image, mask, width, height, frame_count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def encode_video(image: torch.Tensor, output_path: str, fmt: str, mov_codec: str, fps: float) -> None:
    if image.dim() == 3:
        image = image.unsqueeze(0)
    tmp = tempfile.mkdtemp(prefix="nuke_bridge_video_encode_")
    try:
        arr = image.clamp(0.0, 1.0).detach().cpu().numpy()
        for idx, frame in enumerate(arr, start=1):
            frame8 = (frame[:, :, :3] * 255.0).round().astype(np.uint8)
            Image.fromarray(frame8, mode="RGB").save(os.path.join(tmp, f"{idx:06d}.png"))
        cmd = [_ffmpeg(), "-y", "-framerate", str(float(fps)), "-i", os.path.join(tmp, "%06d.png")]
        if fmt == "mov":
            profile = "4" if mov_codec == "prores_4444" else "3"
            pix_fmt = "yuva444p10le" if mov_codec == "prores_4444" else "yuv422p10le"
            cmd += ["-c:v", "prores_ks", "-profile:v", profile, "-pix_fmt", pix_fmt]
        else:
            cmd += ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p"]
        cmd.append(output_path)
        _run(cmd)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
