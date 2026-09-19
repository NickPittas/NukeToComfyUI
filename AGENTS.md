# Repository Instructions

- This repo is a Nuke ↔ ComfyUI bridge project; see `PLAN.md` before implementing.
- Read `NUKE_PLUGIN_NOTES.md` before changing Nuke startup, node, gizmo, or knob code.
- Target architecture: a Nuke `ComfyUIBridge` node serves connected input on demand; ComfyUI `FromNuke`/`ToNuke` nodes own workflow-side pull/return. Do not build a panel-driven or Nuke-submitted workflow unless the plan changes.
- Nuke install convention: user `~/.nuke/init.py` should only call `nuke.pluginAddPath('/path/to/repo/nuke')`; this repo's `nuke/menu.py` registers the Tab-menu command.
- Do not create, generate, recreate, or expand tests, test infrastructure, or CI/CD without the user's explicit permission. This applies to delegated agents too.
- Ask before running tests or verification commands; do not start them automatically.
