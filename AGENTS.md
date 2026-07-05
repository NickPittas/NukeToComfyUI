# Repository Instructions

- This repo is a Nuke ↔ ComfyUI bridge project; see `PLAN.md` before implementing.
- Target architecture: a Nuke `ComfyUIBridge` node serves connected input on demand; ComfyUI `FromNuke`/`ToNuke` nodes own workflow-side pull/return. Do not build a panel-driven or Nuke-submitted workflow unless the plan changes.
- No build, test, lint, or typecheck commands are defined yet.
- Git is initialized, but no initial commit has been requested or made.
