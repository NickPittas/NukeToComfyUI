#!/usr/bin/env python3
"""Assert ComfyUIBridge UI layout without Nuke."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "nuke"))
from comfyui_bridge import napi, node, workflow_selection


class Knob:
    def __init__(self, name, label="", values=None):
        self.name = name
        self.label = label
        self.value = None
        self.width = None
        self.flags = []

    def setValue(self, value):
        self.value = value

    def setWidth(self, width):
        self.width = width

    def setFlag(self, flag):
        self.flags.append(flag)

    def setLabel(self, label):
        self.label = label


class FakeNuke:
    STARTLINE = "STARTLINE"

    def __getattr__(self, name):
        if not name.endswith("_Knob"):
            raise AttributeError(name)
        return lambda knob_name, label="", values=None: Knob(knob_name, label, values)


class FakeNode:
    def __init__(self):
        self.knobs = {}

    def addKnob(self, knob):
        self.knobs[knob.name] = knob

    def knob(self, name):
        return self.knobs.get(name)


def check_media_modes():
    rendered = []
    originals = (
        workflow_selection._selected_workflow_id,
        workflow_selection._render_video_bundle_before_comfy,
        workflow_selection._request_json,
        napi.knob_value,
        napi.set_knob_value,
    )
    workflow_selection._selected_workflow_id = lambda _node: "workflow-id"
    workflow_selection._render_video_bundle_before_comfy = lambda _node: rendered.append(True) or {}
    workflow_selection._request_json = lambda *_args, **_kwargs: {"submitted": True}
    napi.knob_value = lambda _node, name: {"comfyui_host": "127.0.0.1", "comfyui_port": 8188}.get(name)
    napi.set_knob_value = lambda *_args, **_kwargs: None
    try:
        workflow_selection._run_selected_workflow_sync(object(), media_mode="image")
        assert rendered == []
        workflow_selection._run_selected_workflow_sync(object(), media_mode="video")
        assert rendered == [True]
        try:
            workflow_selection._run_selected_workflow_sync(object(), media_mode="sequence")
            raise AssertionError("invalid media mode accepted")
        except ValueError:
            pass
    finally:
        (
            workflow_selection._selected_workflow_id,
            workflow_selection._render_video_bundle_before_comfy,
            workflow_selection._request_json,
            napi.knob_value,
            napi.set_knob_value,
        ) = originals


def main():
    fake_nuke = FakeNuke()
    fake_node = FakeNode()
    specs = node._knob_specs()
    for _, builder in specs:
        knob = builder(fake_nuke)
        if knob is not None:
            fake_node.addKnob(knob)

    names = [name for name, _ in specs]
    assert names.count("run_selected_workflow") == 1
    assert names.count("run_selected_workflow_video") == 1
    assert names.index("run_selected_workflow_video") > names.index("refresh_video_colorspaces")
    image_command = fake_node.knob("run_selected_workflow").value
    video_command = fake_node.knob("run_selected_workflow_video").value
    assert "media_mode='image'" in image_command
    assert "media_mode='video'" in video_command
    assert image_command != video_command

    fake_node.knob("run_selected_workflow").setValue("old image command")
    fake_node.knob("run_selected_workflow_video").setValue("old video command")
    node._apply_layout(fake_node, fake_nuke)
    assert "media_mode='image'" in fake_node.knob("run_selected_workflow").value
    assert "media_mode='video'" in fake_node.knob("run_selected_workflow_video").value
    assert fake_node.knob("run_selected_workflow").label == "Run selected image workflow"
    assert fake_node.knob("run_selected_workflow_video").label == "Run selected video workflow"
    assert fake_node.knob("workflow_choices").width == 400
    for name in ("run_selected_workflow", "run_selected_workflow_video"):
        assert FakeNuke.STARTLINE in fake_node.knob(name).flags

    check_media_modes()
    print("check_nuke_ui_layout: PASS")


if __name__ == "__main__":
    main()
