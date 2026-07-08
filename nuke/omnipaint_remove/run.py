"""Run-current-frame plumbing for the OmniPaintRemove node.

Called from the `run_current_frame` PyScript knob (main thread). Renders the
source (input 1) and mask (input 0) to temp PNG8 files on the main thread,
then spawns a worker thread that invokes the external adapter:

    {python_executable} {backend_script} --source <src.png> --mask <mask.png> --output <out.png>

Inputs: 0 = Mask (white = remove, black = keep), 1 = Image/source plate.
Exit 0 + output file exists == success.

On success the output is canonized via `comfyui_bridge.result.save_result`.
All Nuke access from the worker thread goes through `comfyui_bridge.napi`.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import tempfile
import threading
from typing import Any, List, Tuple

from comfyui_bridge import comfy_progress, napi, result

# Per-node running guard keyed by node identity. No queue (Phase 1).
_RUNNING: set = set()

# Repo root (nuke/omnipaint_remove/run.py -> up three levels). Used only by the
# filesystem-only local backend check fallback (no heavy imports).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_REPO = os.path.join(
    _REPO_ROOT, ".slim", "clonedeps", "repos", "yeates__OmniPaint"
)
# Plugin-local implementation paths, resolved automatically (no UI knobs). The
# OmniPaint venv/repo are installed by tools/install_omnipaint_local.sh.
_VENV_PY = os.path.join(_REPO_ROOT, ".slim", "venvs", "omnipaint", "bin", "python")
_ADAPTER = os.path.join(_REPO_ROOT, "tools", "omnipaint_adapter.py")
# Relative paths the upstream OmniPaint CLI expects inside the repo.
_REPO_ARTIFACTS: Tuple[Tuple[str, str], ...] = (
    ("scripts/omnipaint_remove.py", "remove script"),
    ("weights/omnipaint_remove.safetensors", "weights"),
    ("demo_assets/embeddings/remove.npz", "embeddings"),
)

# Repo-local cache defaults. Nuke's env usually has no HF_HOME etc., so without
# these the adapter fails to find the already-downloaded FLUX model and tries to
# hit the network ("Access ... restricted ... Please log in."). Set from run.py's
# own location (not cwd) so they resolve regardless of how Nuke was launched.
_CACHE_ROOT = os.path.join(_REPO_ROOT, ".slim", "cache")
_CACHE_DEFAULTS: Tuple[Tuple[str, str], ...] = (
    ("HF_HOME", os.path.join(_CACHE_ROOT, "huggingface")),
    ("HUGGINGFACE_HUB_CACHE", os.path.join(_CACHE_ROOT, "huggingface", "hub")),
    ("TORCH_HOME", os.path.join(_CACHE_ROOT, "torch")),
    ("XDG_CACHE_HOME", os.path.join(_CACHE_ROOT, "xdg")),
)
# Local FLUX snapshot dir; if a usable snapshot is present, default to offline so
# Nuke runs never need network auth. black-forest-labs/FLUX.1-dev -> hub dir name
# uses -- separators.
_FLUX_CACHE_DIR = os.path.join(
    _CACHE_DEFAULTS[1][1], "models--black-forest-labs--FLUX.1-dev"
)


def _flux_snapshot_ok() -> bool:
    """True if a local FLUX snapshot has the files our optimized load needs.

    Mirrors the adapter's `_flux_snapshot_missing` (no heavy imports). The
    optimized load skips text encoders/tokenizers, so only the pipeline index,
    scheduler, transformer (config + a weight shard/index), and VAE (config +
    weight) are required.
    """
    snaps = os.path.join(_FLUX_CACHE_DIR, "snapshots")
    if not os.path.isdir(snaps):
        return False
    for name in os.listdir(snaps):
        snap = os.path.join(snaps, name)
        if not os.path.isdir(snap):
            continue
        if _flux_snapshot_has_required(snap):
            return True
    return False


def _flux_snapshot_has_required(snap: str) -> bool:
    def has(rel: str) -> bool:
        return os.path.isfile(os.path.join(snap, rel))

    for rel in ("model_index.json", "scheduler/scheduler_config.json",
                "transformer/config.json", "vae/config.json"):
        if not has(rel):
            return False
    idx = os.path.join(snap, "transformer", "diffusion_pytorch_model.safetensors.index.json")
    if os.path.isfile(idx):
        try:
            with open(idx, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            shards = sorted(set((data.get("weight_map") or {}).values()))
        except Exception:
            shards = []
        if not shards:
            return False
        for shard in shards:
            if not os.path.isfile(os.path.join(snap, "transformer", shard)):
                return False
    elif not (glob.glob(os.path.join(snap, "transformer", "diffusion_pytorch_model*.safetensors"))
              or has("transformer/model.safetensors")):
        return False
    if not (has("vae/diffusion_pytorch_model.safetensors")
            or has("vae/model.safetensors")
            or glob.glob(os.path.join(snap, "vae", "*.bin"))):
        return False
    return True


class _StepError(Exception):
    """Raised during prep to surface a clean status message."""


def run_current_frame(node: Any) -> None:
    """Entry point for the Run current frame button."""
    if not napi.has_nuke():
        return
    key = id(node)
    if key in _RUNNING:
        _set_status(node, "already running")
        return
    try:
        (py, script, src_png, mask_png, out_png, out_dir, create_read, frame, env,
         x0, y0, x1, y1, target_side) = _prepare(node)
    except _StepError as e:
        _set_status(node, str(e))
        return
    except Exception as e:  # render failure etc.
        _set_status(node, f"error: {e}")
        return

    _RUNNING.add(key)
    _set_status(node, f"running frame {frame}...")
    threading.Thread(
        target=_worker,
        args=(node, key, py, script, src_png, mask_png, out_png, out_dir,
              create_read, env, x0, y0, x1, y1, target_side),
        daemon=True,
    ).start()


def _resolve_paths(node: Any) -> Tuple[str, str, str]:
    """Resolve (python, backend_script, omnipaint_repo) from the plugin root.

    No UI knobs for these. A saved node from before this change may still carry
    the old (now hidden/removed) knob values; honor those if set and valid, else
    fall back to the plugin-local defaults. Python falls back to `python` if the
    venv is missing (callers surface a clear venv-missing error separately).
    """
    py = _kstr(node, "python_executable").strip()
    if not py or not os.path.isfile(py):
        py = _VENV_PY if os.path.isfile(_VENV_PY) else "python"
    script = _kstr(node, "backend_script").strip()
    if not script or not os.path.isfile(script):
        script = _ADAPTER
    repo = _kstr(node, "omnipaint_repo").strip()
    if not repo or not os.path.isdir(repo):
        repo = _DEFAULT_REPO
    return py, script, repo


# --- main-thread prep ------------------------------------------------------

def _read_box_knob_values(knob) -> Tuple[int, int, int, int]:
    """Read x, y, r, t from a Box-type knob."""
    x0 = int(float(knob.getValue(0)))
    y0 = int(float(knob.getValue(1)))
    x1 = int(float(knob.getValue(2)))
    y1 = int(float(knob.getValue(3)))
    return x0, y0, x1, y1


def _switch_value(node: Any) -> int:
    """Read the omnipaint_mask_source knob as int 0 or 1."""
    try:
        k = node.knob("omnipaint_mask_source")
        if k is None:
            return 0
        try:
            return int(k.getValue())
        except Exception:
            pass
        try:
            return int(k.value())
        except Exception:
            return 0
    except Exception:
        return 0


def _run_curvetool_autocrop(nuke: Any, ct: Any, frame: int) -> None:
    """Reset ROI then run CurveTool Auto Crop analysis for the current frame.

    resetROI and Go are mandatory: if either is missing or fails, raises.
    ROI is expression-linked in the gizmo, so no manual ROI setting here.
    """
    def _press(btn):
        if hasattr(btn, "execute"):
            btn.execute()
            return
        btn.setValue(1)

    # 1. resetROI — mandatory
    roi_btn = ct.knob("resetROI")
    if roi_btn is None:
        raise _StepError("CurveTool1 has no resetROI knob")
    _press(roi_btn)

    # 2. Go/go/update/Update — mandatory (first match; fail loudly)
    go_btn = None
    for btn_name in ("go", "Go", "update", "Update"):
        go_btn = ct.knob(btn_name)
        if go_btn is not None:
            break
    if go_btn is None:
        raise _StepError("CurveTool1 has no go/Go/update/Update knob")
    _press(go_btn)

    # 3. Fallback execute
    nuke.execute(ct, frame, frame)


def _execute_curvetool_and_write(node: Any, nuke: Any, frame: int,
                                  src_png: str, mask_png: str
                                  ) -> Tuple[int, int, int, int, int]:
    """Execute CurveTool1 inside the gizmo, read CropMask.box, write crops.

    Returns (x0, y0, x1, y1, target_side). target_side is always 1024 (Nuke
    exports pre-cropped 1024x1024 from the Crop expressions).
    """
    target_side = 1024
    try:
        node.begin()
    except Exception as e:
        raise _StepError(f"cannot enter gizmo ({e})")
    try:
        ct = nuke.toNode("CurveTool1")
        crop_mask = nuke.toNode("CropMask")
        if ct is None:
            raise _StepError("internal CurveTool1 node not found")
        if crop_mask is None:
            raise _StepError("internal CropMask node not found")
        try:
            _run_curvetool_autocrop(nuke, ct, frame)
        except _StepError:
            raise
        except Exception as e:
            raise _StepError(f"CurveTool1 autocrop failed: {e}")
        box = crop_mask.knob("box")
        if box is None:
            raise _StepError("CropMask.box knob not found")
        x0, y0, x1, y1 = _read_box_knob_values(box)
        if x1 <= x0 or y1 <= y0:
            raise _StepError("invalid crop box from CurveTool1")
        _wire_export(nuke, "WriteRGB", src_png, frame)
        _wire_export(nuke, "WriteMask", mask_png, frame)
    finally:
        try:
            node.end()
        except Exception:
            pass
    _set_node_knob(node, "crop_x0", x0)
    _set_node_knob(node, "crop_y0", y0)
    _set_node_knob(node, "crop_x1", x1)
    _set_node_knob(node, "crop_y1", y1)
    _set_node_knob(node, "target_side", target_side)
    return x0, y0, x1, y1, target_side


def _wire_export(nuke: Any, write_name: str, path: str, frame: int) -> None:
    """Set file path and execute a gizmo-internal Write node."""
    write = nuke.toNode(write_name)
    if write is None:
        raise _StepError(f"internal Write node missing: {write_name}")
    write.knob("file").setValue(path)
    nuke.execute(write, frame, frame)


def _prepare(node: Any):
    """Render source + mask crop PNGs via gizmo-internal CurveTool/Crop/Write."""
    nuke: Any = napi._nuke
    py, script, repo = _resolve_paths(node)
    out_dir = _kstr(node, "output_directory")
    create_read = _kbool(node, "create_read_on_result")
    env = _build_env(node)
    env["OMNIPAINT_PREPARED_CROP"] = "1"

    if node.input(1) is None:
        raise _StepError("Image input (1) not connected")
    if _switch_value(node) == 0 and node.input(0) is None:
        raise _StepError("Switch1=0 (Mask) selected but Mask input (0) is not connected")
    if not os.path.isfile(_VENV_PY):
        raise _StepError("local venv missing (.slim/venvs/omnipaint); run tools/install_omnipaint_local.sh")
    if not os.path.isfile(script):
        raise _StepError("backend script not found: " + script)
    if not os.path.isdir(repo):
        raise _StepError("OmniPaint repo missing: " + repo)
    if not out_dir:
        out_dir = os.path.join(os.path.expanduser("~"), "omnipaint_results")
    os.makedirs(out_dir, exist_ok=True)

    frame = int(nuke.frame())
    src_png = _tmp(".png")
    mask_png = _tmp(".png")
    out_png = _tmp(".png")

    x0, y0, x1, y1, target_side = _execute_curvetool_and_write(
        node, nuke, frame, src_png, mask_png
    )

    return py, script, src_png, mask_png, out_png, out_dir, create_read, frame, env, x0, y0, x1, y1, target_side


# --- worker thread ---------------------------------------------------------

def _read_progress(path: str):
    """Return (progress, message) from the adapter's progress JSON, or (None,None)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data.get("progress", 0)), str(data.get("message", "") or "")
    except Exception:
        return None, None


