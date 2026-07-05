"""Nuke-side dropdown: list open ComfyUI workflows and trigger the selected one.

Reads the bridge node's `comfyui_host`/`comfyui_port` knobs, fetches
`/nuke_bridge/workflows`, fills the `workflow_choices` Enumeration_Knob, and on
request asks the backend to run the selected workflow. If the backend can't
submit itself, it returns the API prompt and this module POSTs `/prompt`.

The mapping from Enumeration_Knob label back to workflow_id is kept in an
in-process dict keyed by `bridge_id` (refreshed on every `refresh_workflow_choices`).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

# bridge_id -> last fetched workflow list (metadata only).
_LAST_LIST: Dict[str, List[Dict[str, Any]]] = {}


def _base_url(host: str, port: int) -> str:
    return f"http://{(host or '127.0.0.1').strip()}:{int(port or 8188)}"


def list_workflows(host: str, port: int, timeout: float = 5.0) -> List[Dict[str, Any]]:
    """GET /nuke_bridge/workflows and return the workflow list."""
    import requests  # local import; only needed on this path

    resp = requests.get(f"{_base_url(host, port)}/nuke_bridge/workflows", timeout=timeout)
    resp.raise_for_status()
    data = resp.json() or {}
    return list(data.get("workflows") or [])


def _enum_labels(workflows: List[Dict[str, Any]]) -> List[str]:
    labels: List[str] = []
    for wf in workflows:
        name = str(wf.get("name") or wf.get("id") or "workflow")
        tags = []
        if wf.get("has_from_nuke"):
            tags.append("F")
        if wf.get("has_to_nuke"):
            tags.append("T")
        tag = (" [" + "".join(tags) + "]") if tags else ""
        label = f"{name}{tag}"
        if label in labels:  # ponytail: dedupe by appending short id
            label = f"{label} ({str(wf.get('id', ''))[:6]})"
        labels.append(label)
    return labels


def refresh_workflow_choices(bridge_node: Any) -> int:
    """PyScript entrypoint: pull workflows and repopulate the choices knob."""
    from . import napi

    host = napi.knob_value(bridge_node, "comfyui_host") or "127.0.0.1"
    port = int(napi.knob_value(bridge_node, "comfyui_port") or 8188)
    bridge_id = str(napi.knob_value(bridge_node, "bridge_id") or "")

    try:
        workflows = list_workflows(host, port)
    except Exception as exc:
        napi.set_knob_value(bridge_node, "status", f"refresh failed: {exc}")
        _LAST_LIST[bridge_id] = []
        _set_choices(bridge_node, ["(refresh failed)"])
        return 0

    workflows = [w for w in workflows if w.get("has_from_nuke") or w.get("has_to_nuke")]
    _LAST_LIST[bridge_id] = workflows

    labels = _enum_labels(workflows) if workflows else ["(none)"]
    _set_choices(bridge_node, labels)

    napi.set_knob_value(
        bridge_node, "status", f"{len(workflows)} workflow(s)" if workflows else "no workflows"
    )
    return len(workflows)


def _set_choices(bridge_node: Any, labels: List[str]) -> None:
    """Update the Enumeration_Knob on the main thread."""
    from . import napi

    def _update() -> None:
        k = bridge_node.knob("workflow_choices")
        if k is None:
            return
        # ponytail: Enumeration_Knob.setValues replaces the menu items on every
        # Nuke version we care about; if it's missing, fall back to recreation
        # is not worth the churn, so we just bail.
        try:
            k.setValues(labels)
        except Exception:
            return
        try:
            k.setValue(0)
        except Exception:
            pass

    try:
        napi.call(_update)
    except Exception:
        pass


def _selected_workflow_id(bridge_node: Any) -> Optional[str]:
    from . import napi

    bridge_id = str(napi.knob_value(bridge_node, "bridge_id") or "")
    workflows = _LAST_LIST.get(bridge_id) or []
    if not workflows:
        return None
    try:
        idx = int(napi.knob_value(bridge_node, "workflow_choices") or 0)
    except Exception:
        idx = 0
    if idx < 0 or idx >= len(workflows):
        return None
    wid = workflows[idx].get("id")
    return str(wid) if wid else None


def run_selected_workflow(bridge_node: Any, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """PyScript entrypoint: run the workflow chosen in `workflow_choices`."""
    from . import napi
    import requests  # local import

    wid = _selected_workflow_id(bridge_node)
    if not wid:
        napi.set_knob_value(bridge_node, "status", "no workflow selected")
        return None

    host = napi.knob_value(bridge_node, "comfyui_host") or "127.0.0.1"
    port = int(napi.knob_value(bridge_node, "comfyui_port") or 8188)
    client_id = uuid.uuid4().hex

    try:
        resp = requests.post(
            f"{_base_url(host, port)}/nuke_bridge/run_workflow",
            json={"workflow_id": wid, "client_id": client_id},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as exc:
        napi.set_knob_value(bridge_node, "status", f"run failed: {exc}")
        return None

    # Backend submitted it itself; nothing else to do.
    if data.get("submitted"):
        napi.set_knob_value(bridge_node, "status", f"submitted: {wid}")
        return data

    # Backend handed us the prompt; POST /prompt ourselves.
    prompt = data.get("prompt")
    if not prompt:
        napi.set_knob_value(bridge_node, "status", f"run: no prompt for {wid}")
        return None

    try:
        presp = requests.post(
            f"{_base_url(host, port)}/prompt",
            json={"prompt": prompt, "client_id": data.get("client_id") or client_id},
            timeout=timeout,
        )
        presp.raise_for_status()
    except Exception as exc:
        napi.set_knob_value(bridge_node, "status", f"prompt post failed: {exc}")
        return None

    napi.set_knob_value(bridge_node, "status", f"submitted: {wid}")
    return {"client_id": client_id, "response": presp.json()}
