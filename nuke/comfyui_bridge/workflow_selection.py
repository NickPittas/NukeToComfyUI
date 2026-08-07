"""Nuke-side dropdowns: list open ComfyUI workflows and trigger the selected one.

Reads the bridge node's `comfyui_host`/`comfyui_port` knobs, fetches
`/nuke_bridge/workflows`, fills the `workflow_choices` (image) and
`video_workflow_choices` (video) Enumeration_Knobs, and on request asks the
backend to run the selected workflow. If the backend can't submit itself, it
returns the API prompt and this module POSTs `/prompt`.

The mapping from Enumeration_Knob label back to workflow_id is kept in an
in-process dict keyed by `bridge_id` (refreshed on every `refresh_workflow_choices`).
Both selectors share that list; each keeps its own selected index.
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

# bridge_id -> last fetched workflow list (metadata only).
_LAST_LIST: Dict[str, List[Dict[str, Any]]] = {}


def _base_url(host: str, port: int) -> str:
    return f"http://{(host or '127.0.0.1').strip()}:{int(port or 8188)}"


def _request_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:300]}") from exc
    return json.loads(raw) if raw else {}


def list_workflows(host: str, port: int, timeout: float = 5.0) -> List[Dict[str, Any]]:
    """GET /nuke_bridge/workflows and return the workflow list."""
    data = _request_json("GET", f"{_base_url(host, port)}/nuke_bridge/workflows", timeout=timeout)
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
    _set_choices(bridge_node, labels, "workflow_choices")
    _set_choices(bridge_node, labels, "video_workflow_choices")

    # Keep the existing colorspace menus current, but never let that optional
    # Nuke-side refresh block the workflow list that this action promises.
    try:
        from . import node as node_module

        node_module.refresh_colorspace_choices(bridge_node)
        node_module.refresh_video_colorspace_choices(bridge_node)
    except Exception:
        pass

    napi.set_knob_value(
        bridge_node, "status", f"{len(workflows)} workflow(s)" if workflows else "no workflows"
    )
    return len(workflows)


def _set_choices(bridge_node: Any, labels: List[str], knob_name: str = "workflow_choices") -> None:
    """Update one Enumeration_Knob on the main thread."""
    from . import napi

    def _update() -> None:
        k = bridge_node.knob(knob_name)
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
            if 0 <= k.value() < len(labels):
                return  # keep the current selection when the list still fits
            k.setValue(0)
        except Exception:
            pass

    try:
        napi.call(_update)
    except Exception:
        pass


def _selected_workflow_id(bridge_node: Any, media_mode: str = "image") -> Optional[str]:
    from . import napi

    mode = _media_mode(media_mode)
    knob_name = "workflow_choices" if mode == "image" else "video_workflow_choices"
    bridge_id = str(napi.knob_value(bridge_node, "bridge_id") or "")
    workflows = _LAST_LIST.get(bridge_id) or []
    if not workflows:
        return None
    try:
        idx = int(napi.knob_value(bridge_node, knob_name) or 0)
    except Exception:
        idx = 0
    if idx < 0 or idx >= len(workflows):
        return None
    wid = workflows[idx].get("id")
    return str(wid) if wid else None


def _media_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode not in ("image", "video"):
        raise ValueError(f"invalid workflow media mode: {value!r}")
    return mode


def run_selected_workflow(
    bridge_node: Any, timeout: float = 30.0, media_mode: str = "image"
) -> Optional[Dict[str, Any]]:
    """Run the selected image or video workflow."""
    from . import napi

    mode = _media_mode(media_mode)

    def _worker() -> None:
        _run_selected_workflow_sync(bridge_node, timeout=timeout, media_mode=mode)

    napi.set_knob_value(bridge_node, "status", f"{mode} workflow starting…")
    threading.Thread(target=_worker, name="ComfyUIBridgeRunSelected", daemon=True).start()
    return {"status": "started", "media_mode": mode}


def _run_selected_workflow_sync(
    bridge_node: Any, timeout: float = 30.0, media_mode: str = "image"
) -> Optional[Dict[str, Any]]:
    """Worker-thread implementation for run_selected_workflow."""
    from . import napi

    mode = _media_mode(media_mode)
    wid = _selected_workflow_id(bridge_node, mode)
    if not wid:
        napi.set_knob_value(bridge_node, "status", "no workflow selected")
        return None

    host = napi.knob_value(bridge_node, "comfyui_host") or "127.0.0.1"
    port = int(napi.knob_value(bridge_node, "comfyui_port") or 8188)
    client_id = uuid.uuid4().hex

    try:
        data = _request_json(
            "POST",
            f"{_base_url(host, port)}/nuke_bridge/run_workflow",
            {"workflow_id": wid, "client_id": client_id},
            timeout=timeout,
        )
    except Exception as exc:
        napi.set_knob_value(bridge_node, "status", f"run failed: {exc}")
        return None

    # Backend submitted it itself; nothing else to do.
    if data.get("submitted"):
        napi.set_knob_value(bridge_node, "status", f"submitted: {wid}")
        return data

    # Backend handed us the prompt; POST /prompt ourselves and monitor progress.
    prompt = data.get("prompt")
    if not prompt:
        napi.set_knob_value(bridge_node, "status", f"run: no prompt for {wid}")
        return None
    try:
        prompt = _patch_nuke_bridge_prompt(prompt, bridge_node)
    except Exception as exc:
        napi.set_knob_value(bridge_node, "status", f"prompt patch failed: {exc}")
        return None

    from . import comfy_progress
    cid = data.get("client_id") or client_id
    prompt_id, status = comfy_progress.submit_and_monitor(
        bridge_node,
        host,
        port,
        cid,
        prompt_payload=prompt,
        submit_response=None,  # submit_and_monitor will POST /prompt
    )
    label = prompt_id or "(no prompt_id)"
    napi.set_knob_value(bridge_node, "status", f"workflow {status}: {label}")
    return {"client_id": cid, "prompt_id": prompt_id, "status": status}


_WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]")


def _advertised_host(bridge_node: Any) -> str:
    """Address ComfyUI must reach: `bridge_host` knob, falling back to `host`."""
    from . import napi

    host = str(
        napi.knob_value(bridge_node, "bridge_host")
        or napi.knob_value(bridge_node, "host")
        or "127.0.0.1"
    ).strip()
    if host in _WILDCARD_HOSTS:
        raise ValueError(
            f"bridge advertises wildcard address {host!r}; set bridge_host to "
            "the LAN/Tailscale address reachable from ComfyUI"
        )
    return host


def _patch_nuke_bridge_prompt(prompt: Any, bridge_node: Any) -> Any:
    """Apply Nuke-side bridge settings to NukeBridge nodes before submit."""
    from . import napi

    fmt = str(napi.knob_value(bridge_node, "send_format") or "png8").strip().lower()
    if fmt not in ("png8", "exr16"):
        fmt = "png8"
    colorspace = str(napi.knob_value(bridge_node, "send_colorspace") or "")
    video_format = str(napi.knob_value(bridge_node, "video_format") or "mov").strip().lower()
    if video_format not in ("mov", "mp4"):
        video_format = "mov"
    video_codec = str(napi.knob_value(bridge_node, "video_mov_codec") or "prores_422hq").strip().lower()
    if video_codec not in ("prores_422hq", "prores_4444"):
        video_codec = "prores_422hq"
    video_colorspace = str(napi.knob_value(bridge_node, "video_colorspace") or "")

    bridge_id = str(napi.knob_value(bridge_node, "bridge_id") or "")
    host = _advertised_host(bridge_node)
    port = int(napi.knob_value(bridge_node, "port") or 8765)

    patched = copy.deepcopy(prompt)
    for node in (patched or {}).values() if isinstance(patched, dict) else []:
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type not in ("FromNuke", "ToNuke", "FromNukeVideo", "ToNukeVideo"):
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            inputs["bridge_id"] = bridge_id
            inputs["host"] = host
            inputs["port"] = port
            if class_type in ("FromNuke", "ToNuke"):
                inputs["format"] = fmt
                inputs["colorspace"] = colorspace if class_type == "FromNuke" else ""
            else:
                inputs["format"] = video_format
                inputs["mov_codec"] = video_codec
                inputs["colorspace"] = video_colorspace if class_type == "FromNukeVideo" else ""
    return patched
