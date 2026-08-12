"""Native Nuke Write based video export/import for the bridge."""

from __future__ import annotations

import os
import shutil
import time
import uuid
from typing import Any, Dict, Optional

from . import napi, session_files


VIDEO_FORMATS = ("mov", "mp4")
MOV_CODECS = ("prores_422hq", "prores_4444")
_VIDEO_CACHE: dict[tuple, Dict[str, Any]] = {}


def clear_cache() -> None:
    _VIDEO_CACHE.clear()


def normalized_frame_range(first: int, last: int, enabled: bool) -> tuple[int, int]:
    first = int(first)
    last = int(last)
    if last < first:
        raise ValueError(f"invalid video frame range: {first}-{last}")
    if not enabled:
        return first, last
    count = last - first + 1
    effective_count = ((count - 1 + 7) // 8) * 8 + 1
    return first, first + effective_count - 1


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _set_enum_by_alias(knob: Any, aliases: tuple[str, ...]) -> str:
    if knob is None:
        raise RuntimeError(f"missing knob for {aliases!r}")
    values = [str(v) for v in list(knob.values())]
    for alias in aliases:
        a = _norm(alias)
        for value in values:
            v = _norm(value)
            if a and (a in v or v in a):
                token = value.split("\t", 1)[0].strip()
                if not token:
                    continue
                knob.setValue(token)
                return value
    raise RuntimeError(f"could not match {aliases!r}; available: {values!r}")


def _knob_values(knob: Any) -> list[str]:
    try:
        return [str(v) for v in list(knob.values())]
    except Exception:
        return []


def _codec_debug(write: Any) -> str:
    found = []
    try:
        items = write.knobs().items()
    except Exception:
        items = []
    for name, knob in items:
        lname = str(name).lower()
        label = ""
        try:
            label = str(knob.label())
        except Exception:
            pass
        llabel = label.lower()
        if any(token in lname or token in llabel for token in ("codec", "compression", "profile", "quality", "format")):
            found.append(f"{name}({label})={_knob_values(knob)!r}")
    if not found:
        for name, knob in items:
            try:
                label = str(knob.label())
            except Exception:
                label = ""
            found.append(f"{name}({label})")
    return "; ".join(found)


def _set_movie_format(write: Any) -> None:
    k = write.knob("file_type")
    if k is not None:
        _set_enum_by_alias(k, ("mov\t\t\tffmpeg", "mov", "mov64", "movie", "quicktime", "quicktime/mov"))
        try:
            k.setValue("mov")
        except Exception:
            pass
    enc = write.knob("meta_encoder")
    if enc is not None:
        try:
            enc.setValue("mov64")
        except Exception:
            pass


def _set_mov64_format(write: Any, fmt: str) -> None:
    k = write.knob("mov64_format")
    if k is None:
        return
    if fmt == "mp4":
        _set_enum_by_alias(k, ("mp4 (MP4 (MPEG-4 Part 14))", "mp4"))
    else:
        _set_enum_by_alias(k, ("mov (QuickTime / MOV)", "mov"))


def _set_movie_codec(write: Any, fmt: str, mov_codec: str) -> str:
    _set_mov64_format(write, fmt)
    codec_knob = write.knob("mov64_codec")
    if codec_knob is None:
        raise RuntimeError(f"missing mov64_codec knob; codec knobs: {_codec_debug(write)}")

    if fmt == "mp4":
        write.knob("mov64_format").setValue("mp4 (MP4 (MPEG-4 Part 14))")
        codec_knob.setValue("h264")
        write.knob("mov_h264_codec_profile").setValue("High 4:2:0 8-bit")
        write.knob("mov64_quality").setValue("High")
        _set_h264_defaults(write)
        return "h264, High 4:2:0 8-bit, High"
    elif mov_codec == "prores_4444":
        codec = _set_enum_by_alias(codec_knob, ("appr\tApple ProRes", "Apple ProRes", "appr"))
        profile = _set_enum_by_alias(write.knob("mov_prores_codec_profile"), ("ProRes 4:4:4:4 12-bit",))
        return ", ".join((codec, profile))
    else:
        codec = _set_enum_by_alias(codec_knob, ("appr\tApple ProRes", "Apple ProRes", "appr"))
        profile = _set_enum_by_alias(write.knob("mov_prores_codec_profile"), ("ProRes 4:2:2 HQ 10-bit",))
        return ", ".join((codec, profile))


def _set_h264_defaults(write: Any) -> None:
    for name, value in (
        ("mov64_fast_start", True),
        ("mov64_write_timecode", True),
        ("mov64_gop_size", 12),
        ("mov64_b_frames", 0),
        ("mov64_bitrate", 28000),
        ("mov64_bitrate_tolerance", 0),
        ("mov64_quality_min", 1),
        ("mov64_quality_max", 3),
    ):
        k = write.knob(name)
        if k is not None:
            try:
                k.setValue(value)
            except Exception:
                pass


def _set_first_matching_knob(write: Any, names: tuple[str, ...], aliases: tuple[str, ...], required: bool = True) -> str:
    candidates = list(names)
    wanted = {_norm(name) for name in names}
    try:
        for name, knob in write.knobs().items():
            label = ""
            try:
                label = str(knob.label())
            except Exception:
                pass
            if _norm(name) in wanted or _norm(label) in wanted:
                candidates.append(name)
    except Exception:
        pass

    for name in dict.fromkeys(candidates):
        k = write.knob(name)
        if k is None:
            continue
        values = _knob_values(k)
        try:
            if values:
                return _set_enum_by_alias(k, aliases)
            k.setValue(aliases[0])
            return aliases[0]
        except Exception:
            pass
    if required:
        raise RuntimeError(f"could not set {names[0]}; codec knobs: {_codec_debug(write)}")
    return ""


def _set_colorspace(write: Any, colorspace: str) -> None:
    cs = str(colorspace or "").strip()
    if not cs:
        return
    k = write.knob("colorspace")
    if k is None:
        return
    values = [str(v) for v in list(k.values())]
    if values and cs not in values:
        raise RuntimeError(f"invalid video colorspace {cs!r}")
    k.setValue(cs)


def _write_movie(nuke: Any, input_node: Any, path: str, first: int, last: int, fmt: str, mov_codec: str, colorspace: str) -> None:
    write = nuke.createNode("Write", "", inpanel=False)
    try:
        write.setInput(0, input_node)
        write.knob("file").setValue(path)
        try:
            write.knob("channels").setValue("rgb")
        except Exception:
            pass
        _set_movie_format(write)
        _set_movie_codec(write, fmt, mov_codec)
        _set_colorspace(write, colorspace)
        nuke.execute(write, int(first), int(last))
    finally:
        try:
            nuke.delete(write)
        except Exception:
            pass


def _expr_mask(nuke: Any, node: Any, expr: str, temp_nodes: list[Any]) -> Any:
    e = nuke.nodes.Expression(inputs=[node])
    temp_nodes.append(e)
    for idx in (0, 1, 2):
        try:
            e.knob(f"expr{idx}").setValue(expr)
        except Exception:
            pass
    try:
        e.knob("expr3").setValue("1")
    except Exception:
        pass
    return e


def _mask_chain(nuke: Any, bridge_node: Any, mask_source: str, temp_nodes: list[Any]) -> Any:
    src = bridge_node.input(0)
    if src is None:
        raise napi.NukeError("ComfyUIBridge input 0 is not connected")
    mask = bridge_node.input(1)
    if mask_source == "source alpha":
        return _expr_mask(nuke, src, "1-a", temp_nodes)
    if mask_source == "invert source alpha":
        return _expr_mask(nuke, src, "a", temp_nodes)
    if mask_source == "mask input":
        return _expr_mask(nuke, mask or src, "a" if mask is not None else "0", temp_nodes)
    if mask_source == "invert mask input":
        return _expr_mask(nuke, mask or src, "1-a" if mask is not None else "0", temp_nodes)
    return _expr_mask(nuke, src, "0", temp_nodes)


def _node_name(node: Any) -> str:
    if node is None:
        return ""
    for name in ("fullName", "name"):
        fn = getattr(node, name, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:
                pass
    return str(node)


def _video_mask_source(bridge_node: Any) -> str:
    for name in ("video_mask_source", "mask_source"):
        try:
            knob = bridge_node.knob(name)
            value = knob.value() if knob is not None else ""
            if value:
                return str(value)
        except Exception:
            pass
    return "source alpha"


def _cache_key(bridge_node: Any, frame_start: int, frame_end: int, fps: float, fmt: str, mov_codec: str, colorspace: str) -> tuple:
    def _build() -> tuple:
        return (
            str(napi.knob_value(bridge_node, "bridge_id") or ""),
            _node_name(bridge_node.input(0)),
            _node_name(bridge_node.input(1)),
            _video_mask_source(bridge_node),
            int(frame_start),
            int(frame_end),
            float(fps),
            str(fmt),
            str(mov_codec),
            str(colorspace or ""),
        )
    return napi.call(_build)


def _valid_bundle(bundle: Dict[str, Any]) -> bool:
    for key in ("main_path", "mask_path"):
        path = str(bundle.get(key) or "")
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
    return True


def export_video_bundle(bridge_node: Any, output_directory: str, frame_start: int, frame_end: int, fps: float, fmt: str, mov_codec: str, colorspace: str) -> Dict[str, Any]:
    fmt = fmt if fmt in VIDEO_FORMATS else "mov"
    mov_codec = mov_codec if mov_codec in MOV_CODECS else "prores_422hq"
    key = _cache_key(bridge_node, frame_start, frame_end, fps, fmt, mov_codec, colorspace)
    cached = _VIDEO_CACHE.get(key)
    if cached and _valid_bundle(cached):
        return cached
    comp = napi.comp_stem()
    suffix = ".mov" if fmt == "mov" else ".mp4"
    main_path = _unique_path(output_directory, f"{comp}_source", suffix.lstrip("."))
    mask_path = _unique_path(output_directory, f"{comp}_mask", "mp4")
    # Register before writing so a partial/failed bundle is still cleaned up;
    # missing tracked files are harmless.
    session_files.track(main_path)
    session_files.track(mask_path)
    nuke: Any = napi._nuke

    def _export() -> Dict[str, Any]:
        src = bridge_node.input(0)
        if src is None:
            raise napi.NukeError("ComfyUIBridge input 0 is not connected")
        temp_nodes: list[Any] = []
        try:
            _write_movie(nuke, src, main_path, frame_start, frame_end, fmt, mov_codec, colorspace)
            mask_source = _video_mask_source(bridge_node)
            mask_node = _mask_chain(nuke, bridge_node, mask_source, temp_nodes)
            _write_movie(nuke, mask_node, mask_path, frame_start, frame_end, "mp4", "prores_422hq", "")
            f = src.format()
            return {"width": int(f.width()), "height": int(f.height())}
        finally:
            for node in reversed(temp_nodes):
                try:
                    nuke.delete(node)
                except Exception:
                    pass

    meta = napi.call(_export)
    meta.update({
        "format": fmt,
        "mov_codec": mov_codec if fmt == "mov" else "h264",
        "frame_start": int(frame_start),
        "frame_end": int(frame_end),
        "frame_count": int(frame_end) - int(frame_start) + 1,
        "fps": float(fps),
        "colorspace": str(colorspace or ""),
        "mask_convention": "white=masked",
    })
    bundle = {"main_path": main_path, "mask_path": mask_path, "metadata": meta}
    _VIDEO_CACHE[key] = bundle
    return bundle


def _unique_path(output_directory: str, prefix: str, ext: str) -> str:
    os.makedirs(output_directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(output_directory, f"{prefix}_{stamp}_{os.getpid()}_{uuid.uuid4().hex[:8]}.{ext}")


def _finalize_result(
    path: str,
    fmt: str,
    frame_start: int,
    frame_end: int,
    colorspace: str,
    bridge_node: Any,
    create_read: bool,
) -> str:
    """Shared result finishing: optional Read node + last_result knob."""
    if create_read and bridge_node is not None and napi.has_nuke():
        nuke: Any = napi._nuke

        def _add_read() -> None:
            read = nuke.nodes.Read(file=path)
            for name, value in (("first", frame_start), ("last", frame_end), ("origfirst", frame_start), ("origlast", frame_end)):
                k = read.knob(name)
                if k is not None:
                    try:
                        k.setValue(int(value))
                    except Exception:
                        pass
            cs = str(colorspace or "").strip()
            k = read.knob("colorspace")
            if k is not None and cs:
                values = [str(v) for v in list(k.values())]
                if not values or cs in values:
                    try:
                        k.setValue(cs)
                    except Exception:
                        pass

        napi.call(_add_read)
    if bridge_node is not None:
        napi.set_knob_value(bridge_node, "last_result", path)
    return path


def save_video_result_file(
    src_path: str,
    output_directory: str,
    filename_prefix: Optional[str] = None,
    fmt: str = "mov",
    frame_start: int = 1,
    frame_end: int = 1,
    colorspace: str = "",
    bridge_node: Any = None,
    create_read: bool = False,
) -> str:
    """Move an already-written video file into the output directory and finish it."""
    ext = "mov" if fmt == "mov" else "mp4"
    prefix = filename_prefix or f"{napi.comp_stem()}_result"
    path = _unique_path(output_directory, prefix, ext)
    shutil.move(src_path, path)
    return _finalize_result(path, fmt, frame_start, frame_end, colorspace, bridge_node, create_read)
