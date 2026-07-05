"""Optional: submit a saved ComfyUI API workflow JSON to ComfyUI /prompt.

Phase 1 stub: only POSTs the JSON unchanged with a generated client_id; does
not patch the graph. No progress/WebSocket handling yet (see Phase 4 deferred).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional


def submit_workflow(
    host: str,
    port: int,
    workflow_api_path: str,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Read `workflow_api_path` JSON and POST to ComfyUI /prompt.

    Returns the parsed JSON response. Raises on network / parse / IO error.
    """
    if not workflow_api_path:
        raise ValueError("workflow_api_path is empty")

    expanded = os.path.expanduser(workflow_api_path)
    with open(expanded, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    client_id = uuid.uuid4().hex
    payload = {"prompt": data, "client_id": client_id}

    import requests  # local import; only needed on this path

    url = f"http://{host}:{int(port)}/prompt"
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return {"client_id": client_id, "response": resp.json()}


def submit_from_bridge_node(bridge_node: Any, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """Read workflow_api_path + host/port knobs from a bridge node."""
    from . import napi
    if not napi.has_nuke():
        return None
    path = napi.knob_value(bridge_node, "workflow_api_path")
    host = napi.knob_value(bridge_node, "host")
    port = napi.knob_value(bridge_node, "port")
    if not path:
        return None
    return submit_workflow(host or "127.0.0.1", int(port or 8188), str(path), timeout=timeout)
