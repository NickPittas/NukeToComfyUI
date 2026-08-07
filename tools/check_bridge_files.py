#!/usr/bin/env python3
"""Assert-based self-check for comfyui_bridge file/session helpers, runnable outside Nuke."""

import os
import sys
import tempfile
from typing import Any

REPO_NUKE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "nuke"))
sys.path.insert(0, REPO_NUKE_DIR)

from comfyui_bridge import napi, result, server, session_files, video  # noqa: E402


def _bridge_health(port: int) -> bool:
    """Probe the bridge /health endpoint; True if it answers 200."""
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=2.0) as s:
        s.sendall(b"GET /health HTTP/1.0\r\n\r\n")
        data = s.recv(1024)
    return b"200" in data


def test_start_server_restart_and_rollback() -> None:
    """start_server restarts same host/port transactionally and rolls back to
    the old listener when the new bind fails."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    settings = {"host": "127.0.0.1", "port": port}
    old_server = server._SERVER
    old_load = server.load_settings
    try:
        server._SERVER = None
        server.load_settings = lambda: dict(settings)

        srv = server.start_server()
        assert server._SERVER is srv
        assert (srv.host, srv.port) == ("127.0.0.1", port)
        assert _bridge_health(port)

        # Immediate same-address restart: old listener replaced, still healthy.
        srv2 = server.start_server()
        assert server._SERVER is srv2
        assert srv is not srv2
        assert (srv2.host, srv2.port) == ("127.0.0.1", port)
        assert _bridge_health(port)

        # Invalid host: bind fails, old server must be restored and reachable.
        settings["host"] = "203.0.113.1"  # TEST-NET-3: not a local address
        try:
            server.start_server()
            raise AssertionError("start_server should raise on bind failure")
        except OSError:
            pass
        assert server._SERVER is srv2, "_SERVER must point at restored old server"
        assert _bridge_health(port)
    finally:
        try:
            server._SERVER.stop()
        except Exception:
            pass
        server._SERVER = old_server
        server.load_settings = old_load


def test_sanitize_stem() -> None:
    assert napi.sanitize_stem("/show/shot/My Comp.nk") == "MyComp"
    assert napi.sanitize_stem("C:\\show\\shot_010.nk") == "shot_010"
    for name in ("", "Root", "Untitled", "!!!.nk"):
        assert napi.sanitize_stem(name) == "nuke_bridge"


def test_session_files() -> None:
    with tempfile.TemporaryDirectory() as td:
        files = [os.path.join(td, f"same_{i}.png") for i in range(3)]
        for f in files:
            with open(f, "wb") as fh:
                fh.write(b"x")
        session_files.track(files[0])
        session_files.track(files[1])
        assert session_files.clear() == (2, 0)
        assert not os.path.exists(files[0])
        assert not os.path.exists(files[1])
        assert os.path.exists(files[2])
        session_files.track(os.path.join(td, "missing.png"))
        assert session_files.clear() == (0, 0)


def test_save_result() -> None:
    old = napi.comp_stem
    napi.comp_stem = lambda: "shot_010"  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            body = b"\x89PNG fake payload"
            path = result.save_result(body, td, filename_prefix=None, create_read=False)
            assert os.path.basename(path).startswith("shot_010_result_")
            with open(path, "rb") as fh:
                assert fh.read() == body
            # Results are user outputs backing Read nodes, not session cache:
            # clear() must never touch them.
            assert session_files.clear() == (0, 0)
            assert os.path.exists(path)
            with open(path, "rb") as fh:
                assert fh.read() == body
    finally:
        napi.comp_stem = old


def test_export_video_bundle() -> None:
    """mov/mp4 exports build comp-prefixed source/mask paths (regression: undefined
    `suffix` NameError) and register only their exact files."""
    written: list[str] = []

    class _FakeKnob:
        def value(self) -> str:
            return "source alpha"

    class _FakeFormat:
        def width(self) -> int:
            return 640

        def height(self) -> int:
            return 480

    class _FakeNode:
        def input(self, index: int) -> Any:
            assert index == 0
            return self

        def knob(self, name: str) -> Any:
            return _FakeKnob() if name == "mask_source" else None

        def format(self) -> Any:
            return _FakeFormat()

    def _fake_write(_nuke: Any, _input: Any, path: str, *_a: Any, **_kw: Any) -> None:
        with open(path, "wb") as fh:
            fh.write(b"x")
        written.append(path)

    originals = (video._cache_key, video._write_movie, video._mask_chain, napi.call, napi.comp_stem)
    video._cache_key = lambda _bridge, _first, _last, _fps, fmt, _codec, _cs: ("key", fmt)
    video._write_movie = _fake_write
    video._mask_chain = lambda *_a, **_kw: object()
    napi.call = lambda fn, *a, **kw: fn(*a, **kw)
    napi.comp_stem = lambda: "Shot_010"  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            stray = os.path.join(td, "untracked.png")
            with open(stray, "wb") as fh:
                fh.write(b"x")
            assert session_files.clear() == (0, 0)
            for fmt, ext in (("mov", "mov"), ("mp4", "mp4")):
                bundle = video.export_video_bundle(_FakeNode(), td, 1, 2, 24.0, fmt, "prores_422hq", "rec709")
                main = bundle["main_path"]
                mask = bundle["mask_path"]
                assert os.path.basename(main).startswith("Shot_010_source_")
                assert os.path.basename(main).endswith(f".{ext}"), main
                assert os.path.basename(mask).startswith("Shot_010_mask_")
                assert os.path.basename(mask).endswith(".mp4"), mask
                assert bundle["metadata"]["format"] == fmt
                assert os.path.isfile(main) and os.path.getsize(main) > 0
            # exactly the 4 registered bundle files are tracked/removed; stray untouched.
            assert session_files.clear() == (4, 0)
            assert os.path.exists(stray)
            for p in written:
                assert not os.path.exists(p)
    finally:
        (video._cache_key, video._write_movie, video._mask_chain, napi.call, napi.comp_stem) = originals


def main() -> None:
    test_sanitize_stem()
    test_session_files()
    test_save_result()
    test_export_video_bundle()
    test_start_server_restart_and_rollback()
    print("check_bridge_files: PASS")


if __name__ == "__main__":
    main()
