"""Persistent global settings for the bridge.

Delegates to `ai_config` for schema v2 load/save while preserving the existing
public API (DEFAULT_SETTINGS, SETTINGS_PATH, load_settings, save_settings) used
by comfyui_bridge callers.
"""

from __future__ import annotations

from typing import Any, Dict

# Re-export everything from ai_config so existing callers work unchanged.
from ai_config import (  # noqa: F401
    SETTINGS_PATH,
    DEFAULT_SETTINGS,
    load_settings,
    save_settings,
)
