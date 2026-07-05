"""Local HTTP bridge server: /health, /frame, /result.

Single-threaded (ThreadingHTTPServer + a global render/result lock) bound to
the saved host/port. All Nuke API access goes through `napi` which dispatches
to the main thread when needed.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import napi, render, result
from .settings import DEFAULT_SETTINGS, load_settings, save_settings

# One global lock so concurrent HTTP requests cannot overlap Nuke renders.
_NUKE_LOCK = threading.Lock()

_SERVER: Optional["BridgeServer"] = None
_SERVER_LOCK = threading.Lock()


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
        _json_response(self, 404, {"ok": False, "error": "not found"})

    # -- /frame --

    def _handle_frame(self, bridge_id: str) -> None:
        req = self._read_json()
        frame_req = int(req.get("frame", -1))
        requested_colorspace = req.get("colorspace")

        with _NUKE_LOCK:
            try:
                node = napi.find_bridge_node(bridge_id)
                if node is None:
                    _json_response(
                        self, 404, {"ok": False, "error": f"bridge_id {bridge_id!r} not found"}
                    )
                    return
                mask_source = str(napi.knob_value(node, "mask_source") or "source alpha")
                cs = str(requested_colorspace or napi.knob_value(node, "send_colorspace") or "raw")
                frame = render._resolve_frame(frame_req)
                png, width, height = render.render_frame_png(node, frame, mask_source, cs)
                prompt = str(napi.knob_value(node, "prompt") or "")
            except NotImplementedError as exc:
                _json_response(self, 501, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # render errors, Nuke unavailable, etc.
                _json_response(self, 500, {"ok": False, "error": repr(exc)})
                return

        headers = {
            "Content-Type": "image/png",
            "X-NukeBridge-Bridge-Id": bridge_id,
            "X-NukeBridge-Frame": str(frame),
            "X-NukeBridge-Format": "png8",
            "X-NukeBridge-Width": str(width),
            "X-NukeBridge-Height": str(height),
            "X-NukeBridge-Prompt": urllib.parse.quote(prompt),
            "X-NukeBridge-Mask-Source": mask_source,
            "X-NukeBridge-Colorspace": cs,
        }
        _binary_response(self, 200, png, headers)

    # -- /result --

    def _handle_result(self, bridge_id: str) -> None:
        body = self._read_body()
        prefix = self.headers.get("X-NukeBridge-Filename-Prefix") or "comfy_result"
        colorspace = self.headers.get("X-NukeBridge-Colorspace") or "sRGB"

        srv = get_server()
        output_dir = (srv.settings if srv else load_settings()).get(
            "output_directory"
        ) or DEFAULT_SETTINGS["output_directory"]

        with _NUKE_LOCK:
            node = None
            create_read = False
            try:
                node = napi.find_bridge_node(bridge_id)
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
                    ext="png",
                )
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
