# Nuke ↔ ComfyUI Bridge

A Nuke `ComfyUIBridge` node and four ComfyUI nodes exchange images, masks,
video, prompts, and returned results without moving workflow ownership into
Nuke.

- `FromNuke` pulls one image and mask from Nuke.
- `ToNuke` returns one image to Nuke.
- `FromNukeVideo` pulls a frame range and mask sequence from Nuke.
- `ToNukeVideo` returns an image batch as MOV or MP4.

Nuke can trigger an open ComfyUI workflow, or the workflow can be queued
normally in ComfyUI. Both directions use the same source mapping.

## Architecture

```text
Nuke graph                              ComfyUI workflow
----------                              ----------------
source image / optional mask
          |
ComfyUIBridge A  <--- HTTP ----------  FromNuke
          |                              |
          |                         processing nodes
          |                              |
          +-------- HTTP <-----------  ToNuke

video source
          |
ComfyUIBridge B  <--- HTTP ----------  FromNukeVideo
                                         |
                                    processing nodes
                                         |
                    HTTP <-----------  ToNukeVideo
```

The `ComfyUIBridge` node is a passthrough in the Nuke graph. ComfyUI owns and
executes the workflow. Nuke only serves connected media on demand, receives
results, and optionally queues an already-published workflow.

## Automatic bridge IDs

Every `FromNuke`, `FromNukeVideo`, `ToNuke`, and `ToNukeVideo` node receives an
automatic persistent `bridge-*` ID from the ComfyUI frontend extension.

- New nodes receive an ID when created.
- Blank nodes from older saved workflows receive an ID when loaded.
- A copied node with a duplicate ID receives a new ID.
- Existing non-duplicate IDs remain unchanged.
- Save an upgraded workflow once so generated IDs persist.
- Manual ID entry is not required.

These ComfyUI route IDs are separate from each Nuke `ComfyUIBridge` node's
native `bridge_id`. Mapping never overwrites the Nuke ID.

## Workflow discovery and source mapping

Open ComfyUI browser tabs publish API prompts and routing metadata to Nuke.
Closed tabs expire from the workflow list after about 60 seconds.

On a Nuke `ComfyUIBridge` node:

1. Click **Refresh workflows**.
2. Select the open ComfyUI workflow under **Image** or **Video**.
3. Select the ComfyUI source node that this Nuke bridge should serve.

The source selectors are type-specific:

- **Image → FromNuke input** lists only `FromNuke` nodes.
- **Video → FromNukeVideo input** lists only `FromNukeVideo` nodes.
- Labels include the node title, automatic bridge ID, and ComfyUI graph ID.

A workflow can map different source nodes to different Nuke bridge nodes. Each
source must map to exactly one Nuke bridge. Missing, duplicate, stale, or
ambiguous mappings fail before queueing and are reported in the Nuke node's
Status and Logs.

Mappings are held by the running Nuke process. After restarting Nuke, click
**Refresh workflows** and confirm the source selections before queueing from
either application.

## Running workflows

### Run from Nuke

Select the workflow and source, then click **Run selected image workflow** or
**Run selected video workflow**. Nuke:

1. requests the selected tab's current API-format prompt;
2. gathers every source mapping used by the workflow;
3. validates automatic IDs and rejects missing or duplicate mappings;
4. applies the mapped Nuke host, port, format, codec, and colorspace values to
   the bridge nodes in the submitted prompt;
5. queues the prompt on ComfyUI and follows its progress.

ComfyUI source IDs remain stable. `ToNuke` and `ToNukeVideo` are routed through
the sole matching upstream mapped media source.

### Queue directly in ComfyUI

The normal ComfyUI Queue button uses the same mapping:

- `FromNuke*` sends its automatic source ID to Nuke.
- Nuke resolves that ID to the selected `ComfyUIBridge` node.
- `ToNuke*` sends its own automatic output ID.
- Nuke resolves the output through its published upstream source metadata and
  returns the result to the corresponding mapped bridge.

An output with no mapped upstream source or more than one possible Nuke target
is rejected instead of being saved or attached to the wrong node.

## Image transport

`FromNuke` requests a frame from the connected Nuke input. Nuke renders through
a temporary Write node and returns PNG8 or EXR16 data. The node outputs:

```text
IMAGE, MASK, prompt, width, height
```

`ToNuke` encodes its image input, posts it to Nuke, saves it under the configured
output directory, and optionally creates a Read node. Its IMAGE output remains
a passthrough.

### PNG8

PNG8 is the default transport. Nuke explicitly chooses the outgoing colorspace
and clamp behavior. The ComfyUI side decodes the bytes without applying a
hidden transform.

### EXR16

EXR16 transports half-float RGB and float alpha. It requires OpenEXR support on
the ComfyUI Python environment and a Nuke installation able to read/write EXR.
If EXR support is unavailable, use PNG8.

### Masks

Nuke supports:

```text
source alpha
invert source alpha
mask input
invert mask input
```

