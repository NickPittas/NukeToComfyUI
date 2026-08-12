"""In-memory registry of open ComfyUI workflows, plus aiohttp routes.

The ComfyUI frontend extension (web/nuke_bridge.js) periodically publishes the
open workflow(s) here; Nuke pulls the list to populate its dropdown and asks
this backend to run a selected workflow.

Storage is in-memory only — a ComfyUI restart clears everything (acceptable for
Phase 1). Each workflow entry expires if the frontend hasn't re-published it
within `_EXPIRY_SECONDS`, so closing the tab drops it from the dropdown.

Route summary (registered against PromptServer.instance.routes):
  GET  /nuke_bridge/workflows  -> {ok, workflows:[{id,name,has_from_nuke,has_to_nuke}]}
  POST /nuke_bridge/workflows  -> frontend publishes; body {workflows:[...]}
  POST /nuke_bridge/run_workflow -> {workflow_id, client_id?}
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()
_WORKFLOWS: Dict[str, Dict[str, Any]] = {}
_EXPIRY_SECONDS = 60.0  # drop a workflow if not re-published within this window


def store_workflows(workflows: List[Dict[str, Any]]) -> None:
    """Upsert published workflow entries (touching their timestamp)."""
    now = time.time()
    with _LOCK:
        _expire_locked(now)
        for wf in workflows or []:
            wid = wf.get("id") if isinstance(wf, dict) else None
            if not wid:
                continue
            entry = dict(wf)
            entry["_ts"] = now
            _WORKFLOWS[str(wid)] = entry


def _expire_locked(now: float) -> None:
    for wid in list(_WORKFLOWS.keys()):
        if now - _WORKFLOWS[wid].get("_ts", 0) > _EXPIRY_SECONDS:
            _WORKFLOWS.pop(wid, None)


def _prompt_from_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    prompt = entry.get("prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("output"), dict):
        prompt = prompt["output"]
    elif isinstance(prompt, dict) and isinstance(prompt.get("prompt"), dict):
        prompt = prompt["prompt"]
    return prompt if isinstance(prompt, dict) else {}


def _linked_node_ids(node: Dict[str, Any]) -> List[str]:
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    linked: List[str] = []
    for value in inputs.values():
        if isinstance(value, (list, tuple)) and len(value) == 2:
            source_id = value[0]
            if isinstance(source_id, (str, int)):
                linked.append(str(source_id))
    return linked


def _upstream_source_ids(
    prompt: Dict[str, Any], node_id: str, source_class: str
) -> List[str]:
    found: List[str] = []
    seen = {str(node_id)}
    stack = _linked_node_ids(prompt.get(str(node_id), {}))
    while stack:
        current_id = stack.pop()
        if current_id in seen:
            continue
        seen.add(current_id)
        current = prompt.get(current_id)
        if not isinstance(current, dict):
            continue
        if current.get("class_type") == source_class:
            found.append(current_id)
            continue
        stack.extend(_linked_node_ids(current))
    return sorted(set(found))


def _bridge_nodes(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    prompt = _prompt_from_entry(entry)
    nodes: List[Dict[str, Any]] = []
    output_sources = {"ToNuke": "FromNuke", "ToNukeVideo": "FromNukeVideo"}
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if class_type not in ("FromNuke", "FromNukeVideo", "ToNuke", "ToNukeVideo"):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        raw_bridge_id = inputs.get("bridge_id", "")
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        info: Dict[str, Any] = {
            "node_id": str(node_id),
            "title": str(meta.get("title") or f"{class_type} #{node_id}"),
            "class_type": class_type,
            "bridge_id": raw_bridge_id if isinstance(raw_bridge_id, str) else "",
        }
        if class_type in output_sources:
            info["source_node_ids"] = _upstream_source_ids(
                prompt, str(node_id), output_sources[class_type]
            )
        nodes.append(info)
    return nodes


def list_workflows() -> List[Dict[str, Any]]:
    """Public listing: routing metadata only, no prompt payload."""
    now = time.time()
    with _LOCK:
        _expire_locked(now)
        workflows: List[Dict[str, Any]] = []
        for value in _WORKFLOWS.values():
            bridge_nodes = _bridge_nodes(value)
            workflows.append({
                "id": value.get("id"),
                "name": value.get("name") or value.get("id"),
                "has_from_nuke": bool(value.get("has_from_nuke")),
                "has_to_nuke": bool(value.get("has_to_nuke")),
                "from_nuke_nodes": [
                    node for node in bridge_nodes
                    if node["class_type"] in ("FromNuke", "FromNukeVideo")
                ],
                "to_nuke_nodes": [
                    node for node in bridge_nodes
                    if node["class_type"] in ("ToNuke", "ToNukeVideo")
                ],
            })
        return workflows


def get_workflow(workflow_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _WORKFLOWS.get(str(workflow_id))


def try_submit_workflow(
    workflow_id: str, client_id: Optional[str] = None
) -> Dict[str, Any]:
    """Resolve `workflow_id` and either submit it or hand the prompt back.

    ponytail: backend submission via PromptServer.prompt_queue is
    version-dependent and untestable here, so we always return the stored API
    prompt and let Nuke POST /prompt itself. That path is reliable and uses no
    undocumented APIs. See PROTOCOL.md / TASKS Phase 4 for the upgrade.
    """
    wf = get_workflow(workflow_id)
    if not wf:
        return {"ok": False, "error": "workflow not found"}
    prompt = wf.get("prompt")
    if isinstance(prompt, dict):
        if isinstance(prompt.get("output"), dict):
            prompt = prompt["output"]
        elif isinstance(prompt.get("prompt"), dict):
            prompt = prompt["prompt"]
    if not prompt:
        return {"ok": False, "error": "workflow has no stored prompt"}
    cid = client_id or uuid.uuid4().hex
    return {"ok": True, "submitted": False, "client_id": cid, "prompt": prompt}


# --- Route registration ----------------------------------------------------

def add_routes(server: Any) -> bool:
    """Register aiohttp routes on a PromptServer instance. Returns True on success."""
    try:
        from aiohttp import web  # local import; only available inside ComfyUI
    except Exception:
        return False

    routes = getattr(server, "routes", None)
    if routes is None:
        return False

    async def _get_workflows(_request: Any) -> Any:
        return web.json_response({"ok": True, "workflows": list_workflows()})

    async def _post_workflows(request: Any) -> Any:
        try:
            data = await request.json()
        except Exception:
            data = {}
        workflows = (data or {}).get("workflows") or []
        store_workflows(workflows)
        return web.json_response({"ok": True, "count": len(list_workflows())})

    async def _run_workflow(request: Any) -> Any:
        try:
            data = await request.json()
        except Exception:
            data = {}
        result = try_submit_workflow(
            (data or {}).get("workflow_id") or "",
            (data or {}).get("client_id"),
        )
        status = 200 if result.get("ok") else 404
        return web.json_response(result, status=status)

    spec = (
        ("GET", "/nuke_bridge/workflows", _get_workflows),
        ("POST", "/nuke_bridge/workflows", _post_workflows),
        ("POST", "/nuke_bridge/run_workflow", _run_workflow),
    )
    for method, path, handler in spec:
        try:
            getattr(routes, "add_" + method.lower())(path, handler)
        except Exception:
            try:
                # Decorator-style fallback: routes.get(path)(handler)
                getattr(routes, method.lower())(path)(handler)
            except Exception:
                pass
    return True
