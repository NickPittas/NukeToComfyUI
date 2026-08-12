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

# Nuke bridge_id -> last fetched workflow list (metadata only).
_LAST_LIST: Dict[str, List[Dict[str, Any]]] = {}
_SOURCE_CLASS = {"image": "FromNuke", "video": "FromNukeVideo"}
_TARGET_KNOB = {"image": "image_workflow_input", "video": "video_workflow_input"}


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
    refresh_workflow_input_choices(bridge_node)

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


def _enum_index(bridge_node: Any, knob_name: str) -> int:
    from . import napi

    try:
        return int(napi.call(lambda: bridge_node.knob(knob_name).getValue()))
    except Exception:
        return int(napi.knob_value(bridge_node, knob_name))


def _selected_workflow(bridge_node: Any, media_mode: str = "image") -> Optional[Dict[str, Any]]:
    from . import napi

    mode = _media_mode(media_mode)
    knob_name = "workflow_choices" if mode == "image" else "video_workflow_choices"
    bridge_id = str(napi.knob_value(bridge_node, "bridge_id") or "")
    workflows = _LAST_LIST.get(bridge_id) or []
    if not workflows:
        return None
    try:
        idx = _enum_index(bridge_node, knob_name)
    except Exception:
        idx = 0
    return workflows[idx] if 0 <= idx < len(workflows) else None


def _selected_workflow_id(bridge_node: Any, media_mode: str = "image") -> Optional[str]:
    workflow = _selected_workflow(bridge_node, media_mode)
    wid = workflow.get("id") if workflow else None
    return str(wid) if wid else None


def _workflow_sources(workflow: Optional[Dict[str, Any]], media_mode: str) -> List[Dict[str, Any]]:
    mode = _media_mode(media_mode)
    if not workflow:
        return []
    return [
        source for source in workflow.get("from_nuke_nodes") or []
        if isinstance(source, dict) and source.get("class_type") == _SOURCE_CLASS[mode]
    ]


def _source_label(source: Dict[str, Any]) -> str:
    title = str(source.get("title") or source.get("class_type") or "FromNuke")
    bridge_id = str(source.get("bridge_id") or "(missing automatic ID)")
    return f"{title} [{bridge_id}] #{source.get('node_id', '?')}"


def refresh_workflow_input_choices(
    bridge_node: Any, media_mode: Optional[str] = None, reset: bool = False
) -> None:
    from . import napi

    modes = (_media_mode(media_mode),) if media_mode else ("image", "video")
    for mode in modes:
        knob_name = _TARGET_KNOB[mode]
        sources = _workflow_sources(_selected_workflow(bridge_node, mode), mode)
        _set_choices(bridge_node, ["(none)"] + [_source_label(source) for source in sources], knob_name)
        if reset:
            try:
                napi.call(lambda: bridge_node.knob(knob_name).setValue(0))
            except Exception:
                pass


def _selected_workflow_input(bridge_node: Any, media_mode: str) -> Optional[Dict[str, Any]]:
    from . import napi

    mode = _media_mode(media_mode)
    sources = _workflow_sources(_selected_workflow(bridge_node, mode), mode)
    if not sources:
        return None
    try:
        idx = _enum_index(bridge_node, _TARGET_KNOB[mode]) - 1
    except Exception:
        idx = -1
    return sources[idx] if 0 <= idx < len(sources) else None


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
        mapping = _collect_source_mapping(prompt, wid)
        prompt = _patch_nuke_bridge_prompt(prompt, bridge_node, mapping)
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


