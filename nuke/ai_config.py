"""Central settings, path resolution, and health reporting for the Nuke ↔ ComfyUI bridge.

Stdlib-only; no nuke import at module level. Safe to import from installer,
tests, and Nuke runtime alike.

Settings schema v2 lives at ~/.nuke/comfyui_bridge/settings.json (same path as
legacy bridge settings). Flat v1 keys (host, port, output_directory) remain
readable and writable for backward compatibility. Retired legacy keys
(sammie_root, ltx_root, ltx_models_dir) are dropped at load and save.
"""

from __future__ import annotations

import ast
import json
import os
from typing import Any, Dict, List

_SCHEMA_VERSION = 2

# Path keys whose stored values are filesystem roots and must be normalized
# (expanduser + abspath + normpath) at the save/use boundary.
_PATH_KEYS = ("comfyui_root",)

# Markers for the managed init.py pluginAddPath block (shared with installer).
INIT_MARK_BEGIN = "# >>> NukeToComfyUI >>>"
INIT_MARK_END = "# <<< NukeToComfyUI <<<"

# Retired AI-toolchain keys removed from stored settings at load and save.
_LEGACY_KEYS = ("sammie_root", "ltx_root", "ltx_models_dir")


# --- path normalization ----------------------------------------------------

def normalize_path(p: str) -> str:
    """Resolve a user-entered path to an absolute, normalized form.

    expanduser + abspath + normpath. Empty input returns "".
    """
    if not p or not str(p).strip():
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(p).strip())))


def paths_equal(a: str, b: str) -> bool:
    """Case-insensitive (platform-correct) equality of two filesystem paths."""
    return os.path.normcase(normalize_path(a)) == os.path.normcase(normalize_path(b))


