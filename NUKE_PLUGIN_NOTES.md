# Nuke Plugin Notes

These rules are project knowledge for future implementation work.

## Startup files

- User-level `~/.nuke/init.py` should only add this plugin directory to Nuke's plugin path:

  ```python
  nuke.pluginAddPath('/path/to/repo/nuke')
  ```

- Plugin `init.py` adds `nodes/` and `icons/` to pluginPath (so the gizmo and
  any icons are discovered) and registers the ComfyUIBridge `onCreate`
  callback. It runs in all modes (GUI, terminal, render fanout).
- Plugin `menu.py` is GUI-only and registers the Tab/toolbar command.

## Node creation — real gizmo

- `ComfyUIBridge` is a real Nuke gizmo: `nuke/nodes/ComfyUIBridge.gizmo`. It
  is auto-discovered from pluginPath as node class `ComfyUIBridge`, so
  `nuke.createNode('ComfyUIBridge')` works natively and Nuke handles input
  attachment and graph placement.
- The gizmo must stay minimal. It defines only the native shell and internal
  Input/Output nodes. Do **not** hand-write `addUserKnob` entries for bridge
  controls; wrong knob codes/types break Nuke UI. Bridge knobs are added by
  `comfyui_bridge.callbacks` using real Nuke Python knob classes.
- External inputs are defined by internal `Input { inputs 0 ... }` nodes. Do not
  add an `inputs` property to the `Gizmo` block. Do not add `inputs` to `Output`.
  Nuke's Tcl stack wires the first `Input` to `Output` in the minimal passthrough.
- The gizmo file must end with `end_group`, like Nuke-exported gizmos.
- `menu.py` registers the toolbar/Tab entry via
  `nuke.menu('Nodes').addCommand('ComfyUI/ComfyUIBridge', ..., icon=...)`,
  calling native `createNode`. Do NOT do manual `setInput`/`xpos`/`ypos` from
  Python — let Nuke place and attach. (The previous hack was removed.)
- A Python-built Group fallback (`node._create_group_fallback`) exists for the
  rare case the gizmo isn't on pluginPath; it builds the same internal tree +
  knob layout but also relies on Nuke for placement.

## onCreate callback (dynamic defaults)

- `ComfyUIBridge.gizmo` has an `onCreate` string that calls
  `callbacks.initialize_this_node()`. `callbacks.register()` also installs
  `nuke.addOnCreate(..., nodeClass='ComfyUIBridge')` as a safety net. On node
  creation it:
  1. `node.ensure_knobs(node)` — adds any knob from the Python spec that's
     missing on the node.
  2. `node.initialize_defaults(node)` — fills empty `bridge_id` (uuid),
     `host`/`port`/`output_directory` from settings, `comfyui_host`/`port`,
     `create_read_on_result`, `status`.
- All fills are "if empty", so loading a saved script keeps the user's values.
- The callback is registered from `init.py` (covers all modes) and again from
  `menu.py` (idempotent — `callbacks._REGISTERED` guards double registration).

## Knobs

- Knob layout has a single source of truth: `node._knob_specs()`, a list of
  `(name, builder(nuke))`. The onCreate callback and Group fallback consume it.
  If you add/remove a bridge knob, update `_knob_specs()` only; do not add static
  `addUserKnob` lines to the gizmo.
- `File_Knob` (with `String_Knob` fallback) for filesystem paths.
- `Multiline_Eval_String_Knob` (with `String_Knob` fallback) for prompts.
- `Enumeration_Knob` for mode dropdowns.
- `PyScript_Knob` for buttons, set via `.setValue(command_string)`; commands
  call existing Python modules using `nuke.thisNode()`.

## Main-thread rule

All Nuke graph, node, and knob access from HTTP worker threads must go through
`nuke.executeInMainThreadWithResult` via `comfyui_bridge.napi.call`. The
`onCreate` callback runs on the main thread already, so it uses the Nuke API
directly.
