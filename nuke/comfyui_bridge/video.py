"""Native Nuke Write based video export/import for the bridge."""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from typing import Any, Dict

from . import napi


VIDEO_FORMATS = ("mp4", "mov")
MOV_CODECS = ("prores_422hq", "prores_4444")


def _tmp_path(suffix: str) -> str:
    return tempfile.NamedTemporaryFile(prefix="comfyui_bridge_video_", suffix=suffix, delete=False).name


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _set_enum_by_alias(knob: Any, aliases: tuple[str, ...]) -> str:
    values = [str(v) for v in list(knob.values())]
    for alias in aliases:
        a = _norm(alias)
        for value in values:
            v = _norm(value)
            if a and (a in v or v in a):
                knob.setValue(value)
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
    if k is None:
        return
    _set_enum_by_alias(k, ("mov", "mov64", "movie", "quicktime", "quicktime/mov"))


def _set_movie_codec(write: Any, fmt: str, mov_codec: str) -> str:
    if fmt == "mp4":
        codec_aliases = ("H.264", "h264", "h.264", "avc", "mpeg4avc", "x264")
        profile_aliases = ("High 4:2:0 8-bit", "high4208bit", "high")
        quality_aliases = ("High",)
    elif mov_codec == "prores_4444":
        codec_aliases = ("Apple ProRes", "prores", "appleprores", "ap4h")
        profile_aliases = ("ProRes 4:4:4:4 12-bit", "prores444412bit", "prores4444")
        quality_aliases = ()
    else:
        codec_aliases = ("Apple ProRes", "prores", "appleprores", "apch")
        profile_aliases = ("ProRes 4:2:2 HQ 10-bit", "prores422hq10bit", "prores422hq", "proreshq")
        quality_aliases = ()

    codec = _set_first_matching_knob(write, ("Codec", "codec", "mov_codec", "mov64_codec", "video_codec", "compression"), codec_aliases)
    profile = _set_first_matching_knob(write, ("Codec Profile", "codec_profile", "profile", "mov_profile", "video_profile"), profile_aliases, required=False)
    quality = _set_first_matching_knob(write, ("Quality", "quality"), quality_aliases, required=False) if quality_aliases else ""
    return ", ".join(v for v in (codec, profile, quality) if v)


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
    write = nuke.nodes.Write(inputs=[input_node])
    try:
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


def export_video_bundle(bridge_node: Any, frame_start: int, frame_end: int, fps: float, fmt: str, mov_codec: str, colorspace: str) -> Dict[str, Any]:
    fmt = fmt if fmt in VIDEO_FORMATS else "mp4"
    mov_codec = mov_codec if mov_codec in MOV_CODECS else "prores_422hq"
    suffix = ".mov" if fmt == "mov" else ".mp4"
    main_path = _tmp_path(suffix)
    mask_path = _tmp_path(".mp4")
    nuke: Any = napi._nuke

    def _export() -> Dict[str, Any]:
        src = bridge_node.input(0)
        if src is None:
            raise napi.NukeError("ComfyUIBridge input 0 is not connected")
        temp_nodes: list[Any] = []
        try:
            _write_movie(nuke, src, main_path, frame_start, frame_end, fmt, mov_codec, colorspace)
            mask_source = str(napi.knob_value(bridge_node, "mask_source") or "source alpha")
            mask_node = _mask_chain(nuke, bridge_node, mask_source, temp_nodes)
            _write_movie(nuke, mask_node, mask_path, frame_start, frame_end, "mp4", "h264", "")
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
    return {"main_path": main_path, "mask_path": mask_path, "metadata": meta}


def _unique_path(output_directory: str, prefix: str, ext: str) -> str:
    os.makedirs(output_directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(output_directory, f"{prefix}_{stamp}_{os.getpid()}.{ext}")


def save_video_result(body: bytes, output_directory: str, filename_prefix: str, fmt: str, frame_start: int, frame_end: int, colorspace: str, bridge_node: Any, create_read: bool) -> str:
    ext = "mov" if fmt == "mov" else "mp4"
    path = _unique_path(output_directory, filename_prefix or "comfy_video_result", ext)
    with open(path, "wb") as fh:
        fh.write(body)
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
