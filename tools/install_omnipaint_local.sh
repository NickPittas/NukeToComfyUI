#!/usr/bin/env bash
# Local OmniPaint backend setup for the Nuke OmniPaintRemove node.
#
# Plugin-local only — creates nothing outside this repo:
#   .slim/venvs/omnipaint/                     removal-runtime venv
#   .slim/clonedeps/repos/yeates__OmniPaint/   OmniPaint source clone
#   .slim/cache/{huggingface,torch,xdg}/       FLUX.1-dev + hub/torch cache
#
# Idempotent: re-running skips anything already present. The Nuke node resolves
# these paths automatically; there are no path UI knobs to fill in.
#
# carvekit is intentionally NOT installed: OmniPaint only uses it for
# insertion/demo preprocessing, and it pulls a heavy CUDA toolchain that
# conflicts with the removal runtime. Object removal does not import it.

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SLIM="$PLUGIN_ROOT/.slim"
VENV="$SLIM/venvs/omnipaint"
VENV_PY="$VENV/bin/python"
CLONE_ROOT="$SLIM/clonedeps/repos"
REPO="$CLONE_ROOT/yeates__OmniPaint"
CACHE="$SLIM/cache"

# All caches stay inside the repo so the adapter (local_files_only / offline by
# default) finds them without any HF token or network on Nuke runs.
export HF_HOME="$CACHE/huggingface"
export HUGGINGFACE_HUB_CACHE="$CACHE/huggingface/hub"
export TORCH_HOME="$CACHE/torch"
export XDG_CACHE_HOME="$CACHE/xdg"

log()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

mkdir -p "$CLONE_ROOT" "$CACHE"

# --- 1. venv ---------------------------------------------------------------
if [ -x "$VENV_PY" ]; then
  log "venv exists: $VENV"
else
  PYBIN=python3.11
  have "$PYBIN" || PYBIN=python3
  log "creating venv at $VENV ($PYBIN)"
  "$PYBIN" -m venv "$VENV"
  "$VENV_PY" -m pip install --upgrade pip wheel setuptools
fi

# --- 2. removal runtime deps ----------------------------------------------
# torch/torchvision from the PyTorch CUDA 12.8 wheel index (extra-index so pip
# still resolves torch's PyPI deps). Pinned to the tested local build.
log "installing torch (CUDA 12.8)"
"$VENV_PY" -m pip install --upgrade \
  "torch==2.11.0+cu128" "torchvision==0.26.0+cu128" \
  --extra-index-url https://download.pytorch.org/whl/cu128

# carvekit skipped (see header). gradio/gradio_image_annotation skipped
# (demo/UI only, not used by removal).
log "installing removal runtime deps"
"$VENV_PY" -m pip install --upgrade \
  "diffusers==0.31.0" "transformers==4.41.0" "peft==0.10.0" \
  "accelerate==1.14.0" "huggingface-hub==0.28.1" "torchmetrics==0.6.0" \
  "opencv-python==4.8.0.74" "numpy==1.26.4" "sentencepiece" \
  Pillow safetensors tqdm

# NF4 quantized FLUX path (OMNIPAINT_QUANT=nf4). This is optional at runtime,
# but installing it here makes the production venv capable of the tested ~6 GiB
# resident mode instead of requiring the isolated .slim/quant_test venv.
log "installing NF4 quantization runtime"
"$VENV_PY" -m pip install --upgrade "bitsandbytes==0.49.2"

if [ "${OMNIPAINT_INSTALL_SAGE:-0}" = "1" ]; then
  log "installing SageAttention (builds a wheel locally if no compatible wheel exists)"
  "$VENV_PY" -m pip install --upgrade ninja packaging wheel
  "$VENV_PY" -m pip install --upgrade sageattention
else
  log "SageAttention skipped"
  note "Enable with: OMNIPAINT_INSTALL_SAGE=1 bash tools/install_omnipaint_local.sh"
fi

