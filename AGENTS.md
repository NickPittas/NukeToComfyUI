# Repository Instructions

- This repo is a Nuke ↔ ComfyUI bridge project; see `PLAN.md` before implementing.
- Read `NUKE_PLUGIN_NOTES.md` before changing Nuke startup, node, gizmo, or knob code.
- Target architecture: a Nuke `ComfyUIBridge` node serves connected input on demand; ComfyUI `FromNuke`/`ToNuke` nodes own workflow-side pull/return. Do not build a panel-driven or Nuke-submitted workflow unless the plan changes.
- Nuke install convention: user `~/.nuke/init.py` should only call `nuke.pluginAddPath('/path/to/repo/nuke')`; this repo's `nuke/menu.py` registers the Tab-menu command.
- No build, test, lint, or typecheck commands are defined yet; use `python -m compileall nuke comfyui` for syntax checks outside Nuke.

## OmniPaintRemove Hard Rules

- OmniPaintRemove preview/export image processing must use **BlinkScript and Write nodes only** unless the user explicitly authorizes another node class in that same turn.
- Do **not** replace a requested BlinkScript implementation with Constant/Crop/Merge/Reformat/Transform/CurveTool/nodegraph generation as an implementation shortcut.
- Do **not** remove, disable, stub, or no-op a requested feature to avoid a crash. Fix the feature or stop and report the blocker.
- Do **not** create, delete, regenerate, or mutate user-visible root nodegraph helper nodes for OmniPaintRemove preview/export.
- If a subagent/fixer is used for OmniPaintRemove, the orchestrator must provide exact file paths, line ranges, variable names, code snippets, allowed edits, forbidden edits, validation commands, and hard stop conditions. Loose prompts are forbidden.
- A subagent/fixer must stop immediately and return a blocker if it lacks exact BlinkScript syntax, Nuke gizmo knob syntax, internal Write-node knob names, coordinate conversion math, or viewer/output routing details needed for the requested change.
- All BlinkScript work must preserve this architecture: BlinkScript performs image/alpha/crop/overlay processing inside the gizmo; Write node writes the non-overlay source/mask output; returned inference can be placed for the user to merge manually.
- Any deviation from these rules requires explicit user approval before editing files.

## Cloned Dependency Source

Read-only dependency source repositories are available under `.slim/clonedeps/repos/` for inspection. Do not edit these clones.

- `.slim/clonedeps/repos/yeates__OmniPaint/` — `https://github.com/yeates/OmniPaint.git` at `main@cdb7c263edbd463ff8df60694fd97f9f43da187b`; promptless FLUX-based object removal backend used by `tools/omnipaint_adapter.py`.
