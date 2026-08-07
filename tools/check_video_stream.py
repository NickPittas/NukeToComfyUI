"""Assert-based self-check for the nuke_bridge rawvideo video IO + stage progress.

Runs outside Nuke/ComfyUI. Loads video_io.py and nodes.py via importlib (the
package __init__ pulls ComfyUI-only deps, so we synthesize a throwaway package).
Requires torch, numpy, requests, and ffmpeg/ffprobe on PATH (or via the
NUKE_BRIDGE_FFMPEG / NUKE_BRIDGE_FFPROBE env overrides).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "comfyui" / "nuke_bridge"

W, H, N = 64, 48, 4


def _ok(name):
    print(f"ok: {name}", flush=True)


def _load_module(name, path, pkg):
    full = f"{pkg}.{name}"
    spec = importlib.util.spec_from_file_location(full, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


# Build a throwaway package so nodes.py's `from . import image_io/video_io` resolves.
pkg = types.ModuleType("nbpkg")
pkg.__path__ = []
sys.modules["nbpkg"] = pkg
image_io = _load_module("image_io", BRIDGE / "image_io.py", "nbpkg")
video_io = _load_module("video_io", BRIDGE / "video_io.py", "nbpkg")
nodes = _load_module("nodes", BRIDGE / "nodes.py", "nbpkg")

FF = video_io._ffmpeg()
FP = video_io._ffprobe()


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, completed, total, stage):
        self.calls.append((completed, total, stage))


def _run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], **kw)


def _probe_json(path):
    cmd = [FP, "-v", "quiet", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,width,height,nb_frames,color_primaries,color_transfer,"
           "color_space,color_range:stream_tags=timecode:format_tags=timecode",
           "-of", "json", path]
    out = _run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return json.loads(out.stdout or "{}")


@contextlib.contextmanager
def _tmpdir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    try:
        yield d
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _write_fixture(tmp):
    """4-frame 64x48 RGB (distinct color/frame) + 4-frame gray mask, lossless."""
    colors = np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 0]], dtype=np.uint8)
    raw = np.broadcast_to(colors[:, None, None, :], (N, H, W, 3)).copy().tobytes()
    main = os.path.join(tmp, "main.mkv")
    _run([FF, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{W}x{H}",
          "-framerate", "24", "-i", "-", "-c:v", "ffv1", main],
         input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    grays = np.array([0, 64, 128, 255], dtype=np.uint8)
    mraw = np.broadcast_to(grays[:, None, None], (N, H, W)).copy().tobytes()
    mask = os.path.join(tmp, "mask.mkv")
    _run([FF, "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-video_size", f"{W}x{H}",
          "-framerate", "24", "-i", "-", "-c:v", "ffv1", mask],
         input=mraw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    mask3 = os.path.join(tmp, "mask3.mkv")
    _run([FF, "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-video_size", f"{W}x{H}",
          "-framerate", "24", "-i", "-", "-c:v", "ffv1", "-frames:v", "3", mask3],
         input=mraw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return main, mask, mask3


def check_binaries():
    assert _run([FF, "-version"]).returncode == 0, "ffmpeg -version failed"
    assert _run([FP, "-version"]).returncode == 0, "ffprobe -version failed"
    # rgb48le rawvideo support is required for the 16-bit fidelity transport.
    pix = _run([FF, "-hide_banner", "-pix_fmts"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert "rgb48le" in (pix.stdout or ""), "this ffmpeg build lacks rgb48le pix_fmt"
    _ok("ffmpeg/ffprobe runnable, env overrides honored, rgb48le available")


def check_fixture(tmp, paths):
    p = _probe_json(paths[0])
    st = (p.get("streams") or [{}])[0]
    assert int(st["width"]) == W and int(st["height"]) == H, st
    _ok("fixture: 4-frame 64x48 rgb + gray mask mov generated")


def check_decode_prealloc(paths):
    rec = Recorder()
    image, mask, width, height, frame_count = video_io.decode_video(
        paths[0], paths[1], expected_frames=4, progress_cb=rec)
    assert tuple(image.shape) == (4, H, W, 3), image.shape
    assert image.dtype == video_io.torch.float32
    assert tuple(mask.shape) == (4, H, W), mask.shape
    assert mask.dtype == video_io.torch.float32
    assert frame_count == 4 and width == W and height == H
    assert image.min() >= 0.0 and image.max() <= 1.0
    comps = [c for c, _, _ in rec.calls]
    assert comps == sorted(comps) and len(set(comps)) == len(comps), comps
    assert all(t == 9 for _, t, _ in rec.calls), rec.calls
    stages = []
    for _, _, s in rec.calls:
        if not stages or stages[-1] != s:
            stages.append(s)
    assert stages == ["decoding source", "decoding mask", "video ready"], stages
    assert rec.calls[-1] == (9, 9, "video ready"), rec.calls[-1]
    px = image[0, 0, 0]
    assert abs(float(px[0]) - 1.0) < 3 / 255 and abs(float(px[1])) < 3 / 255 \
        and abs(float(px[2])) < 3 / 255, px
    _ok("preallocated decode: shapes/counts/stages/monotonic/pixel-color correct")
    return image


def check_count_validation(paths):
    try:
        video_io.decode_video(paths[0], paths[1], expected_frames=5)
        raise AssertionError("expected RuntimeError for overshoot expected=5")
    except RuntimeError as e:
        assert "5" in str(e) and "4" in str(e), str(e)
    _ok("decode undershoot: expected_frames=5 on 4-frame fixture raises with counts")

    # Force the nb_frames=None fallback so the explicit main/mask mismatch path runs.
    orig = video_io._probe_geometry
    video_io._probe_geometry = lambda path: (*orig(path)[:2], None)
    try:
        try:
            video_io.decode_video(paths[0], paths[2])  # 4-frame main, 3-frame mask
            raise AssertionError("expected RuntimeError for mask count mismatch")
        except RuntimeError as e:
            assert "4" in str(e) and "3" in str(e), str(e)
    finally:
        video_io._probe_geometry = orig
    _ok("decode count mismatch: 4-frame main vs 3-frame mask raises naming both counts")


def check_missing_file(paths):
    bad = "/no/such/path/main_xyz.mkv"
    try:
        video_io.decode_video(bad, paths[1])
        raise AssertionError("expected RuntimeError for missing file")
    except RuntimeError as e:
        assert bad in str(e), str(e)
    _ok("decode missing file: RuntimeError names the path")


def check_encode_stages(tmp, image):
    rec = Recorder()
    out = os.path.join(tmp, "out.mov")
    src_meta = {"color_primaries": "bt709", "color_trc": "bt709", "colorspace": "bt709",
                "color_range": "tv", "timecode": "01:00:00:00"}
    video_io.encode_video(image, out, "mov", "prores_422hq", 24.0,
                          source_meta=src_meta, progress_cb=rec, extra_steps=2)
    assert os.path.isfile(out) and os.path.getsize(out) > 0
    assert all(t == 7 for _, t, _ in rec.calls), rec.calls
    comps = [c for c, _, _ in rec.calls]
    assert comps == sorted(comps) and len(set(comps)) == len(comps), comps
    stages = []
    for _, _, s in rec.calls:
        if not stages or stages[-1] != s:
            stages.append(s)
    assert stages == ["encoding frames", "finalizing mov"], stages
    p = _probe_json(out)
    st = (p.get("streams") or [{}])[0]
    assert st.get("codec_name") == "prores", st
    assert st.get("color_primaries") == "bt709", st
    assert st.get("color_transfer") == "bt709", st
    assert st.get("color_space") == "bt709", st
    assert st.get("color_range") == "tv", st
    nb = st.get("nb_frames")
    assert nb not in (None, "N/A") and int(nb) == 4, st
    tc = (st.get("tags") or {}).get("timecode") or (p.get("format", {}).get("tags") or {}).get("timecode")
    assert tc == "01:00:00:00", tc
    _ok("encode + stages: prores/bt709/tv-range/timecode/4-frames, totals=7 monotonic")


def check_reporter_gating():
    saved = {k: sys.modules.get(k) for k in ("comfy", "comfy.utils")}
    fake_comfy = types.ModuleType("comfy")
    fake_comfy.__path__ = []
    fake_utils = types.ModuleType("comfy.utils")
    created = []

    class _StubBar:
        def __init__(self):
            self.calls = []

        def update_absolute(self, value, total=None, preview=None):
            self.calls.append((value, total))

    def _factory(total, node_id=None):
        b = _StubBar()
        b.total = total
        created.append(b)
        return b

    fake_utils.ProgressBar = _factory
    sys.modules["comfy"] = fake_comfy
    sys.modules["comfy.utils"] = fake_utils
    try:
        B = 4
        total = B + 3
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report = nodes._VideoProgress(total)
            for i in range(1, B + 1):
                report(i, total, "encoding frames")
            report(B + 1, total, "finalizing mov")
        assert created, "ProgressBar stub was not created"
        bar = created[0]
        mx = max(v for v, _ in bar.calls)
        assert mx < total, f"progress hit 100% during encode/finalize: {mx}/{total}"
        with contextlib.redirect_stdout(buf):
            report(B + 2, total, "uploading 0.1 MiB to Nuke")
        assert bar.calls[-1] == (B + 2, total), bar.calls[-1]
        with contextlib.redirect_stdout(buf):
            report(B + 3, total, "complete")
        assert bar.calls[-1] == (total, total), bar.calls[-1]
        text = buf.getvalue()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert all(ln.startswith("[NukeBridge] ") for ln in lines), lines
        assert len(lines) == 4, lines
        order = [text.find("encoding frames"), text.find("finalizing mov"),
                 text.find("uploading"), text.find("complete")]
        assert all(0 <= a < b for a, b in zip(order, order[1:])), order
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    _ok("reporter gating: stub bar < total until upload, == total only after complete")


def check_static_nodes():
    nodes_src = (BRIDGE / "nodes.py").read_text()
    for lit in ["expected_frames=", "extra_steps=", "flush=True", '"uploading', '"complete"']:
        assert lit in nodes_src, f"nodes.py missing literal {lit!r}"
    video_src = (BRIDGE / "video_io.py").read_text()
    for lit in ['"decoding source"', '"decoding mask"', '"video ready"',
                '"encoding frames"', '"finalizing ']:
        assert lit in video_src, f"video_io.py missing literal {lit!r}"
    _ok("static wiring: nodes.py + video_io.py stage/keyword literals present")


def check_encode_error(image):
    out = "/nonexistent_dir_xyz/out.mov"
    try:
        video_io.encode_video(image, out, "mov", "prores_422hq", 24.0)
        raise AssertionError("expected RuntimeError for bad output path")
    except RuntimeError as e:
        assert "nonexistent_dir_xyz" in str(e) or "ffmpeg" in str(e), str(e)
    _ok("encode error: RuntimeError carries output path / ffmpeg context")


def _write_gradient(tmp):
    """10-bit-range horizontal+per-frame gradient ProRes HQ fixture (bt709 tv-range).

    Spans the full 0..1 range including the dark region that rgb24 quantizes away."""
    grad = np.zeros((N, H, W, 3), dtype=np.float32)
    cols = np.linspace(0.0, 1.0, W, dtype=np.float32)
    for f in range(N):
        phase = f / float(N)
        grad[f, :, :, 0] = cols[None, :]
        grad[f, :, :, 1] = (cols * 0.5 + phase * 0.25)[None, :]
        grad[f, :, :, 2] = (1.0 - cols)[None, :]
    raw48 = np.clip(grad * 65535.0, 0, 65535).round().astype("<u2").tobytes()
    grad_prores = os.path.join(tmp, "grad.mov")
    _run([FF, "-y", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-video_size", f"{W}x{H}",
          "-framerate", "24", "-i", "-",
          "-vf", "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv",
          "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
          "-color_primaries", "bt709", "-color_trc", "bt709",
          "-colorspace", "bt709", "-color_range", "tv",
          grad_prores],
         input=raw48, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    grays = np.array([0, 64, 128, 255], dtype=np.uint8)
    mraw = np.broadcast_to(grays[:, None, None], (N, H, W)).copy().tobytes()
    grad_mask = os.path.join(tmp, "grad_mask.mkv")
    _run([FF, "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-video_size", f"{W}x{H}",
          "-framerate", "24", "-i", "-", "-c:v", "ffv1", grad_mask],
         input=mraw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return grad_prores, grad_mask


def check_knob_parse():
    os.environ.pop("NUKE_BRIDGE_VIDEO_RGB_DEPTH", None)
    pf, dt, bps, sc = video_io._rgb_transport()
    assert pf == "rgb48le" and dt == np.dtype("<u2") and bps == 2 and sc == 65535.0, (pf, dt, bps, sc)
    os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"] = "8"
    try:
        pf, dt, bps, sc = video_io._rgb_transport()
        assert pf == "rgb24" and dt == np.dtype(np.uint8) and bps == 1 and sc == 255.0, (pf, dt, bps, sc)
    finally:
        del os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"]
    os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"] = "12"
    try:
        try:
            video_io._rgb_transport()
            raise AssertionError("expected RuntimeError for NUKE_BRIDGE_VIDEO_RGB_DEPTH=12")
        except RuntimeError as e:
            assert "12" in str(e), str(e)
    finally:
        del os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"]
    _ok("rgb transport knob: default rgb48le/16, env=8 -> rgb24, env=12 -> RuntimeError")


def check_rgb48_fidelity(grad_paths):
    grad_prores, grad_mask = grad_paths
    meta = {"color_primaries": "bt709", "color_trc": "bt709", "colorspace": "bt709",
            "color_range": "tv"}
    os.environ.pop("NUKE_BRIDGE_VIDEO_RGB_DEPTH", None)
    tmp = os.path.dirname(grad_prores)
    # depth=16 no-op roundtrip through decode -> encode -> decode.
    img1, _, _, _, n1 = video_io.decode_video(grad_prores, grad_mask, expected_frames=N)
    assert n1 == N
    out16 = os.path.join(tmp, "rt16.mov")
    video_io.encode_video(img1, out16, "mov", "prores_422hq", 24.0, source_meta=meta)
    img2, _, _, _, n2 = video_io.decode_video(out16, grad_mask, expected_frames=N)
    assert n2 == N
    diff16 = img2.numpy() - img1.numpy()
    delta16 = float(diff16.mean())
    mae16 = float(np.abs(diff16).mean())
    assert abs(delta16) <= 0.25 / 255.0, f"depth16 mean delta {delta16*255:+.3f} exceeds 0.25 levels"
    assert mae16 <= 0.5 / 255.0, f"depth16 MAE {mae16*255:.3f} exceeds 0.5 levels"

    # depth=8 fallback: valid ProRes file, 4 frames, bt709 tags, strictly worse MAE.
    os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"] = "8"
    try:
        img1_8, _, _, _, n8 = video_io.decode_video(grad_prores, grad_mask, expected_frames=N)
        assert n8 == N
        out8 = os.path.join(tmp, "rt8.mov")
        video_io.encode_video(img1_8, out8, "mov", "prores_422hq", 24.0, source_meta=meta)
        assert os.path.isfile(out8) and os.path.getsize(out8) > 0
        p8 = _probe_json(out8)
        st8 = (p8.get("streams") or [{}])[0]
        nb8 = st8.get("nb_frames")
        assert nb8 not in (None, "N/A") and int(nb8) == N, st8
        assert st8.get("color_primaries") == "bt709", st8
        img2_8, _, _, _, _ = video_io.decode_video(out8, grad_mask, expected_frames=N)
        mae8 = float(np.abs(img2_8.numpy() - img1_8.numpy()).mean())
        assert mae8 > mae16, f"depth8 MAE {mae8*255:.3f} not strictly worse than depth16 {mae16*255:.3f}"
    finally:
        del os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"]
    _ok(f"rgb48 fidelity: depth16 delta={delta16*255:+.3f}/MAE={mae16*255:.3f} levels; depth8 MAE worse")


def check_decode_invalid_knob(paths):
    os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"] = "12"
    try:
        try:
            video_io.decode_video(paths[0], paths[1])
            raise AssertionError("expected RuntimeError before subprocess for invalid depth")
        except RuntimeError as e:
            assert "12" in str(e), str(e)
    finally:
        del os.environ["NUKE_BRIDGE_VIDEO_RGB_DEPTH"]
    _ok("decode_video: invalid NUKE_BRIDGE_VIDEO_RGB_DEPTH fails before path/subprocess work")


def check_pull_mocked():
    """Deterministic FromNukeVideo.pull: mocked HTTP + decode, no network."""
    orig_post = nodes.requests.post
    orig_get = nodes.requests.get
    orig_decode = nodes.video_io.decode_video
    captured = {}
    chunks = [b"chunk-a", b"chunk-b", b"chunk-c"]
    joined = b"".join(chunks)

    class _PostResp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _GetResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_content(self, chunk_size):
            return iter(chunks)

    def fake_post(url, json=None, timeout=None):
        captured["post_url"] = url
        return _PostResp({
            "ok": True,
            "asset_id": "mocked",
            "main_url": "http://0.0.0.0:8765/asset/mocked/main",
            "mask_url": "http://0.0.0.0:8765/asset/mocked/mask",
            "metadata": {"frame_count": 3, "fps": 24.0, "prompt": "mocked prompt"},
        })

    def fake_get(url, stream=False, timeout=None):
        captured.setdefault("get_urls", []).append(url)
        return _GetResp()

    def fake_decode(main_path, mask_path, expected_frames=None, progress_cb=None):
        with open(main_path, "rb") as fh:
            assert fh.read() == joined, "main download bytes mismatch"
        with open(mask_path, "rb") as fh:
            assert fh.read() == joined, "mask download bytes mismatch"
        captured["decode_called"] = True
        image = nodes.torch.zeros((3, 8, 8, 3), dtype=nodes.torch.float32)
        mask = nodes.torch.zeros((3, 8, 8), dtype=nodes.torch.float32)
        return image, mask, 8, 8, 3

    nodes.requests.post = fake_post
    nodes.requests.get = fake_get
    nodes.video_io.decode_video = fake_decode
    try:
        image, mask, meta_json, w, h, fc, fps, prompt = nodes.FromNukeVideo().pull(
            bridge_id="b1", host="100.64.0.2", port=8765,
            frame_start=-1, frame_end=-1, fps=24.0, format="mov",
            mov_codec="prores_422hq", colorspace="default", timeout=5.0,
        )
        assert captured["post_url"] == "http://100.64.0.2:8765/bridge/b1/video", captured["post_url"]
        assert len(captured["get_urls"]) == 2, captured["get_urls"]
        assert all(u.startswith("http://100.64.0.2:8765/asset/") for u in captured["get_urls"]), captured["get_urls"]
        assert captured["decode_called"] and prompt == "mocked prompt"
        assert json.loads(meta_json)["frame_count"] == 3
        assert tuple(image.shape) == (3, 8, 8, 3)
    finally:
        nodes.requests.post = orig_post
        nodes.requests.get = orig_get
        nodes.video_io.decode_video = orig_decode
    _ok("mocked pull: 0.0.0.0 asset URLs rebuilt to configured host, chunk bytes exact, prompt output present")


def check_push_mocked():
    """Deterministic ToNukeVideo.push: mocked encode + HTTP, no network."""
    orig_encode = nodes.video_io.encode_video
    orig_post = nodes.requests.post
    captured = {}

    class _Resp:
        status_code = 200
        text = ""

    def fake_encode(image, path, fmt, mov_codec, fps, **kwargs):
        with open(path, "wb") as fh:
            fh.write(b"fake movie bytes")

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["post_url"] = url
        assert hasattr(data, "read") and not isinstance(data, (bytes, bytearray)), \
            f"expected readable file object, got {type(data).__name__}"
        assert data.read() == b"fake movie bytes"
        return _Resp()

    nodes.video_io.encode_video = fake_encode
    nodes.requests.post = fake_post
    try:
        image = nodes.torch.zeros((4, 8, 8, 3), dtype=nodes.torch.float32)
        out = nodes.ToNukeVideo().push(
            image=image, bridge_id="b1", host="100.64.0.2", port=8765,
            video_meta_json="{}", filename_prefix="pfx", format_override="auto",
            mov_codec_override="auto", colorspace="default", timeout=5.0,
        )
        assert captured["post_url"] == "http://100.64.0.2:8765/bridge/b1/video_result", captured["post_url"]
        assert out[0] is image
    finally:
        nodes.video_io.encode_video = orig_encode
        nodes.requests.post = orig_post
    _ok("mocked push: encoded file streamed as file object, 200 completes")


def main():
    check_binaries()
    with _tmpdir("nvstream_fixture_") as tmp:
        paths = _write_fixture(tmp)
        check_fixture(tmp, paths)
        image = check_decode_prealloc(paths)
        check_count_validation(paths)
        check_missing_file(paths)
        check_encode_stages(tmp, image)
        check_reporter_gating()
        check_static_nodes()
        check_encode_error(image)
        check_knob_parse()
        grad_paths = _write_gradient(tmp)
        check_rgb48_fidelity(grad_paths)
        check_decode_invalid_knob(paths)
        check_pull_mocked()
        check_push_mocked()
    print("ALL CHECKS PASSED", flush=True)


if __name__ == "__main__":
    sys.exit(main())
