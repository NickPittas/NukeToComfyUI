"""OmniPaintRemove knob layout + creation helpers.

Single source of truth for the user-knob layout, mirroring the pattern in
`comfyui_bridge.node`. The `.gizmo` file defines only the native shell; this
module adds the real knob types from Python via `ensure_knobs` (called from the
onCreate callback) and fills dynamic per-instance defaults.

Only artist/runtime knobs live here. Implementation paths (python venv, backend
adapter script, OmniPaint repo) are intentionally NOT exposed — they are
plugin-local, resolved automatically by `run.py`, and set up by
`tools/install_omnipaint_local.sh`.
"""

from __future__ import annotations

from typing import Any, Callable, List, Tuple

from comfyui_bridge import napi
from comfyui_bridge.settings import load_settings

NODE_CLASS_NAME = "OmniPaintRemove"
_DEPRECATED_KNOBS = (
    "python_executable", "backend_script", "omnipaint_repo",
    "model_colorspace", "mask_colorspace", "result_colorspace",
)


def _file_or_string(nuke: Any, name: str, label: str) -> Any:
    File = getattr(nuke, "File_Knob", None)
    if File is not None:
        return File(name, label)
    return nuke.String_Knob(name, label)


def _pyscript(nuke: Any, name: str, label: str, command: str) -> Any:
    """Build a PyScript_Knob, or None on Nuke builds without it."""
    Py = getattr(nuke, "PyScript_Knob", None)
    if Py is None:
        return None
    k = Py(name, label)
    k.setValue(command)
    return k


def _knob_specs() -> List[Tuple[str, Callable[[Any], Any]]]:
    """Return [(knob_name, builder(nuke) -> knob | None), ...].

    Only artist/runtime controls. python_executable / backend_script /
    omnipaint_repo are intentionally NOT exposed — they are plugin-local and
    resolved automatically by run.py.
    """
    return [
        ("OmniPaintRemove", lambda n: n.Tab_Knob("OmniPaintRemove")),
        ("output_directory", lambda n: _file_or_string(n, "output_directory", "output directory")),
        ("omnipaint_device", lambda n: n.String_Knob("omnipaint_device", "device")),
        ("omnipaint_steps", lambda n: n.Int_Knob("omnipaint_steps", "steps")),
        ("omnipaint_seed", lambda n: n.Int_Knob("omnipaint_seed", "seed")),
        ("omnipaint_max_side", lambda n: n.Int_Knob("omnipaint_max_side", "max side")),
        ("omnipaint_mask_source", lambda n: n.Enumeration_Knob("omnipaint_mask_source", "switch input", ["0", "1"])),
        ("omnipaint_low_vram", lambda n: n.Boolean_Knob("omnipaint_low_vram", "low VRAM (legacy)")),
        ("omnipaint_memory_mode", lambda n: n.Enumeration_Knob("omnipaint_memory_mode", "memory mode",
            ["auto block swap", "manual block swap", "fast GPU", "sequential offload"])),
        ("omnipaint_swap_blocks", lambda n: n.Int_Knob("omnipaint_swap_blocks", "swap blocks")),
        ("omnipaint_attention", lambda n: n.Enumeration_Knob("omnipaint_attention", "attention", ["auto", "default", "sage"])),
        ("omnipaint_quant", lambda n: n.Enumeration_Knob("omnipaint_quant", "quantization", ["off", "nf4"])),
        ("preview_enabled", lambda n: n.Boolean_Knob("preview_enabled", "preview enabled")),
        ("crop_x0", lambda n: n.Int_Knob("crop_x0", "crop x0")),
        ("crop_y0", lambda n: n.Int_Knob("crop_y0", "crop y0")),
        ("crop_x1", lambda n: n.Int_Knob("crop_x1", "crop x1")),
        ("crop_y1", lambda n: n.Int_Knob("crop_y1", "crop y1")),
        ("target_side", lambda n: n.Int_Knob("target_side", "target side")),
        ("create_read_on_result", lambda n: n.Boolean_Knob("create_read_on_result", "create Read on result")),
        (
            "run_current_frame",
            lambda n: _pyscript(
                n, "run_current_frame", "Run current frame",
                "from omnipaint_remove import run; "
                "run.run_current_frame(nuke.thisNode())",
            ),
        ),
        (
            "check_backend",
            lambda n: _pyscript(
                n, "check_backend", "Check backend",
                "from omnipaint_remove import run; "
                "run.check_backend(nuke.thisNode())",
            ),
        ),
        (
            "clear_vram",
            lambda n: _pyscript(
                n, "clear_vram", "Clear VRAM",
                "from omnipaint_remove import run; "
                "run.clear_vram(nuke.thisNode())",
            ),
        ),
        (
            "create_preview_overlay",
            lambda n: _pyscript(
                n, "create_preview_overlay", "Preview crop overlay",
                "from omnipaint_remove import run; "
                "run.create_preview_overlay(nuke.thisNode())",
            ),
        ),
        (
            "clear_preview_overlay",
            lambda n: _pyscript(
                n, "clear_preview_overlay", "Clear preview",
                "from omnipaint_remove import run; "
                "run.clear_preview_overlay(nuke.thisNode())",
            ),
        ),
        ("status", lambda n: n.String_Knob("status", "status")),
        ("last_result", lambda n: n.String_Knob("last_result", "last result")),
    ]


