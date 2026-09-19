"""Nuke GUI helpers: settings + health dialogs. Stdlib-only.

Uses ai_config for path resolution — no hardcoded /home paths.
"""

from __future__ import annotations

import os
import subprocess

from ai_config import (
    health_report,
    load_settings,
    save_settings,
    SETTINGS_PATH,
)


def open_settings() -> None:
    """Open the central settings JSON in the system text editor."""
    import nuke
    if not os.path.isfile(SETTINGS_PATH):
        try:
            save_settings(load_settings())
        except OSError as e:
            nuke.message(f"Could not create settings file:\n{SETTINGS_PATH}\n{e}")
            return
    try:
        if os.name == "nt":
            os.startfile(SETTINGS_PATH)  # type: ignore[attr-defined]
        elif sys_platform() == "darwin":
            subprocess.Popen(["open", "-t", SETTINGS_PATH])
        else:
            subprocess.Popen(["xdg-open", SETTINGS_PATH])
    except Exception as e:
        nuke.message(f"Could not open settings:\n{SETTINGS_PATH}\n{e}")


def show_health() -> None:
    """Show a health-check dialog summarising all components."""
    import nuke
    lines = []
    for item in health_report():
        icon = "✓" if item["status"] == "ok" else "✗"
        lines.append(f"{icon} {item['name']}: {item['status']}")
        if item["status"] != "ok":
            lines.append(f"    {item['detail']}")
    nuke.message("AI Toolchain Health Check\n\n" + "\n".join(lines))


def sys_platform() -> str:
    import sys
    return sys.platform