def _prompt_sources(prompt: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(prompt, dict):
        return {}
    return {
        str(node_id): node for node_id, node in prompt.items()
        if isinstance(node, dict)
        and node.get("class_type") in ("FromNuke", "FromNukeVideo")
    }


def _linked_node_ids(node: Dict[str, Any]) -> List[str]:
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    linked: List[str] = []
    for value in inputs.values():
        if isinstance(value, (list, tuple)) and len(value) == 2:
            source_id = value[0]
            if isinstance(source_id, (str, int)):
                linked.append(str(source_id))
    return linked


def _upstream_source_ids(prompt: Dict[str, Any], node_id: str, source_class: str) -> List[str]:
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
        else:
            stack.extend(_linked_node_ids(current))
    return sorted(set(found))


def _collect_source_mapping(prompt: Any, workflow_id: str) -> Dict[str, Any]:
    from . import napi

    required = _prompt_sources(prompt)
    mapping: Dict[str, Any] = {}
    route_ids: Dict[str, str] = {}
    for candidate in napi.all_nodes():
        for mode in ("image", "video"):
            try:
                if _selected_workflow_id(candidate, mode) != workflow_id:
                    continue
                source = _selected_workflow_input(candidate, mode)
            except Exception:
                continue
            if not source:
                continue
            node_id = str(source.get("node_id") or "")
            if node_id not in required:
                raise ValueError(f"workflow input #{node_id or '?'} is not in the queued prompt")
            inputs = required[node_id].get("inputs")
            prompt_id = str(inputs.get("bridge_id") or "") if isinstance(inputs, dict) else ""
            published_id = str(source.get("bridge_id") or "")
            if not prompt_id or prompt_id != published_id:
                raise ValueError(
                    f"workflow input #{node_id} automatic Bridge ID is missing or stale; reload ComfyUI"
                )
            if prompt_id in route_ids and route_ids[prompt_id] != node_id:
                raise ValueError(f"duplicate ComfyUI Bridge ID {prompt_id!r}")
            if node_id in mapping:
                raise ValueError(f"workflow input #{node_id} is mapped by more than one Nuke bridge")
            route_ids[prompt_id] = node_id
            mapping[node_id] = candidate
    missing = [node_id for node_id in required if node_id not in mapping]
    if missing:
        raise ValueError(f"workflow inputs missing Nuke bridge mapping: {', '.join(missing)}")
    return mapping


def _patch_bridge_inputs(
    inputs: Dict[str, Any], class_type: str, bridge_node: Any, route_id: str
) -> None:
    from . import napi

    inputs["bridge_id"] = route_id
    inputs["host"] = _advertised_host(bridge_node)
    inputs["port"] = int(napi.knob_value(bridge_node, "port") or 8765)
    if class_type in ("FromNuke", "ToNuke"):
        fmt = str(napi.knob_value(bridge_node, "send_format") or "png8").strip().lower()
        inputs["format"] = fmt if fmt in ("png8", "exr16") else "png8"
        colorspace = str(napi.knob_value(bridge_node, "send_colorspace") or "")
        inputs["colorspace"] = colorspace if class_type == "FromNuke" else ""
        return
    fmt = str(napi.knob_value(bridge_node, "video_format") or "mov").strip().lower()
    codec = str(napi.knob_value(bridge_node, "video_mov_codec") or "prores_422hq").strip().lower()
    inputs["format"] = fmt if fmt in ("mov", "mp4") else "mov"
    inputs["mov_codec"] = codec if codec in ("prores_422hq", "prores_4444") else "prores_422hq"
    colorspace = str(napi.knob_value(bridge_node, "video_colorspace") or "")
    inputs["colorspace"] = colorspace if class_type == "FromNukeVideo" else ""


def _output_route(
    prompt: Dict[str, Any], node_id: str, class_type: str, mapping: Dict[str, Any]
) -> tuple[Any, str]:
    source_class = "FromNuke" if class_type == "ToNuke" else "FromNukeVideo"
    source_ids = _upstream_source_ids(prompt, node_id, source_class)
    if not source_ids:
        raise ValueError(f"{class_type} #{node_id} has no upstream {source_class}")
    missing = [source_id for source_id in source_ids if source_id not in mapping]
    if missing:
        raise ValueError(f"{class_type} #{node_id} has unmapped sources: {', '.join(missing)}")
    targets = {id(mapping[source_id]): mapping[source_id] for source_id in source_ids}
    route_ids = {
        str(prompt[source_id].get("inputs", {}).get("bridge_id") or "")
        for source_id in source_ids
    }
    route_ids.discard("")
    if len(targets) != 1 or len(route_ids) != 1:
        raise ValueError(f"{class_type} #{node_id} routes to multiple Nuke bridges")
    return next(iter(targets.values())), next(iter(route_ids))


def bridge_node_for_external_id(bridge_id: str) -> Any:
    """Resolve a Comfy node's persistent ID through current Nuke dropdown mappings."""
    from . import napi

    matches: Dict[int, Any] = {}
    for candidate in napi.all_nodes():
        for mode in ("image", "video"):
            try:
                workflow = _selected_workflow(candidate, mode)
                source = _selected_workflow_input(candidate, mode)
            except Exception:
                continue
            if not workflow or not source:
                continue
            aliases = {str(source.get("bridge_id") or "")}
            source_node_id = str(source.get("node_id") or "")
            for output in workflow.get("to_nuke_nodes") or []:
                if source_node_id in [str(value) for value in output.get("source_node_ids") or []]:
                    aliases.add(str(output.get("bridge_id") or ""))
            if bridge_id in aliases:
                matches[id(candidate)] = candidate
    if len(matches) > 1:
        raise ValueError(f"ComfyUI Bridge ID {bridge_id!r} maps to more than one Nuke bridge")
    return next(iter(matches.values())) if matches else None


def _patch_nuke_bridge_prompt(
    prompt: Any, bridge_node: Any, mapping: Optional[Dict[str, Any]] = None
) -> Any:
    """Patch mapped source/output routes and Nuke-side transport settings."""
    patched = copy.deepcopy(prompt)
    for node_id, node in (patched or {}).items() if isinstance(patched, dict) else []:
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if class_type not in ("FromNuke", "ToNuke", "FromNukeVideo", "ToNukeVideo"):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        target = bridge_node
        route_id = str(inputs.get("bridge_id") or "")
        if mapping is not None and class_type in ("FromNuke", "FromNukeVideo"):
            target = mapping.get(str(node_id))
            if target is None:
                raise ValueError(f"workflow input #{node_id} has no Nuke bridge mapping")
        elif mapping is not None:
            target, route_id = _output_route(patched, str(node_id), class_type, mapping)
        if not route_id:
            raise ValueError(f"{class_type} #{node_id} has no automatic Bridge ID")
        _patch_bridge_inputs(inputs, class_type, target, route_id)
    return patched
