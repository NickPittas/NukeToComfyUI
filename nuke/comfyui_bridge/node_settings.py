"""Bridge node settings actions used by PyScript knobs."""

from __future__ import annotations

from typing import Any

from . import napi
from .settings import load_settings, save_settings


def save_defaults_from_node(bridge_node: Any) -> None:
    """Persist global server defaults from a ComfyUIBridge node and restart.

    Saves the node's values, then restarts the bridge listener immediately so
    the new host/port are active without a Nuke restart. If the restart fails,
    the previous settings are restored on disk and the old listener is kept
    (server.start_server rolls back); node knobs keep the entered values so the
    user can correct them.
    """
    from . import server

    previous = load_settings()
    settings = save_settings(
        {
            "host": napi.knob_value(bridge_node, "host") or "127.0.0.1",
            "port": int(napi.knob_value(bridge_node, "port") or 8765),
            "output_directory": napi.knob_value(bridge_node, "output_directory") or "",
            "bridge_host": napi.knob_value(bridge_node, "bridge_host") or "",
            "comfyui_host": napi.knob_value(bridge_node, "comfyui_host") or "127.0.0.1",
            "comfyui_port": int(napi.knob_value(bridge_node, "comfyui_port") or 8188),
        }
    )
    napi.set_knob_value(bridge_node, "host", settings["host"])
    napi.set_knob_value(bridge_node, "port", int(settings["port"]))
    napi.set_knob_value(bridge_node, "output_directory", settings["output_directory"])
    napi.set_knob_value(bridge_node, "bridge_host", settings.get("bridge_host") or "")
    napi.set_knob_value(bridge_node, "comfyui_host", settings["comfyui_host"])
    napi.set_knob_value(bridge_node, "comfyui_port", int(settings["comfyui_port"]))
    try:
        srv = server.start_server()
    except Exception as exc:
        try:
            save_settings(previous)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"restart failed ({exc}); settings rollback failed ({rollback_exc})"
            ) from rollback_exc
        old = server.get_server()
        old_addr = f"{old.host}:{old.port}" if old is not None else "unavailable"
        napi.set_knob_value(
            bridge_node, "status", f"save failed; bridge kept {old_addr}: {exc}"
        )
        return
    napi.set_knob_value(
        bridge_node, "status", f"saved; Nuke bridge listening on {srv.host}:{srv.port}"
    )
