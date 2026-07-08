# OmniPaintRemove

`AI/OmniPaintRemove` is a Nuke-side runner node for promptless OmniPaint object removal.

It is separate from `ComfyUIBridge`:

```text
input 0: source plate
input 1: mask, white = remove / black = keep
output: passthrough source
button: Run current frame
```

The button renders source and mask to temporary PNG files, runs the configured adapter, then creates a `Read` node for the result.

## Knobs

Only artist/runtime controls are exposed. The python venv, backend adapter
script, and OmniPaint repo are **not** knobs — they are plugin-local, resolved
automatically from the plugin root, and set up by
`tools/install_omnipaint_local.sh`.

```text
output_directory    where result PNGs are written
omnipaint_device    model device (default cuda:0)
omnipaint_steps     inference steps (default 28)
omnipaint_seed      seed (default 42)
omnipaint_max_side  max side (px) OmniPaint runs the crop at (default 1024)
omnipaint_low_vram  off = direct GPU (default); on = sequential CPU offload (shared GPU)
create_read_on_result
status              last one-line status
last_result         path of the most recent result
Run current frame   renders inputs and runs the adapter for the current frame
Check backend       validates setup (venv/repo/weights/embeddings/imports) without loading the model
Create preview overlay  builds a red, alpha-0.25 overlay of the source-space generated crop
```

`omnipaint_device/steps/seed/max_side/low_vram` are passed to the adapter as the
`OMNIPAINT_DEVICE`, `OMNIPAINT_STEPS`, `OMNIPAINT_SEED`, `OMNIPAINT_MAX_SIDE`,
and `OMNIPAINT_LOW_VRAM` environment variables (`omnipaint_low_vram` as `1`/`0`).

The **Check backend** button first verifies the plugin-local venv, adapter
script, and OmniPaint repo exist (clear setup error if not), then runs the
adapter `--check` (repo artifacts, weights, embeddings, imports, and the local
FLUX snapshot).

## Output resolution & preview

The output is **full source resolution**: the adapter computes a square crop
around the mask bbox (padded by `OMNAPAINT_CROP_PADDING`, default 128 px,
edge-padded near source edges), runs FLUX only on that square (downscaled to a
multiple of 8 up to `omnipaint_max_side`), then composites the generated region
back into the original full-res source using the mask as alpha. Source alpha is
reattached unchanged. So a large plate gets a full-size result while FLUX only
ever sees the small masked region.

The **Create preview overlay** button runs the adapter's `--crop-info` (no model
load) and builds best-effort Nuke nodes near `OmniPaintRemove`: a red, alpha-0.25
Constant at the source format, Cropped to the source-space generated crop box,
Merged over the source input — showing exactly where FLUX will generate. The
overlay is not part of the Run export.

## Real OmniPaint adapter

The adapter and OmniPaint repo are fixed plugin-local paths, resolved
automatically by the node (no knobs):

```text
adapter script:  tools/omnipaint_adapter.py
omnipaint repo:  .slim/clonedeps/repos/yeates__OmniPaint/
python venv:     .slim/venvs/omnipaint/bin/python
```

Both `omnipaint_low_vram` modes use the same **optimized internal adapter** —
an equivalent of upstream `process_single_image`. The upstream CLI script is
**not** used for normal runs. The optimization: OmniPaint removal uses static
embeddings (`remove.npz`) and never encodes text, so the adapter loads FLUX with
`text_encoder=None, text_encoder_2=None, tokenizer=None, tokenizer_2=None`,
skipping the unused text encoders/tokenizers. This avoids the multi-GB of VRAM
the upstream CLI wastes on encoders it never uses (which caused OOMs even with
~29 GB free). `omnipaint_low_vram` only changes the offload strategy after LoRA
and adapter setup:

- **Off (default, high-VRAM):** the text-encoder-free pipeline is moved with
  `.to(device)`. Fastest, and fits in much less VRAM than the upstream CLI.
- **On (low-VRAM):** `pipe.enable_sequential_cpu_offload(gpu_id=...)` instead of
  `.to(device)`. Sequential offload hooks every submodule individually, so it is
  compatible with OmniPaint's custom `tranformer_forward` (which calls submodules
  directly) and the model never has to fit wholesale in free VRAM. Slower
  (per-submodule host<->device transfer each step) but coexists with other GPU
  consumers (Nuke/ComfyUI). Enable from the `omnipaint_low_vram` knob when the
  GPU is shared.

`omnipaint_max_side` (`OMNIPAINT_MAX_SIDE`, default 1024) caps the
condition-image side (applies in both modes in this internal adapter).

## Setup

All setup is **plugin-local** and owned by one script — the Nuke user never
touches paths. From the repo root:

```bash
bash tools/install_omnipaint_local.sh
```

It is idempotent (re-running skips what exists) and creates, all under `.slim/`:

- `venvs/omnipaint/` — removal-runtime venv (`python3.11`, else `python3`).
  Installs CUDA torch (12.8 index), diffusers, transformers, peft, accelerate,
  huggingface-hub, torchmetrics, opencv, numpy, sentencepiece, etc. `carvekit`
  is intentionally skipped (insertion/demo only; conflicts with the removal
  runtime and is not imported by removal).