# --- 3. OmniPaint clone (pinned commit) -----------------------------------
# Default to the tested commit. An existing clone must sit at it; set
# OMNIPAINT_REF=<branch/commit> to accept a different ref. Idempotent.
PINNED_COMMIT="cdb7c263edbd463ff8df60694fd97f9f43da187b"
REF_EXPLICIT="${OMNIPAINT_REF:+set}"
OMNIPAINT_REF="${OMNIPAINT_REF:-$PINNED_COMMIT}"
have git || { printf 'error: git is required for the OmniPaint clone\n' >&2; exit 1; }

if [ -d "$REPO/.git" ]; then
  current="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$REF_EXPLICIT" ]; then
    log "OmniPaint clone exists at ${current:-unknown} (OMNIPAINT_REF override; left as-is)"
  elif [ "$current" != "$PINNED_COMMIT" ]; then
    printf 'error: OmniPaint clone HEAD (%s) is not the pinned commit (%s).\n' "${current:-unknown}" "$PINNED_COMMIT" >&2
    printf '       Re-pin with:  git -C %s checkout %s\n' "$REPO" "$PINNED_COMMIT" >&2
    printf '       Or set OMNIPAINT_REF=<branch/commit> to accept a different ref.\n' >&2
    exit 1
  else
    log "OmniPaint clone at pinned commit ($current)"
  fi
else
  log "cloning OmniPaint into $REPO (ref $OMNIPAINT_REF)"
  git clone https://github.com/yeates/OmniPaint.git "$REPO"
  git -C "$REPO" checkout "$OMNIPAINT_REF" >/dev/null
fi

# --- 4. OmniPaint weights + embeddings (public yeates/OmniPaint repo) -----
# via huggingface_hub; placed into the clone paths the adapter expects.
# (embeddings live under embeddings/ in the HF repo but demo_assets/embeddings/
# locally, so each file is downloaded to a scratch dir then moved.)
log "ensuring OmniPaint weights/embeddings"
"$VENV_PY" - "$REPO" <<'PY'
import os, shutil, sys, tempfile
from huggingface_hub import hf_hub_download
src = "yeates/OmniPaint"
repo_dir = sys.argv[1]

def ensure(repo_file, dest):
    if os.path.isfile(dest):
        print("   exists:", dest)
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        p = hf_hub_download(src, repo_file, local_dir=td)
        shutil.move(p, dest)
    print("   fetched:", dest)

ensure("weights/omnipaint_remove.safetensors",
       os.path.join(repo_dir, "weights", "omnipaint_remove.safetensors"))
ensure("embeddings/remove.npz",
       os.path.join(repo_dir, "demo_assets", "embeddings", "remove.npz"))
PY

# --- 5. FLUX.1-dev snapshot (gated; best-effort) --------------------------
# Best-effort: if access/token is missing, warn with the exact next step but do
# not undo the rest of the setup. The backend will report "FLUX cache missing"
# via Check backend until this succeeds.
log "FLUX.1-dev snapshot (gated; best-effort)"
if "$VENV_PY" - <<'PY'
from huggingface_hub import snapshot_download
# Full snapshot (text-encoder files are fetched but the adapter skips loading
# them, so they cost only disk, not VRAM).
print("   snapshot at:", snapshot_download("black-forest-labs/FLUX.1-dev"))
PY
then
  log "FLUX.1-dev snapshot ready"
else
  log "WARNING: FLUX.1-dev snapshot not downloaded (gated / no token)."
  note "Backend will not run until it is present. To fetch:"
  note "  1. accept the license: https://huggingface.co/black-forest-labs/FLUX.1-dev"
  note "  2. either run: HF_TOKEN=... bash tools/install_omnipaint_local.sh"
  note "     or login repo-locally: HF_HOME='$HF_HOME' '$VENV/bin/huggingface-cli' login"
  note "  3. re-run:  bash tools/install_omnipaint_local.sh"
fi

log "done."
note "venv:        $VENV"
note "repo:        $REPO"
note "hf cache:    $HF_HOME"
note "verify in Nuke with the OmniPaintRemove 'Check backend' button."
