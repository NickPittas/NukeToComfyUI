"""ComfyUI execution progress for Nuke-triggered runs.

Stdlib-only (no `requests`, no `websocket-client`). Connects to ComfyUI's
`/ws?clientId=...` with a minimal RFC6455 client built on `socket`/`ssl`,
shows a `nuke.ProgressTask` while events stream, and updates the bridge
node's `status` knob. Falls back to polling `GET /history/{prompt_id}` if the
websocket handshake or read fails.

This module imports cleanly outside Nuke (the `nuke` access is via `napi`,
which degrades to no-ops when Nuke is unavailable); the monitoring helpers
short-circuit when there is no Nuke to update.

Event contract (subset ComfyUI actually emits, see ComfyUI server.py):
  status / execution_start / execution_cached / executing / executed /
  progress / execution_error / execution_interrupted

Terminal conditions for a prompt_id:
  - `executing` with `data.node is None` and matching prompt_id -> done, ok
  - `execution_error` with matching prompt_id                -> failed
  - `execution_interrupted` with matching prompt_id          -> cancelled
  - socket closed without terminal event                     -> fall back to history
  - total_timeout elapsed                                    -> timeout
"""

from __future__ import annotations

import base64
import json
import os
import select
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple


# --- ComfyUI HTTP helpers (stdlib) ----------------------------------------

def _base_url(host: str, port: int) -> str:
    return f"http://{(host or '127.0.0.1').strip()}:{int(port or 8188)}"


def extract_prompt_id(response: Dict[str, Any]) -> Optional[str]:
    """Pull prompt_id out of a ComfyUI /prompt response (handles list or scalar)."""
    pid = response.get("prompt_id") if isinstance(response, dict) else None
    if isinstance(pid, str) and pid:
        return pid
    if isinstance(pid, list) and pid:
        first = pid[0]
        if isinstance(first, str) and first:
            return first
    return None


def fetch_history(host: str, port: int, prompt_id: str, timeout: float = 5.0) -> Dict[str, Any]:
    """GET /history/{prompt_id}; returns {} if not present or on error."""
    url = f"{_base_url(host, port)}/history/{urllib.parse.quote(prompt_id)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


# --- Minimal websocket client (RFC6455, text frames only) -----------------

