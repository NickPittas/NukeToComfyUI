#!/usr/bin/env python3
"""Assert ComfyUIBridge UI layout without Nuke."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "nuke"))
from ai_config import DEFAULT_SETTINGS
from comfyui_bridge import comfy_progress, napi, node, node_settings, workflow_selection


class Knob:
    def __init__(self, name, label="", values=None, klass=""):
        self.name = name
        self.label = label
        self.values = list(values or [])
        self._value = None
        self.width = None
        self.flags = []
        self.tooltip = ""
        self.klass = klass

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = value

    def setWidth(self, width):
        self.width = width

    def setFlag(self, flag):
        self.flags.append(flag)

    def setLabel(self, label):
        self.label = label

    def setTooltip(self, tooltip):
        self.tooltip = tooltip


class _FakeServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port


class FakeNuke:
    STARTLINE = "STARTLINE"

    def __getattr__(self, name):
        if not name.endswith("_Knob"):
            raise AttributeError(name)
        return lambda knob_name, label="", values=None: Knob(
            knob_name, label, values, klass=name
        )


class FakeNode:
    def __init__(self):
        self.knobs = {}

    def addKnob(self, knob):
        self.knobs[knob.name] = knob

    def knob(self, name):
        return self.knobs.get(name)


def _build_specs():
    fake_nuke = FakeNuke()
    fake_node = FakeNode()
    specs = node._knob_specs()
    built = []
    for name, builder in specs:
        knob = builder(fake_nuke)
        built.append((name, knob))
        if knob is not None:
            fake_node.addKnob(knob)
    return specs, built, fake_nuke, fake_node


def check_tab_layout():
    specs, built, _, fake_node = _build_specs()
    names = [name for name, _ in specs]
    tabs = [name for name, knob in built if knob is not None and knob.klass == "Tab_Knob"]
    assert tabs == ["Image", "Video", "ComfyUI", "Logs"], tabs

    assert names.index("prompt") < names.index("Video")
    assert names.index("mask_source") < names.index("Video")
    assert names.index("workflow_choices") < names.index("Video")
    assert names.index("run_selected_workflow") < names.index("Video")

    assert names.index("Video") < names.index("video_prompt") < names.index("ComfyUI")
    assert names.index("video_prompt") < names.index("video_mask_source") < names.index("video_format")
    assert fake_node.knob("video_mask_source").values == list(node.MASK_SOURCES)
    assert names.index("video_workflow_choices") < names.index("ComfyUI")
    assert names.index("run_selected_workflow_video") < names.index("ComfyUI")

    assert names.index("ComfyUI") < names.index("bridge_id") < names.index("Logs")
    assert names.index("bridge_host") < names.index("Logs")
    assert names.index("host") < names.index("bridge_host")
    assert names.index("refresh_workflows") < names.index("Logs")
    assert names.index("save_defaults") < names.index("Logs")
    assert names.index("clear_frame_cache") < names.index("Logs")

    assert names.index("Logs") < names.index("status") < names.index("last_result")
    assert names.index("last_result") < names.index("log") < names.index("clear_log")

    log_knob = fake_node.knob("log")
    assert log_knob is not None
    assert log_knob.klass == "Multiline_Eval_String_Knob", log_knob.klass
    clear_command = fake_node.knob("clear_log").value()
    assert "napi.clear_log" in clear_command, clear_command

    assert fake_node.knob("prompt") is not None
    assert fake_node.knob("video_prompt") is not None
    assert fake_node.knob("workflow_choices") is not None
    assert fake_node.knob("video_workflow_choices") is not None
    assert fake_node.knob("bridge_id").label == "Bridge ID"
    assert fake_node.knob("host").label == "Listen on"
    assert fake_node.knob("bridge_host").label == "Nuke host"
    assert fake_node.knob("port").label == "Nuke port"
    assert fake_node.knob("comfyui_host").label == "ComfyUI host"
    assert fake_node.knob("comfyui_port").label == "ComfyUI port"
    assert fake_node.knob("output_directory").label == "Output folder"
    assert fake_node.knob("create_read_on_result").label == "Create Read node"


def check_run_buttons():
    _, built, fake_nuke, fake_node = _build_specs()
    names = [name for name, _ in built]
    assert names.count("run_selected_workflow") == 1
    assert names.count("run_selected_workflow_video") == 1
    assert names.index("run_selected_workflow_video") > names.index("refresh_video_colorspaces")
    image_command = fake_node.knob("run_selected_workflow").value()
    video_command = fake_node.knob("run_selected_workflow_video").value()
    assert "media_mode='image'" in image_command
    assert "media_mode='video'" in video_command
    assert image_command != video_command

    fake_node.knob("run_selected_workflow").setValue("old image command")
    fake_node.knob("run_selected_workflow_video").setValue("old video command")
    node._apply_layout(fake_node, fake_nuke)
    assert "media_mode='image'" in fake_node.knob("run_selected_workflow").value()
    assert "media_mode='video'" in fake_node.knob("run_selected_workflow_video").value()
    assert fake_node.knob("run_selected_workflow").label == "Run selected image workflow"
    assert fake_node.knob("run_selected_workflow_video").label == "Run selected video workflow"
    assert fake_node.knob("workflow_choices").width == 400
    assert fake_node.knob("video_workflow_choices").width == 400
    assert fake_node.knob("log").width == 400
    for name in ("run_selected_workflow", "run_selected_workflow_video"):
        assert FakeNuke.STARTLINE in fake_node.knob(name).flags


def check_refresh_buttons():
    """All three refresh buttons use the same known-working workflow command,
    and _apply_layout repairs saved nodes."""
    _, _, fake_nuke, fake_node = _build_specs()
    image_refresh = fake_node.knob("refresh_colorspaces")
    video_refresh = fake_node.knob("refresh_video_colorspaces")
    assert image_refresh is not None
    assert video_refresh is not None
    assert image_refresh.label == "Refresh workflows", image_refresh.label
    assert video_refresh.label == "Refresh workflows", video_refresh.label
    image_cmd = image_refresh.value()
    video_cmd = video_refresh.value()
    settings_refresh = fake_node.knob("refresh_workflows")
    assert settings_refresh is not None
    assert image_cmd == settings_refresh.value(), image_cmd
    assert video_cmd == settings_refresh.value(), video_cmd
    assert "refresh_workflow_choices" in settings_refresh.value()

    # _apply_layout repairs saved-node labels/commands and row/width flags.
    image_refresh.setLabel("old")
    video_refresh.setLabel("old")
    image_refresh.setValue("old image refresh")
    video_refresh.setValue("old video refresh")
    node._apply_layout(fake_node, fake_nuke)
    assert image_refresh.label == "Refresh workflows", image_refresh.label
    assert video_refresh.label == "Refresh workflows", video_refresh.label
    assert image_refresh.value() == settings_refresh.value()
    assert video_refresh.value() == settings_refresh.value()

    for name in (
        "refresh_colorspaces", "run_selected_workflow",
        "refresh_video_colorspaces", "run_selected_workflow_video",
        "refresh_workflows", "save_defaults", "clear_frame_cache",
        "bridge_id", "host", "bridge_host", "port", "comfyui_host",
        "comfyui_port", "output_directory", "create_read_on_result",
    ):
        assert FakeNuke.STARTLINE in fake_node.knob(name).flags, name
    for name in (
        "prompt", "video_prompt", "workflow_choices", "video_workflow_choices",
        "log", "bridge_id", "host", "bridge_host", "comfyui_host",
        "output_directory", "status", "last_result",
    ):
        assert fake_node.knob(name).width == 400, name
    # Width must not be forced on int/bool/button knobs.
    for name in (
        "port", "comfyui_port", "create_read_on_result",
        "refresh_colorspaces", "run_selected_workflow",
        "refresh_video_colorspaces", "run_selected_workflow_video",
        "refresh_workflows", "save_defaults", "clear_frame_cache",
    ):
        assert fake_node.knob(name).width is None, name


def check_sync_behavior():
    _, _, _, fake_node = _build_specs()
    prompt = fake_node.knob("prompt")
    video_prompt = fake_node.knob("video_prompt")

    prompt.setValue("a cat")
    video_prompt.setValue("old")
    node.sync_prompts(fake_node, "prompt")
    assert video_prompt.value() == "a cat"

    video_prompt.setValue("a dog")
    node.sync_prompts(fake_node, "video_prompt")
    assert prompt.value() == "a dog"

    # Recursion guard: equal values must be a no-op.
    before = (prompt.value(), video_prompt.value())
    node.sync_prompts(fake_node, "prompt")
    node.sync_prompts(fake_node, "video_prompt")
    assert (prompt.value(), video_prompt.value()) == before

    # Init convergence: prompt authoritative unless empty.
    prompt.setValue("saved prompt")
    video_prompt.setValue("")
    node.converge_prompts(fake_node)
    assert video_prompt.value() == "saved prompt"
    prompt.setValue("")
    video_prompt.setValue("legacy video prompt")
    node.converge_prompts(fake_node)
    assert prompt.value() == "legacy video prompt"


def check_media_modes():
    originals = (
        workflow_selection._selected_workflow_id,
        workflow_selection._request_json,
        napi.knob_value,
        napi.set_knob_value,
    )
    workflow_selection._selected_workflow_id = lambda _node, _mode="image": "workflow-id"
    workflow_selection._request_json = lambda *_args, **_kwargs: {"submitted": True}
    napi.knob_value = lambda _node, name: {"comfyui_host": "127.0.0.1", "comfyui_port": 8188}.get(name)
    napi.set_knob_value = lambda *_args, **_kwargs: None
    try:
        # Pull architecture: video workflows must NOT pre-render locally.
        assert not hasattr(workflow_selection, "_render_video_bundle_before_comfy"), \
            "pre-render path must be removed"
        workflow_selection._run_selected_workflow_sync(object(), media_mode="image")
        workflow_selection._run_selected_workflow_sync(object(), media_mode="video")
        try:
            workflow_selection._run_selected_workflow_sync(object(), media_mode="sequence")
            raise AssertionError("invalid media mode accepted")
        except ValueError:
            pass
    finally:
        (
            workflow_selection._selected_workflow_id,
            workflow_selection._request_json,
            napi.knob_value,
            napi.set_knob_value,
        ) = originals


def check_prompt_patch():
    values = {
        "bridge_id": "bridge-abc",
        "bridge_host": "10.0.0.5",
        "host": "0.0.0.0",  # listen/bind host must not leak into the prompt
        "port": 8765,
        "send_format": "png8",
        "send_colorspace": "raw",
        "video_format": "mov",
        "video_mov_codec": "prores_422hq",
        "video_colorspace": "rec709",
    }
    originals = (napi.knob_value,)
    napi.knob_value = lambda _node, name: values.get(name)
    try:
        prompt = {
            "1": {"class_type": "FromNuke", "inputs": {}},
            "2": {"class_type": "ToNuke", "inputs": {}},
            "3": {"class_type": "FromNukeVideo", "inputs": {}},
            "4": {"class_type": "ToNukeVideo", "inputs": {}},
        }
        patched = workflow_selection._patch_nuke_bridge_prompt(prompt, object())
        assert set(patched) == set(prompt)
        for node in patched.values():
            ins = node["inputs"]
            assert ins["bridge_id"] == "bridge-abc", ins
            assert ins["host"] == "10.0.0.5", ins
            assert ins["port"] == 8765, ins
        assert patched["1"]["inputs"]["format"] == "png8"
        assert patched["1"]["inputs"]["colorspace"] == "raw"
        assert patched["2"]["inputs"]["format"] == "png8"
        assert patched["2"]["inputs"]["colorspace"] == ""
        assert patched["3"]["inputs"]["format"] == "mov"
        assert patched["3"]["inputs"]["mov_codec"] == "prores_422hq"
        assert patched["3"]["inputs"]["colorspace"] == "rec709"
        assert patched["4"]["inputs"]["format"] == "mov"
        assert patched["4"]["inputs"]["mov_codec"] == "prores_422hq"
        assert patched["4"]["inputs"]["colorspace"] == ""

        for wildcard in ("0.0.0.0", "::", "[::]"):
            values["bridge_host"] = wildcard
            try:
                workflow_selection._patch_nuke_bridge_prompt(prompt, object())
                raise AssertionError(f"wildcard bridge_host {wildcard!r} accepted")
            except ValueError as e:
                assert wildcard in str(e), str(e)

        # Fallback: empty bridge_host uses the bind host.
        values["bridge_host"] = ""
        values["host"] = "10.0.0.9"
        patched = workflow_selection._patch_nuke_bridge_prompt(prompt, object())
        assert patched["1"]["inputs"]["host"] == "10.0.0.9", patched["1"]["inputs"]
        # ...and the bind host being a wildcard still rejects.
        values["host"] = "0.0.0.0"
        try:
            workflow_selection._patch_nuke_bridge_prompt(prompt, object())
            raise AssertionError("wildcard fallback host accepted")
        except ValueError:
            pass

        # Back to a valid bridge_host so the patcher runs normally again
        # (the wildcard-fallback block above leaves it raising).
        values["bridge_host"] = "100.64.0.2"

        # Nodes that are not NukeBridge classes are left untouched.
        prompt["9"] = {"class_type": "KSampler", "inputs": {"seed": 7}}
        patched = workflow_selection._patch_nuke_bridge_prompt(prompt, object())
        assert patched["9"] == {"class_type": "KSampler", "inputs": {"seed": 7}}
    finally:
        napi.knob_value = originals[0]


def check_selector_routing():
    values = {"bridge_id": "b1", "workflow_choices": 1, "video_workflow_choices": 2}
    originals = (napi.knob_value, workflow_selection._LAST_LIST)
    napi.knob_value = lambda _node, name: values.get(name)
    workflow_selection._LAST_LIST = {"b1": [{"id": "w0"}, {"id": "w1"}, {"id": "w2"}]}
    try:
        assert workflow_selection._selected_workflow_id(object(), "image") == "w1"
        assert workflow_selection._selected_workflow_id(object(), "video") == "w2"
        try:
            workflow_selection._selected_workflow_id(object(), "sequence")
            raise AssertionError("invalid media mode accepted")
        except ValueError:
            pass
    finally:
        napi.knob_value, workflow_selection._LAST_LIST = originals


def check_network_knob_repair():
    """Network knobs carry the current labels + tooltips, and _apply_layout
    repairs stale labels/tooltips on already-saved nodes."""
    _, _, fake_nuke, fake_node = _build_specs()
    node._apply_layout(fake_node, fake_nuke)
    for name, label in (
        ("host", "Listen on"),
        ("bridge_host", "Nuke host"),
        ("port", "Nuke port"),
        ("comfyui_host", "ComfyUI host"),
        ("comfyui_port", "ComfyUI port"),
    ):
        assert fake_node.knob(name).label == label, name
        assert fake_node.knob(name).tooltip, name

    # Saved-node repair: stale labels and missing tooltips are fixed.
    for name in ("host", "bridge_host", "port", "comfyui_host", "comfyui_port"):
        fake_node.knob(name).setLabel("old")
        fake_node.knob(name).tooltip = ""
    fake_node.knob("save_defaults").tooltip = ""
    node._apply_layout(fake_node, fake_nuke)
    for name, label in (
        ("host", "Listen on"),
        ("bridge_host", "Nuke host"),
        ("port", "Nuke port"),
        ("comfyui_host", "ComfyUI host"),
        ("comfyui_port", "ComfyUI port"),
    ):
        assert fake_node.knob(name).label == label, name
        assert fake_node.knob(name).tooltip, name
    assert fake_node.knob("save_defaults").tooltip == (
        "Save these values and restart the Nuke bridge listener immediately."
    )


def check_persistence_wiring():
    assert "bridge_host" in DEFAULT_SETTINGS
    import comfyui_bridge.server as bridge_server

    previous = {"host": "old-host", "port": 7777, "bridge_host": "old-bh"}
    saves = []
    statuses = []
    values = {
        "host": "10.0.0.1",
        "port": 9000,
        "output_directory": "/tmp/out",
        "bridge_host": "10.0.0.1",
        "comfyui_host": "192.168.1.5",
        "comfyui_port": 8199,
    }
    originals = (
        node_settings.save_settings,
        node_settings.load_settings,
        napi.knob_value,
        napi.set_knob_value,
        bridge_server.start_server,
        bridge_server.get_server,
    )
    node_settings.save_settings = lambda settings: saves.append(dict(settings)) or dict(settings)
    node_settings.load_settings = lambda: dict(previous)
    napi.knob_value = lambda _node, name: values.get(name)
    napi.set_knob_value = lambda _node, name, value: statuses.append((name, value))
    try:
        # Success path: settings persisted and listener restarted immediately.
        bridge_server.start_server = lambda: _FakeServer("10.0.0.1", 9000)
        bridge_server.get_server = lambda: None
        node_settings.save_defaults_from_node(object())
        assert saves[0].get("bridge_host") == "10.0.0.1"
        assert saves[0].get("comfyui_host") == "192.168.1.5"
        assert saves[0].get("comfyui_port") == 8199
        assert statuses[-1][1] == "saved; Nuke bridge listening on 10.0.0.1:9000", statuses[-1]

        # Failure path: disk rolled back, old listener kept, informative status.
        saves.clear()
        statuses.clear()

        def _boom(*_a, **_kw):
            raise OSError("bind: address in use")

        bridge_server.start_server = _boom
        bridge_server.get_server = lambda: _FakeServer("old-host", 7777)
        node_settings.save_defaults_from_node(object())
        assert saves[-1] == previous, saves[-1]
        assert statuses[-1][1] == (
            "save failed; bridge kept old-host:7777: bind: address in use"
        ), statuses[-1]
        assert "10.0.0.1" not in saves[-1].values()
    finally:
        (
            node_settings.save_settings,
            node_settings.load_settings,
            napi.knob_value,
            napi.set_knob_value,
            bridge_server.start_server,
            bridge_server.get_server,
        ) = originals


def check_node_label_mapping():
    labels = comfy_progress.node_labels(
        {
            "1": {"_meta": {"title": "My Sampler"}, "class_type": "KSampler"},
            "2": {"class_type": "VAEDecode"},
            "3": {},
            "10": {"_meta": {"title": "Display Title"}, "class_type": "KSampler"},
        }
    )
    assert labels["1"] == "My Sampler"
    assert labels["2"] == "VAEDecode"
    assert labels["3"] == "3"
    assert labels["10"] == "Display Title"
    assert comfy_progress.event_node_label(labels, "1") == "My Sampler"
    assert comfy_progress.event_node_label(labels, "2") == "VAEDecode"
    assert comfy_progress.event_node_label(labels, "3") == "3"
    # display_node only fills in when the prompt mapping lacks the id.
    assert comfy_progress.event_node_label(labels, "1", display_node="Other") == "My Sampler"
    assert comfy_progress.event_node_label(labels, "9", display_node="KSampler") == "KSampler"
    # display_node maps through labels when the node id itself is unknown.
    assert comfy_progress.event_node_label(labels, "ephemeral", display_node="10") == "Display Title"
    assert comfy_progress.event_node_label(labels, "9") == "9"
    assert comfy_progress.event_node_label({}, None) == ""


def check_log_helpers():
    originals = (napi.call,)
    napi.call = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    try:
        _, _, _, fake_node = _build_specs()
        napi.append_log(fake_node, "hello")
        napi.append_log(fake_node, "hello")
        assert fake_node.knob("log").value().count("\n") == 0, "duplicate collapsed"
        first = fake_node.knob("log").value()
        assert first.startswith("[") and first.endswith("INFO hello")
        napi.append_log(fake_node, "hello", level="WARN")
        assert "WARN hello" in fake_node.knob("log").value()
        for i in range(350):
            napi.append_log(fake_node, f"line {i}")
        lines = fake_node.knob("log").value().splitlines()
        assert len(lines) == 300, len(lines)
        napi.clear_log(fake_node)
        assert fake_node.knob("log").value() == ""

        # status updates mirror into the log without recursion or dupes.
        fake_node.knob("status").setValue("")
        napi.set_knob_value(fake_node, "status", "ready")
        napi.set_knob_value(fake_node, "status", "ready")
        assert fake_node.knob("status").value() == "ready"
        assert fake_node.knob("log").value().count("INFO ready") == 1
    finally:
        napi.call = originals[0]


def check_safe_set_status_routing():
    """status knobs route through napi.set_knob_value (log mirror) when Nuke is
    available; other knobs and outside-Nuke behavior stay direct."""
    _, _, _, fake_node = _build_specs()
    originals = (napi.has_nuke, napi.set_knob_value)
    routed = []
    napi.set_knob_value = lambda _node, name, value: routed.append((name, value))
    try:
        napi.has_nuke = lambda: False
        fake_node.knob("status").setValue("")
        node._safe_set(fake_node, "status", "outside")
        assert routed == []
        assert fake_node.knob("status").value() == "outside"

        napi.has_nuke = lambda: True
        fake_node.knob("status").setValue("")
        node._safe_set(fake_node, "status", "ready")
        assert routed == [("status", "ready")]
        assert fake_node.knob("status").value() == ""

        node._safe_set(fake_node, "prompt", "x")
        assert routed == [("status", "ready")]
        assert fake_node.knob("prompt").value() == "x"
    finally:
        napi.has_nuke, napi.set_knob_value = originals


def main():
    check_tab_layout()
    check_run_buttons()
    check_refresh_buttons()
    check_sync_behavior()
    check_media_modes()
    check_prompt_patch()
    check_selector_routing()
    check_persistence_wiring()
    check_network_knob_repair()
    check_node_label_mapping()
    check_log_helpers()
    check_safe_set_status_routing()
    print("check_nuke_ui_layout: PASS")


if __name__ == "__main__":
    main()
