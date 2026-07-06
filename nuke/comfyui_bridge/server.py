"""Local HTTP bridge server: /health, /frame, /result.

Single-threaded (ThreadingHTTPServer + a global render/result lock) bound to
the saved host/port. All Nuke API access goes through `napi` which dispatches
to the main thread when needed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import napi, node as bridge_node_module, render, result, video
from .settings import DEFAULT_SETTINGS, load_settings, save_settings

# One global lock so concurrent HTTP requests cannot overlap Nuke renders.
_NUKE_LOCK = threading.Lock()

_SERVER: Optional["BridgeServer"] = None
_SERVER_LOCK = threading.Lock()
_VIDEO_ASSETS: Dict[str, Dict[str, Any]] = {}
_VIDEO_ASSET_TTL = 3600.0


class BridgeServer:
    """Holds server state: bound address, settings."""

    def __init__(self, host: str, port: int, settings: Dict[str, Any]) -> None:
        self.host = host
        self.port = port
        self.settings = settings
        self._http: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._http is not None:
            return
        http = ThreadingHTTPServer((self.host, self.port), _BridgeHandler)
        http.daemon_threads = True
        self._http = http
        self._thread = threading.Thread(
            target=http.serve_forever, name="ComfyUIBridgeHTTP", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def get_server() -> Optional[BridgeServer]:
    return _SERVER


def start_server() -> BridgeServer:
    """Start (or restart) the global bridge server from saved settings."""
    global _SERVER
    with _SERVER_LOCK:
        settings = load_settings()
        host = str(settings.get("host") or DEFAULT_SETTINGS["host"])
        port = int(settings.get("port") or DEFAULT_SETTINGS["port"])
        if _SERVER is not None:
            _SERVER.stop()
        srv = BridgeServer(host, port, settings)
        srv.start()
        _SERVER = srv
        return srv


def ensure_server() -> BridgeServer:
    """Return the running server, starting it only if needed."""
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is not None:
            return _SERVER
    return start_server()


# --- Request handler -------------------------------------------------------

def _json_response(handler: BaseHTTPRequestHandler, status: int, obj: Dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _binary_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    headers: Dict[str, str],
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", headers.get("Content-Type", "application/octet-stream"))
    handler.send_header("Content-Length", str(len(body)))
    for k, v in headers.items():
        if k.lower() in ("content-type", "content-length"):
            continue
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _cleanup_video_assets() -> None:
    now = time.time()
    for asset_id, asset in list(_VIDEO_ASSETS.items()):
        if now - float(asset.get("created_at") or 0) <= _VIDEO_ASSET_TTL:
            continue
        _VIDEO_ASSETS.pop(asset_id, None)
        for key in ("main_path", "mask_path"):
            try:
                os.remove(str(asset.get(key) or ""))
            except OSError:
                pass


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ComfyUIBridge/0.1"
    # ponytail: silence default stderr logging; HTTPServer logs enough.
    def log_message(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return

    # -- helpers --

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # -- routes --

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            _json_response(self, 200, {"ok": True, "app": "nuke-comfyui-bridge"})
            return
        if path == "/bridges":
            bridges: list[str] = []
            try:
                for n in napi.all_nodes():
                    k = n.knob("bridge_id")
                    if k is not None:
                        bridges.append(str(k.value()))
            except Exception:
                pass
            _json_response(self, 200, {"ok": True, "bridges": bridges})
            return
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "asset":
            self._handle_asset(parts[1], parts[2])
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        parts = path.strip("/").split("/")
        # /bridge/{bridge_id}/{frame|result}
        if len(parts) == 3 and parts[0] == "bridge":
            _, bridge_id, action = parts
            if action == "frame":
                self._handle_frame(bridge_id)
                return
            if action == "result":
                self._handle_result(bridge_id)
                return
            if action == "video":
                self._handle_video(bridge_id)
                return
            if action == "video_result":
                self._handle_video_result(bridge_id)
                return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    # -- /frame --

    def _resolve_bridge_node(self, bridge_id: str) -> tuple[Any, str]:
        if bridge_id and bridge_id not in ("_active", "default"):
            node = napi.find_bridge_node(bridge_id)
        else:
            node = napi.find_default_bridge_node()
        if node is None:
            return None, bridge_id
        resolved = napi.bridge_id_for_node(node) or bridge_id
        return node, resolved

    def _clean_colorspace(self, colorspace: Any) -> str:
        cs = str(colorspace or "").strip()
        if not cs:
            return ""
        try:
            float(cs)
            return ""
        except ValueError:
            return cs

    def _bridge_colorspace(self, node: Any, requested: Any = None) -> str:
        requested_cs = self._clean_colorspace(requested)

        def _resolve() -> str:
            k = node.knob("send_colorspace") if node is not None else None
            try:
                values = bridge_node_module.write_colorspaces(napi._nuke)
            except Exception:
                values = []
            if requested_cs and values and requested_cs in values:
                return requested_cs
            if k is None:
                return ""
            current = self._clean_colorspace(k.value())
            if current and values and current in values:
                return current
            return ""

        try:
            return napi.call(_resolve)
        except Exception:
            return ""

    def _handle_frame(self, bridge_id: str) -> None:
        req = self._read_json()
        frame_req = int(req.get("frame", -1))
        requested_colorspace = req.get("colorspace")
        requested_format = req.get("format")  # optional client override

        with _NUKE_LOCK:
            try:
                node, resolved_bridge_id = self._resolve_bridge_node(bridge_id)
                if node is None:
                    _json_response(
                        self, 404, {"ok": False, "error": f"bridge_id {bridge_id!r} not found"}
                    )
                    return
                mask_source = str(napi.knob_value(node, "mask_source") or "source alpha")
                cs = self._bridge_colorspace(node, requested_colorspace)
                fmt = str(
                    requested_format or napi.knob_value(node, "send_format") or "png8"
                )
                frame = render._resolve_frame(frame_req)
                data, width, height, actual_fmt = render.render_frame(
                    node, frame, mask_source, cs, fmt
                )
                prompt = str(napi.knob_value(node, "prompt") or "")
            except NotImplementedError as exc:
                _json_response(self, 501, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # render errors, Nuke unavailable, etc.
                _json_response(self, 500, {"ok": False, "error": repr(exc)})
                return

        content_type = "image/exr" if actual_fmt == "exr16" else "image/png"
        headers = {
            "Content-Type": content_type,
            "X-NukeBridge-Bridge-Id": resolved_bridge_id,
            "X-NukeBridge-Frame": str(frame),
            "X-NukeBridge-Format": actual_fmt,
            "X-NukeBridge-Width": str(width),
            "X-NukeBridge-Height": str(height),
            "X-NukeBridge-Prompt": urllib.parse.quote(prompt),
            "X-NukeBridge-Mask-Source": mask_source,
            "X-NukeBridge-Colorspace": cs,
        }
        _binary_response(self, 200, data, headers)

    # -- /result --

    def _handle_result(self, bridge_id: str) -> None:
        body = self._read_body()
        prefix = self.headers.get("X-NukeBridge-Filename-Prefix") or "comfy_result"
        colorspace = self._clean_colorspace(self.headers.get("X-NukeBridge-Colorspace"))
        fmt = (self.headers.get("X-NukeBridge-Format") or "png8").strip().lower()
        ext = "exr" if fmt == "exr16" else "png"

        srv = get_server()
        output_dir = (srv.settings if srv else load_settings()).get(
            "output_directory"
        ) or DEFAULT_SETTINGS["output_directory"]

        with _NUKE_LOCK:
            node = None
            create_read = False
            try:
                node, _ = self._resolve_bridge_node(bridge_id)
                if node is not None:
                    create_read = bool(napi.knob_value(node, "create_read_on_result"))
            except Exception:
                pass

            try:
                path = result.save_result(
                    body=body,
                    output_directory=str(output_dir),
                    filename_prefix=prefix,
                    colorspace=colorspace,
                    bridge_node=node,
                    create_read=create_read,
                    ext=ext,
                )
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": repr(exc)})
                return

        _json_response(self, 200, {"ok": True, "path": path})

    # -- /video + /asset + /video_result --

    def _handle_video(self, bridge_id: str) -> None:
        req = self._read_json()
        with _NUKE_LOCK:
            try:
                node, _resolved_bridge_id = self._resolve_bridge_node(bridge_id)
                if node is None:
                    _json_response(self, 404, {"ok": False, "error": f"bridge_id {bridge_id!r} not found"})
                    return
                req_first = int(req.get("frame_start", -1))
                req_last = int(req.get("frame_end", -1))
                node_first = int(napi.knob_value(node, "video_first") or napi.root_frame())
                node_last = int(napi.knob_value(node, "video_last") or node_first)
                first = req_first if req_first >= 0 else node_first
                last = req_last if req_last >= 0 else node_last
                if last < first:
                    raise ValueError(f"invalid video frame range: {first}-{last}")
                fps = float(req.get("fps") or napi.knob_value(node, "video_fps") or 24.0)
                fmt = str(req.get("format") or napi.knob_value(node, "video_format") or "mp4").lower()
                mov_codec = str(req.get("mov_codec") or napi.knob_value(node, "video_mov_codec") or "prores_422hq").lower()
                colorspace = self._clean_colorspace(req.get("colorspace") or napi.knob_value(node, "video_colorspace"))
                bundle = video.export_video_bundle(node, first, last, fps, fmt, mov_codec, colorspace)
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": repr(exc)})
                return

        _cleanup_video_assets()
        asset_id = uuid.uuid4().hex
        _VIDEO_ASSETS[asset_id] = {"created_at": time.time(), **bundle}
        srv = get_server()
        base = srv.url() if srv else ""
        meta = dict(bundle["metadata"])
        _json_response(self, 200, {
            "ok": True,
            "asset_id": asset_id,
            "main_url": f"{base}/asset/{asset_id}/main",
            "mask_url": f"{base}/asset/{asset_id}/mask",
            "metadata": meta,
        })

    def _handle_asset(self, asset_id: str, kind: str) -> None:
        asset = _VIDEO_ASSETS.get(asset_id)
        if not asset or kind not in ("main", "mask"):
            _json_response(self, 404, {"ok": False, "error": "asset not found"})
            return
        path = str(asset.get("main_path" if kind == "main" else "mask_path") or "")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            _json_response(self, 404, {"ok": False, "error": repr(exc)})
            return
        meta = asset.get("metadata") or {}
        fmt = meta.get("format") if kind == "main" else "mp4"
        _binary_response(self, 200, body, {
            "Content-Type": "video/quicktime" if fmt == "mov" else "video/mp4",
            "X-NukeBridge-Format": str(fmt),
            "X-NukeBridge-Frame-Start": str(meta.get("frame_start") or ""),
            "X-NukeBridge-Frame-End": str(meta.get("frame_end") or ""),
            "X-NukeBridge-FPS": str(meta.get("fps") or ""),
        })

    def _handle_video_result(self, bridge_id: str) -> None:
        body = self._read_body()
        prefix = self.headers.get("X-NukeBridge-Filename-Prefix") or "comfy_video_result"
        fmt = (self.headers.get("X-NukeBridge-Format") or "mp4").strip().lower()
        first = int(float(self.headers.get("X-NukeBridge-Frame-Start") or 1))
        last = int(float(self.headers.get("X-NukeBridge-Frame-End") or first))
        colorspace = self._clean_colorspace(self.headers.get("X-NukeBridge-Colorspace"))
        srv = get_server()
        output_dir = (srv.settings if srv else load_settings()).get("output_directory") or DEFAULT_SETTINGS["output_directory"]
        with _NUKE_LOCK:
            node = None
            create_read = False
            try:
                node, _ = self._resolve_bridge_node(bridge_id)
                if node is not None:
                    create_read = bool(napi.knob_value(node, "create_read_on_result"))
                path = video.save_video_result(body, str(output_dir), prefix, fmt, first, last, colorspace, node, create_read)
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": repr(exc)})
                return
        _json_response(self, 200, {"ok": True, "path": path})


def autostart_if_in_nuke() -> Optional[BridgeServer]:
    """Called from menu.py. Starts the server only if Nuke is available."""
    if not napi.has_nuke():
        return None
    try:
        return ensure_server()
    except OSError as exc:
        # Port in use / bind failure: surface to Nuke console but don't crash.
        import sys
        sys.stderr.write(f"[comfyui_bridge] failed to start server: {exc}\n")
        return None
