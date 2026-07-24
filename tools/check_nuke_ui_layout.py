#!/usr/bin/env python3
"""Assert ComfyUIBridge UI layout without Nuke."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "nuke"))
from comfyui_bridge import node


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
    assert fake_node.knob("run_selected_workflow").value == fake_node.knob("run_selected_workflow_video").value

    node._apply_layout(fake_node, fake_nuke)
    assert fake_node.knob("workflow_choices").width == 400
    for name in ("run_selected_workflow", "run_selected_workflow_video"):
        assert FakeNuke.STARTLINE in fake_node.knob(name).flags

    print("check_nuke_ui_layout: PASS")


if __name__ == "__main__":
    main()