def _log_tail(out_log: str, err_log: str, rc: int) -> str:
    """Last useful line from the adapter logs (stderr first), else exit code."""
    for path in (err_log, out_log):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            if lines:
                return lines[-1][:200]
        except Exception:
            pass
    return f"exit {rc}"


def _create_result_placement(node: Any, napi_module: Any, path: str,
                             x0: int, y0: int, x1: int, y1: int, target_side: int) -> None:
    """Create Read + Transform in the root graph to place the result patch.

    The result is at crop resolution (x1-x0) x (y1-y0). Placement uses
    scale = crop_side / target_side, center = (target_side/2, target_side/2),
    translate = x0 + crop_side/2 - target_side/2. Transform is approved by the
    user-approved CropPreviewGizmo reference graph.
    """
    crop_side = max(1, x1 - x0)
    scale = crop_side / target_side if target_side else 1.0
    half_target = target_side / 2.0 if target_side else 512.0
    tr_x = x0 + crop_side / 2.0 - half_target
    tr_y = y0 + crop_side / 2.0 - half_target

    def work() -> None:
        nuke = napi_module._nuke
        read = nuke.nodes.Read(file=path)
        transform = nuke.nodes.Transform(inputs=[read])
        try:
            sc = transform.knob("scale")
            if sc is not None:
                sc.setValue(float(scale))
        except Exception:
            pass
        try:
            ct = transform.knob("center")
            if ct is not None:
                ct.setValue(float(half_target), 0)
                ct.setValue(float(half_target), 1)
        except Exception:
            pass
        try:
            tr = transform.knob("translate")
            if tr is not None:
                tr.setValue(float(tr_x), 0)
                tr.setValue(float(tr_y), 1)
        except Exception:
            pass
        try:
            lk = transform.knob("label")
            if lk is not None:
                lk.setValue(f"OmniPaint result ({crop_side}px @ {x0},{y0})")
        except Exception:
            pass

    try:
        napi_module.call(work)
    except Exception:
        pass


