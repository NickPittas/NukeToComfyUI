#!/usr/bin/env python3
"""OmniPaint adapter for the Nuke OmniPaintRemove node.

Contract used by `nuke/omnipaint_remove/run.py`:

    python omnipaint_adapter.py --source SRC.png --mask MASK.png --output OUT.png

Mask semantics: white = remove, black = keep.

Execution model — both modes use the same optimized internal implementation
(an equivalent of upstream `process_single_image`); the upstream CLI script is
not used for normal runs. The optimization: OmniPaint removal uses static
embeddings (`remove.npz`) and never encodes text, so FLUX is loaded with
`text_encoder=None, text_encoder_2=None, tokenizer=None, tokenizer_2=None` to
skip the unused encoders/tokenizers and avoid the VRAM the upstream CLI wastes
there (falls back to a normal load on diffusers versions that reject the skip
kwargs).

- Fast GPU (`OMNIPAINT_LOW_VRAM=0`): after LoRA + adapter setup the
  (text-encoder-free) pipeline is moved with `.to(device)`. Fastest; fits in much
  less VRAM than the upstream CLI because the encoders are not loaded.
- Low-VRAM (`OMNIPAINT_LOW_VRAM=1`): `pipe.enable_sequential_cpu_offload(gpu_id=...)`
  instead of `.to(device)`. Sequential offload hooks every submodule individually,
  so it is compatible with OmniPaint's custom `tranformer_forward` (which calls
  submodules directly and bypasses whole-model hooks); the model never has to fit
  wholesale in free VRAM. Slower (per-submodule host<->device transfer each step)
  but coexists with other GPU consumers (Nuke/ComfyUI).

In both modes condition encoding is routed to the pipeline's `_execution_device`
via `_offload_safe_encode_images`. In the Nuke node these are exposed as the
`omnipaint_low_vram` (default on), `omnipaint_max_side`, `omnipaint_steps`, and
`omnipaint_seed` knobs, which the node forwards as the corresponding
`OMNIPAINT_*` env vars.

Backend health check (no model load):

    python omnipaint_adapter.py --check

Validates repo + upstream script + weights + embeddings presence and that
PIL/torch/diffusers import. Exit 0/nonzero.

Configuration is intentionally environment-based so the Nuke node can keep a
small, stable contract:

    OMNIPAINT_REPO=/path/to/OmniPaint        optional repo override
    OMNIPAINT_DEVICE=cuda:0                  default cuda:0
    OMNIPAINT_STEPS=28                       default 28
    OMNIPAINT_SEED=42                        default 42
    OMNIPAINT_LOW_VRAM=1                     default 1; 0 = fast GPU mode if enough free VRAM
    OMNIPAINT_OUTPUT_MODE=composite          composite | generated_overlay
    OMNIPAINT_MAX_SIDE=1024                  default 1024; condition image resize cap
    OMNIPAINT_LOCAL_FILES_ONLY=1            default 1; FLUX load uses local_files_only=True (offline).
                                             Set 0 to allow re-downloading / online fetch.
    OMNIPAINT_QUANT=                         default '' (off). 'nf4' = load the FLUX
                                             transformer bitsandbytes-NF4 4-bit (~6 GiB vs
                                             ~23 GiB bf16) and run resident (sequential offload
                                             is incompatible with bnb 4-bit device pinning; it is
                                             auto-disabled with a warning when low_vram=1). Needs
                                             `pip install bitsandbytes`. Default is byte-identical
                                             to the unquantized path.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
from pathlib import Path


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[1]


def _apply_local_cache_defaults() -> None:
    """Point HF/torch caches at the repo-local .slim/cache unless already set.

    Called before any HF/diffusers load or cache check so standalone runs (no
    Nuke env, e.g. `--check` from a shell) find the model that
    install_omnipaint_local.sh downloaded. Uses setdefault, so explicit env
    (including what the Nuke node sets) always wins.
    """
    cache = _repo_root() / ".slim" / "cache"
    defaults = {
        "HF_HOME": str(cache / "huggingface"),
        "HUGGINGFACE_HUB_CACHE": str(cache / "huggingface" / "hub"),
        "TORCH_HOME": str(cache / "torch"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)


def _omnipaint_repo() -> Path:
    override = os.environ.get("OMNIPAINT_REPO", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / ".slim" / "clonedeps" / "repos" / "yeates__OmniPaint"


def _flux_cache_dir() -> Path:
    """Resolve the local FLUX model dir under the HF hub cache.

    Honors `HUGGINGFACE_HUB_CACHE` (set by the Nuke node) when present, else
    falls back to the repo-local cache default.
    """
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE", "").strip()
    if not hub:
        hub = str(_repo_root() / ".slim" / "cache" / "huggingface" / "hub")
    return Path(hub) / "models--black-forest-labs--FLUX.1-dev"


def _flux_snapshot_missing() -> list:
    """Return required-file paths missing from the local FLUX snapshot.

    Empty list == a usable snapshot for our optimized load (which skips the
    text encoders/tokenizers, so those are NOT required). We require the
    pipeline index, scheduler, transformer (config + a weight shard or shard
    index), and VAE (config + weight). Falls back to a single descriptive
    entry if there is no snapshot at all.
    """
    snaps = _flux_cache_dir() / "snapshots"
    try:
        revs = [d for d in snaps.iterdir() if d.is_dir()] if snaps.is_dir() else []
    except OSError:
        revs = []
    if not revs:
        return [str(snaps) + "/<rev> (no FLUX snapshot present)"]

    snap = revs[0]
    missing = [
        rel
        for rel in (
            "model_index.json",
            "scheduler/scheduler_config.json",
            "transformer/config.json",
            "vae/config.json",
        )
        if not (snap / rel).is_file()
    ]
    # transformer weights: if an index exists, every referenced shard must exist.
    idx = snap / "transformer" / "diffusion_pytorch_model.safetensors.index.json"
    if idx.is_file():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            shards = sorted(set((data.get("weight_map") or {}).values()))
        except Exception:
            shards = []
        if not shards:
            missing.append("transformer/diffusion_pytorch_model.safetensors.index.json (invalid weight_map)")
        else:
            for shard in shards:
                if not (snap / "transformer" / shard).is_file():
                    missing.append("transformer/" + shard)
    elif not (
        glob.glob(str(snap / "transformer" / "diffusion_pytorch_model*.safetensors"))
        or (snap / "transformer" / "model.safetensors").is_file()
    ):
        missing.append("transformer/*.safetensors (weight shard/index)")
    # VAE weights
    if not (
        (snap / "vae" / "diffusion_pytorch_model.safetensors").is_file()
        or (snap / "vae" / "model.safetensors").is_file()
        or glob.glob(str(snap / "vae" / "*.bin"))
    ):
        missing.append("vae/*.safetensors|*.bin (weight)")
    return missing


def _quant_mode() -> str:
    """OMNIPAINT_QUANT backend: '' (off, default) | 'nf4'. Lower-cased + stripped.

    When set to 'nf4' the FLUX transformer is loaded bitsandbytes-NF4 4-bit
    (~6 GiB vs ~23 GiB bf16) and the pipeline runs resident (sequential CPU
    offload is incompatible with bnb 4-bit device-pinned weights). Default '' is
    byte-identical to the unquantized load path.
    """
    return os.environ.get("OMNIPAINT_QUANT", "").strip().lower()


def _flux_snapshot_dir() -> Path:
    """First local FLUX.1-dev snapshot revision (for quantized subfolder loads).

    diffusers 0.31.0's subcomponent load with a repo_id + local_files_only hits a
    None local_dir bug in hub_utils._check_if_shards_exist_locally; loading from
    the resolved snapshot path sidesteps it.
    """
    snap = _flux_cache_dir() / "snapshots"
    revs = [d for d in snap.iterdir() if d.is_dir()] if snap.is_dir() else []
    if not revs:
        raise SystemExit(
            "OMNIPAINT_QUANT needs a local FLUX snapshot; none under " + str(snap)
        )
    return revs[0]


def _build_nf4_transformer(torch, local_only: bool):
    """Load only the FLUX transformer, NF4-quantized via bitsandbytes."""
    try:
        import bitsandbytes  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "OMNIPAINT_QUANT=nf4 needs bitsandbytes installed (pip install bitsandbytes)."
        ) from exc
    from diffusers import BitsAndBytesConfig, FluxTransformer2DModel

    qcfg = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    return FluxTransformer2DModel.from_pretrained(
        str(_flux_snapshot_dir()), subfolder="transformer",
        quantization_config=qcfg, torch_dtype=torch.bfloat16, local_files_only=local_only,
    )


def _env_int(name: str, default: int, min_val=None) -> int:
    """Read an integer env var. If `min_val` is given, enforce value >= min_val.

    Self-contained (reads os.environ inline) and raises SystemExit with a clear
    message on a missing int or below-min value.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if min_val is not None and value < min_val:
        raise SystemExit(f"{name} must be >= {min_val}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _memory_mode(fallback_low_vram=None) -> str:
    """Resolve OMNIPAINT_MEMORY_MODE; backward-compat via low_vram env.

    Returns one of: auto_block_swap, manual_block_swap, fast_gpu,
    sequential_offload. If unset/unknown, falls back to the legacy
    OMNIPAINT_LOW_VRAM bool (True -> sequential_offload, False -> fast_gpu).
    """
    raw = os.environ.get("OMNIPAINT_MEMORY_MODE", "").strip().lower()
    if raw in ("auto_block_swap", "manual_block_swap", "fast_gpu", "sequential_offload"):
        return raw
    if raw:
        print(f"unknown OMNIPAINT_MEMORY_MODE={raw!r}; using low_vram fallback", file=sys.stderr)
    if fallback_low_vram is None:
        fallback_low_vram = _env_bool("OMNIPAINT_LOW_VRAM", True)
    return "sequential_offload" if fallback_low_vram else "fast_gpu"


def _attention_backend() -> str:
    raw = os.environ.get("OMNIPAINT_ATTENTION", "auto").strip().lower()
    return raw if raw in ("auto", "default", "sage") else "auto"


def _patch_sage_attention(flux_core, backend: str):
    """Patch OmniPaint's SDPA call to SageAttention. Returns a restore callback."""
    if backend == "default":
        return lambda: None
    try:
        from sageattention import sageattn
    except Exception as exc:
        if backend == "sage":
            raise SystemExit(
                "OMNIPAINT_ATTENTION=sage needs SageAttention installed. Run: "
                "OMNIPAINT_INSTALL_SAGE=1 bash tools/install_omnipaint_local.sh"
            ) from exc
        print(f"[OmniPaint] SageAttention unavailable; using default SDPA ({exc})", file=sys.stderr)
        return lambda: None

    orig = flux_core.F.scaled_dot_product_attention

    def _sage_or_sdpa(q, k, v, attn_mask=None, dropout_p=0.0, is_causal=False, **kw):
        if attn_mask is not None or dropout_p:
            return orig(q, k, v, attn_mask=attn_mask, dropout_p=dropout_p, is_causal=is_causal, **kw)
        try:
            return sageattn(q, k, v, tensor_layout="HND", is_causal=is_causal)
        except TypeError:
            return sageattn(q, k, v, is_causal=is_causal)
        except Exception as exc:
            if backend == "sage":
                raise
            print(f"[OmniPaint] SageAttention failed; falling back to SDPA ({exc})", file=sys.stderr)
            return orig(q, k, v, attn_mask=attn_mask, dropout_p=dropout_p, is_causal=is_causal, **kw)

    flux_core.F.scaled_dot_product_attention = _sage_or_sdpa
    print("[OmniPaint] attention=sage", file=sys.stderr)

    def _restore():
        flux_core.F.scaled_dot_product_attention = orig

    return _restore


def _gpu_index(device: str) -> int:
    """'cuda:2' -> 2, 'cuda' or anything non-numeric -> 0."""
    if ":" in device:
        try:
            return int(device.split(":", 1)[1])
        except ValueError:
            return 0
    return 0


def _guard_fast_gpu_memory(torch, device: str) -> None:
    """Fail before full `.to(cuda)` when fast mode cannot fit safely."""
    if not device.startswith("cuda") or not torch.cuda.is_available():
        return
    min_gb = float(os.environ.get("OMNIPAINT_FAST_MIN_FREE_GB", "24") or "24")
    free, _total = torch.cuda.mem_get_info(_gpu_index(device))
    free_gb = free / (1024 ** 3)
    if free_gb < min_gb:
        raise SystemExit(
            f"fast GPU mode needs at least {min_gb:.1f} GiB free VRAM; "
            f"only {free_gb:.1f} GiB free. Enable low VRAM mode."
        )


def _require(path: Path, label: str) -> Path:
    if not path.exists():
        raise SystemExit(f"{label} not found: {path}")
    return path


def _write_normalized_mask(mask_path: Path, output_path: Path) -> None:
    """Write single-channel mask PNG, using alpha if it carries the mask.

    Upstream OmniPaint converts masks with `.convert("L")`, which ignores alpha.
    Nuke masks may carry the useful matte in alpha, so normalize here.
    """
    from PIL import Image  # local import; keeps --check usable without PIL

    img = Image.open(mask_path)
    if "A" in img.getbands():
        alpha = img.getchannel("A")
        lo, hi = alpha.getextrema()
        if lo != hi:
            alpha.save(output_path)
            return
    img.convert("L").save(output_path)


# Max side (px) for the condition image, matching upstream MAX_LENGTH.
_MAX_LENGTH = 1024


def _offload_safe_encode_images(pipeline, images):
    """Drop-in replacement for upstream `src.flux_core.encode_images`.

    Under accelerate model-cpu-offload, `pipeline.device` reports CPU while the
    VAE weights are paged to CUDA by the offload hook, so the upstream
    `images.to(pipeline.device)` leaves inputs on CPU and the VAE conv fails
    with `Input type CPUBFloat16Type and weight type CUDABFloat16Type`.

    Fix: route tensors to the pipeline's true execution device via
    `getattr(pipeline, '_execution_device', pipeline.device)`. Upstream logic
    (preprocess, VAE encode, shift/scale, `_pack_latents`,
    `_prepare_latent_image_ids`, fallback shape handling) is otherwise preserved.
    """
    device = getattr(pipeline, "_execution_device", pipeline.device)
    # Under block-swap mode the pipe was never .to(device), so
    # _execution_device reports CPU while the VAE was explicitly moved to GPU.
    # Prefer the VAE's actual device when it's on GPU.
    vae_device = getattr(getattr(pipeline, "vae", None), "device", None)
    if vae_device is not None and str(vae_device) != "cpu":
        device = vae_device
    # Use the VAE's dtype (the module actually encoding here) rather than
    # `pipeline.dtype`: for a bitsandbytes-NF4 transformer `pipeline.dtype`
    # resolves to uint8 (quantized storage) and would cast inputs to uint8,
    # crashing the VAE conv. VAE is never quantized, so this is exact for VAE
    # encode and identical to `pipeline.dtype` for the unquantized path.
    dtype = getattr(pipeline.vae, "dtype", pipeline.dtype)
    images = pipeline.image_processor.preprocess(images)
    images = images.to(device).to(dtype)
    images = pipeline.vae.encode(images).latent_dist.sample()
    images = (
        images - pipeline.vae.config.shift_factor
    ) * pipeline.vae.config.scaling_factor
    images_tokens = pipeline._pack_latents(images, *images.shape)
    images_ids = pipeline._prepare_latent_image_ids(
        images.shape[0],
        images.shape[2],
        images.shape[3],
        device,
        dtype,
    )
    if images_tokens.shape[1] != images_ids.shape[0]:
        images_ids = pipeline._prepare_latent_image_ids(
            images.shape[0],
            images.shape[2] // 2,
            images.shape[3] // 2,
            device,
            pipeline.dtype,
        )
    return images_tokens, images_ids


def _patch_encode_images_for_offload() -> None:
    """Patch upstream `encode_images` in both namespaces it is reached through.

    `src/condition.py` did `from .flux_core import encode_images`, binding its
    own reference at import time, and `Condition.encode` calls that bound name.
    So patching `flux_core.encode_images` alone has no effect on the runtime
    path; `src.condition.encode_images` must be patched too.
    """
    import src.flux_core as _flux_core
    import src.condition as _condition

    _flux_core.encode_images = _offload_safe_encode_images
    _condition.encode_images = _offload_safe_encode_images


def _write_progress(progress_file, progress, message):
    """Atomically write {progress, message} JSON to `progress_file` (best-effort)."""
    if not progress_file:
        return
    data = {"progress": int(progress), "message": str(message)}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(os.path.abspath(progress_file)) or None, suffix=".json"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, progress_file)
    except Exception:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _normalized_mask_image(mask_path):
    """Single-channel (L) mask: alpha channel if it carries the mask, else luma."""
    from PIL import Image

    img = Image.open(mask_path)
    if "A" in img.getbands():
        alpha = img.getchannel("A")
        lo, hi = alpha.getextrema()
        if lo != hi:
            return alpha
    return img.convert("L")


