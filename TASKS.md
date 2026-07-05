# Implementation Tasks

## Phase 0 - project skeleton

- [ ] Define exact HTTP protocol in `PROTOCOL.md` before endpoint code.
- [ ] Create Nuke plugin package under `nuke/comfyui_bridge/`.
- [ ] Create ComfyUI custom node package under `comfyui/nuke_bridge/`.
- [ ] Add smoke-test helpers that can run outside Nuke/ComfyUI where possible.

## Phase 1 - PNG proof round trip

Goal: prove ComfyUI can pull one frame from a connected Nuke node and return one image.

### Nuke side

- [ ] Implement persistent settings JSON: `~/.nuke/comfyui_bridge/settings.json`.
- [ ] Treat `host`/`port` as global server settings; node knobs display/edit defaults but do not create per-node servers.
- [ ] Start one local HTTP server on plugin load, using saved host/port defaults.
- [ ] Implement `GET /health`.
- [ ] Register a `ComfyUIBridge` Group/Gizmo-style node from `menu.py`.
- [ ] Add bridge knobs: `bridge_id`, Nuke server `host`/`port`, `comfyui_host`, `comfyui_port`, `output_directory`, `prompt`, `workflow_api_path`, `mask_source`, `send_format`, `send_colorspace`, `create_read_on_result`, `status`, `last_result`.
- [ ] Keep user install to one `nuke.pluginAddPath('/path/to/repo/nuke')` line in `~/.nuke/init.py`; repo `nuke/menu.py` owns Tab-menu registration.
- [ ] Generate stable `bridge_id` values and handle duplicate IDs from copied nodes.
- [ ] Locate bridge nodes by main-thread scan on request; postpone live registry unless needed.
- [ ] Route every Nuke API access through the main thread, including node lookup, knob reads/writes, rendering, Read creation, and status updates.
- [ ] Add one render/result lock so concurrent HTTP requests cannot run overlapping Nuke renders.
- [ ] Implement `POST /bridge/{bridge_id}/frame`.
- [ ] Render bridge input 0 through a temporary Write node for one frame.
- [ ] Support mask modes for first transport:
  - source alpha
  - invert source alpha
  - mask input
  - invert mask input
- [ ] Return PNG RGBA bytes plus `X-NukeBridge-*` metadata headers as defined in `PROTOCOL.md`.
- [ ] Clean up temporary Write nodes/files after frame requests.
- [ ] Implement `POST /bridge/{bridge_id}/result`.
- [ ] Save returned result to the bridge output directory with deterministic unique filenames.
- [ ] Create a Nuke Read node for the returned result when enabled.
- [ ] Add optional `Run workflow` button that submits a selected ComfyUI API workflow JSON without building/patching the workflow graph.
- [ ] Add progress indication for Nuke-triggered runs using ComfyUI WebSocket events and Nuke `ProgressTask` where possible.

### ComfyUI side

- [ ] Implement `FromNuke` custom node.
- [ ] `FromNuke` calls Nuke `/frame`, decodes PNG RGBA, returns `IMAGE`, `MASK`, `prompt`, `width`, `height`.
- [ ] Implement `FromNuke.IS_CHANGED` so ComfyUI pulls again on repeated runs/current-frame changes.
- [ ] Implement `ToNuke` custom node.
- [ ] Mark `ToNuke` as an output node so ComfyUI does not prune it.
- [ ] `ToNuke` encodes IMAGE to PNG and posts to Nuke `/result`.
- [ ] Add node registration in `comfyui/nuke_bridge/__init__.py`.

### Phase 1 manual acceptance

- [ ] In Nuke: `Constant/Read -> Grade -> ComfyUIBridge`.
- [ ] In ComfyUI: `FromNuke -> simple image operation -> ToNuke`.
- [ ] Confirm returned image appears as a new Nuke Read node.
- [ ] Confirm all four mask modes behave as named.
- [ ] Restart Nuke and confirm saved host/port defaults are reused.
- [ ] Run the same ComfyUI workflow twice after changing the Nuke input/frame and confirm `FromNuke` repulls.
- [ ] Trigger the workflow from Nuke and confirm progress indication updates.

## Phase 2 - EXR/half-float transport

Goal: stop relying on 8-bit PNG for production use.

- [ ] Add `exr16` send format on Nuke side using temporary Write node with `rgba`.
- [ ] Add EXR decode in ComfyUI, preferring OpenImageIO if available.
- [ ] Preserve alpha/mask behavior for EXR.
- [ ] Add `exr16` result format from `ToNuke` back to Nuke.
- [ ] Set created Read node colorspace/raw knobs consistently with result metadata.
- [ ] Keep PNG8 fallback working.

## Phase 3 - color controls

Goal: make color conversion explicit without building a full OCIO suite.

- [ ] Implement `send_colorspace=raw` as no conversion.
- [ ] Implement `send_colorspace=sRGB` as Nuke-side conversion before transport.
- [ ] Add metadata fields: `source_colorspace`, `send_colorspace`, `range`, `format`, `bit_depth`, `mask_source`.
- [ ] Implement `result_colorspace` tagging for created Read nodes.
- [ ] Document expected model-space behavior: ComfyUI receives model-ready pixels and does not guess hidden transforms.

## Phase 4 - usability hardening

- [ ] Add `/bridges` endpoint for discovery.
- [ ] Add readable errors in ComfyUI node output when Nuke is closed, bridge ID is missing, or render fails.
- [ ] Add Nuke node status updates and failure tile color.
- [ ] Add deterministic result filename/versioning.
- [ ] Add timeout knobs for `FromNuke` and `ToNuke`.

## Deferred

- [ ] Sequence/batch mode.
- [ ] WebSocket progress streaming.
- [ ] ComfyUI bridge discovery dropdown.
- [ ] Full OCIO display/view support.
- [ ] Remote-machine authentication/security.
