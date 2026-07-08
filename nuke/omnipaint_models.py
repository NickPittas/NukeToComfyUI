"""OmniPaint/FLUX/NF4 model + asset health for the removal backend.

Stdlib-only. Path resolution mirrors ``tools/omnipaint_adapter.py`` and
``tools/install_omnipaint_local.sh`` so this report describes exactly what the
backend needs to run. No downloads here — repair is the installer's job
(``Install OmniPaint backend`` calls the install script).
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
from typing import Dict, List

from ai_config import repo_root

_WIN_BLOCKED = "OmniPaint runtime not supported on Windows"


# --- path helpers ----------------------------------------------------------

def venv_python() -> str:
    """OmniPaint venv interpreter path for the current platform."""
    if os.name == "nt":
        return os.path.join(repo_root(), ".slim", "venvs", "omnipaint",
                            "Scripts", "python.exe")
    return os.path.join(repo_root(), ".slim", "venvs", "omnipaint", "bin", "python")


def omnipaint_repo() -> str:
    return os.path.join(repo_root(), ".slim", "clonedeps", "repos", "yeates__OmniPaint")


def lora_weights() -> str:
    return os.path.join(omnipaint_repo(), "weights", "omnipaint_remove.safetensors")


def remove_embeddings() -> str:
    return os.path.join(omnipaint_repo(), "demo_assets", "embeddings", "remove.npz")


def hf_cache_root() -> str:
    return os.path.join(repo_root(), ".slim", "cache", "huggingface")


def flux_cache_dir() -> str:
    """Local FLUX model dir, honoring ``HUGGINGFACE_HUB_CACHE`` like the adapter."""
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE", "").strip()
    if not hub:
        hub = os.path.join(hf_cache_root(), "hub")
    return os.path.join(hub, "models--black-forest-labs--FLUX.1-dev")


# --- FLUX snapshot ---------------------------------------------------------

def flux_snapshot_missing() -> List[str]:
    """Required-file relpaths missing from the FLUX snapshot (``[]`` == usable).

    Mirrors ``tools/omnipaint_adapter.py::_flux_snapshot_missing``: requires the
    pipeline index, scheduler, transformer (config + weight shard/index), and
    VAE (config + weight). Text encoders/tokenizers are NOT required.
    """
    snaps = os.path.join(flux_cache_dir(), "snapshots")
    try:
        revs = [d for d in os.listdir(snaps)
                if os.path.isdir(os.path.join(snaps, d))]
    except OSError:
        revs = []
    if not revs:
        return [f"{snaps}/<rev> (no FLUX snapshot present)"]

    snap = os.path.join(snaps, revs[0])
    missing = [
        rel for rel in (
            "model_index.json",
            "scheduler/scheduler_config.json",
            "transformer/config.json",
            "vae/config.json",
        ) if not os.path.isfile(os.path.join(snap, rel))
    ]
    # transformer weights: if an index exists, every referenced shard must exist.
    idx = os.path.join(snap, "transformer",
                       "diffusion_pytorch_model.safetensors.index.json")
    if os.path.isfile(idx):
        try:
            with open(idx, encoding="utf-8") as fh:
                data = json.load(fh)
            shards = sorted(set((data.get("weight_map") or {}).values()))
        except (OSError, ValueError):
            shards = []
        if not shards:
            missing.append("transformer/diffusion_pytorch_model.safetensors.index.json (invalid weight_map)")
        else:
            for shard in shards:
                if not os.path.isfile(os.path.join(snap, "transformer", shard)):
                    missing.append("transformer/" + shard)
    elif not (
        glob.glob(os.path.join(snap, "transformer", "diffusion_pytorch_model*.safetensors"))
        or os.path.isfile(os.path.join(snap, "transformer", "model.safetensors"))
    ):
        missing.append("transformer/*.safetensors (weight shard/index)")
    # VAE weights
    if not (
        os.path.isfile(os.path.join(snap, "vae", "diffusion_pytorch_model.safetensors"))
        or os.path.isfile(os.path.join(snap, "vae", "model.safetensors"))
        or glob.glob(os.path.join(snap, "vae", "*.bin"))
    ):
        missing.append("vae/*.safetensors|*.bin (weight)")
    return missing


# --- NF4 readiness ---------------------------------------------------------

def _venv_site_packages(venv_py: str) -> str:
    """Best-effort site-packages dir for the venv (posix + windows)."""
    base = os.path.dirname(os.path.dirname(venv_py))  # venv root
    if os.name == "nt":
        return os.path.join(base, "Lib", "site-packages")
    lib = os.path.join(base, "lib")
    try:
        for d in os.listdir(lib):
            sp = os.path.join(lib, d, "site-packages")
            if d.lower().startswith("python") and os.path.isdir(sp):
                return sp
    except OSError:
        pass
    return ""


def _nf4_filecheck(venv_py: str) -> bool:
    """Fast filesystem hint: bitsandbytes + diffusers present in site-packages."""
    sp = _venv_site_packages(venv_py)
    if not sp:
        return False
    return os.path.isdir(os.path.join(sp, "bitsandbytes")) and os.path.isdir(
        os.path.join(sp, "diffusers"))


def _nf4_importcheck(venv_py: str) -> Dict[str, str]:
    """Real NF4 readiness: import bitsandbytes + diffusers.BitsAndBytesConfig."""
    try:
        proc = subprocess.run(
            [venv_py, "-c",
             "import bitsandbytes; from diffusers import BitsAndBytesConfig"],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "partial", "detail": "NF4 import check failed to run"}
    if proc.returncode == 0:
        return {"status": "ok",
                "detail": "bitsandbytes + diffusers.BitsAndBytesConfig importable"}
    msg = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = msg[-1] if msg else f"exit {proc.returncode}"
    return {"status": "partial", "detail": "NF4 import failed: " + tail}


def _runtime_check(venv_py: str) -> Dict[str, str]:
    """Run ``omnipaint_adapter.py --check`` in the venv (loads imports, no FLUX).

    The adapter sets repo-local HF/torch cache defaults itself, so no extra env
    is required. Returns ok/missing/partial with the adapter's summary line.
    """
    adapter = os.path.join(repo_root(), "tools", "omnipaint_adapter.py")
    if not os.path.isfile(adapter):
        return {"status": "missing", "detail": f"adapter missing: {adapter}"}
    try:
        proc = subprocess.run(
            [venv_py, adapter, "--check"],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "partial", "detail": "runtime check failed to run/timeout"}
    lines = (proc.stdout or "").strip().splitlines()
    tail = lines[-1] if lines else f"exit {proc.returncode}"
    if proc.returncode == 0:
        return {"status": "ok", "detail": tail}
    return {"status": "missing", "detail": "backend not ready: " + tail}


# --- report ----------------------------------------------------------------

def model_report(check_nf4: bool = False,
                 check_runtime: bool = False) -> List[Dict[str, str]]:
    """Return ``[{name, status, detail}]`` for OmniPaint assets/FLUX/NF4.

    Fast by default (filesystem + site-packages hint). ``check_nf4=True`` runs
    the real bitsandbytes/diffusers import; ``check_runtime=True`` additionally
    runs ``omnipaint_adapter.py --check`` (loads imports, no FLUX). Use the slow
    flags from the installer's explicit model check / post-install, not frequent
    health sweeps.
    """
    on_windows = os.name == "nt"
    out: List[Dict[str, str]] = []
    venv_py = venv_python()
    repo = omnipaint_repo()

    def fs_entry(name: str, ok: bool, detail: str) -> Dict[str, str]:
        if on_windows:
            return {"name": name, "status": "blocked", "detail": _WIN_BLOCKED}
        return {"name": name, "status": "ok" if ok else "missing", "detail": detail}

    out.append(fs_entry("OmniPaint venv", os.path.isfile(venv_py), venv_py))
    out.append(fs_entry("OmniPaint repo", os.path.isdir(repo), repo))
    out.append(fs_entry("OmniPaint LoRA", os.path.isfile(lora_weights()), lora_weights()))
    out.append(fs_entry("OmniPaint embeddings", os.path.isfile(remove_embeddings()), remove_embeddings()))

    # FLUX.1-dev — thorough snapshot check.
    if on_windows:
        out.append({"name": "FLUX.1-dev", "status": "blocked", "detail": _WIN_BLOCKED})
    else:
        miss = flux_snapshot_missing()
        if miss:
            out.append({"name": "FLUX.1-dev", "status": "missing",
                        "detail": "missing: " + ", ".join(miss)})
        else:
            out.append({"name": "FLUX.1-dev", "status": "ok", "detail": flux_cache_dir()})

    # NF4 — fast hint is never a positive ok (package dirs don't prove import).
    # Real import only on demand (check_nf4=True); else partial/missing.
    if on_windows:
        out.append({"name": "OmniPaint NF4", "status": "blocked", "detail": _WIN_BLOCKED})
    elif not os.path.isfile(venv_py):
        out.append({"name": "OmniPaint NF4", "status": "missing", "detail": "venv missing: " + venv_py})
    elif check_nf4:
        nf = _nf4_importcheck(venv_py)
        out.append({"name": "OmniPaint NF4", "status": nf["status"], "detail": nf["detail"]})
    elif _nf4_filecheck(venv_py):
        out.append({"name": "OmniPaint NF4", "status": "partial",
                    "detail": "packages present; run Check OmniPaint models to verify import"})
    else:
        out.append({"name": "OmniPaint NF4", "status": "missing",
                    "detail": "bitsandbytes/diffusers not found in site-packages"})

    # Runtime deps — runs omnipaint_adapter.py --check only on demand.
    if check_runtime:
        if on_windows:
            out.append({"name": "OmniPaint runtime deps", "status": "blocked",
                        "detail": _WIN_BLOCKED})
        elif not os.path.isfile(venv_py):
            out.append({"name": "OmniPaint runtime deps", "status": "missing",
                        "detail": "venv missing: " + venv_py})
        else:
            rt = _runtime_check(venv_py)
            out.append({"name": "OmniPaint runtime deps",
                        "status": rt["status"], "detail": rt["detail"]})

    return out
