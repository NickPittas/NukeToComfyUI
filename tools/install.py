#!/usr/bin/env python3
"""Cross-platform stdlib-only installer for the Nuke <-> ComfyUI bridge.

Usage:
    python tools/install.py                 # interactive terminal menu
    python tools/install.py --dry-run --yes # non-interactive preview
    python tools/install.py --log install.log

Functions are importable for testing without running the menu.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

# Add nuke/ to sys.path so ai_config is importable.
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TOOLS_DIR)
_NUKE_DIR = os.path.join(_REPO_ROOT, "nuke")
if _NUKE_DIR not in sys.path:
    sys.path.insert(0, _NUKE_DIR)

from ai_config import (  # noqa: E402
    INIT_MARK_BEGIN,
    INIT_MARK_END,
    SETTINGS_PATH,
    comfyui_node_source,
    dirs_match,
    health_report,
    init_has_plugin,
    load_settings,
    normalize_path,
    paths_equal,
    resolve_comfyui_root,
    save_settings,
)

_MARK_BEGIN = INIT_MARK_BEGIN
_MARK_END = INIT_MARK_END


# --- helpers --------------------------------------------------------------

def _log(msg: str, log_fh: Any = None) -> None:
    print(msg)
    if log_fh:
        log_fh.write(msg + "\n")
        log_fh.flush()


def _run(cmd: list, cwd: str | None = None, log_fh: Any = None,
         timeout: int = 60) -> tuple[int, str, str]:
    """Run subprocess, return (returncode, stdout, stderr). Readable errors."""
    import subprocess
    _log(f"  $ {' '.join(cmd)}", log_fh)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"timeout after {timeout}s"


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def _confirm(prompt: str, default: bool = False) -> bool:
    d = "y" if default else "n"
    try:
        val = input(f"{prompt} (y/n) [{d}]: ").strip().lower()
    except EOFError:
        val = ""
    if not val:
        return default
    return val in ("y", "yes")


def _tk_status() -> tuple[bool, str]:
    """(tkinter_importable, detail). Import-only probe: never opens a display,
    so actual dialog availability stays unverified until one is opened."""
    try:
        import tkinter  # noqa: F401
    except Exception as e:
        return False, f"tkinter import failed ({e})"
    return True, ("tkinter importable; dialog availability unverified "
                  "until a picker is opened")


def _tk_install_help() -> str:
    """Tell the user how to install Tk for this Python; never elevate privileges."""
    if sys.platform == "win32":
        action = ("Modify your matching Python installation using the python.org "
                  "installer and enable 'tcl/tk and IDLE'.")
    elif sys.platform == "darwin":
        if any(part in sys.base_prefix.lower() for part in ("homebrew", "cellar")):
            action = f"Run in your terminal: brew install python-tk@{sys.version_info.major}.{sys.version_info.minor}"
        else:
            action = ("Install a matching Python distribution with Tcl/Tk support "
                      "from python.org, and run this installer with that Python.")
    else:
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        distro = release.get("ID", "")
        family = {distro, *release.get("ID_LIKE", "").split()}
        if distro == "omarchy":
            command = "omarchy pkg add tk"
        elif "arch" in family:
            command = "sudo pacman -S --needed tk"
        elif family & {"debian", "ubuntu"}:
            command = "sudo apt install python3-tk"
        elif family & {"fedora", "rhel", "centos"}:
            command = "sudo dnf install python3-tkinter"
        elif family & {"suse", "opensuse", "opensuse-tumbleweed", "opensuse-leap"}:
            command = "sudo zypper install python3-tk"
        elif "alpine" in family:
            command = "sudo apk add py3-tkinter"
        else:
            command = ""
        action = (f"Run in your terminal: {command}" if command else
                  "Use your OS package manager to install Tcl/Tk and the tkinter "
                  "binding matching your Python version.")
        action += ("\nFor a custom-built/pyenv Python, installing OS packages may not "
                   "be enough: rebuild that Python with Tcl/Tk development libraries "
                   "or use your OS Python with tkinter support.")
    return (f"Folder browsing needs Tk for Python {sys.executable}.\n"
            f"Please install it yourself:\n{action}\n"
            "Then restart this installer. Until then, type paths manually.\n"
            "No OS packages are installed automatically, including with --yes or --dry-run.")


def pick_directory(title: str, initial: str = "") -> str | None:
    """Open a Tk file dialog (an external dialog window) to pick a directory.

    Not the OS-native file manager and not a full-screen TUI: a small Tk
    dialog. Returns the chosen path, "" if the user cancelled the dialog, or
    None when the dialog could not be opened (tkinter missing, or no working
    display) so the caller can fall back to a typed path. The Tk root is
    always destroyed, including when the dialog itself fails.
    """
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return None  # tkinter not installed -> typed fallback
    root = None
    try:
        root = tkinter.Tk()
        root.withdraw()
        chosen = filedialog.askdirectory(
            title=title, initialdir=initial or os.path.expanduser("~"),
            parent=root, mustexist=True)
    except Exception:
        return None  # no display or dialog error -> typed fallback
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    return normalize_path(chosen) if chosen else ""


def _ask_path(label: str, current: str) -> str:
    """Get a directory path: try a Tk file dialog, else prompt for a typed path."""
    picked = pick_directory(f"Select {label}", current)
    if picked is None:
        tk_ok, detail = _tk_status()
        if tk_ok:
            print("Folder dialog could not open. Tk imports, but a working graphical "
                  "display is needed; type the path instead.")
        else:
            print(detail)
            print(_tk_install_help())
        return normalize_path(_ask(label, current))
    if picked == "":  # dialog opened but user cancelled -> keep current
        return current
    return picked


# --- ComfyUI root resolution / validation -----------------------------------

def _comfyui_root_error(root: str) -> str:
    """Return "" when *root* is a ComfyUI engine directory, else why not.

    An engine directory contains main.py and a comfy/ subfolder (standard
    checkout, manual install, or the inner ComfyUI folder of a portable
    install). Deliberately never scans the disk for candidate installs.
    """
    if not root or not str(root).strip():
        return "no path given (select the ComfyUI folder that contains main.py and comfy/)"
    root = normalize_path(root)
    if not os.path.exists(root):
        return "path does not exist"
    if not os.path.isdir(root):
        return "path is not a directory"
    has_main = os.path.isfile(os.path.join(root, "main.py"))
    has_comfy = os.path.isdir(os.path.join(root, "comfy"))
    if not (has_main and has_comfy):
        return ("not a ComfyUI engine directory: it must contain main.py and a "
                "comfy/ folder; for portable installs select the inner ComfyUI "
                "folder (e.g. ComfyUI_windows_portable/ComfyUI), not the wrapper")
    return ""


def _describe_root(settings: dict) -> tuple[str, str]:
    """(effective_root, note) for env > stored settings resolution.

    The note is non-empty when COMFYUI_ROOT overrides the stored selection, so
    callers can report it instead of silently installing/checking one root
    while the user picked another.
    """
    env = os.environ.get("COMFYUI_ROOT", "").strip()
    stored = str(settings.get("comfyui_root") or "").strip()
    root = resolve_comfyui_root(settings)
    if env and normalize_path(env) != normalize_path(stored):
        note = (f"COMFYUI_ROOT is set ({normalize_path(env)}); it overrides the "
                f"stored setting ({stored or 'none'}) and is used for install "
                f"and checks.")
        return root, note
    return root, ""


def _ask_validated_root(settings: dict, log_fh: Any = None) -> str:
    """Interactively pick/validate a ComfyUI root; "" leaves it unset (Nuke-only).

    Re-prompts until the answer is empty or a valid engine directory.
    """
    while True:
        default = settings.get("comfyui_root", "") or resolve_comfyui_root(settings)
        val = _ask_path("ComfyUI root", default)
        if not val:
            return ""
        err = _comfyui_root_error(val)
        if not err:
            return normalize_path(val)
        _log(f"  Invalid ComfyUI root: {val} ({err})", log_fh)


# --- preflight -------------------------------------------------------------

def preflight(dry_run: bool = False, log_fh: Any = None) -> dict:
    """Check platform, Python, git, Nuke home, disk. Returns dict."""
    info: dict = {"platform": platform.system(), "python": sys.version.split()[0]}
    _log("\n=== Preflight ===", log_fh)
    _log(f"  Platform: {info['platform']}", log_fh)
    _log(f"  Python:   {info['python']}", log_fh)
    _log(f"  Repo:     {_REPO_ROOT}", log_fh)

    rc, _, _ = _run(["git", "--version"], log_fh=log_fh, timeout=10)
    info["git"] = rc == 0
    _log(f"  Git:      {'ok' if info['git'] else 'MISSING'}", log_fh)

    nuke_home = os.environ.get("NUKE_HOME_DIR") or os.path.join(
        os.path.expanduser("~"), ".nuke")
    info["nuke_home"] = nuke_home
    info["nuke_home_exists"] = os.path.isdir(nuke_home)
    _log(f"  Nuke home: {nuke_home} ({'exists' if info['nuke_home_exists'] else 'absent'})", log_fh)

    tk_ok, tk_detail = _tk_status()
    info["tkinter"] = tk_ok
    info["file_picker"] = "unverified" if tk_ok else "unavailable"
    _log(f"  Tk picker:  {info['file_picker']} ({tk_detail})", log_fh)
    if not tk_ok:
        _log(_tk_install_help(), log_fh)

    return info


# --- configure paths -------------------------------------------------------

def configure_paths(settings: dict, dry_run: bool = False,
                    yes: bool = False, log_fh: Any = None) -> dict:
    """Select the ComfyUI root, update settings dict.

    The effective root is COMFYUI_ROOT (environment) when set, else the stored
    setting. An active environment override is reported, never silently
    applied. Non-interactive (--yes/--dry-run) never prompts: it normalizes
    and reports. Interactive answers are validated: empty leaves the root
    unset (Nuke-only install is still possible); anything else must be a
    ComfyUI engine directory (contains main.py and comfy/).
    """
    _log("\n=== Configure Paths ===", log_fh)
    settings["comfyui_root"] = normalize_path(settings.get("comfyui_root", ""))
    root, note = _describe_root(settings)
    if note:
        _log(f"  NOTE: {note}", log_fh)
    if dry_run or yes:
        _log(f"  ComfyUI root (effective): {root or 'not configured (ComfyUI steps will be reported incomplete)'}",
             log_fh)
        return settings

    val = _ask_validated_root(settings, log_fh)
    settings["comfyui_root"] = normalize_path(val)
    if val:
        _log(f"  ComfyUI root: {settings['comfyui_root']}", log_fh)
        _, note = _describe_root(settings)
        if note:
            _log(f"  NOTE: {note}", log_fh)
    else:
        # Empty selection: recompute the effective root — COMFYUI_ROOT (if set)
        # is still what install/health will use, so never claim Nuke-only here.
        cr, note = _describe_root(settings)
        if note:
            _log("  ComfyUI root: (nothing stored here — COMFYUI_ROOT stays "
                 "effective for install and health)", log_fh)
            _log(f"  NOTE: {note}", log_fh)
        else:
            _log("  ComfyUI root: (not set — Nuke-only install; ComfyUI steps "
                 "will be reported incomplete)", log_fh)
    return settings


# --- Nuke init patch -------------------------------------------------------

def _init_block() -> str:
    """The managed pluginAddPath block (rebuilt fresh each time)."""
    return f"{_MARK_BEGIN}\nimport nuke\nnuke.pluginAddPath(r\"{_NUKE_DIR}\")\n{_MARK_END}\n"


def install_nuke_plugin(dry_run: bool = False, log_fh: Any = None) -> bool:
    """Patch ~/.nuke/init.py with marked pluginAddPath block.

    Detects the plugin by parsing actual ``nuke.pluginAddPath(...)`` args and
    requiring an exact path match. A marker block with a wrong path is repaired
    (replaced); a missing block is appended. Never false-OKs on ``import nuke``.
    """
    nuke_home = os.environ.get("NUKE_HOME_DIR") or os.path.join(
        os.path.expanduser("~"), ".nuke")
    init_path = os.path.join(nuke_home, "init.py")
    _log("\n=== Install Nuke Plugin ===", log_fh)
    _log(f"  init.py: {init_path}", log_fh)

    try:
        existing = Path(init_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    except OSError as e:
        _log(f"  ERROR: cannot read init.py: {e}", log_fh)
        return False

    if init_has_plugin(existing, _NUKE_DIR):
        _log("  Already installed (exact pluginAddPath match).", log_fh)
        return True

    has_marker = _MARK_BEGIN in existing and _MARK_END in existing
    if has_marker:
        _log("  Marker present but pluginAddPath path wrong -> will repair.", log_fh)

    if dry_run:
        action = "repair marked block" if has_marker else "append marked block"
        _log(f"  [DRY-RUN] Would {action} to init.py", log_fh)
        return True

    os.makedirs(nuke_home, exist_ok=True)
    if existing:
        bak = init_path + f".bak-{int(time.time())}"
        try:
            shutil.copy2(init_path, bak)
            _log(f"  Backup: {bak}", log_fh)
        except OSError as e:
            _log(f"  ERROR: backup failed: {e}", log_fh)
            return False

    block = _init_block()
    try:
        if has_marker:
            # Repair: replace the stale marked block with the FULL fresh block.
            # Replacement via a function keeps path backslashes literal (re.sub
            # treats `\` in string replacements specially).
            pattern = re.escape(_MARK_BEGIN) + r".*?" + re.escape(_MARK_END) + r"\n?"
            new_content = re.sub(pattern, lambda m: block, existing,
                                 count=1, flags=re.DOTALL)
        else:
            new_content = existing
            if existing and not existing.endswith("\n"):
                new_content += "\n"
            new_content += block
        with open(init_path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        _log("  Repaired marked block." if has_marker else "  Appended pluginAddPath block.", log_fh)
        return True
    except OSError as e:
        _log(f"  ERROR: cannot write init.py: {e}", log_fh)
        return False


# --- ComfyUI link ----------------------------------------------------------

def _create_link_or_copy(source: str, dest: str, log_fh: Any) -> str:
    """Symlink source->dest, falling back to a copy. Returns mode or ''."""
    try:
        os.symlink(source, dest)
        _log(f"  Symlinked: {dest}", log_fh)
        return "symlink"
    except OSError:
        pass
    try:
        shutil.copytree(source, dest)
        _log(f"  Copied (symlink failed): {dest}", log_fh)
        return "copy"
    except OSError as e:
        _log(f"  ERROR: copy failed: {e}", log_fh)
        return ""


def _backup_existing(dest: str, log_fh: Any) -> bool:
    """Move dest (dir/file/symlink) to a timestamped backup. Returns success."""
    bak = dest + f".bak-{int(time.time())}"
    try:
        shutil.move(dest, bak)
        _log(f"  Backed up existing to {bak}", log_fh)
        return True
    except OSError as e:
        _log(f"  ERROR: backup failed: {e}", log_fh)
        return False


def link_comfyui_node(comfyui_root: str, dry_run: bool = False,
                      yes: bool = False, log_fh: Any = None) -> str:
    """Link/copy the ComfyUI custom node into ``<comfyui_root>/custom_nodes``.

    Lifecycle (no mutation before dry-run handling):
      - correct symlink            -> ok
      - matching copy              -> ok
      - wrong symlink / stale copy / file -> backup + replace after confirm/--yes
      - nothing at dest            -> create (after confirm unless --yes)
    Returns mode ("symlink"|"copy") or "" on skip/failure.
    """
    _log("\n=== Link ComfyUI Custom Node ===", log_fh)
    source = comfyui_node_source()
    comfyui_root = normalize_path(comfyui_root)
    custom_nodes = os.path.join(comfyui_root, "custom_nodes")
    dest = os.path.join(custom_nodes, "nuke_bridge")

    if not os.path.isdir(source):
        _log(f"  ERROR: source not found: {source}", log_fh)
        return ""
    err = _comfyui_root_error(comfyui_root)
    if err:
        _log(f"  ERROR: invalid ComfyUI root {comfyui_root!r}: {err}", log_fh)
        return ""

    if not os.path.lexists(dest):
        if dry_run:
            _log(f"  [DRY-RUN] Would create {custom_nodes} and link {source} -> {dest}", log_fh)
            return "symlink"
        if not yes and not _confirm(f"  Create custom node link at {dest}?"):
            _log("  Skipped (no link created).", log_fh)
            return ""
        os.makedirs(custom_nodes, exist_ok=True)
        return _create_link_or_copy(source, dest, log_fh)

    if os.path.islink(dest):
        if paths_equal(os.path.realpath(dest), source):
            _log(f"  Already symlinked (correct): {dest}", log_fh)
            return "symlink"
        action = f"replace wrong symlink target {dest} -> {source}"
        planned = "symlink"
    elif os.path.isdir(dest):
        if dirs_match(source, dest):
            _log(f"  Existing copy matches source: {dest}", log_fh)
            return "copy"
        action = f"back up stale copy {dest} and replace with link to {source}"
        planned = "symlink"
    else:
        action = f"back up existing file {dest} and replace with link to {source}"
        planned = "symlink"

    if dry_run:
        _log(f"  [DRY-RUN] Would: {action}", log_fh)
        return planned
    if not yes and not _confirm(f"  {dest} needs replacement. {action}?"):
        _log("  Skipped (existing entry kept).", log_fh)
        return ""
    os.makedirs(custom_nodes, exist_ok=True)
    if not _backup_existing(dest, log_fh):
        return ""
    return _create_link_or_copy(source, dest, log_fh)


# --- health check ----------------------------------------------------------

def do_health_check(comfyui_root: str | None = None, log_fh: Any = None) -> None:
    """Print bridge health report from filesystem checks.

    comfyui_root is the caller's current effective root (env > in-memory
    settings, "" meaning deliberately unset); it is passed straight through to
    health_report() so the report never reloads stored settings behind the
    caller's back.
    """
    _log("\n=== Health Check ===", log_fh)
    for item in health_report(comfyui_root=comfyui_root):
        icon = "OK" if item["status"] == "ok" else "!!"
        _log(f"  [{icon}] {item['name']}: {item['status']}", log_fh)
        if item["status"] != "ok":
            _log(f"      {item['detail']}", log_fh)


# --- settings save ---------------------------------------------------------

def save_and_log(settings: dict, dry_run: bool = False, log_fh: Any = None) -> None:
    if dry_run:
        _log(f"\n[DRY-RUN] Would save settings to {SETTINGS_PATH}", log_fh)
        _log(f"  {json.dumps(settings, indent=2)}", log_fh)
    else:
        save_settings(settings)
        _log(f"\nSaved settings to {SETTINGS_PATH}", log_fh)


# --- terminal menu -----------------------------------------------------------

_MENU_ITEMS = [
    ("Preflight checks", "preflight"),
    ("Configure ComfyUI path", "configure"),
    ("Install Nuke plugin (init.py)", "nuke"),
    ("Link ComfyUI custom node", "comfyui"),
    ("Health check", "health"),
    ("Save settings", "save"),
    ("Run all (install everything)", "all"),
    ("Quit", "quit"),
]


def _run_all(settings: dict, dry_run: bool = False, yes: bool = False,
             log_fh: Any = None) -> tuple[dict, dict]:
    """Preflight + configure + all install steps. Returns (settings, results).

    The effective ComfyUI root (env > settings) is validated BEFORE any
    mutation. When it is unset or not a valid engine directory nothing is
    written (no init.py edit, no settings save, no link), both results are
    False, and callers must report incomplete. Nuke-only installation stays
    available through the explicit "Install Nuke plugin" menu item.
    """
    preflight(dry_run=dry_run, log_fh=log_fh)
    settings = configure_paths(settings, dry_run=dry_run, yes=yes, log_fh=log_fh)
    cr, note = _describe_root(settings)
    if note:
        _log(f"  NOTE: {note}", log_fh)
    err = _comfyui_root_error(cr)
    if err:
        if cr:
            _log(f"\n  ERROR: ComfyUI root invalid: {cr}: {err}", log_fh)
        else:
            _log("\n  ComfyUI: root not configured.", log_fh)
        _log("  Run all halted before any writes (init.py, settings and link "
             "untouched). Configure a valid ComfyUI root, or use the individual "
             "menu items for a Nuke-only install.", log_fh)
        return settings, {"nuke": False, "comfyui": False}

    results: dict[str, bool] = {}
    results["nuke"] = install_nuke_plugin(dry_run=dry_run, log_fh=log_fh)
    mode = link_comfyui_node(cr, dry_run=dry_run, yes=yes, log_fh=log_fh)
    settings["comfyui_node_mode"] = mode
    results["comfyui"] = mode != ""
    save_and_log(settings, dry_run=dry_run, log_fh=log_fh)
    do_health_check(comfyui_root=cr, log_fh=log_fh)
    return settings, results


def _menu_action(choice: str) -> str | None:
    """Map a menu number (1..len) to its action name; None for anything else
    (0, negative, non-numeric, out of range — all invalid, dispatch nothing)."""
    try:
        idx = int(choice)
    except (TypeError, ValueError):
        return None
    if 1 <= idx <= len(_MENU_ITEMS):
        return _MENU_ITEMS[idx - 1][1]
    return None


def _interactive_menu(settings: dict, log_fh: Any = None) -> int:
    """Terminal menu loop (not a full-screen TUI). Returns the exit code."""
    while True:
        print("\n=== Nuke <-> ComfyUI Bridge Installer ===")
        for i, (label, _) in enumerate(_MENU_ITEMS, 1):
            print(f"  {i}. {label}")
        try:
            choice = input("\nChoice: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if not choice:
            continue
        action = _menu_action(choice)
        if action is None:
            print("Invalid choice.")
            continue

        if action == "quit":
            return 0
        elif action == "preflight":
            preflight(log_fh=log_fh)
        elif action == "configure":
            settings = configure_paths(settings, log_fh=log_fh)
        elif action == "nuke":
            install_nuke_plugin(log_fh=log_fh)
        elif action == "comfyui":
            cr, note = _describe_root(settings)
            if note:
                _log(f"  NOTE: {note}", log_fh)
            if not cr:
                cr = _ask_validated_root(settings, log_fh)
                if not cr:
                    _log("  No ComfyUI root given; nothing linked.", log_fh)
                    continue
                settings["comfyui_root"] = cr
            mode = link_comfyui_node(cr, log_fh=log_fh)
            if mode:
                settings["comfyui_node_mode"] = mode
        elif action == "health":
            cr, note = _describe_root(settings)
            if note:
                _log(f"  NOTE: {note}", log_fh)
            do_health_check(comfyui_root=cr, log_fh=log_fh)
        elif action == "save":
            save_and_log(settings, log_fh=log_fh)
        elif action == "all":
            settings, _ = _run_all(settings, log_fh=log_fh)
    return 0  # unreachable


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Nuke <-> ComfyUI bridge installer")
    parser.add_argument("--dry-run", action="store_true", help="preview without changes")
    parser.add_argument("--yes", action="store_true", help="auto-confirm prompts")
    parser.add_argument("--log", type=str, default=None, help="log file path")
    args = parser.parse_args()

    log_fh = None
    if args.log:
        try:
            log_fh = open(args.log, "a", encoding="utf-8")
            _log(f"=== Install session {time.strftime('%Y-%m-%d %H:%M:%S')} ===", log_fh)
        except OSError as e:
            print(f"WARNING: cannot open log file {args.log}: {e}", file=sys.stderr)
            log_fh = None

    settings = load_settings()
    exit_code = 0
    try:
        if args.yes or args.dry_run:
            settings, results = _run_all(settings, dry_run=args.dry_run,
                                         yes=args.yes, log_fh=log_fh)
            failed = [name for name, ok in results.items() if not ok]
            if failed:
                _log(f"\nINCOMPLETE: failed steps: {', '.join(failed)}", log_fh)
                _log("Dry-run preview only; nothing was changed." if args.dry_run
                     else "Fix the reported steps and re-run the installer.", log_fh)
                exit_code = 1
            else:
                _log("\nDry-run preview OK; nothing was changed." if args.dry_run
                     else "\nAll install steps succeeded.", log_fh)
        else:
            tk_ok, detail = _tk_status()
            if not tk_ok:
                _log(detail, log_fh)
                _log(_tk_install_help(), log_fh)
            exit_code = _interactive_menu(settings, log_fh)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        exit_code = 130
    except OSError as e:
        print(f"\nERROR: I/O failure: {e}", file=sys.stderr)
        exit_code = 1
    finally:
        if log_fh:
            try:
                log_fh.close()
            except OSError:
                pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
