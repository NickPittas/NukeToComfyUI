# Nuke ↔ ComfyUI Bridge

A Nuke node (`ComfyUIBridge`) plus ComfyUI custom nodes (`FromNuke`, `ToNuke`)
that let a ComfyUI workflow pull the current frame from a connected Nuke node
and push a result image back.

See `PLAN.md` and `PROTOCOL.md` for the full architecture.

## Status

**Phase 1 + Phase 2 transport.** PNG8 RGBA round trip is the default and has no
extra dependencies. EXR16 half-float RGBA is available for higher-fidelity
exchange. Explicit color controls (Phase 3) and discovery/robustness (Phase 4)
are deferred — see `TASKS.md`.

All four mask modes are implemented in `/frame`:
- `source alpha` / `invert source alpha`: handled by a small in-Nuke tree
  (native EXR write for exr16).
- `mask input` / `invert mask input`: source RGB is rendered in Nuke, then the
  mask input's carrier alpha (alpha channel if it varies, else luma) is
  composited in via a robust PIL fallback (exact Nuke Copy/Shuffle knob names
  vary across versions and can't be tested here). Disconnected mask input falls
  back to all-keep (carrier alpha = 1). See `nuke/comfyui_bridge/render.py`.

### EXR16 half-float transport (Phase 2)

- Nuke → ComfyUI: set the bridge node's `send_format` knob (or the `FromNuke`
  `format` input) to `exr16`. `/frame` writes a half-float RGBA EXR via a temp
  Nuke Write node. Source-alpha modes are native EXR; **mask-input modes
  currently degrade to PNG compose** (response `X-NukeBridge-Format: png8`)
  because an in-Nuke Copy/Shuffle alpha-inject tree would depend on unverified
  knob names. See `nuke/comfyui_bridge/render.py`.
- ComfyUI → Nuke: set the `ToNuke` `format` input to `exr16`. The image is
  encoded as half-float RGB EXR and saved with a `.exr` extension. The created
  Nuke Read node leaves input transform/colorspace untouched; Nuke defaults or
  project settings decide how it is interpreted.