- `clonedeps/repos/yeates__OmniPaint/` — OmniPaint clone at the pinned, tested
  commit (`cdb7c263...`). An existing clone must sit at that commit or the script
  fails with a re-pin hint; set `OMNIPAINT_REF=<branch/commit>` to accept a
  different ref.
- OmniPaint removal weights + `remove.npz` embeddings (public `yeates/OmniPaint`
  repo, via `huggingface_hub`).
- `cache/huggingface/` — the FLUX.1-dev snapshot, **best-effort**: it is gated,
  so the script attempts it only if you have access/a token, and otherwise
  prints the exact next steps and exits without undoing the rest. Until the
  snapshot is present, **Check backend** reports "FLUX cache missing".
- Sets all caches (`HF_HOME`, `HUGGINGFACE_HUB_CACHE`, `TORCH_HOME`,
  `XDG_CACHE_HOME`) inside `.slim/cache/` so Nuke runs find them with no token.

No tokens are printed or persisted by the script. To fetch the gated FLUX model:

```bash
HF_TOKEN=... bash tools/install_omnipaint_local.sh

# or login into the repo-local HF home, then rerun the installer:
HF_HOME=/home/npittas/.nuke/inpaint/.slim/cache/huggingface \
  /home/npittas/.nuke/inpaint/.slim/venvs/omnipaint/bin/huggingface-cli login
bash tools/install_omnipaint_local.sh
```

Optional environment overrides (adapter-level; the node already sets the ones it
needs):

```bash
OMNIPAINT_DEVICE=cuda:0
OMNIPAINT_STEPS=28
OMNIPAINT_SEED=42
OMNIPAINT_LOW_VRAM=0            # default 0 = .to(device); 1 = sequential CPU offload (shared GPU)
OMNIPAINT_MAX_SIDE=1024         # condition-image resize cap
OMNIPAINT_LOCAL_FILES_ONLY=1    # default 1 = load FLUX from local cache (offline); 0 = allow online/redownload
```

The Nuke node also sets `HF_HUB_OFFLINE=1` on the adapter subprocess when the
local FLUX snapshot exists, so Nuke runs never need a network login.

## Model health

The repo also includes a stdlib model inventory module used by the installer and
Nuke menu:

```text
nuke/omnipaint_models.py
```

It checks:

```text
OmniPaint venv
OmniPaint repo
OmniPaint LoRA
remove.npz embeddings
FLUX.1-dev required snapshot files
NF4 bitsandbytes/diffusers readiness
adapter runtime deps via tools/omnipaint_adapter.py --check
```

Run from the installer TUI:

```text
Check OmniPaint models
```

or from Nuke:

```text
Nuke > AI Setup > Model Health
```

Fast health may show `OmniPaint NF4: partial` when the package directories are
present but the real import check has not been run. The installer's **Check
OmniPaint models** runs the full check.

## Offline / local-only

By default the adapter loads FLUX with `local_files_only=True`
(`OMNIPAINT_LOCAL_FILES_ONLY=1`, the default), and both the adapter and the Nuke
node point HF/torch caches at `.slim/cache/` (the adapter does this itself via
`os.environ.setdefault`, so even a standalone `--check` finds the model). The
node additionally sets `HF_HUB_OFFLINE=1`, but **only when a usable local FLUX
snapshot is present** (required files validated) and local-only is not disabled —
so it never forces offline with nothing usable cached. A Nuke run therefore uses
the model already cached and does **not** require an HF token or network.

`Check backend` validates the snapshot's required files (pipeline index,
scheduler, transformer config + weights, VAE config + weights — the text
encoders are not required since the optimized load skips them), the runtime
source files (`src/*`), and that all runtime deps import.

If you ever need to re-download or fetch online, disable it:

```bash
OMNIPAINT_LOCAL_FILES_ONLY=0   # the node then will not set HF_HUB_OFFLINE
```

## Constraints

- Requires FLUX.1-dev through Hugging Face on first run.
- Needs a CUDA GPU; CPU is not practical.
- Both `omnipaint_low_vram` modes use the optimized internal adapter, which
  skips the unused FLUX text encoders/tokenizers at load (OmniPaint uses static
  embeddings). The default (off) moves the text-encoder-free pipeline with
  `.to(device)` — far smaller than the upstream CLI's load, so it no longer needs
  ~12 GB+ free just for encoders. The node also sets
  `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` on the adapter subprocess to
  reduce fragmentation.
- The optional low-VRAM path (`omnipaint_low_vram` on /
  `enable_sequential_cpu_offload`) pages submodules to the GPU one at a time, so
  it coexists with other GPU consumers and needs only a few GB of free VRAM, not
  the full model. It is slower per frame — enable it when the GPU is shared.
- `omnipaint_max_side` (default 1024) trades resolution for VRAM; lower it if you
  still hit OOM, raise it (with enough VRAM) for detail.
- FLUX.1-dev is non-commercial; check licensing before production use.
- The inspected OmniPaint repo clone has no top-level license file; review/obtain permission before redistributing or using it in production.

## Test adapter (developer plumbing only)

`tools/omnipaint_copy_adapter.py` copies the source image to the output without
running OmniPaint, for testing render/subprocess/import plumbing end-to-end.
There is no UI knob for it anymore; invoke it directly:

```bash
python tools/omnipaint_copy_adapter.py --source s.png --mask m.png --output o.png
```