def parse_plugin_addpaths(content: str) -> List[str]:
    """Return string literals passed to ``nuke.pluginAddPath(...)`` in *content*.

    Walks the parsed AST so comments and string contents never false-match,
    and extra positional/keyword args after the first string are handled.
    Skips non-literal first args (variables/expressions) — cannot verify those.
    Returns [] on SyntaxError (e.g. user file with a syntax error elsewhere).
    """
    paths: List[str] = []
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return paths
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match nuke.pluginAddPath(...) — Attribute named pluginAddPath whose
        # value is the Name "nuke". Other shapes (e.g. import nuke as x) are
        # skipped conservatively.
        if not (isinstance(func, ast.Attribute) and func.attr == "pluginAddPath"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "nuke"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            paths.append(first.value)
    return paths


def init_has_plugin(content: str, plugin_path: str) -> bool:
    """True if *content* has a ``nuke.pluginAddPath`` pointing at *plugin_path*."""
    return any(paths_equal(p, plugin_path) for p in parse_plugin_addpaths(content or ""))


# --- paths -----------------------------------------------------------------

def repo_root() -> str:
    """This repository root (parent of nuke/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def nuke_plugin_path() -> str:
    return os.path.join(repo_root(), "nuke")


def comfyui_node_source() -> str:
    """Repo source dir for the ComfyUI custom node (linked/copied into place)."""
    return os.path.join(repo_root(), "comfyui", "nuke_bridge")


def _nuke_home() -> str:
    return os.environ.get("NUKE_HOME_DIR") or os.path.join(
        os.path.expanduser("~"), ".nuke"
    )


def settings_dir() -> str:
    return os.path.join(_nuke_home(), "comfyui_bridge")


SETTINGS_PATH = os.path.join(settings_dir(), "settings.json")


def _default_output_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "comfyui_bridge_results")


# --- defaults --------------------------------------------------------------

DEFAULT_SETTINGS: Dict[str, Any] = {
    "schema_version": _SCHEMA_VERSION,
    "host": "127.0.0.1",
    "bridge_host": "",
    "port": 8765,
    "output_directory": _default_output_dir(),
    "comfyui_root": "",
    "comfyui_host": "127.0.0.1",
    "comfyui_port": 8188,
    "comfyui_node_mode": "",
}


# --- load / save with migration --------------------------------------------

def _normalize_settings(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Drop retired legacy keys, stamp schema, normalize paths in-place. Returns merged.

    Bridge keys and arbitrary unknown keys are preserved.
    """
    for key in _LEGACY_KEYS:
        merged.pop(key, None)
    merged["schema_version"] = _SCHEMA_VERSION
    out = merged.get("output_directory") or DEFAULT_SETTINGS["output_directory"]
    merged["output_directory"] = os.path.expanduser(str(out))
    for key in _PATH_KEYS:
        val = merged.get(key)
        if isinstance(val, str) and val.strip():
            merged[key] = normalize_path(val)
        elif val:
            merged[key] = normalize_path(str(val))
        else:
            merged[key] = ""
    return merged


def _write_settings(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically persist *merged* to disk. Returns merged."""
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2, sort_keys=True)
    os.replace(tmp, SETTINGS_PATH)
    return merged


def load_settings() -> Dict[str, Any]:
    """Load settings from disk with v1→v2 migration. Returns merged dict.

    Read-only: never mutates, renames, or backs up the on-disk file. Retired
    legacy keys are dropped from the returned dict only. If the JSON is
    corrupt/unreadable, returns defaults (merged). Explicit repair/
    backup is a separate flow; callers that need to write use save_settings().
    """
    merged = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            merged.update(data)
            merged.setdefault("schema_version", 1)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        # Corrupt/unreadable: fall through to defaults. No on-disk mutation.
        pass
    return _normalize_settings(merged)


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically persist settings, merging *settings* over the current on-disk
    state so a partial save cannot erase previously-stored paths.

    Returns the merged dict.
    """
    merged = load_settings()  # current on-disk values + defaults (no recursion)
    merged.update(settings or {})
    return _write_settings(_normalize_settings(merged))


# --- resolvers: env > settings > common locations --------------------------

def resolve_comfyui_root(settings: Dict[str, Any] | None = None) -> str:
    env = os.environ.get("COMFYUI_ROOT", "").strip()
    if env:
        return normalize_path(env)
    if settings is None:
        settings = load_settings()
    return str(settings.get("comfyui_root") or "").strip()


# --- health report ---------------------------------------------------------

def _rel_files(root: str) -> Dict[str, int]:
    """Return {normcase(relpath): size} for every file under *root*."""
    out: Dict[str, int] = {}
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root)
            try:
                out[os.path.normcase(rel)] = os.path.getsize(full)
            except OSError:
                out[os.path.normcase(rel)] = -1
    return out


def dirs_match(a: str, b: str) -> bool:
    """True if *a* and *b* are both dirs with identical relative file set+size.

    Sufficient for this small custom node. stdlib only.
    """
    if not os.path.isdir(a) or not os.path.isdir(b):
        return False
    return _rel_files(a) == _rel_files(b)


def health_report(comfyui_root: str | None = None) -> List[Dict[str, str]]:
    """Check filesystem for each component. Returns list of {name, status, detail}.

    status values: ok | missing | stale | blocked | partial. Never reports ok on
    a coarse or platform-blocked check.
    """
    settings = load_settings()
    report: List[Dict[str, str]] = []
    npp = nuke_plugin_path()

    # Nuke plugin
    ok = os.path.isdir(npp)
    report.append({"name": "Nuke plugin", "status": "ok" if ok else "missing",
                   "detail": npp if ok else f"not found: {npp}"})

    # Nuke init.py — parse actual pluginAddPath args; exact match only.
    init_path = os.path.join(_nuke_home(), "init.py")
    init_status = "missing"
    init_detail = init_path
    try:
        with open(init_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        if init_has_plugin(content, npp):
            init_status = "ok"
        elif INIT_MARK_BEGIN in content:
            init_status = "stale"
            init_detail = "marker present but pluginAddPath path wrong; re-run installer"
        else:
            init_detail = "init.py has no pluginAddPath for this plugin"
    except FileNotFoundError:
        init_detail = "no init.py in " + _nuke_home()
    except OSError:
        init_detail = "cannot read init.py"
    report.append({"name": "Nuke init.py", "status": init_status, "detail": init_detail})

    # ComfyUI custom node — symlink/copy correctness.
    # comfyui_root=None resolves stored settings / env default; explicit ""
    # means "not configured" (env is not consulted); nonempty is normalized.
    cr = resolve_comfyui_root(settings) if comfyui_root is None else normalize_path(comfyui_root)
    source = comfyui_node_source()
    if cr:
        link = os.path.join(cr, "custom_nodes", "nuke_bridge")
        if os.path.islink(link):
            if paths_equal(os.path.realpath(link), source):
                report.append({"name": "ComfyUI node", "status": "ok",
                               "detail": f"symlink: {link}"})
            else:
                report.append({"name": "ComfyUI node", "status": "stale",
                               "detail": f"wrong symlink target: {link}"})
        elif os.path.isdir(link):
            if dirs_match(source, link):
                report.append({"name": "ComfyUI node", "status": "ok",
                               "detail": f"copy: {link}"})
            else:
                report.append({"name": "ComfyUI node", "status": "stale",
                               "detail": f"copy out of sync with source: {link}"})
        else:
            report.append({"name": "ComfyUI node", "status": "missing",
                           "detail": f"not linked in {cr}/custom_nodes/"})
    else:
        report.append({"name": "ComfyUI node", "status": "missing",
                       "detail": "ComfyUI root not configured"})

    return report