def _bbox_from_mask(mask_l):
    """Bounding box (x0,y0,x1,y1) of pixels > 0 in an L mask, or None if empty."""
    import numpy as np

    arr = np.asarray(mask_l)
    ys, xs = np.where(arr > 0)
    if xs.size == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def compute_crop_info(source_path, mask_path, padding, max_side):
    """Compute the square-crop geometry without loading FLUX (JSON-serializable).

    source_size is [w,h]; bbox and crop_box are in source pixel coords. The mask
    is normalized (alpha-if-varying else luma) and resized to source size before
    the bbox. padded_side = max(bbox w,h) + 2*padding (the square). crop_box is
    that square centered on the bbox, clamped to source bounds. target_side =
    min(max_side, padded_side) rounded DOWN to a multiple of 8 (min 64) — the
    resolution OmniPaint actually runs at.
    """
    from PIL import Image

    src = Image.open(source_path)
    sw, sh = src.size
    mask_l = (
        _normalized_mask_image(mask_path)
        .convert("L")
        .resize((sw, sh), Image.Resampling.LANCZOS)
    )
    bbox = _bbox_from_mask(mask_l)
    if bbox is None:
        raise SystemExit("mask is empty (no pixels > 0); nothing to remove")
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    side = max(bw, bh) + 2 * padding
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    x0 = int(round(cx - side / 2.0))
    y0 = int(round(cy - side / 2.0))
    x1, y1 = x0 + side, y0 + side
    # clamp to source bounds, then keep `side`-wide where possible
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(sw, x1)
    y1 = min(sh, y1)
    x0 = max(0, x1 - side)
    y0 = max(0, y1 - side)
    target_side = max(64, (min(max_side, side) // 8) * 8)
    return {
        "source_size": [sw, sh],
        "bbox": [bbox[0], bbox[1], bbox[2], bbox[3]],
        "crop_box": [x0, y0, x1, y1],
        "pad": int(padding),
        "padded_side": int(side),
        "target_side": int(target_side),
    }


def _prepare_crop(source_path, mask_path, padding, max_side):
    """Build the square condition + mask for generate, plus the full-res rebuild ctx."""
    import numpy as np
    from PIL import Image

    info = compute_crop_info(source_path, mask_path, padding, max_side)
    x0, y0, x1, y1 = info["crop_box"]
    side, target = info["padded_side"], info["target_side"]
    crop_w, crop_h = x1 - x0, y1 - y0

    src = Image.open(source_path)
    has_alpha = "A" in src.getbands()
    alpha_full = src.getchannel("A") if has_alpha else None
    src_rgb = src.convert("RGB")
    mask_full = (
        _normalized_mask_image(mask_path)
        .convert("L")
        .resize(src_rgb.size, Image.Resampling.LANCZOS)
    )

    top = (side - crop_h) // 2
    bottom = side - crop_h - top
    left = (side - crop_w) // 2
    right = side - crop_w - left

    # source crop -> padded square (edge replication for out-of-source pixels)
    src_crop = np.asarray(src_rgb.crop((x0, y0, x1, y1)))
    src_sq = np.pad(src_crop, ((top, bottom), (left, right), (0, 0)), mode="edge")
    cond_rgb = Image.fromarray(src_sq, "RGB").resize((target, target), Image.Resampling.LANCZOS)

    # mask crop -> padded square (black/0 padding), kept at source-crop res too
    mask_crop = np.asarray(mask_full.crop((x0, y0, x1, y1)))
    original_mask_crop = Image.fromarray(mask_crop, "L")
    mask_sq = np.pad(mask_crop, ((top, bottom), (left, right)), mode="constant", constant_values=0)
    gen_mask = Image.fromarray(mask_sq, "L").resize((target, target), Image.Resampling.LANCZOS)

    ctx = {
        "info": info,
        "src_rgb": src_rgb,
        "alpha_full": alpha_full,
        "crop_box": (x0, y0, x1, y1),
        "side": side,
        "top": top,
        "left": left,
        "crop_w": crop_w,
        "crop_h": crop_h,
        "original_mask_crop": original_mask_crop,
    }
    return cond_rgb, gen_mask, ctx


def _rebuild_fullres(result_square, ctx, output):
    """Resize generated square back to padded side, composite into source, save.

    The generated square (target_side) is upsized to padded_side, the pad is
    stripped to the clamped source-crop size, and the result is composited into
    the original RGB using the original mask crop as alpha (white=remove).
    Original source alpha is reattached unchanged. Output is full source size.
    """
    from PIL import Image

    side = ctx["side"]
    top, left = ctx["top"], ctx["left"]
    crop_w, crop_h = ctx["crop_w"], ctx["crop_h"]
    x0, y0 = ctx["crop_box"][0], ctx["crop_box"][1]
    src_rgb = ctx["src_rgb"]

    big = result_square.resize((side, side), Image.Resampling.LANCZOS)
    generated_crop = big.crop((left, top, left + crop_w, top + crop_h)).convert("RGB")
    original_crop = src_rgb.crop((x0, y0, x0 + crop_w, y0 + crop_h)).convert("RGB")
    blended = Image.composite(generated_crop, original_crop, ctx["original_mask_crop"])
    src_rgb.paste(blended, (x0, y0))

    if ctx["alpha_full"] is not None:
        out = src_rgb.convert("RGBA")
        out.putalpha(ctx["alpha_full"])
    else:
        out = src_rgb.convert("RGB")
    out.save(str(output))


def _rebuild_overlay(result_square, ctx, output):
    """Save full-frame generated RGBA overlay; final merge belongs to Nuke."""
    from PIL import Image

    side = ctx["side"]
    top, left = ctx["top"], ctx["left"]
    crop_w, crop_h = ctx["crop_w"], ctx["crop_h"]
    x0, y0 = ctx["crop_box"][0], ctx["crop_box"][1]
    src_rgb = ctx["src_rgb"]

    big = result_square.resize((side, side), Image.Resampling.LANCZOS)
    generated_crop = big.crop((left, top, left + crop_w, top + crop_h)).convert("RGB")
    overlay = Image.new("RGBA", src_rgb.size, (0, 0, 0, 0))
    crop_rgba = generated_crop.convert("RGBA")
    crop_rgba.putalpha(ctx["original_mask_crop"])
    overlay.paste(crop_rgba, (x0, y0))
    overlay.save(str(output))


def _prepare_prepared(source_path, mask_path):
    """Prepare condition + mask from a pre-cropped source (OMNIPAINT_PREPARED_CROP=1).

    Nuke already exported the cropped+sized source/mask at model resolution via
    internal Crop/Reformat/Write nodes. We just normalize the mask and round
    dimensions to a multiple of 8.
    """
    from PIL import Image

    cond_rgb = Image.open(source_path).convert("RGB")
    gen_mask = _normalized_mask_image(mask_path).convert("L")
    w, h = cond_rgb.size
    nw, nh = (w // 8) * 8, (h // 8) * 8
    if (nw, nh) != (w, h):
        cond_rgb = cond_rgb.resize((nw, nh), Image.Resampling.LANCZOS)
        gen_mask = gen_mask.resize((nw, nh), Image.Resampling.LANCZOS)
    return cond_rgb, gen_mask


def _save_prepared(result_img, gen_mask, output):
    """Save generated result as RGBA: RGB=generated, alpha=normalized mask."""
    from PIL import Image

    out = result_img.convert("RGBA")
    out.putalpha(gen_mask.resize(result_img.size, Image.Resampling.LANCZOS))
    out.save(str(output))


class BlockSwapManager:
    """Just-in-time block swapping for the FLUX transformer.

    Patches `src.flux_core.block_forward` and `single_block_forward` so each
    transformer block is moved to GPU just before its forward pass and back to
    CPU after. In auto mode, a live VRAM probe decides how many blocks stay
    resident. In manual mode, `swap_blocks` blocks are offloaded.
    """

    _CORE_ATTRS = ("x_embedder", "time_text_embed", "context_embedder",
                   "pos_embed", "norm_out", "proj_out")

    def __init__(self, transformer, device, mode, manual_swap=0):
        self.transformer = transformer
        self.device = device
        self.mode = mode
        self._blocks = self._enumerate(transformer)
        self._total = len(self._blocks)
        self._kept = []
        self._swapped = []
        self._swapped_ids = set()
        self._manual_swap = max(0, min(int(manual_swap or 0), self._total))
        self._orig = {}

    @property
    def kept_count(self):
        return len(self._kept)

    @property
    def swapped_count(self):
        return len(self._swapped)

    @staticmethod
    def _enumerate(transformer):
        blocks = []
        for attr in ("transformer_blocks", "single_transformer_blocks"):
            seq = getattr(transformer, attr, None)
            if seq:
                blocks.extend(seq)
        return blocks

    @staticmethod
    def _mod_bytes(mod):
        total = 0
        for p in mod.parameters():
            total += p.nelement() * p.element_size()
        for b in mod.buffers():
            total += b.nelement() * b.element_size()
        return total

    def _core_bytes(self):
        total = 0
        for attr in self._CORE_ATTRS:
            mod = getattr(self.transformer, attr, None)
            if mod is not None:
                total += self._mod_bytes(mod)
        return total

    def plan(self, torch, target_side):
        """Decide kept vs swapped blocks from live VRAM budget."""
        block_b = self._mod_bytes(self._blocks[0]) if self._blocks else 0
        core_b = self._core_bytes()
        # Conservative activation reserve for the packed-sequence area.
        seq = max(1, (target_side // 16)) ** 2
        reserve = max(512 * (1024 ** 2), seq * 3072 * 32 * 2)

        free = 0
        if self.device.startswith("cuda") and torch.cuda.is_available():
            try:
                free, _t = torch.cuda.mem_get_info(_gpu_index(self.device))
            except Exception:
                free = 0
        budget = int(free * 0.90) - core_b - reserve

        if self.mode == "manual_block_swap":
            n_swap = self._manual_swap
        else:  # auto
            if block_b <= 0 or budget <= 0:
                n_swap = self._total
            else:
                n_keep = min(self._total, budget // block_b)
                n_swap = self._total - n_keep

        n_swap = max(0, min(n_swap, self._total))
        n_keep = self._total - n_swap
        self._kept = self._blocks[:n_keep]
        self._swapped = self._blocks[n_keep:]
        self._swapped_ids = set(id(b) for b in self._swapped)

        fg = free / (1024 ** 3) if free else 0
        bg = budget / (1024 ** 3) if budget else 0
        print(
            f"[BlockSwap] mode={self.mode} blocks={self._total} kept={n_keep} "
            f"swapped={n_swap} free={fg:.1f}GiB budget={bg:.1f}GiB "
            f"core={core_b / (1024**3):.2f}GiB block={block_b / (1024**3):.2f}GiB "
            f"reserve={reserve / (1024**3):.2f}GiB",
            file=sys.stderr,
        )

    def force_all_swapped(self):
        self._kept = []
        self._swapped = list(self._blocks)
        self._swapped_ids = set(id(b) for b in self._blocks)

    def prepare(self, torch):
        cpu = torch.device("cpu")
        dev = torch.device(self.device) if self.device.startswith("cuda") else cpu
        for attr in self._CORE_ATTRS:
            mod = getattr(self.transformer, attr, None)
            if mod is not None:
                try:
                    mod.to(dev)
                except Exception:
                    pass
        for b in self._blocks:
            try:
                b.to(dev if id(b) not in self._swapped_ids else cpu)
            except Exception:
                pass
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass

    def run_block(self, block, fn, *args, **kwargs):
        swapped = id(block) in self._swapped_ids
        if swapped:
            try:
                block.to(self.device)
            except Exception:
                pass
        result = fn(block, *args, **kwargs)
        if swapped:
            import torch
            try:
                block.to(torch.device("cpu"))
                torch.cuda.empty_cache()
            except Exception:
                pass
        return result

    def patch(self, flux_core):
        orig_b = flux_core.block_forward
        orig_s = flux_core.single_block_forward
        self._orig["b"] = orig_b
        self._orig["s"] = orig_s
        mgr = self

        def _wb(block, *a, **kw):
            return mgr.run_block(block, orig_b, *a, **kw)

        def _ws(block, *a, **kw):
            return mgr.run_block(block, orig_s, *a, **kw)

        flux_core.block_forward = _wb
        flux_core.single_block_forward = _ws

    def restore(self, flux_core):
        if "b" in self._orig:
            flux_core.block_forward = self._orig["b"]
        if "s" in self._orig:
            flux_core.single_block_forward = self._orig["s"]

    def cleanup(self, torch):
        try:
            self.transformer.to(torch.device("cpu"))
        except Exception:
            pass
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass


def _run_internal(
    source: Path,
    mask: Path,
    output: Path,
    repo: Path,
    device: str,
    steps: int,
    seed: int,
    lora: Path,
    embed: Path,
    max_side: int,
    memory_mode: str = "auto_block_swap",
    swap_blocks: int = 0,
    progress_file: str = "",
) -> int:
    """Optimized internal removal; supports block-swap memory modes.

    Full-resolution output: a square crop around the mask bbox (padded, edge-
    padded) is run through FLUX at target_side, then composited back into the
    original. FLUX text encoders/tokenizers are skipped at load. `memory_mode`
    selects placement: fast_gpu (.to), sequential_offload, or auto/manual
    block swap (live VRAM-probed). Progress JSON at each stage.
    """
    import torch
    from PIL import Image, ImageOps
    from diffusers.pipelines import FluxPipeline

    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from src.condition import Condition
    from src.generate import generate, seed_everything
    from src.embedding_loader import load_npz_embeddings

    padding = _env_int("OMNIPAINT_CROP_PADDING", 128, 0)
    local_only = _env_bool("OMNIPAINT_LOCAL_FILES_ONLY", True)
    output_mode = os.environ.get("OMNIPAINT_OUTPUT_MODE", "composite").strip().lower()

    pipe = None
    result = None
    manager = None
    _attn_restore = lambda: None
    try:
        _write_progress(progress_file, 5, "loading FLUX...")
        base_kwargs = {"torch_dtype": torch.bfloat16, "local_files_only": local_only}
        skip_kwargs = dict(text_encoder=None, text_encoder_2=None, tokenizer=None, tokenizer_2=None)
        quant = _quant_mode()
        if quant == "nf4":
            if memory_mode not in ("fast_gpu",):
                print(f"[OmniPaint] NF4: ignoring memory_mode={memory_mode} (resident ~6 GiB)", file=sys.stderr)
                memory_mode = "fast_gpu"
            transformer = _build_nf4_transformer(torch, local_only)
            pipe = FluxPipeline.from_pretrained(
                str(_flux_snapshot_dir()), transformer=transformer, **base_kwargs, **skip_kwargs,
            )
        elif quant:
            raise SystemExit(f"unsupported OMNIPAINT_QUANT={quant!r} (use 'nf4')")
        else:
            try:
                pipe = FluxPipeline.from_pretrained("black-forest-labs/FLUX.1-dev", **base_kwargs, **skip_kwargs)
            except TypeError:
                pipe = FluxPipeline.from_pretrained("black-forest-labs/FLUX.1-dev", **base_kwargs)
        _write_progress(progress_file, 10, "FLUX loaded")
        pipe.load_lora_weights(str(lora.parent), weight_name=lora.name, adapter_name="removal", local_files_only=local_only)
        pipe.set_adapters(["removal"])
        _write_progress(progress_file, 15, f"LoRA loaded ({memory_mode})")

        # ---- placement ----
        if memory_mode == "sequential_offload":
            try:
                kw = {"gpu_id": _gpu_index(device)} if device.startswith("cuda") else {}
                pipe.enable_sequential_cpu_offload(**kw)
            except TypeError:
                pipe.enable_sequential_cpu_offload()
        elif memory_mode in ("auto_block_swap", "manual_block_swap"):
            # Block swap: do NOT move the whole pipe. Move only the VAE to device
            # so condition encode + result decode work; the transformer is managed
            # by BlockSwapManager after the crop is known.
            try:
                pipe.vae.to(device)
            except Exception:
                pass
        else:  # fast_gpu / NF4 resident
            if quant != "nf4":
                _guard_fast_gpu_memory(torch, device)
            pipe.to(device)
        _patch_encode_images_for_offload()

        # Attention backend (sage if requested and available).
        _attn_backend = _attention_backend()
        try:
            import src.flux_core as _fc_attn
            _attn_restore = _patch_sage_attention(_fc_attn, _attn_backend)
        except SystemExit:
            raise
        except Exception as e:
            if _attn_backend == "sage":
                raise SystemExit(f"OMNIPAINT_ATTENTION=sage patch failed: {e}") from e
            print(f"[OmniPaint] attention patch skipped (auto/default): {e}", file=sys.stderr)

        exec_device = torch.device(device) if device.startswith("cuda") else torch.device("cpu")
        dtype = torch.bfloat16 if quant == "nf4" else pipe.transformer.dtype
        prompt_embeds, pooled_prompt_embeds, text_ids = load_npz_embeddings(
            str(embed), device=exec_device, dtype=dtype
        )

        prepared = _env_bool("OMNIPAINT_PREPARED_CROP", False)
        ctx = None
        if prepared:
            cond_rgb, gen_mask = _prepare_prepared(source, mask)
            target = cond_rgb.size[0]
        else:
            cond_rgb, gen_mask, ctx = _prepare_crop(source, mask, padding, max_side)
            target = ctx["info"]["target_side"]
        _write_progress(progress_file, 20, "crop ready")

        # ---- block-swap manager (needs target_side) ----
        if memory_mode in ("auto_block_swap", "manual_block_swap") and quant != "nf4":
            manager = BlockSwapManager(pipe.transformer, device, memory_mode, swap_blocks)
            manager.plan(torch, target)
            manager.prepare(torch)
            try:
                import src.flux_core as _fc
                manager.patch(_fc)
            except Exception as e:
                print(f"[BlockSwap] patch failed: {e}", file=sys.stderr)
                manager = None

        composite_square = Image.composite(
            cond_rgb, Image.new("RGB", cond_rgb.size, (0, 0, 0)), ImageOps.invert(gen_mask)
        )
        condition = Condition("removal", composite_square)
        seed_everything(seed)

        def _step_cb(pipe_, step, timestep, kwargs):
            pct = 20 + int(70 * (step + 1) / max(1, steps))
            _write_progress(progress_file, min(90, pct), f"step {step + 1}/{steps}")
            return {}

        def _do_generate():
            seed_everything(seed)
            return generate(
                pipe, conditions=[condition], width=target, height=target,
                num_inference_steps=steps, prompt=None,
                prompt_embeds=prompt_embeds, pooled_prompt_embeds=pooled_prompt_embeds,
                text_ids=text_ids, callback_on_step_end=_step_cb,
            ).images[0]

        # ---- generate with OOM retry (auto block swap) ----
        try:
            result = _do_generate()
        except RuntimeError as e:
            if "out of memory" in str(e).lower() and manager is not None and manager.kept_count > 0:
                print("[BlockSwap] OOM; retrying with ALL blocks swapped", file=sys.stderr)
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                manager.force_all_swapped()
                manager.prepare(torch)
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                result = _do_generate()
            else:
                raise

        _write_progress(progress_file, 95, "saving...")
        if prepared:
            _save_prepared(result, gen_mask, output)
        elif output_mode in ("generated_overlay", "overlay"):
            _rebuild_overlay(result, ctx, output)
        else:
            _rebuild_fullres(result, ctx, output)
        _write_progress(progress_file, 100, "done")
        return 0
    finally:
        try:
            _attn_restore()
        except Exception:
            pass
        if manager is not None:
            try:
                import src.flux_core as _fc
                manager.restore(_fc)
            except Exception:
                pass
            try:
                manager.cleanup(torch)
            except Exception:
                pass
        try:
            if pipe is not None and hasattr(pipe, "maybe_free_model_hooks"):
                pipe.maybe_free_model_hooks()
        except Exception:
            pass
        result = None
        pipe = None
        try:
            import gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass


def _run_env():
    """Parse and validate OmniPaint runtime environment."""
    device = os.environ.get("OMNIPAINT_DEVICE", "cuda:0").strip() or "cuda:0"
    seed = _env_int("OMNIPAINT_SEED", 42)
    low_vram = _env_bool("OMNIPAINT_LOW_VRAM", True)
    steps = _env_int("OMNIPAINT_STEPS", 28, 1)
    max_side = _env_int("OMNIPAINT_MAX_SIDE", _MAX_LENGTH, 64)
    memory_mode = _memory_mode(low_vram)
    swap_blocks = _env_int("OMNIPAINT_SWAP_BLOCKS", 0, 0)
    return device, seed, low_vram, steps, max_side, memory_mode, swap_blocks


def _check() -> int:
    """Validate the backend without loading the FLUX model.

    Checks the runtime source files the optimized load imports (src/*), weights,
    embeddings; that all runtime deps import (PIL/torch/diffusers/accelerate/
    peft/safetensors/numpy); validates numeric env; and (when local-only) that
    the FLUX snapshot has the required files. Prints OK or an error list.
    """
    import importlib

    problems = []
    repo = _omnipaint_repo()
    if not repo.is_dir():
        problems.append(f"repo missing: {repo}")
    else:
        for rel, label in (
            ("src/condition.py", "src/condition.py"),
            ("src/generate.py", "src/generate.py"),
            ("src/embedding_loader.py", "src/embedding_loader.py"),
            ("src/flux_core.py", "src/flux_core.py"),
            ("weights/omnipaint_remove.safetensors", "removal weights"),
            ("demo_assets/embeddings/remove.npz", "removal embeddings"),
        ):
            if not (repo / rel).is_file():
                problems.append(f"{label} missing: {rel}")

    missing_imports = []
    for mod in ("PIL.Image", "torch", "accelerate", "peft", "safetensors", "numpy"):
        try:
            importlib.import_module(mod)
        except Exception:
            missing_imports.append(mod.split(".")[0])
    try:
        dp = importlib.import_module("diffusers.pipelines")
        if getattr(dp, "FluxPipeline", None) is None:
            missing_imports.append("diffusers.FluxPipeline")
    except Exception:
        missing_imports.append("diffusers")
    if missing_imports:
        problems.append("imports missing: " + ", ".join(missing_imports))

    local_only = _env_bool("OMNIPAINT_LOCAL_FILES_ONLY", True)
    if local_only:
        missing = _flux_snapshot_missing()
        if missing:
            problems.append("FLUX cache incomplete (local_files_only on): " + ", ".join(missing))

    quant = _quant_mode()
    attention = _attention_backend()
    if attention == "sage":
        try:
            importlib.import_module("sageattention")
        except Exception:
            problems.append(
                "OMNIPAINT_ATTENTION=sage needs SageAttention; run "
                "OMNIPAINT_INSTALL_SAGE=1 bash tools/install_omnipaint_local.sh"
            )
    if quant:
        if quant != "nf4":
            problems.append(f"OMNIPAINT_QUANT={quant!r} unsupported (use 'nf4')")
        else:
            try:
                importlib.import_module("bitsandbytes")
            except Exception:
                problems.append(
                    "OMNIPAINT_QUANT=nf4 needs bitsandbytes (pip install bitsandbytes)"
                )
            try:
                from diffusers import BitsAndBytesConfig  # noqa: F401
            except Exception:
                problems.append("OMNIPAINT_QUANT=nf4 needs diffusers BitsAndBytesConfig")

    try:
        device, seed, low_vram, steps, max_side, memory_mode, swap_blocks = _run_env()
    except SystemExit as exc:
        problems.append(str(exc))
        device, seed, low_vram, steps, max_side, memory_mode, swap_blocks = (
            "?", 0, False, 0, 0, "?", 0
        )

    if problems:
        print(
            f"OmniPaint backend NOT ready (memory_mode={memory_mode}, "
            f"max_side={max_side}, local_files_only={local_only}, "
            f"quant={quant or 'off'}, attention={attention}):"
        )
        for p in problems:
            print("  - " + p)
        return 1
    print(
        f"OmniPaint backend OK | repo={repo} | device={device} | steps={steps} "
        f"| seed={seed} | memory_mode={memory_mode} | swap_blocks={swap_blocks} "
        f"| max_side={max_side} | local_files_only={local_only} | quant={quant or 'off'} "
        f"| attention={attention}"
    )
    return 0


def _clear_vram() -> int:
    """Best-effort CUDA cache clear for the current adapter process."""
    try:
        import gc
        import torch
    except Exception as exc:
        print(f"Clear VRAM unavailable: {exc}")
        return 1
    before = after = None
    if torch.cuda.is_available():
        try:
            free, total = torch.cuda.mem_get_info(0)
            before = (free, total)
        except Exception:
            pass
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        try:
            torch.cuda.ipc_collect()
        except Exception:
            pass
        try:
            free, total = torch.cuda.mem_get_info(0)
            after = (free, total)
        except Exception:
            pass
    if before and after:
        print(
            "VRAM cache cleared for adapter process "
            f"(free {before[0] / (1024**3):.2f} -> {after[0] / (1024**3):.2f} GiB). "
            "Persistent Nuke/ComfyUI VRAM is outside this process."
        )
    else:
        print("VRAM cache cleared for adapter process. Persistent Nuke/ComfyUI VRAM is outside this process.")
    return 0


def main() -> int:
    # Point HF/torch caches at the repo-local .slim/cache before any load/check
    # so standalone runs find the downloaded model (Nuke env overrides win).
    _apply_local_cache_defaults()
    p = argparse.ArgumentParser(description="Run OmniPaint removal behind the Nuke adapter contract.")
    p.add_argument("--source")
    p.add_argument("--mask")
    p.add_argument("--output")
    p.add_argument("--progress-file", dest="progress_file", default=None,
                   help="path to write JSON {progress, message} updates")
    p.add_argument("--crop-info", dest="crop_info", action="store_true",
                   help="compute and print the crop geometry JSON; do not load FLUX")
    p.add_argument("--check", action="store_true", help="validate backend without running the model")
    p.add_argument("--clear-vram", action="store_true", help="best-effort CUDA cache clear in this process")
    args = p.parse_args()

    if args.check:
        return _check()

    if args.clear_vram:
        return _clear_vram()

    if args.crop_info:
        if not args.source or not args.mask:
            p.error("--source and --mask are required for --crop-info")
        padding = _env_int("OMNIPAINT_CROP_PADDING", 128, 0)
        max_side = _env_int("OMNIPAINT_MAX_SIDE", _MAX_LENGTH, 64)
        print(json.dumps(compute_crop_info(Path(args.source), Path(args.mask), padding, max_side), indent=2))
        return 0

    missing = [n for n in ("source", "mask", "output") if not getattr(args, n)]
    if missing:
        p.error("the following arguments are required in run mode: " + ", ".join("--" + m for m in missing))

    source = _require(Path(args.source), "source")
    mask = _require(Path(args.mask), "mask")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    repo = _require(_omnipaint_repo(), "OmniPaint repo")
    lora = _require(repo / "weights" / "omnipaint_remove.safetensors", "OmniPaint removal weights")
    embed = _require(repo / "demo_assets" / "embeddings" / "remove.npz", "OmniPaint removal embeddings")

    # Validates steps>=1 and max_side>=64 (clear SystemExit on bad values).
    device, seed, low_vram, steps, max_side, memory_mode, swap_blocks = _run_env()

    return _run_internal(
        source, mask, output, repo, device, steps, seed, lora, embed, max_side,
        memory_mode=memory_mode, swap_blocks=swap_blocks,
        progress_file=args.progress_file or "",
    )


if __name__ == "__main__":
    sys.exit(main())
