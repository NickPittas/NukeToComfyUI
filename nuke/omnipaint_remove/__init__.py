"""OmniPaint removal Nuke node plugin (Phase 1: external-runner shell).

Independent of ComfyUIBridge. Owns a separate `OmniPaintRemove` gizmo whose
dynamic knobs are created from Python. The node renders its source + mask
inputs to temp PNGs, calls a user-configurable external adapter script, and
creates a Read node from the result. No OmniPaint model code lives here yet.

All Nuke API access is isolated via `comfyui_bridge.napi` so this package
imports cleanly outside Nuke for syntax checks.
"""

__version__ = "0.1.0"
