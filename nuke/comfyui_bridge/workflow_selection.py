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
import copy
import json
import os
import threading
import urllib.error
import urllib.request
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
    wid = _selected_workflow_id(bridge_node)
    if not wid:
        napi.set_knob_value(bridge_node, "status", "no workflow selected")
        return None

    host = napi.knob_value(bridge_node, "comfyui_host") or "127.0.0.1"
    port = int(napi.knob_value(bridge_node, "comfyui_port") or 8188)
    client_id = uuid.uuid4().hex

    video_bundle = None
    if mode == "video":
        try:
            video_bundle = _render_video_bundle_before_comfy(bridge_node)
        except Exception as exc:
            napi.set_knob_value(bridge_node, "status", f"video render failed: {exc}")
            return None

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
    prompt = _patch_nuke_bridge_prompt(prompt, bridge_node, video_bundle)

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


def _render_video_bundle_before_comfy(bridge_node: Any) -> Dict[str, Any]:
    from . import napi, video
    from .settings import DEFAULT_SETTINGS, load_settings

    first = int(napi.knob_value(bridge_node, "video_first") or napi.root_frame())
    last = int(napi.knob_value(bridge_node, "video_last") or first)
    if last < first:
        raise ValueError(f"invalid video frame range: {first}-{last}")
    fps = float(napi.knob_value(bridge_node, "video_fps") or 24.0)
    fmt = str(napi.knob_value(bridge_node, "video_format") or "mov").strip().lower()
    codec = str(napi.knob_value(bridge_node, "video_mov_codec") or "prores_422hq").strip().lower()
    colorspace = str(napi.knob_value(bridge_node, "video_colorspace") or "")
    output_dir = str(napi.knob_value(bridge_node, "output_directory") or load_settings().get("output_directory") or DEFAULT_SETTINGS["output_directory"])

    napi.set_knob_value(bridge_node, "status", f"rendering video {first}-{last}…")
    bundle = video.export_video_bundle(bridge_node, output_dir, first, last, fps, fmt, codec, colorspace)
    for key in ("main_path", "mask_path"):
        path = str(bundle.get(key) or "")
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise RuntimeError(f"missing rendered {key}: {path}")
    napi.set_knob_value(bridge_node, "status", "video render complete")
    return bundle


def _patch_nuke_bridge_prompt(prompt: Any, bridge_node: Any, video_bundle: Optional[Dict[str, Any]] = None) -> Any:
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

    video_meta_json = json.dumps({**(video_bundle.get("metadata") or {}), "main_path": video_bundle["main_path"]}) if video_bundle else "{}"

    patched = copy.deepcopy(prompt)
    for node in (patched or {}).values() if isinstance(patched, dict) else []:
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type not in ("FromNuke", "ToNuke", "FromNukeVideo", "ToNukeVideo"):
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            if class_type in ("FromNuke", "ToNuke"):
                inputs["format"] = fmt
                inputs["colorspace"] = colorspace if class_type == "FromNuke" else ""
            else:
                inputs["format"] = video_format
                inputs["mov_codec"] = video_codec
                inputs["colorspace"] = video_colorspace if class_type == "FromNukeVideo" else ""
                if class_type == "FromNukeVideo" and video_bundle:
                    inputs["main_path"] = video_bundle["main_path"]
                    inputs["mask_path"] = video_bundle["mask_path"]
                    inputs["metadata_json"] = video_meta_json
                elif class_type == "ToNukeVideo" and video_bundle:
                    inputs["video_meta_json"] = video_meta_json
    return patched
