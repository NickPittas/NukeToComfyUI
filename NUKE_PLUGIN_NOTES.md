# Nuke Plugin Notes

These rules are project knowledge for future implementation work.

## Startup files

- User-level `~/.nuke/init.py` should only add this plugin directory to Nuke's plugin path:

  ```python
  nuke.pluginAddPath('/path/to/NukeToComfyUI/nuke')
  ```

- Plugin `init.py` is for non-UI setup such as adding `nodes/` or `icons/` paths. Do not create menus or nodes there.
- Plugin `menu.py` is GUI-only and registers commands in Nuke's Nodes toolbar/tab menu.

## Node creation

- `ComfyUIBridge` is added from the node graph Tab menu via:

  ```python
  nuke.menu('Nodes').addCommand('ComfyUI/ComfyUIBridge', create_bridge_node)
  ```

- Because Phase 1 uses a Python-created Group, not a `.gizmo` file, it is not auto-discovered as a node class. The menu command must call the Python factory.
- The factory must create a real passthrough Group with internal `Input` -> `Output` nodes using `group.begin()` / `group.end()`.

## Gizmo vs Python-created Group

- `.gizmo` files in `pluginPath()` are discoverable by `nuke.createNode('Name')` and can also be exposed in `menu.py`.
- Python-created Groups are embedded in the script and need a `menu.py` factory command.
- This project may move to a `.gizmo`/`.nk` template later, but Phase 1 keeps the Group factory because the node is still changing quickly.

## Knobs

- Use `File_Knob` for filesystem paths.
- Use `Multiline_Eval_String_Knob` for prompts.
- Use `Enumeration_Knob` for mode dropdowns.
- Use `PyScript_Knob` for buttons and set its Python command string explicitly.

## Main-thread rule

All Nuke graph, node, and knob access from HTTP worker threads must go through `nuke.executeInMainThreadWithResult` via `comfyui_bridge.napi.call`.