ComfyUI MASK uses `0 = keep` and `1 = masked`. The bridge converts Nuke alpha
accordingly. A disconnected mask input produces an all-keep mask.

## Video transport

`FromNukeVideo` requests a Nuke frame range, downloads the main and mask video
assets, and outputs an IMAGE batch, MASK batch, metadata, dimensions, frame
count, FPS, and prompt. `ToNukeVideo` encodes an IMAGE batch and returns MOV or
MP4 to Nuke. The returned video can create a Read node when enabled.

Nuke performs source and mask rendering with native Write nodes. ComfyUI video
decode/encode requires `ffmpeg` and `ffprobe`.

### Optional 8n+1 normalization

Enable **expand to 8n+1** on the Nuke Video tab when a downstream model requires
frame counts of the form `8n+1`.

- The requested first frame stays fixed.
- The range expands only at the end.
- An already-valid count is unchanged.
- Disabled behavior is unchanged.

Examples:

```text
1-80  (80 frames) -> 1-81 (81 frames)
1-73  (73 frames) -> 1-73 (73 frames)
```

The Video tab shows requested and effective ranges. Returned metadata includes
requested/effective start, end, count, and whether normalization was enabled.
Video caching uses the effective range.

## Progress and cancellation

Nuke-run workflows listen to ComfyUI WebSocket progress events and update a
Nuke `ProgressTask`. Cancellation sends ComfyUI `/interrupt` and marks the Nuke
node cancelled. Image and video transfers also publish stage progress.

## Installation

### Installer

```bash
python install.py --nuke-home ~/.nuke --comfyui /path/to/ComfyUI
```

Useful options:

```text
--repo PATH
--no-nuke
--no-comfy
--dry-run
--uninstall
--skip-nuke-checks
--install-deps
--python PATH
--keep-going
--yes
--enable-exr
```

The installer links the ComfyUI custom node, configures the Nuke plugin path,
and can install optional Python dependencies.

### Manual Nuke installation

User `~/.nuke/init.py` should contain only the plugin-path registration:

```python
nuke.pluginAddPath('/path/to/NukeToComfyUI/nuke')
```

The repository's `nuke/menu.py` registers **ComfyUI/ComfyUIBridge** in Nuke's
Tab/Nodes menu. Do not copy node-registration code into the user `init.py`.

### Manual ComfyUI installation

Link or copy:

```text
comfyui/nuke_bridge
```

into:

```text
ComfyUI/custom_nodes/nuke_bridge
```

Restart ComfyUI and confirm these nodes appear under **Nuke Bridge**:

```text
From Nuke
To Nuke
From Nuke Video
To Nuke Video
```

Keep only one active `nuke_bridge` custom-node installation. Duplicate copies
can load stale Python or frontend code.

## Network configuration

Defaults:

```text
Nuke bridge listen address: 0.0.0.0:8765
ComfyUI API:                127.0.0.1:8188
```

For same-machine use, ComfyUI can reach Nuke at `127.0.0.1`. For different
machines:

- leave Nuke **Listen on** at `0.0.0.0` if remote access is required;
- set **Nuke host** to the hostname or IP ComfyUI can reach;
- set **ComfyUI host/port** to the API address Nuke can reach;
- allow both ports through the host firewall;
- do not expose the unauthenticated bridge directly to the public internet.

Changing the Nuke listener host or port requires restarting the bridge listener
with **Save defaults** or restarting Nuke.

## Troubleshooting

### Run button reports a missing mapping

Click **Refresh workflows**, reselect the workflow, and choose the required
**FromNuke input** or **FromNukeVideo input**. The error includes the unmapped
ComfyUI graph node ID.

### Workflow list is empty

Keep the workflow open in a ComfyUI browser tab, confirm it contains at least
one bridge node, then click **Refresh workflows**. Reload ComfyUI if the frontend
extension was installed while the server was running.

### ComfyUI cannot pull from Nuke

Check Nuke Status/Logs, the advertised **Nuke host**, port `8765`, firewall, and
that only one active ComfyUI bridge plugin copy is installed.

### Video transfer fails before decoding

Confirm `ffmpeg` and `ffprobe` are on `PATH`, then check the Nuke log for Write
format/codec errors.

## Dependencies

Core image operation:

```text
Nuke
ComfyUI
Python 3
requests
numpy
Pillow
PyTorch
```

EXR16 adds:

```text
OpenEXR
Imath
```

Video adds:

```text
ffmpeg
ffprobe
```

## Repository layout

```text
nuke/
  init.py
  menu.py
  nodes/ComfyUIBridge.gizmo
  comfyui_bridge/
    callbacks.py
    napi.py
    node.py
    render.py
    result.py
    server.py
    video.py
    workflow_selection.py

comfyui/nuke_bridge/
  __init__.py
  nodes.py
  image_io.py
  video_io.py
  workflow_registry.py
  web/nuke_bridge.js

install.py
PLAN.md
PROTOCOL.md
TASKS.md
```
