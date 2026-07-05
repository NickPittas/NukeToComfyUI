"""Bridge node settings actions used by PyScript knobs."""

from __future__ import annotations

from typing import Any

from . import napi
from .settings import save_settings


def save_defaults_from_node(bridge_node: Any) -> None:
    """Persist global server defaults from a ComfyUIBridge node."""
    settings = save_settings(
        {
            "host": napi.knob_value(bridge_node, "host") or "127.0.0.1",
            "port": int(napi.knob_value(bridge_node, "port") or 8765),
            "output_directory": napi.knob_value(bridge_node, "output_directory") or "",
        }
    )
    napi.set_knob_value(bridge_node, "host", settings["host"])
    napi.set_knob_value(bridge_node, "port", int(settings["port"]))
    napi.set_knob_value(bridge_node, "output_directory", settings["output_directory"])
    napi.set_knob_value(bridge_node, "status", "saved defaults; restart server to use host/port")