def ensure_knobs(node: Any) -> set:
    """Add any knob from the spec that is missing on `node` (main thread).

    Returns the set of newly-added knob names.
    """
    if not napi.has_nuke():
        return set()
    nuke: Any = napi._nuke
    added: set = set()
    for name in _DEPRECATED_KNOBS:
        try:
            knob = node.knob(name)
            if knob is not None:
                node.removeKnob(knob)
        except Exception:
            pass
    for name, builder in _knob_specs():
        try:
            if node.knob(name) is None:
                knob = builder(nuke)
                if knob is not None:
                    node.addKnob(knob)
                    added.add(name)
        except Exception:
            # Never let one bad knob break node creation.
            pass
    # Internal preview state is not artist-facing; hide it best-effort.
    for name in ("preview_enabled", "crop_x0", "crop_y0", "crop_x1", "crop_y1", "target_side"):
        try:
            k = node.knob(name)
            if k is not None and hasattr(k, "setVisible"):
                k.setVisible(False)
        except Exception:
            pass
    return added


def initialize_defaults(node: Any, added: "set | None" = None) -> None:
    """Fill empty/new dynamic defaults. Idempotent: does not override saved values.

    String knobs: only filled when empty (works for saved scripts).
    Bool/int/enum knobs: only filled for newly-added knobs (in `added` set).
    """
    if not napi.has_nuke():
        return
    settings = load_settings()

    def _str(name: str, value: str) -> None:
        try:
            k = node.knob(name)
            if k is None:
                return
            cur = k.value()
            if cur is None or str(cur).strip() == "":
                k.setValue(value)
        except Exception:
            pass

    if added is None:
        added = set()

    def _bool(name: str, value: bool) -> None:
        try:
            k = node.knob(name)
            if k is None:
                return
            if name in added:
                k.setValue(bool(value))
        except Exception:
            pass

    def _int(name: str, value: int) -> None:
        try:
            k = node.knob(name)
            if k is None:
                return
            if name in added:
                k.setValue(int(value))
        except Exception:
            pass

    def _enum(name: str, value: str) -> None:
        """Set an Enumeration_Knob default only when the current value is not
        already a valid selection (i.e., new/corrupt knob). Does not override
        saved values.
        """
        try:
            k = node.knob(name)
            if k is None:
                return
            cur = str(k.value() or "").strip()
            try:
                valid = [str(v) for v in list(k.values())]
            except Exception:
                valid = []
            if cur and (not valid or cur in valid):
                return
            try:
                k.setValue(value)
                return
            except Exception:
                pass
            for i, v in enumerate(valid):
                if v == value:
                    try:
                        k.setValue(i)
                    except Exception:
                        pass
                    return
        except Exception:
            pass

    _str("output_directory", str(settings.get("output_directory") or ""))
    _str("omnipaint_device", "cuda:0")
    _int("omnipaint_steps", 28)
    _int("omnipaint_seed", 42)
    _int("omnipaint_max_side", 1024)
    _enum("omnipaint_mask_source", "0")
    _bool("omnipaint_low_vram", True)
    _enum("omnipaint_memory_mode", "auto block swap")
    _enum("omnipaint_attention", "auto")
    _enum("omnipaint_quant", "off")
    _bool("create_read_on_result", True)
    _bool("preview_enabled", False)
    _str("status", "ready")