def _worker(
    node: Any,
    key: int,
    py: str,
    script: str,
    src_png: str,
    mask_png: str,
    out_png: str,
    out_dir: str,
    create_read: bool,
    env: dict,
    x0: int = 0,
    y0: int = 0,
    x1: int = 0,
    y1: int = 0,
    target_side: int = 0,
) -> None:
    import time as _time

    progress_file = _tmp(".json")
    out_log = _tmp(".log")
    err_log = _tmp(".log")
    task = comfy_progress._make_progress("OmniPaint removal")
    proc: Any = None
    out_fh = err_fh = None
    try:
        out_fh = open(out_log, "wb")
        err_fh = open(err_log, "wb")
        try:
            proc = subprocess.Popen(
                [py, script, "--source", src_png, "--mask", mask_png,
                 "--output", out_png, "--progress-file", progress_file],
                env=env, shell=False, stdout=out_fh, stderr=err_fh,
            )
        except FileNotFoundError:
            _set_status(node, f"error: python executable not found: {py}")
            return

        last = -1
        while True:
            pct, msg = _read_progress(progress_file)
            if pct is not None and pct != last:
                comfy_progress._progress_set(task, pct, msg)
                _set_status(node, msg or f"{pct}%")
                last = pct
            if comfy_progress._progress_cancelled(task):
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                _set_status(node, "cancelled")
                return
            if proc.poll() is not None:
                break
            _time.sleep(0.5)

        # Handles are closed in finally; subprocess has flushed by now.
        if proc.returncode == 0 and os.path.isfile(out_png):
            with open(out_png, "rb") as fh:
                body = fh.read()
            final_path = result.save_result(body, out_dir, "omnipaint", "", node, False, "png")
            if create_read and target_side:
                _create_result_placement(node, napi, final_path, x0, y0, x1, y1, target_side)
            _set_status(node, "done")
        else:
            _set_status(node, "error: " + _log_tail(out_log, err_log, proc.returncode))
    except Exception as e:
        _set_status(node, f"error: {e}")
    finally:
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        for fh in (out_fh, err_fh):
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        comfy_progress._progress_destroy(task)
        _RUNNING.discard(key)
        for p in (src_png, mask_png, out_png, progress_file, out_log, err_log):
            try:
                os.remove(p)
            except OSError:
                pass


