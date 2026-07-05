# Nuke ↔ ComfyUI Bridge Plan

## Goal

Build a Nuke node + ComfyUI custom nodes that let a ComfyUI workflow pull the current frame from a connected Nuke node and send the result back to Nuke.

Nuke does not build the ComfyUI workflow. ComfyUI owns workflow execution. Nuke may optionally trigger a saved ComfyUI API workflow for convenience, but the workflow must still use `FromNuke`/`ToNuke` to pull/return data.

## First architecture

```text
Nuke graph                         ComfyUI workflow
----------                         ----------------
upstream image/mask
      |
ComfyUIBridge node  <--- HTTP ---  FromNuke
      |                            workflow nodes
      |                 HTTP --->  ToNuke
new Read node/result
```

## Nuke plugin

Files:

```text
nuke/menu.py
nuke/comfyui_bridge/__init__.py
nuke/comfyui_bridge/node.py
nuke/comfyui_bridge/server.py
nuke/comfyui_bridge/render.py
nuke/comfyui_bridge/settings.py
nuke/comfyui_bridge/result.py
```

### `ComfyUIBridge` node

Use a Nuke Group/Gizmo-style node, not a panel.

Inputs:

```text
input 0: source image
input 1: optional mask
```

Output:

```text
passthrough of input 0
```

Knobs:

```text
bridge_id
host
port
comfyui_host
comfyui_port
output_directory
prompt
workflow_api_path
frame_mode: current / explicit
frame

send_format: exr16 / png8
send_colorspace: raw / sRGB
clamp_before_send

mask_source:
  source alpha
  invert source alpha
  mask input
  invert mask input

result_format: png8 / exr16
result_colorspace: sRGB / raw
create_read_on_result
status
last_result
```

### Persistent settings

`host`, `port`, and default output directory are saved globally so a Nuke restart does not require re-entry.

Use a small JSON file under the user's Nuke home, for example:

```text
~/.nuke/comfyui_bridge/settings.json
```

One Nuke process owns one bound bridge server. Node knobs may display/edit the saved global host/port defaults, but changing them requires a server restart or Nuke restart.

Install convention: user `~/.nuke/init.py` only calls `nuke.pluginAddPath('/path/to/repo/nuke')`. This repo's `nuke/menu.py` registers `ComfyUI/ComfyUIBridge` in the node graph Tab menu.

### Nuke HTTP server

Start once when the plugin loads.

Endpoints:

```text
GET  /health
GET  /bridges
POST /bridge/{bridge_id}/frame
POST /bridge/{bridge_id}/result
```

See `PROTOCOL.md` for the exact payload contract.

`FromNuke` calls:

```text
POST /bridge/{bridge_id}/frame
```

Nuke renders the connected input on demand using a temporary Write node. All Nuke API work must run on Nuke's main thread via `nuke.executeInMainThreadWithResult`.

`ToNuke` calls:

```text
POST /bridge/{bridge_id}/result
```

Nuke writes the returned image to the bridge output directory and creates a Read node if enabled.

### Optional run from Nuke

The bridge node may include a `Run workflow` button for convenience. It posts a selected ComfyUI API workflow JSON to ComfyUI and watches progress, but it must not build or understand the workflow graph. The workflow still pulls image data through `FromNuke` and returns through `ToNuke`.

Progress indication should use ComfyUI WebSocket events plus a Nuke `ProgressTask` where possible.

## ComfyUI plugin

Files:

```text
comfyui/nuke_bridge/__init__.py
comfyui/nuke_bridge/nodes.py
comfyui/nuke_bridge/image_io.py
```

### `FromNuke`

Inputs:

```text
bridge_id: STRING
host: STRING
port: INT
frame: INT, -1 means Nuke current frame
```

Outputs:

```text
IMAGE
MASK
STRING prompt
INT width
INT height
```

It requests the frame from the Nuke bridge server and converts the returned image to ComfyUI tensors.

### `ToNuke`

Inputs:

```text
image: IMAGE
bridge_id: STRING
host: STRING
port: INT
filename_prefix: STRING
format: png8 / exr16
```

Output:

```text
IMAGE passthrough
```

It encodes the image and posts it back to Nuke.

## Mask behavior

Nuke exposes four mask modes:

```text
source alpha
invert source alpha
mask input
invert mask input
```

Nuke sends RGBA-like data to ComfyUI. `FromNuke` converts the selected Nuke mask/alpha to ComfyUI's mask convention:

```text
ComfyUI mask: 0 = keep/opaque, 1 = masked/transparent
Nuke alpha:   1 = opaque,      0 = transparent
```

So source alpha becomes:

```text
mask = 1 - alpha
```

Inverted modes flip before/after conversion consistently so the user-facing meaning stays clear.

## Color and transport

First serious default:

```text
Nuke -> ComfyUI: EXR half-float RGBA
ComfyUI -> Nuke: PNG8 first, EXR half-float next
```

Fallback/proof mode:

```text
PNG8, explicit sRGB/clamp
```

Nuke decides outgoing color. ComfyUI decodes pixels and does not guess hidden color transforms.

Initial color knobs stay minimal:

```text
send_colorspace: raw / sRGB
result_colorspace: sRGB / raw
```

## Borrowed implementation ideas

- From `vinavfx/ComfyUI-for-Nuke`: Nuke Group/Gizmo node patterns, temp Write-node rendering, EXR-first transfer, Read-node result creation, Nuke threading discipline.
- From `sumitchatterjee13/nuke-nodes-comfyui`: OpenImageIO/EXR and color utility ideas where useful.
- From Krita/ComfyUI tooling nodes: external host bridge node pattern and HTTP image transfer concepts.

Licensing is not a blocker for this personal project; prioritize working results.

## Phase 1 acceptance test

1. In Nuke, create `ComfyUIBridge` and connect a Read/Constant/Grade chain to input 0.
2. In ComfyUI, run `FromNuke -> preview/simple operation -> ToNuke`.
3. `FromNuke` receives the Nuke image and mask according to `mask_source`.
4. `ToNuke` sends the result back.
5. Nuke writes the result and creates a Read node.
6. If `workflow_api_path` is set, clicking `Run workflow` in Nuke triggers the same ComfyUI workflow and shows progress.

## Deferred

- Full OCIO display/view support.
- Sequence/batch rendering.
- Bridge discovery dropdown in ComfyUI.
- WebSocket progress streaming.
- Remote multi-machine mode.
