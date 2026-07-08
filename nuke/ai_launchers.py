"""Nuke GUI launchers for external AI apps (Sammie-Roto, LTX Desktop).

Uses ai_config for path resolution — no hardcoded /home paths. Stdlib-only.
Safe to import even if apps are missing; missing apps show nuke.message.
"""

from __future__ import annotations

import os
import subprocess

from ai_config import (
    health_report,
    resolve_ltx_root,
    resolve_sammie_root,
    sammie_launcher,
    ltx_command,
    load_settings,
    save_settings,
    SETTINGS_PATH,
    tool_env,
)


def _selected_file_path() -> str:
    """Return a resolved file path from the first selected node's file knob, or ''."""
    try:
        import nuke
        for node in nuke.selectedNodes():
            fk = node.knob("file")
            if fk is not None:
                val = str(fk.value() or "").strip()
                if val:
                    try:
                        resolved = nuke.filename(node)
                        if resolved:
                            return resolved
                    except Exception:
                        pass
                    return val
    except Exception:
        pass
    return ""


def launch_sammie_roto() -> None:
    """Launch Sammie-Roto, optionally passing the selected node's file path."""
    import nuke
    root = resolve_sammie_root()
    launcher = sammie_launcher(root)
    if not os.path.isfile(launcher):
        nuke.message("Sammie-Roto launcher not found.\n"
                     f"Expected: {launcher}\n"
                     "Set SAMMIE_ROOT env or run the installer.")
        return
    filepath = _selected_file_path()
    cmd = [launcher]
    if filepath:
        cmd.append(filepath)
    try:
        env = tool_env()
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c"] + cmd, cwd=root, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["bash"] + cmd, cwd=root, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        nuke.message(f"Failed to launch Sammie-Roto:\n{e}")


def launch_ltx_desktop() -> None:
    """Launch LTX Desktop (binary if built, else pnpm dev). No CLI/prepopulate."""
    import nuke
    root = resolve_ltx_root()
    cmd = ltx_command(root)
    if cmd is None:
        nuke.message("LTX Desktop not found.\n"
                     f"Expected root: {root}\n"
                     "Set LTX_ROOT env or run the installer.")
        return
    try:
        subprocess.Popen(cmd, cwd=root if os.path.isdir(root) else None, env=tool_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        nuke.message(f"Failed to launch LTX Desktop:\n{e}")
    nuke.message(
        "LTX Desktop started.\n\n"
        "Note: LTX Desktop has no CLI or prepopulate support for "
        "RGB/Alpha/IC-LoRA. This launcher only starts the app."
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
