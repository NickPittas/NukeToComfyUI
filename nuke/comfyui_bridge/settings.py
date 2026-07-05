"""Persistent global settings for the bridge.

One JSON file at ~/.nuke/comfyui_bridge/settings.json holds host/port/output
directory defaults. One Nuke process owns one bound HTTP server; node knobs may
display/edit these but changing host/port needs a Nuke restart.
"""

from __future__ import annotations

import json
import os
from threading import RLock
from typing import Any, Dict

_LOCK = RLock()


def _settings_dir() -> str:
    nuke_home = os.environ.get("NUKE_HOME_DIR") or os.path.join(
        os.path.expanduser("~"), ".nuke"
    )
    return os.path.join(nuke_home, "comfyui_bridge")


SETTINGS_PATH = os.path.join(_settings_dir(), "settings.json")


def _default_output_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "comfyui_bridge_results")


DEFAULT_SETTINGS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "output_directory": _default_output_dir(),
}


def load_settings() -> Dict[str, Any]:
    """Read settings from disk, deep-merged onto DEFAULT_SETTINGS."""
    with _LOCK:
        merged = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                merged.update(data)
        except (OSError, ValueError):
            pass
        # Always normalize an absolute output directory.
        out = merged.get("output_directory") or DEFAULT_SETTINGS["output_directory"]
        merged["output_directory"] = os.path.expanduser(str(out))
        return merged


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Persist settings to disk and return the normalized merged dict."""
    with _LOCK:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(settings or {})
        out = merged.get("output_directory") or DEFAULT_SETTINGS["output_directory"]
        merged["output_directory"] = os.path.expanduser(str(out))
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, sort_keys=True)
        os.replace(tmp, SETTINGS_PATH)
        return merged
