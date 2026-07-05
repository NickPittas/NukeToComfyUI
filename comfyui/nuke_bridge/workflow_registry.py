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


def list_workflows() -> List[Dict[str, Any]]:
    """Public listing: metadata only, no prompt payload."""
    now = time.time()
    with _LOCK:
        _expire_locked(now)
        return [
            {
                "id": v.get("id"),
                "name": v.get("name") or v.get("id"),
                "has_from_nuke": bool(v.get("has_from_nuke")),
                "has_to_nuke": bool(v.get("has_to_nuke")),
            }
            for v in _WORKFLOWS.values()
        ]


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
