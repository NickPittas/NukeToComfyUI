# Nuke ↔ ComfyUI Bridge

A Nuke node (`ComfyUIBridge`) plus ComfyUI custom nodes (`FromNuke`, `ToNuke`)
that let a ComfyUI workflow pull the current frame from a connected Nuke node
and push a result image back.

See `PLAN.md` and `PROTOCOL.md` for the full architecture.

## Status

**Phase 1 PNG proof of concept.** Round trip is PNG8 RGBA only. EXR/half-float
(Phase 2), explicit color controls (Phase 3), and discovery/robustness (Phase 4)
are deferred — see `TASKS.md`.

All four mask modes are implemented in `/frame`:
- `source alpha` / `invert source alpha`: handled by a small in-Nuke tree.
- `mask input` / `invert mask input`: source RGB is rendered in Nuke, then the
  mask input's carrier alpha (alpha channel if it varies, else luma) is
  composited in via a robust PIL fallback (exact Nuke Copy/Shuffle knob names
  vary across versions and can't be tested here). Disconnected mask input falls
  back to all-keep (carrier alpha = 1). See `nuke/comfyui_bridge/render.py`.

Returned Read nodes from `/result` are placed next to the originating
`ComfyUIBridge` node (to the right) using `xpos`/`ypos` knobs, guarded in
`try`.

## Install

### Nuke side

Add one plugin path entry to your user `~/.nuke/init.py`:

```python
# ~/.nuke/init.py
nuke.pluginAddPath("/home/npittas/.nuke/inpaint/nuke")
```

Do not paste plugin logic into user `init.py` or `menu.py`. Nuke will discover
this repo's `nuke/init.py` and `nuke/menu.py` from the plugin path. The bridge is
created from the node graph Tab menu:

```text
ComfyUI > ComfyUIBridge
```

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

- Nuke side: standard library only (plus optional `requests` for the
  Nuke-triggered `Run workflow` button).
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
  init.py                     # path-only setup loaded by Nuke
  menu.py                     # Tab-menu command registration
  comfyui_bridge/
    __init__.py
    settings.py               # ~/.nuke/comfyui_bridge/settings.json
    napi.py                   # main-thread isolation for all Nuke API access
    node.py                   # ComfyUIBridge Group factory + knobs
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
