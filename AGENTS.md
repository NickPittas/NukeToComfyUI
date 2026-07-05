# Repository Instructions

- This repo is a Nuke ↔ ComfyUI bridge project; see `PLAN.md` before implementing.
- Read `NUKE_PLUGIN_NOTES.md` before changing Nuke startup, node, gizmo, or knob code.
- Target architecture: a Nuke `ComfyUIBridge` node serves connected input on demand; ComfyUI `FromNuke`/`ToNuke` nodes own workflow-side pull/return. Do not build a panel-driven or Nuke-submitted workflow unless the plan changes.
- Nuke install convention: user `~/.nuke/init.py` should only call `nuke.pluginAddPath('/path/to/repo/nuke')`; this repo's `nuke/menu.py` registers the Tab-menu command.
- No build, test, lint, or typecheck commands are defined yet; use `python -m compileall nuke comfyui` for syntax checks outside Nuke.