# --- preview overlay (native Crop/CurveTool, main thread) ------------------
#
# The gizmo uses native Nuke nodes: CurveTool1 (AutoCrop on mask) drives
# CropMask/CropRGB box expressions, WriteRGB/WriteMask export the crops.
# RedOverlay + OverlayPositionTransform1 + MergeRedCrop compose the preview.
# Python executes CurveTool1 and reads CropMask.box for metadata.

def _set_node_knob(node, name: str, value) -> None:
    try:
        k = node.knob(name)
        if k is not None:
            k.setValue(value)
    except Exception:
        pass


def create_preview_overlay(node: Any) -> None:
    """Execute CurveTool1 autocrop for current frame, read CropMask.box, set status.

    Sets Switch1 to select mask source (separate or embedded alpha), resets ROI,
    runs CurveTool1 Auto Crop, reads the resulting crop box, and updates status.
    """
    if not napi.has_nuke():
        return
    if node.input(1) is None:
        _set_status(node, "Image input (1) not connected")
        return
    nuke: Any = napi._nuke
    frame = int(nuke.frame())
    target_side = 1024
    x0 = y0 = x1 = y1 = 0
    if _switch_value(node) == 0 and node.input(0) is None:
        _set_status(node, "Switch1=0 (Mask) selected but Mask input (0) is not connected")
        return
    try:
        node.begin()
        try:
            ct = nuke.toNode("CurveTool1")
            crop_mask = nuke.toNode("CropMask")
            if ct is None or crop_mask is None:
                _set_status(node, "preview error: CurveTool1 or CropMask not found")
                return
            try:
                _run_curvetool_autocrop(nuke, ct, frame)
            except Exception as e:
                _set_status(node, f"preview error: CurveTool1 autocrop failed: {e}")
                return
            try:
                box = crop_mask.knob("box")
                if box is None:
                    _set_status(node, "preview error: CropMask.box knob not found")
                    return
                x0, y0, x1, y1 = _read_box_knob_values(box)
            except Exception as e:
                _set_status(node, f"preview error: cannot read crop box: {e}")
                return
            if x1 <= x0 or y1 <= y0:
                _set_status(node, "preview error: invalid crop box")
                return
        finally:
            try:
                node.end()
            except Exception:
                pass
    except Exception as e:
        _set_status(node, f"preview error: {e}")
        return
    _set_node_knob(node, "crop_x0", x0)
    _set_node_knob(node, "crop_y0", y0)
    _set_node_knob(node, "crop_x1", x1)
    _set_node_knob(node, "crop_y1", y1)
    _set_node_knob(node, "target_side", target_side)
    _set_node_knob(node, "preview_enabled", True)
    _set_status(node, f"preview crop: {x0},{y0},{x1},{y1} -> {target_side}")


