"""Optional: submit a saved ComfyUI API workflow JSON to ComfyUI /prompt.

Phase 1 stub: only POSTs the JSON unchanged with a generated client_id; does
not patch the graph. No progress/WebSocket handling yet (see Phase 4 deferred).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
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

    url = f"http://{host}:{int(port)}/prompt"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    return {"client_id": client_id, "response": json.loads(raw) if raw else {}}


def submit_from_bridge_node(bridge_node: Any, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """Read workflow_api_path + host/port knobs from a bridge node."""
    from . import napi
    if not napi.has_nuke():
        return None
    path = napi.knob_value(bridge_node, "workflow_api_path")
    host = napi.knob_value(bridge_node, "comfyui_host")
    port = napi.knob_value(bridge_node, "comfyui_port")
    if not path:
        return None
    return submit_workflow(host or "127.0.0.1", int(port or 8188), str(path), timeout=timeout)


def run_from_node(bridge_node: Any) -> None:
    """PyScript_Knob entrypoint for the bridge node's Run workflow button.

    Submits `workflow_api_path` to ComfyUI and monitors execution progress
    via websocket with a Nuke ProgressTask + status knob updates.
    """
    from . import napi

    def _worker() -> None:
        from . import comfy_progress

        try:
            result = submit_from_bridge_node(bridge_node)
            if result is None:
                napi.set_knob_value(bridge_node, "status", "workflow_api_path is empty")
                return

            host = napi.knob_value(bridge_node, "comfyui_host") or "127.0.0.1"
            port = int(napi.knob_value(bridge_node, "comfyui_port") or 8188)
            client_id = result.get("client_id") or ""
            response = result.get("response") or {}

            prompt_id, status = comfy_progress.submit_and_monitor(
                bridge_node,
                host,
                port,
                client_id,
                prompt_payload={},
                submit_response=response,
            )
            label = prompt_id or "(no prompt_id)"
            napi.set_knob_value(bridge_node, "status", f"workflow {status}: {label}")
        except Exception as exc:
            napi.set_knob_value(bridge_node, "status", f"workflow submit failed: {exc}")

    try:
        napi.set_knob_value(bridge_node, "status", "workflow starting…")
        threading.Thread(target=_worker, name="ComfyUIBridgeRunWorkflow", daemon=True).start()
    except Exception as exc:
        napi.set_knob_value(bridge_node, "status", f"workflow submit failed: {exc}")
        raise
