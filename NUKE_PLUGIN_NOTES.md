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
- The gizmo bakes in the stable user knobs (`addUserKnob`) so saved scripts
  round-trip cleanly. The internal tree is `Input(source) -> Output` plus a
  second `Input(mask)` for the optional mask pipe (not wired to the output).
- `menu.py` registers the toolbar/Tab entry via
  `nuke.menu('Nodes').addCommand('ComfyUI/ComfyUIBridge', ..., icon=...)`,
  calling native `createNode`. Do NOT do manual `setInput`/`xpos`/`ypos` from
  Python — let Nuke place and attach. (The previous hack was removed.)
- A Python-built Group fallback (`node._create_group_fallback`) exists for the
  rare case the gizmo isn't on pluginPath; it builds the same internal tree +
  knob layout but also relies on Nuke for placement.

## onCreate callback (dynamic defaults)

- `comfyui_bridge.callbacks.register()` installs `nuke.addOnCreate(...,
  nodeClass='ComfyUIBridge')`. On node creation it:
  1. `node.ensure_knobs(node)` — adds any knob from the spec that's missing on
     the running Nuke version (safety net for addUserKnob parsing differences).
  2. `node.initialize_defaults(node)` — fills empty `bridge_id` (uuid),
     `host`/`port`/`output_directory` from settings, `comfyui_host`/`port`,
     `create_read_on_result`, `status`.
- All fills are "if empty", so loading a saved script keeps the user's values.
- The callback is registered from `init.py` (covers all modes) and again from
  `menu.py` (idempotent — `callbacks._REGISTERED` guards double registration).

## Knobs

- Knob layout has a single source of truth: `node._knob_specs()`, a list of
  `(name, builder(nuke))`. Both the Group fallback and `ensure_knobs` consume
  it. If you add/remove a knob, update `_knob_specs()` AND the matching
  `addUserKnob` line in `ComfyUIBridge.gizmo`.
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