def clear_preview_overlay(node: Any) -> None:
    """Clear preview state."""
    if not napi.has_nuke():
        return
    _set_node_knob(node, "preview_enabled", False)
    _set_status(node, "preview cleared")


# --- small helpers ---------------------------------------------------------

def check_backend(node: Any) -> None:
    """Entry point for the Check backend button.

    Pre-flights the plugin-local setup (venv / adapter / repo) with clear
    messages, then runs the adapter `--check`. Falls back to filesystem-only
    checks if the adapter rejects `--check`. Sets a one-line status. No heavy
    imports here.
    """
    if not napi.has_nuke():
        return
    py, script, repo = _resolve_paths(node)
    problems: List[str] = []
    if not os.path.isfile(_VENV_PY):
        problems.append("local venv missing (.slim/venvs/omnipaint); run tools/install_omnipaint_local.sh")
    if not os.path.isfile(script):
        problems.append("backend script missing: " + script)
    if not os.path.isdir(repo):
        problems.append("OmniPaint repo missing: " + repo)
    if problems:
        _set_status(node, "setup incomplete: " + "; ".join(problems))
        return
    env = _build_env(node)
    try:
        proc = subprocess.run(
            [py, script, "--check"],
            env=env,
            shell=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        _set_status(node, f"python executable not found: {py}")
        return

    if proc.returncode == 0:
        line = (proc.stdout or "").strip().splitlines()
        _set_status(node, (line[0] if line else "backend OK"))
        return

    combined = (proc.stderr or "") + (proc.stdout or "")
    low = combined.lower()
    # Adapter without --check support (argparse rejects the flag): local checks.
    if "unrecognized" in low and "--check" in low:
        _set_status(node, _local_check(env))
        return
    lines = combined.strip().splitlines()
    _set_status(node, "backend error: " + (lines[-1] if lines else f"exit {proc.returncode}"))


def clear_vram(node: Any) -> None:
    """Clear CUDA cache in a short-lived adapter process; future service hook point."""
    if not napi.has_nuke():
        return
    py, script, _repo = _resolve_paths(node)
    env = _build_env(node)
    try:
        proc = subprocess.run(
            [py, script, "--clear-vram"], env=env, shell=False,
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        _set_status(node, f"python executable not found: {py}")
        return
    lines = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    msg = lines[-1] if lines else f"exit {proc.returncode}"
    if proc.returncode == 0:
        _set_status(node, msg)
    else:
        _set_status(node, "clear VRAM error: " + msg)


def _local_check(env: dict) -> str:
    """Filesystem-only checks when the adapter has no `--check` mode."""
    repo = env.get("OMNIPAINT_REPO", "").strip() or _DEFAULT_REPO
    problems: List[str] = []
    if not os.path.isdir(repo):
        problems.append(f"repo missing: {repo}")
    else:
        for rel, label in _REPO_ARTIFACTS:
            if not os.path.isfile(os.path.join(repo, rel)):
                problems.append(f"{label} missing")
    return "backend (local check): " + ("OK" if not problems else "; ".join(problems))


def _build_env(node: Any) -> dict:
    """Build the adapter environment from node knobs (main thread)."""
    env = os.environ.copy()
    # repo/python/backend are resolved from the plugin root, not user knobs.
    _, _, repo = _resolve_paths(node)
    env["OMNIPAINT_REPO"] = repo
    env["OMNIPAINT_DEVICE"] = _kstr(node, "omnipaint_device").strip() or "cuda:0"
    env["OMNIPAINT_STEPS"] = str(_kint(node, "omnipaint_steps") or 28)
    _seed_k = node.knob("omnipaint_seed")
    env["OMNIPAINT_SEED"] = str(int(_seed_k.value()) if _seed_k is not None else 42)
    env["OMNIPAINT_MAX_SIDE"] = str(_kint(node, "omnipaint_max_side") or 1024)
    env["OMNIPAINT_LOW_VRAM"] = "1" if _kbool(node, "omnipaint_low_vram") else "0"
    # NF4 quantization is opt-in via the omnipaint_quant knob. Forward only when
    # explicitly 'nf4'; otherwise leave unset (adapter defaults to off).
    if _kstr(node, "omnipaint_quant").strip().lower() == "nf4":
        env["OMNIPAINT_QUANT"] = "nf4"
    else:
        env.pop("OMNIPAINT_QUANT", None)
    # OMNIPAINT_OUTPUT_MODE intentionally unset: adapter defaults to 'composite'
    # (full-res result). 'generated_overlay' is a CLI/debug mode only.
    # Repo-local HF/torch cache so the already-downloaded FLUX model is found
    # without network auth (Nuke's env usually has none of these set).
    for name, default in _CACHE_DEFAULTS:
        if not env.get(name):
            env[name] = default
    # Default to offline when a usable local FLUX snapshot is present, so Nuke
    # runs never trip the "access restricted, please log in" path. Skip if the
    # user explicitly disabled local-only (OMNIPAINT_LOCAL_FILES_ONLY=0).
    local_only_off = (env.get("OMNIPAINT_LOCAL_FILES_ONLY", "1").strip().lower() in ("0", "false", "no", "off"))
    if not env.get("HF_HUB_OFFLINE") and not local_only_off and _flux_snapshot_ok():
        env["HF_HUB_OFFLINE"] = "1"
    # Help PyTorch avoid fragmentation OOM alongside Nuke/ComfyUI GPU consumers.
    if not env.get("PYTORCH_CUDA_ALLOC_CONF"):
        env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    # Memory mode: from the omnipaint_memory_mode knob; backward-compat via low_vram.
    _mem = _kstr(node, "omnipaint_memory_mode").strip().lower()
    _MEM_MAP = {
        "auto block swap": "auto_block_swap",
        "manual block swap": "manual_block_swap",
        "fast gpu": "fast_gpu",
        "sequential offload": "sequential_offload",
    }
    env["OMNIPAINT_MEMORY_MODE"] = _MEM_MAP.get(_mem, "auto_block_swap")
    env["OMNIPAINT_SWAP_BLOCKS"] = str(_kint(node, "omnipaint_swap_blocks") or 0)
    attn = _kstr(node, "omnipaint_attention").strip().lower()
    env["OMNIPAINT_ATTENTION"] = attn if attn in ("auto", "default", "sage") else "auto"
    return env


def _set_status(node: Any, msg: str) -> None:
    try:
        napi.set_knob_value(node, "status", str(msg))
    except Exception:
        pass


def _kstr(node: Any, name: str) -> str:
    try:
        k = node.knob(name)
        return str(k.value()) if k is not None else ""
    except Exception:
        return ""


def _kbool(node: Any, name: str) -> bool:
    try:
        k = node.knob(name)
        return bool(k.value()) if k is not None else False
    except Exception:
        return False


def _kint(node: Any, name: str) -> int:
    try:
        k = node.knob(name)
        return int(k.value()) if k is not None else 0
    except Exception:
        return 0


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="omnipaint_", suffix=suffix)
    os.close(fd)
    return path
