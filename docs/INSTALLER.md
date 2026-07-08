# Installer and central settings

Run from the repo root:

```bash
python3 tools/install.py
```

Dry-run first on a new machine:

```bash
python3 tools/install.py --dry-run --yes
```

Write a log:

```bash
python3 tools/install.py --log install.log
```

## What it changes

- User Nuke init only: adds a marked `nuke.pluginAddPath(".../nuke")` block to
  `~/.nuke/init.py` after backing it up.
- ComfyUI custom node: symlinks `comfyui/nuke_bridge` into
  `<ComfyUI>/custom_nodes/nuke_bridge`. If symlink fails, it informs you and
  copies the node instead.
- Central settings: writes `~/.nuke/comfyui_bridge/settings.json`.
- OmniPaint: runs `tools/install_omnipaint_local.sh` and validates the result.
- Sammie-Roto: clones or accepts an existing release-style folder, then runs
  Sammie's own installer. Sammie's installer owns its model-download choices.
- LTX Desktop: discovery/configuration only. No install, build, setup, or model
  downloads.

It does **not** edit user `~/.nuke/menu.py`.

## Central settings

Path:

```text
~/.nuke/comfyui_bridge/settings.json
```

Important keys:

```json
{
  "comfyui_root": "",
  "sammie_root": "",
  "ltx_root": "",
  "ltx_models_dir": "",
  "comfyui_host": "127.0.0.1",
  "comfyui_port": 8188,
  "output_directory": "~/comfyui_bridge_results"
}
```

Environment overrides:

```text
NUKE_HOME_DIR
COMFYUI_ROOT
SAMMIE_ROOT
LTX_ROOT
LTX_MODELS_DIR
LTX_APP_DATA_DIR
HF_TOKEN
```

## Installer menu

```text
Preflight checks
Configure paths
Configure LTX Desktop (root/models)
Install Nuke plugin (init.py)
Link ComfyUI custom node
Install OmniPaint backend
Check OmniPaint models
Install Sammie-Roto
Health check
Save settings
Quit
```

## Nuke menu

```text
Nuke > AI Setup > Open Settings
Nuke > AI Setup > Health Check
Nuke > AI Setup > Model Health
Nuke > AI Launchers > Sammie-Roto (selected footage)
Nuke > AI Launchers > LTX Desktop
```

## Sammie-Roto

The installer uses Sammie's own setup scripts:

```text
install.sh
install_dependencies.sh
install.bat
install_dependencies.bat
```

Existing release-style installs are accepted when they look like Sammie-Roto
(`launcher.py`, a `run_sammie.*` launcher, and Sammie-specific files). The
installer does not delete or move existing folders.

## LTX Desktop

Discovery only:

- detects a built binary when present;
- otherwise detects a source checkout with `package.json` and `pnpm` for
  `pnpm dev` launch;
- reads LTX Desktop's app settings for `models_dir` / `modelsDir`;
- checks the active-profile or canonical IC-LoRA inpaint adapter path.

It does not install LTX Desktop, run `setup:dev`, build, or download LTX models.

## OmniPaint / FLUX / NF4

`Install OmniPaint backend` calls the existing installer:

```bash
bash tools/install_omnipaint_local.sh
```

`Check OmniPaint models` validates:

```text
OmniPaint venv
OmniPaint repo
OmniPaint LoRA
remove.npz embeddings
FLUX.1-dev required files
NF4 bitsandbytes/diffusers import
adapter runtime deps
```

FLUX.1-dev is gated on Hugging Face. Use `HF_TOKEN` or `huggingface-cli login`
before running the OmniPaint installer if the snapshot is missing.
