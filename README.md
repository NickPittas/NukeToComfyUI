# Nuke ↔ ComfyUI Bridge

A Nuke node (`ComfyUIBridge`) plus ComfyUI custom nodes (`FromNuke`, `ToNuke`)
that let a ComfyUI workflow pull the current frame from a connected Nuke node
and push a result image back.

See `PLAN.md` and `PROTOCOL.md` for the full architecture.

## Status

**Phase 1 PNG proof of concept.** Round trip is PNG8 RGBA only. EXR/half-float
(Phase 2), explicit color controls (Phase 3), and discovery/robustness (Phase 4)
are deferred — see `TASKS.md`.

Mask modes `source alpha` and `invert source alpha` are implemented.
`mask input` / `invert mask input` raise a `NotImplementedError` in `/frame`
until they can be tested inside Nuke (see `nuke/comfyui_bridge/render.py`).

## Install

### Nuke side

Copy (or symlink) the `nuke/comfyui_bridge` package somewhere on your Python
path and load `nuke/menu.py` from your `~/.nuke/menu.py`. Simplest option:

```python
# ~/.nuke/menu.py
import sys
sys.path.insert(0, "/abs/path/to/inpaint/nuke")
import comfyui_bridge  # noqa: F401  (registers settings helpers)
from comfyui_bridge import node as _cb_node
_cb_node.register_node()
from comfyui_bridge import server as _cb_server
_cb_server.autostart_if_in_nuke()
```

Or copy `nuke/menu.py` to `~/.nuke/menu.py` and edit the path insert.

Persistent settings live at `~/.nuke/comfyui_bridge/settings.json` with defaults
`host=127.0.0.1`, `port=8765`, `output_directory=~/comfyui_bridge_results`.

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

- Nuke side: standard library only (plus optional `requests` for the
  Nuke-triggered `Run workflow` button).
- ComfyUI side: `requests`, `Pillow`, `numpy`, `torch` (all standard in
  ComfyUI).

## Quick manual test

1. Nuke: create `ComfyUIBridge`, connect a `Read`/`Constant` chain to input 0,
   note the `bridge_id` knob value.
2. ComfyUI: `FromNuke` (set `bridge_id`) → any image op → `ToNuke`
   (same `bridge_id`).
3. Run the ComfyUI graph. Nuke should write a PNG into
   `~/comfyui_bridge_results/` and (if `create_read_on_result` is on) create a
   Read node for it.

## Layout

```
nuke/
  menu.py                     # Nuke init entry point
  comfyui_bridge/
    __init__.py
    settings.py               # ~/.nuke/comfyui_bridge/settings.json
    napi.py                   # main-thread isolation for all Nuke API access
    node.py                   # ComfyUIBridge Group node + knobs
    server.py                 # /health, /frame, /result HTTP server
    render.py                 # temp-Write PNG render for /frame
    result.py                 # save bytes + create Read node for /result
    run_workflow.py           # optional POST /prompt stub
comfyui/
  nuke_bridge/
    __init__.py               # NODE_CLASS_MAPPINGS / NODE_DISPLAY_NAME_MAPPINGS
    nodes.py                  # FromNuke, ToNuke
    image_io.py               # PNG bytes <-> ComfyUI tensors
```
