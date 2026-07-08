#!/usr/bin/env python3
"""Cross-platform stdlib-only installer for the Nuke/ComfyUI/OmniPaint/Sammie/LTX toolchain.

Usage:
    python tools/install.py                 # interactive TUI
    python tools/install.py --dry-run --yes # non-interactive preview
    python tools/install.py --log install.log

Functions are importable for testing without running the TUI.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
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
    repo_root,
    resolve_ltx_models_dir,
    save_settings,
    sammie_launcher,
    tool_env,
)
from omnipaint_models import model_report

_MARK_BEGIN = INIT_MARK_BEGIN
_MARK_END = INIT_MARK_END


# --- helpers --------------------------------------------------------------

def _log(msg: str, log_fh: Any = None) -> None:
    print(msg)
    if log_fh:
        log_fh.write(msg + "\n")
        log_fh.flush()


def _run(cmd: list, cwd: str | None = None, log_fh: Any = None,
         timeout: int = 1800, env: dict | None = None) -> tuple[int, str, str]:
    """Run subprocess, return (returncode, stdout, stderr). Readable errors."""
    _log(f"  $ {' '.join(cmd)}", log_fh)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"timeout after {timeout}s"


def _run_streaming(cmd: list, cwd: str | None = None, log_fh: Any = None,
                   timeout: int = 3600, env: dict | None = None) -> int:
    """Run a subprocess, capturing combined stdout/stderr.

    Output is printed/logged after the process exits (no live streaming), but
    the timeout actually fires for silent hangs: communicate(timeout=) enforces
    it on both output and exit. Returns the process exit code (negative on
    timeout/launch failure); captured output is logged either way so readable
    errors are preserved.
    """
    _log(f"  $ {' '.join(cmd)}", log_fh)
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )
    except FileNotFoundError:
        _log(f"  ERROR: command not found: {cmd[0]}", log_fh)
        return -1
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        # Drain whatever was buffered so logs show how far it got.
        try:
            out, _ = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out = ""
        if out:
            for line in out.splitlines():
                _log("    " + line, log_fh)
        _log(f"  ERROR: timeout after {timeout}s", log_fh)
        return -2
    if out:
        for line in out.splitlines():
            _log("    " + line, log_fh)
    rc = proc.returncode
    _log(f"  exit {rc}", log_fh)
    return rc


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


# --- preflight -------------------------------------------------------------

def preflight(dry_run: bool = False, log_fh: Any = None) -> dict:
    """Check platform, Python, git, Nuke home, disk, HF token. Returns dict."""
    info: dict = {"platform": platform.system(), "python": sys.version.split()[0]}
    _log(f"\n=== Preflight ===", log_fh)
    _log(f"  Platform: {info['platform']}", log_fh)
    _log(f"  Python:   {info['python']}", log_fh)
    _log(f"  Repo:     {_REPO_ROOT}", log_fh)

    # Git
    rc, _, _ = _run(["git", "--version"], log_fh=log_fh, timeout=10)
    info["git"] = rc == 0
    _log(f"  Git:      {'ok' if info['git'] else 'MISSING'}", log_fh)

    # Nuke home
    nuke_home = os.environ.get("NUKE_HOME_DIR") or os.path.join(
        os.path.expanduser("~"), ".nuke"
    )
    info["nuke_home"] = nuke_home
    info["nuke_home_exists"] = os.path.isdir(nuke_home)
    _log(f"  Nuke home: {nuke_home} ({'exists' if info['nuke_home_exists'] else 'absent'})", log_fh)

    # HF token
    hf_token = bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
    info["hf_token"] = hf_token
    _log(f"  HF token: {'present' if hf_token else 'absent (needed for FLUX.1-dev)'}", log_fh)

    # Disk free on repo root
    try:
        usage = shutil.disk_usage(_REPO_ROOT)
        free_gb = usage.free / (1024 ** 3)
        info["disk_free_gb"] = round(free_gb, 1)
        _log(f"  Disk free: {info['disk_free_gb']} GB", log_fh)
    except OSError:
        info["disk_free_gb"] = -1

    # Bash (for OmniPaint installer)
    if os.name != "nt":
        rc, _, _ = _run(["bash", "--version"], log_fh=log_fh, timeout=10)
        info["bash"] = rc == 0
    else:
        info["bash"] = False
    _log(f"  Bash:     {'ok' if info.get('bash') else 'absent (OmniPaint blocked on Windows)'}", log_fh)

    return info


# --- configure paths -------------------------------------------------------

def configure_paths(settings: dict, dry_run: bool = False,
                    yes: bool = False, log_fh: Any = None) -> dict:
    """Prompt for ComfyUI/Sammie/LTX roots, update settings dict.

    Non-interactive (--yes/--dry-run) never prompts: it uses existing settings
    or defaults, normalizes them, prints the chosen paths, and skips unconfigured
    optional components clearly.
    """
    _log("\n=== Configure Paths ===", log_fh)
    noninteractive = bool(dry_run or yes)
    if noninteractive:
        comfyui = normalize_path(settings.get("comfyui_root", ""))
        sammie = normalize_path(settings.get("sammie_root", "")) or os.path.join(
            os.path.expanduser("~"), "Sammie-Roto-2")
        ltx = normalize_path(settings.get("ltx_root", "")) or os.path.join(
            os.path.expanduser("~"), "LTX-Desktop")
        ltx_models = resolve_ltx_models_dir(settings)
        settings["comfyui_root"] = comfyui
        settings["sammie_root"] = sammie
        settings["ltx_root"] = ltx
        settings["ltx_models_dir"] = ltx_models
        if comfyui:
            _log(f"  ComfyUI root: {comfyui}", log_fh)
        else:
            _log("  ComfyUI root: not configured (ComfyUI steps skipped)", log_fh)
        _log(f"  Sammie root:  {sammie}", log_fh)
        _log(f"  LTX root:     {ltx}", log_fh)
        _log(f"  LTX models:   {ltx_models or '(not discovered)'}", log_fh)
        return settings

    comfyui = _ask("ComfyUI root", settings.get("comfyui_root", ""))
    settings["comfyui_root"] = normalize_path(comfyui)
    sammie_default = settings.get("sammie_root", "") or os.path.join(
        os.path.expanduser("~"), "Sammie-Roto-2")
    sammie = _ask("Sammie-Roto root", sammie_default)
    settings["sammie_root"] = normalize_path(sammie)
    ltx_default = settings.get("ltx_root", "") or os.path.join(
        os.path.expanduser("~"), "LTX-Desktop")
    ltx = _ask("LTX Desktop root", ltx_default)
    settings["ltx_root"] = normalize_path(ltx)
    ltx_models_default = settings.get("ltx_models_dir", "") or resolve_ltx_models_dir(settings)
    ltx_models = _ask("LTX models dir", ltx_models_default)
    settings["ltx_models_dir"] = normalize_path(ltx_models)
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
        os.path.expanduser("~"), ".nuke"
    )
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

    # Exact-match detection via real pluginAddPath arg parsing.
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

    # Backup
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
            # Repair: replace the stale marked block with the FULL fresh block
            # (including its trailing newline) so following code isn't glued
            # onto _MARK_END. count=1 -> only the first (stale) block is
            # touched. Replacement via a function to keep path backslashes
            # literal (re.sub treats `\` in string replacements specially).
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
    if not os.path.isdir(comfyui_root):
        _log(f"  ERROR: ComfyUI root not found: {comfyui_root}", log_fh)
        return ""

    # Nothing at dest -> fresh link/copy.
    if not os.path.lexists(dest):
        if dry_run:
            _log(f"  [DRY-RUN] Would create {custom_nodes} and link {source} -> {dest}", log_fh)
            return "symlink"
        if not yes and not _confirm(f"  Create custom node link at {dest}?"):
            _log("  Skipped (no link created).", log_fh)
            return ""
        os.makedirs(custom_nodes, exist_ok=True)
        return _create_link_or_copy(source, dest, log_fh)

    # Existing entry — classify.
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

    # Needs repair/replacement.
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


# --- OmniPaint install -----------------------------------------------------

def install_omnipaint(dry_run: bool = False, log_fh: Any = None) -> bool:
    """Call tools/install_omnipaint_local.sh on linux/macOS. Blocked on Windows."""
    _log("\n=== Install OmniPaint Backend ===", log_fh)
    script = os.path.join(_TOOLS_DIR, "install_omnipaint_local.sh")
    if os.name == "nt":
        _log("  BLOCKED: OmniPaint installer is bash/Linux-only on Windows.", log_fh)
        _log("  OmniPaint venv/repo must be set up manually.", log_fh)
        return False
    if not os.path.isfile(script):
        _log(f"  ERROR: {script} not found", log_fh)
        return False
    if dry_run:
        _log(f"  [DRY-RUN] Would run: bash {script}", log_fh)
        return True
    _log("  Running OmniPaint installer (capturing output)...", log_fh)
    rc = _run_streaming(["bash", script], cwd=_REPO_ROOT, log_fh=log_fh, timeout=3600)
    if rc != 0:
        _log(f"  PARTIAL/FAILED (exit {rc})", log_fh)
        return False
    # Validate health after script exit 0 — exit code alone never means OK.
    # Core requirements incl. a real runtime-deps import check via the adapter.
    # NF4 is a warning only (removal still works unquantized without it).
    core = ["OmniPaint venv", "OmniPaint repo", "OmniPaint LoRA",
            "OmniPaint embeddings", "FLUX.1-dev", "OmniPaint runtime deps"]
    report = {item["name"]: item for item in model_report(check_runtime=True)}
    bad = [n for n in core if report.get(n, {}).get("status") != "ok"]
    if bad:
        for n in bad:
            item = report[n]
            _log(f"  PARTIAL/BLOCKER: {n} -> {item['status']} ({item['detail']})", log_fh)
        return False
    nf4 = report.get("OmniPaint NF4", {})
    if nf4.get("status") != "ok":
        _log(f"  WARNING: OmniPaint NF4 -> {nf4.get('status')} ({nf4.get('detail')})", log_fh)
    _log("  OK", log_fh)
    return True


# --- Sammie install --------------------------------------------------------

_SAMMIE_URL = "https://github.com/Zarxrax/Sammie-Roto-2.git"
_SAMMIE_REMOTE_HINT = "Zarxrax/Sammie-Roto-2"


def _readme_mentions_sammie(root: str) -> bool:
    """True if README.md's first chunk mentions Sammie-Roto (not a plain README)."""
    readme = os.path.join(root, "README.md")
    if not os.path.isfile(readme):
        return False
    try:
        with open(readme, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return "Sammie-Roto" in head  # also covers "Sammie-Roto 2"


def _looks_like_sammie(root: str) -> bool:
    """True if *root* looks like a Sammie-Roto install (release or git clone).

    Requires launcher.py + at least one platform launcher, plus a Sammie-specific
    marker: sammie_main.py, sammie/ dir, videomama/ dir, or a README.md whose
    first chunk mentions Sammie-Roto. A plain README alone is not accepted.
    """
    if not os.path.isdir(root):
        return False
    if not os.path.isfile(os.path.join(root, "launcher.py")):
        return False
    has_launcher = (
        os.path.isfile(os.path.join(root, "run_sammie.bat"))
        or os.path.isfile(os.path.join(root, "run_sammie.command"))
        or os.path.isfile(os.path.join(root, "run_sammie.sh"))
    )
    if not has_launcher:
        return False
    return (
        os.path.isfile(os.path.join(root, "sammie_main.py"))
        or os.path.isdir(os.path.join(root, "sammie"))
        or os.path.isdir(os.path.join(root, "videomama"))
        or _readme_mentions_sammie(root)
    )


def _sammie_official_cmd(root: str) -> list | None:
    """Return official installer command for the platform, or None if absent.

    Supports upstream main (install.sh/install.bat) and the release layout
    (install_dependencies.sh/install_dependencies.bat).
    """
    if os.name == "nt":
        for name in ("install.bat", "install_dependencies.bat"):
            installer = os.path.join(root, name)
            if os.path.isfile(installer):
                return ["cmd", "/c", installer]
        return None
    for name in ("install.sh", "install_dependencies.sh"):
        installer = os.path.join(root, name)
        if os.path.isfile(installer):
            return ["bash", installer]
    return None


def install_sammie(root: str, dry_run: bool = False,
                   yes: bool = False, log_fh: Any = None) -> bool:
    """Clone Sammie-Roto-2 and run its official installer.

    Verifies the existing remote, checks git pull rc, treats a missing installer
    as a (partial) failure, and validates the launcher exists afterwards. An
    existing non-git directory fails clearly and is never deleted silently.
    """
    _log("\n=== Install Sammie-Roto ===", log_fh)
    root = normalize_path(root)
    _log(f"  Target: {root}", log_fh)

    # Existing directory: classify as git repo, release-style Sammie, or foreign.
    # listdir can throw on permission/IO errors — read safely.
    is_dir = os.path.isdir(root)
    try:
        listing = os.listdir(root) if is_dir else []
    except OSError as e:
        _log(f"  ERROR: cannot read directory {root}: {e}", log_fh)
        return False
    if is_dir and listing:
        if os.path.isdir(os.path.join(root, ".git")):
            # Verify remote origin.
            rc, so, _ = _run(["git", "config", "--get", "remote.origin.url"],
                             cwd=root, log_fh=log_fh, timeout=30)
            if rc != 0 or _SAMMIE_REMOTE_HINT not in (so or ""):
                _log(f"  ERROR: {root} is a git repo but remote is not {_SAMMIE_URL}", log_fh)
                _log(f"    remote: {so.strip() or '(none)'}", log_fh)
                return False
            _log("  Existing Sammie git repo found.", log_fh)
            if dry_run:
                _log("  [DRY-RUN] Would pull updates and run installer", log_fh)
                return True
            if yes or _confirm("  Pull latest?", default=True):
                prc, _, pse = _run(["git", "pull"], cwd=root, log_fh=log_fh, timeout=600)
                if prc != 0:
                    _log(f"  ERROR: git pull failed: {pse.strip()}", log_fh)
                    return False
        elif _looks_like_sammie(root):
            # Release-style install (no .git): accept, do not delete/move it.
            _log(f"  Existing release-style Sammie install found at {root}.", log_fh)
            if dry_run:
                cmd = _sammie_official_cmd(root)
                if cmd:
                    _log(f"  [DRY-RUN] Would run installer: {' '.join(cmd)}", log_fh)
                else:
                    _log("  [DRY-RUN] No installer script; would validate launcher only", log_fh)
                launcher = sammie_launcher(root)
                if os.path.isfile(launcher):
                    _log(f"  [DRY-RUN] Current-platform launcher present: {launcher}", log_fh)
                    return True
                _log(f"  [DRY-RUN] ERROR: current-platform launcher missing: {launcher}", log_fh)
                _log("  [DRY-RUN] Validation would fail.", log_fh)
                return False
            # Fall through to official installer / launcher validation below.
        else:
            _log(f"  ERROR: {root} exists, is not a git repo, and does not look like Sammie.", log_fh)
            _log("  Choose a different path or remove it manually.", log_fh)
            return False
    else:
        if dry_run:
            _log(f"  [DRY-RUN] Would clone {_SAMMIE_URL} -> {root}", log_fh)
            return True
        os.makedirs(os.path.dirname(root) or ".", exist_ok=True)
        env = tool_env()
        rc, _, se = _run(["git", "clone", _SAMMIE_URL, root], log_fh=log_fh,
                         timeout=600, env=env)
        if rc != 0:
            _log(f"  ERROR: clone failed: {se.strip()}", log_fh)
            return False

    # Official installer (shared by git/release/clone paths).
    cmd = _sammie_official_cmd(root)
    if cmd is None:
        # No installer script (e.g. release-style with deps pre-bundled): still
        # usable if the launcher is present.
        launcher = sammie_launcher(root)
        if os.path.isfile(launcher):
            _log(f"  No installer script found; launcher already present: {launcher}", log_fh)
            _log("  OK (release-style, skipping dependency install)", log_fh)
            return True
        _log(f"  ERROR: no official installer found in {root} and no launcher present", log_fh)
        _log("  Sammie setup is partial; check upstream README for manual setup.", log_fh)
        return False

    _log("  Running official installer (streaming)...", log_fh)
    rc = _run_streaming(cmd, cwd=root, log_fh=log_fh, timeout=3600, env=tool_env())
    if rc != 0:
        _log(f"  FAILED (exit {rc})", log_fh)
        return False

    # Validate launcher exists after install.
    launcher = sammie_launcher(root)
    if not os.path.isfile(launcher):
        _log(f"  ERROR: launcher missing after install: {launcher}", log_fh)
        return False
    _log(f"  OK; launcher: {launcher}", log_fh)
    return True


# --- health check ----------------------------------------------------------

def do_health_check(log_fh: Any = None) -> None:
    """Print health report from filesystem checks."""
    _log(f"\n=== Health Check ===", log_fh)
    for item in health_report():
        icon = "OK" if item["status"] == "ok" else "!!"
        _log(f"  [{icon}] {item['name']}: {item['status']}", log_fh)
        if item["status"] != "ok":
            _log(f"      {item['detail']}", log_fh)


def do_model_report(log_fh: Any = None) -> None:
    """Print the OmniPaint/FLUX/NF4 model report (real NF4 import + runtime check)."""
    _log("\n=== OmniPaint Model Report ===", log_fh)
    for item in model_report(check_nf4=True, check_runtime=True):
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


# --- TUI -------------------------------------------------------------------

_MENU_ITEMS = [
    ("Preflight checks", "preflight"),
    ("Configure paths", "configure"),
    ("Configure LTX Desktop (root/models)", "ltx"),
    ("Install Nuke plugin (init.py)", "nuke"),
    ("Link ComfyUI custom node", "comfyui"),
    ("Install OmniPaint backend", "omnipaint"),
    ("Check OmniPaint models", "models"),
    ("Install Sammie-Roto", "sammie"),
    ("Health check", "health"),
    ("Save settings", "save"),
    ("Quit", "quit"),
]


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="AI toolchain installer")
    parser.add_argument("--dry-run", action="store_true", help="preview without changes")
    parser.add_argument("--yes", action="store_true", help="auto-confirm prompts")
    parser.add_argument("--log", type=str, default=None, help="log file path")
    args = parser.parse_args()

    log_fh = None
    if args.log:
        log_fh = open(args.log, "a", encoding="utf-8")
        _log(f"=== Install session {time.strftime('%Y-%m-%d %H:%M:%S')} ===", log_fh)

    settings = load_settings()

    if args.yes or args.dry_run:
        # Non-interactive: run preflight + all applicable steps, collect results.
        preflight(dry_run=args.dry_run, log_fh=log_fh)
        settings = configure_paths(settings, dry_run=args.dry_run, yes=args.yes, log_fh=log_fh)

        # sammie_root default is saved into settings by configure_paths (#7).
        results: dict[str, bool] = {}

        results["nuke"] = install_nuke_plugin(dry_run=args.dry_run, log_fh=log_fh)

        cr = settings.get("comfyui_root", "")
        if cr:
            mode = link_comfyui_node(cr, dry_run=args.dry_run, yes=args.yes, log_fh=log_fh)
            settings["comfyui_node_mode"] = mode
            # In --yes mode (no confirm-skip) "" means a real error; dry-run exit is 0 anyway.
            results["comfyui"] = mode != ""
        else:
            _log("\n  ComfyUI: not configured, skipping link step.", log_fh)

        results["omnipaint"] = install_omnipaint(dry_run=args.dry_run, log_fh=log_fh)

        sr = settings.get("sammie_root", "") or os.path.join(
            os.path.expanduser("~"), "Sammie-Roto-2")
        settings["sammie_root"] = sr
        results["sammie"] = install_sammie(sr, dry_run=args.dry_run, yes=args.yes, log_fh=log_fh)

        save_and_log(settings, dry_run=args.dry_run, log_fh=log_fh)
        do_health_check(log_fh=log_fh)

        # Dry-run only reports planned blockers -> exit 0. A real run returns
        # nonzero if any critical step failed.
        if args.dry_run:
            exit_code = 0
        else:
            failed = [name for name, ok in results.items() if not ok]
            if failed:
                _log(f"\nFAILED steps: {', '.join(failed)}", log_fh)
                exit_code = 1
            else:
                exit_code = 0

        if log_fh:
            log_fh.close()
        return exit_code

    # Interactive TUI
    while True:
        print("\n=== AI Toolchain Installer ===")
        for i, (label, _) in enumerate(_MENU_ITEMS, 1):
            print(f"  {i}. {label}")
        try:
            choice = input("\nChoice: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not choice:
            continue
        try:
            idx = int(choice) - 1
            action = _MENU_ITEMS[idx][1]
        except (ValueError, IndexError):
            print("Invalid choice.")
            continue

        if action == "quit":
            break
        elif action == "preflight":
            preflight(log_fh=log_fh)
        elif action == "configure":
            settings = configure_paths(settings, log_fh=log_fh)
        elif action == "ltx":
            ltx_default = settings.get("ltx_root", "") or os.path.join(
                os.path.expanduser("~"), "LTX-Desktop")
            ltx = _ask("LTX Desktop root", ltx_default)
            settings["ltx_root"] = normalize_path(ltx)
            lmd_default = settings.get("ltx_models_dir", "") or resolve_ltx_models_dir(settings)
            lmd = _ask("LTX models dir", lmd_default)
            settings["ltx_models_dir"] = normalize_path(lmd)
            _log(f"  LTX root:   {settings['ltx_root']}", log_fh)
            _log(f"  LTX models: {settings['ltx_models_dir'] or '(not set)'}", log_fh)
            _log("  (discovery/config only — no install/build/model download)", log_fh)
        elif action == "nuke":
            install_nuke_plugin(log_fh=log_fh)
        elif action == "comfyui":
            cr = settings.get("comfyui_root", "")
            if not cr:
                cr = _ask("ComfyUI root")
                settings["comfyui_root"] = cr
            mode = link_comfyui_node(cr, log_fh=log_fh)
            if mode:
                settings["comfyui_node_mode"] = mode
        elif action == "omnipaint":
            install_omnipaint(log_fh=log_fh)
        elif action == "models":
            do_model_report(log_fh=log_fh)
        elif action == "sammie":
            sr = settings.get("sammie_root", "") or os.path.join(
                os.path.expanduser("~"), "Sammie-Roto-2")
            settings["sammie_root"] = sr
            install_sammie(sr, log_fh=log_fh)
        elif action == "health":
            do_health_check(log_fh=log_fh)
        elif action == "save":
            save_and_log(settings, log_fh=log_fh)

    if log_fh:
        log_fh.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