def _recvn(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv_text_frame(sock: socket.socket) -> Optional[bytes]:
    """Read one websocket frame; returns text payload or None on close/control."""
    hdr = _recvn(sock, 2)
    if not hdr:
        return None
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = _recvn(sock, 2)
        if not ext:
            return None
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = _recvn(sock, 8)
        if not ext:
            return None
        length = int.from_bytes(ext, "big")
    mask = _recvn(sock, 4) if masked else b""
    if mask is None:
        return None
    payload = b""
    if length:
        payload = _recvn(sock, length)
        if payload is None:
            return None
    if masked and payload:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    # Ignore close(8)/ping(9)/pong(10); accept text(1) and binary(2) — some
    # servers serialize JSON as binary frames.
    return payload if opcode in (1, 2) else b""


def _ws_connect(host: str, port: int, client_id: str, use_tls: bool, timeout: float) -> socket.socket:
    """Open a TCP socket, perform the websocket upgrade handshake, return socket."""
    raw = socket.create_connection((host, int(port)), timeout=timeout)
    sock: socket.socket
    if use_tls:
        ctx = ssl.create_default_context()
        # Local dev servers often self-sign; loosening is acceptable for ws-local.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(raw, server_hostname=host)
    else:
        sock = raw
    sock.settimeout(None)  # we use select() for timeouts

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    handshake = (
        f"GET /ws?clientId={client_id} HTTP/1.1\r\n"
        f"Host: {host}:{int(port)}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    sock.sendall(handshake)

    # Read response headers (until blank line).
    resp = b""
    while b"\r\n\r\n" not in resp:
        try:
            chunk = sock.recv(1024)
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"ws handshake read failed: {exc}") from exc
        if not chunk:
            sock.close()
            raise RuntimeError("ws handshake closed before completion")
        resp += chunk
        if len(resp) > 8192:  # ponytail: cap header read to avoid runaway
            sock.close()
            raise RuntimeError("ws handshake response too large")
    status_line = resp.split(b"\r\n", 1)[0]
    if b" 101 " not in status_line:
        sock.close()
        raise RuntimeError(f"ws upgrade rejected: {status_line!r}")
    return sock


# --- Nuke ProgressTask helpers (main-thread safe) -------------------------

def _make_progress(title: str) -> Optional[Any]:
    """Create a nuke.ProgressTask on the main thread; None if Nuke/unavailable."""
    from . import napi
    if not napi.has_nuke():
        return None

    def _create() -> Any:
        nuke = napi._nuke
        cls = getattr(nuke, "ProgressTask", None)
        if cls is None:
            return None
        try:
            return cls(title)
        except Exception:
            return None

    try:
        return napi.call(_create)
    except Exception:
        return None


def _progress_set(task: Any, progress: Optional[int], message: Optional[str]) -> None:
    if task is None:
        return
    from . import napi

    def _set() -> None:
        try:
            if message is not None and hasattr(task, "setMessage"):
                task.setMessage(str(message))
            if progress is not None and hasattr(task, "setProgress"):
                # Nuke ProgressTask takes int 0..100.
                task.setProgress(int(max(0, min(100, progress))))
        except Exception:
            pass

    try:
        napi.call(_set)
    except Exception:
        pass


def _progress_cancelled(task: Any) -> bool:
    if task is None:
        return False
    from . import napi

    def _check() -> bool:
        for name in ("isCancelled", "cancelled"):
            fn = getattr(task, name, None)
            if callable(fn):
                try:
                    return bool(fn())
                except Exception:
                    return False
        return False

    try:
        return bool(napi.call(_check))
    except Exception:
        return False


def _progress_destroy(task: Any) -> None:
    if task is None:
        return
    from . import napi

    def _destroy() -> None:
        for name in ("destroy", "close", "finished"):
            fn = getattr(task, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break

    try:
        napi.call(_destroy)
    except Exception:
        pass


def _set_status(bridge_node: Any, message: str) -> None:
    if bridge_node is None:
        return
    try:
        from . import napi
        napi.set_knob_value(bridge_node, "status", message)
    except Exception:
        pass


# --- Public monitoring API -------------------------------------------------

def monitor_execution(
    bridge_node: Any,
    host: str,
    port: int,
    prompt_id: str,
    client_id: str,
    total_timeout: float = 600.0,
    use_tls: bool = False,
) -> str:
    """Monitor a ComfyUI prompt_id until it finishes or is cancelled.

    Updates the bridge node's `status` knob and a Nuke ProgressTask. Returns a
    short terminal status string: 'ok' | 'cancelled' | 'error' | 'timeout' |
    'unknown'. Best-effort: never raises.
    """
    deadline = time.time() + max(5.0, float(total_timeout))
    task = _make_progress(f"ComfyUI workflow {prompt_id[:8]}")
    _set_status(bridge_node, f"submitted: {prompt_id[:8]}")
    _progress_set(task, 0, "connecting to ComfyUI…")

    # Event counts for a rough progress approximation.
    last_node: Optional[str] = None
    saw_start = False

    try:
        try:
            sock = _ws_connect(host, port, client_id, use_tls, timeout=10.0)
        except Exception as exc:
            _set_status(bridge_node, f"ws failed, polling history: {exc}")
            return _poll_history_until_done(
                bridge_node, host, port, prompt_id, deadline, task
            )

        try:
            while True:
                if _progress_cancelled(task):
                    _set_status(bridge_node, "cancelled")
                    return "cancelled"
                if time.time() > deadline:
                    _set_status(bridge_node, "timeout")
                    return "timeout"

                # Block briefly so the cancel check stays responsive.
                try:
                    r, _, _ = select.select([sock], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not r:
                    continue

                payload = _recv_text_frame(sock)
                if payload is None:
                    # socket closed mid-stream; try history to confirm outcome
                    _set_status(bridge_node, "ws closed, checking history…")
                    outcome = _poll_history_until_done(
                        bridge_node, host, port, prompt_id, deadline, task
                    )
                    return outcome
                if not payload:
                    continue

                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue

                outcome = _handle_event(
                    msg, prompt_id, bridge_node, task, locals_state={"saw_start": saw_start}
                )
                # _handle_event may mutate bookkeeping via return flags:
                if outcome == "_START":
                    saw_start = True
                    continue
                if outcome is not None:
                    return outcome

                # Non-terminal informational event -> bump status/progress.
                data = msg.get("data") or {}
                mtype = msg.get("type")
                if mtype == "progress":
                    value = data.get("value")
                    mx = data.get("max")
                    if isinstance(value, (int, float)) and isinstance(mx, (int, float)) and mx:
                        pct = int(100.0 * float(value) / float(mx))
                        _progress_set(task, pct, f"progress {value}/{mx}")
                        _set_status(bridge_node, f"running: {value}/{mx}")
                elif mtype == "executing" and isinstance(data.get("node"), str):
                    last_node = data.get("node")
                    _progress_set(task, None, f"executing {last_node}")
                    _set_status(bridge_node, f"executing: {last_node}")
                elif mtype == "executed":
                    node = data.get("node")
                    _progress_set(task, None, f"executed {node}")
                    _set_status(bridge_node, f"executed: {node}")
                # Loop continues until a terminal event returns from _handle_event
                # or the deadline/cancel checks above fire.
            # ponytail: defensive — the loop is only exited by return/break, but
            # keep the type checker happy.
            return "unknown"
        finally:
            try:
                sock.close()
            except Exception:
                pass
    finally:
        _progress_destroy(task)


def _handle_event(
    msg: Dict[str, Any],
    prompt_id: str,
    bridge_node: Any,
    task: Any,
    locals_state: Dict[str, Any],
) -> Optional[str]:
    """Return a terminal status string, '_START' for execution_start, or None."""
    mtype = msg.get("type")
    data = msg.get("data") or {}
    pid = data.get("prompt_id")

    # Most execution events carry a prompt_id; only act on ours.
    def _ours() -> bool:
        return (pid is None) or (pid == prompt_id)

    if mtype == "execution_start" and _ours():
        _progress_set(task, 1, "execution started")
        _set_status(bridge_node, "execution started")
        return "_START"

    if mtype == "execution_error" and _ours():
        node_type = data.get("exception_type") or data.get("node_type") or "?"
        _set_status(bridge_node, f"error: {node_type}")
        _progress_set(task, 100, f"error: {node_type}")
        return "error"

    if mtype == "execution_interrupted" and _ours():
        _set_status(bridge_node, "interrupted")
        return "cancelled"

    if mtype == "executing" and _ours():
        node = data.get("node")
        if node is None:  # terminal signal for this prompt_id
            _progress_set(task, 100, "done")
            _set_status(bridge_node, "done")
            return "ok"

    return None


def _poll_history_until_done(
    bridge_node: Any,
    host: str,
    port: int,
    prompt_id: str,
    deadline: float,
    task: Any,
) -> str:
    """Fallback: poll GET /history/{prompt_id} until it appears or timeout."""
    spinner = ["|", "/", "-", "\\"]
    i = 0
    while time.time() < deadline:
        if _progress_cancelled(task):
            _set_status(bridge_node, "cancelled")
            return "cancelled"
        hist = fetch_history(host, port, prompt_id)
        entry = hist.get(prompt_id) if isinstance(hist, dict) else None
        if entry:
            # Look for status in the history entry.
            status = entry.get("status") if isinstance(entry, dict) else None
            completed = (
                bool(status.get("completed", True))
                if isinstance(status, dict)
                else True
            )
            if completed:
                _progress_set(task, 100, "done")
                _set_status(bridge_node, "done (history)")
                return "ok"
            _set_status(bridge_node, "error (history)")
            return "error"
        i = (i + 1) % len(spinner)
        _progress_set(task, None, f"waiting {spinner[i]}")
        try:
            _set_status(bridge_node, f"queued {spinner[i]}")
        except Exception:
            pass
        time.sleep(0.5)
    _set_status(bridge_node, "timeout")
    return "timeout"


def submit_and_monitor(
    bridge_node: Any,
    host: str,
    port: int,
    client_id: str,
    prompt_payload: Dict[str, Any],
    submit_response: Optional[Dict[str, Any]] = None,
    total_timeout: float = 600.0,
) -> Tuple[Optional[str], str]:
    """Convenience: if `submit_response` is None, POST /prompt; then monitor.

    Returns (prompt_id, terminal_status).
    """
    # Be tolerant of frontend/backend wrappers. ComfyUI /prompt wants the API
    # prompt object itself, not {output: ...} or {prompt: ...}.
    if isinstance(prompt_payload, str):
        try:
            prompt_payload = json.loads(prompt_payload)
        except ValueError:
            _set_status(bridge_node, "prompt payload is a non-JSON string")
            return None, "error"
    if isinstance(prompt_payload, dict):
        if isinstance(prompt_payload.get("output"), dict):
            prompt_payload = prompt_payload["output"]
        elif isinstance(prompt_payload.get("prompt"), dict):
            prompt_payload = prompt_payload["prompt"]

    if not isinstance(prompt_payload, dict) or not prompt_payload:
        _set_status(bridge_node, f"invalid prompt payload: {type(prompt_payload).__name__}")
        return None, "error"

    response: Dict[str, Any] = submit_response or {}
    if submit_response is None:
        url = f"{_base_url(host, port)}/prompt"
        body = json.dumps({"prompt": prompt_payload, "client_id": client_id}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                response = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            _set_status(bridge_node, f"prompt post failed: HTTP {exc.code}: {detail[:200]}")
            return None, "error"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _set_status(bridge_node, f"prompt post failed: {exc}")
            return None, "error"

    prompt_id = extract_prompt_id(response)
    if not prompt_id:
        detail = response.get("error") or response.get("node_errors") or response
        text = json.dumps(detail, default=str)[:500]
        _set_status(bridge_node, f"no prompt_id from /prompt: {text}")
        return None, "unknown"

    status = monitor_execution(
        bridge_node, host, port, prompt_id, client_id, total_timeout=total_timeout
    )
    return prompt_id, status