- **EXR on the ComfyUI side requires OpenImageIO** (`pip install OpenImageIO`
  or your distro's `python3-openimageio`). EXR decode also works through an
  OpenCV fallback built with OpenEXR support (read-only). If neither is
  importable, FromNuke raises a clear `RuntimeError` telling you to install
  OIIO or switch `format` to `png8`. EXR **encode** requires OpenImageIO.
- HDR values are clamped to `[0,1]` before the diffusion-model tensor; raw
  scene-referred values are not preserved through this layer yet.

Returned Read nodes from `/result` are placed next to the originating
`ComfyUIBridge` node (to the right) using `xpos`/`ypos` knobs, guarded in
`try`.

Frame exports are cached in memory for 5 minutes, keyed by bridge id, frame,
mask mode, colorspace, and connected source/mask node names. Re-running the same
workflow on the same frame should not re-export from Nuke. Use **Clear frame
cache** after changing the upstream Nuke graph if you need a fresh export for the
same frame.

### Triggering ComfyUI workflows from Nuke

The bridge node has a **workflow** dropdown plus **Refresh workflows** and
**Run selected workflow** buttons. The dropdown lists ComfyUI browser
workflows that are currently open and that contain at least one `FromNuke` or
`ToNuke` node.

Limitations:
- The ComfyUI tab with the workflow must be **open** with the frontend
  extension loaded (`comfyui/nuke_bridge/web/nuke_bridge.js`). Closed tabs are
  dropped from the list within ~60s.
- The list is not live — click **Refresh workflows** in Nuke after opening or
  editing a workflow in ComfyUI.
- Only workflows containing `FromNuke` and/or `ToNuke` are listed.
- If no workflows are visible (ComfyUI closed, extension not loaded, etc.),
  use ComfyUI directly or save/reopen the workflow so the browser extension can
  publish it. The old `workflow_api_path` runner is kept in code only as a
  fallback helper, but new bridge nodes do not expose a second run button.
- Nuke does **not** build or patch the workflow graph — it submits the API
  prompt as-is. The workflow still pulls frames via `FromNuke` and returns via
  `ToNuke`.

### Progress & cancellation

When you click **Run selected workflow**, Nuke opens a
`nuke.ProgressTask` and streams ComfyUI execution events over a stdlib
websocket client connected to `ws://host:port/ws?clientId=...`. The status
knob and the progress dialog update with the current node, `progress value/max`
events, and the final outcome.

- If the websocket connection fails (older ComfyUI, firewall, etc.), the
  bridge falls back to polling `GET /history/{prompt_id}` until the prompt
  appears, with a spinner in the status knob. No `progress` granularity in
  that mode.
- Clicking **Cancel** on the `ProgressTask` stops *monitoring* and marks the
  status `cancelled`. It does **not** remove the prompt from ComfyUI's queue —
  the job keeps running server-side (cancelling the ComfyUI queue is deferred,
  see `TASKS.md` Phase 4).
- Run buttons start a background monitor thread so Nuke can still service
  `FromNuke` frame requests while progress updates are marshalled back to the
  main thread.
- No new dependencies — the websocket client is stdlib `socket`/`ssl`; the
  ComfyUI host/port come from the `comfyui_host`/`comfyui_port` knobs.

## Install

### Recommended installer

Use the stdlib TUI installer from the repo root:

```bash
python3 tools/install.py
```

Useful non-interactive checks:

```bash
python3 tools/install.py --dry-run --yes
python3 tools/install.py --log install.log
```

The installer is intentionally conservative. It:

- adds one marked `nuke.pluginAddPath(...)` block to your user `~/.nuke/init.py`
  after making a backup;
- links `comfyui/nuke_bridge` into ComfyUI `custom_nodes/` by symlink, and if
  symlink creation fails it tells you and copies the custom node instead;
- runs the existing OmniPaint backend installer (`tools/install_omnipaint_local.sh`)
  and then validates the model/runtime state;
- clones or accepts an existing Sammie-Roto install and runs Sammie's own
  installer (`install.sh`, `install_dependencies.sh`, or the Windows `.bat`
  equivalents), including Sammie's own model-download/skip prompts;
- discovers LTX Desktop and its model directory only — it does not install,
  build, download LTX models, or prepopulate LTX projects.

Central settings live at:

```text
~/.nuke/comfyui_bridge/settings.json
```

Environment overrides:

```text
COMFYUI_ROOT
SAMMIE_ROOT
LTX_ROOT
LTX_MODELS_DIR
LTX_APP_DATA_DIR
NUKE_HOME_DIR
```

From Nuke, use:

```text
Nuke > AI Setup > Health Check
Nuke > AI Setup > Model Health
Nuke > AI Setup > Open Settings
Nuke > AI Launchers > Sammie-Roto (selected footage)
Nuke > AI Launchers > LTX Desktop
```

`Model Health` checks OmniPaint/FLUX/NF4 state. Fast health may report NF4 as
`partial` until the full import check is run from the installer menu item
**Check OmniPaint models**.

### Nuke side

Add one plugin path entry to your user `~/.nuke/init.py`:

```python
# ~/.nuke/init.py
nuke.pluginAddPath("/home/npittas/.nuke/inpaint/nuke")
```

Do not paste plugin logic into user `init.py` or `menu.py`. Nuke will discover
this repo's `nuke/init.py` and `nuke/menu.py` from the plugin path. The
plugin's `nuke/init.py` also adds `nuke/nodes/` to pluginPath, so the
`ComfyUIBridge.gizmo` is auto-discovered as a real node class. The bridge is
created from the node graph Tab menu (or Tab-search `ComfyUIBridge`):

```text
ComfyUI > ComfyUIBridge
```

The node is a native gizmo, so Nuke attaches it to the currently selected node
and places it in the graph like any other node — there is no Python-side
attachment or positioning. On creation an `onCreate` callback fills the dynamic
defaults: a generated `bridge_id`, and `host`/`port`/`output_directory` pulled
from `~/.nuke/comfyui_bridge/settings.json`. The same callback creates the
bridge knobs with Nuke's Python knob classes; the gizmo file stays a minimal
Input/Output shell.

Persistent settings live at `~/.nuke/comfyui_bridge/settings.json` with defaults
`host=127.0.0.1`, `port=8765`, `output_directory=~/comfyui_bridge_results`.
Use the bridge node's **Save defaults** button to persist edited host, port, and
output directory. Restart the local bridge server after changing host/port.

### ComfyUI side

Copy (or symlink) the `comfyui/nuke_bridge` directory into ComfyUI's
`custom_nodes/` directory:

```bash
ln -s /abs/path/to/inpaint/comfyui/nuke_bridge \
      /path/to/ComfyUI/custom_nodes/nuke_bridge
```

Restart ComfyUI. You should see **Nuke Bridge: From Nuke** and
**Nuke Bridge: To Nuke** in the node menu.

### Dependencies

No new dependencies beyond what ComfyUI already ships:

- Nuke side: standard library only.
- ComfyUI side: `requests`, `Pillow`, `numpy`, `torch` (all standard in
  ComfyUI).

## Quick manual test

1. Nuke: create `ComfyUIBridge`, connect a `Read`/`Constant` chain to input 0.
2. ComfyUI: `FromNuke` → any image op → `ToNuke`. Leave `bridge_id` blank to
   use the selected bridge in Nuke, or the only bridge if there is just one.
3. Run the ComfyUI graph. Nuke should write a PNG into
   `~/comfyui_bridge_results/` and (if `create_read_on_result` is on) create a
   Read node for it.

## Layout

```
nuke/
  init.py                     # path setup + onCreate callback registration
  menu.py                     # Tab-menu command (native createNode)
  nodes/
    ComfyUIBridge.gizmo       # real Nuke gizmo: minimal Input/Output shell
  comfyui_bridge/
    __init__.py
    settings.py               # ~/.nuke/comfyui_bridge/settings.json
    napi.py                   # main-thread isolation for all Nuke API access
    node.py                   # knob spec + ensure_knobs/initialize_defaults + fallback
    callbacks.py              # onCreate: ensure knobs + fill dynamic defaults
    server.py                 # /health, /frame, /result HTTP server
    render.py                 # temp-Write PNG/EXR render for /frame
    result.py                 # save bytes + create Read node for /result
    run_workflow.py           # legacy helper for saved API workflow submission
    workflow_selection.py     # open-workflow dropdown + run-selected handler
    comfy_progress.py         # stdlib websocket client + ProgressTask + history fallback
comfyui/
  nuke_bridge/
    __init__.py               # NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS / WEB_DIRECTORY
    nodes.py                  # FromNuke, ToNuke
    image_io.py               # PNG bytes <-> ComfyUI tensors
    workflow_registry.py      # /nuke_bridge/* routes + in-memory workflow list
    web/
      nuke_bridge.js          # frontend: publishes open workflows to backend
```
