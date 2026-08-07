"""Central settings, path resolution, and health reporting for the AI toolchain.

Stdlib-only; no nuke import at module level. Safe to import from installer,
tests, and Nuke runtime alike.

Settings schema v2 lives at ~/.nuke/comfyui_bridge/settings.json (same path as
legacy bridge settings). Flat v1 keys (host, port, output_directory) remain
readable and writable for backward compatibility.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
from typing import Any, Dict, List

_SCHEMA_VERSION = 2

# Path keys whose stored values are filesystem roots and must be normalized
# (expanduser + abspath + normpath) at the save/use boundary.
_PATH_KEYS = ("comfyui_root", "sammie_root", "ltx_root", "ltx_models_dir")

# Markers for the managed init.py pluginAddPath block (shared with installer).
INIT_MARK_BEGIN = "# >>> NukeToComfyUI >>>"
INIT_MARK_END = "# <<< NukeToComfyUI <<<"


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
    "sammie_root": "",
    "ltx_root": "",
    "ltx_models_dir": "",
}


# --- load / save with migration --------------------------------------------

def _normalize_settings(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp schema version and normalize path fields in-place. Returns merged."""
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

    Read-only: never mutates, renames, or backs up the on-disk file. If the
    JSON is corrupt/unreadable, returns defaults (merged). Explicit repair/
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

def resolve_sammie_root(settings: Dict[str, Any] | None = None) -> str:
    env = os.environ.get("SAMMIE_ROOT", "").strip()
    if env:
        return normalize_path(env)
    if settings is None:
        settings = load_settings()
    val = str(settings.get("sammie_root") or "").strip()
    if val:
        return normalize_path(val)
    home = os.path.join(os.path.expanduser("~"), "Sammie-Roto-2")
    return home


def resolve_ltx_root(settings: Dict[str, Any] | None = None) -> str:
    env = os.environ.get("LTX_ROOT", "").strip()
    if env:
        return normalize_path(env)
    if settings is None:
        settings = load_settings()
    val = str(settings.get("ltx_root") or "").strip()
    if val:
        return normalize_path(val)
    return os.path.join(os.path.expanduser("~"), "LTX-Desktop")


def _ltx_app_data_dir() -> str:
    """LTXDesktop app-data dir. Honors ``LTX_APP_DATA_DIR`` override."""
    env = os.environ.get("LTX_APP_DATA_DIR", "").strip()
    if env:
        return normalize_path(env)
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    elif sys_platform() == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "LTXDesktop")


def _ltx_desktop_settings_path() -> str:
    """LTXDesktop settings.json path inside the app-data dir."""
    return os.path.join(_ltx_app_data_dir(), "settings.json")


def _read_ltx_desktop_settings() -> Dict[str, Any]:
    """Read LTXDesktop's settings.json as a dict ({} on any failure)."""
    try:
        with open(_ltx_desktop_settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_ltx_models_dir(settings: Dict[str, Any] | None = None) -> str:
    """Resolve the LTX models dir.

    Priority: env ``LTX_MODELS_DIR`` > settings ``ltx_models_dir`` >
    LTXDesktop ``settings.json`` ``models_dir``/``modelsDir`` >
    ``<app_data>/models`` if it already exists > ``''``.
    Discovery-only; never invents or creates a path.
    """
    env = os.environ.get("LTX_MODELS_DIR", "").strip()
    if env:
        return normalize_path(env)
    if settings is None:
        settings = load_settings()
    val = str(settings.get("ltx_models_dir") or "").strip()
    if val:
        return normalize_path(val)
    data = _read_ltx_desktop_settings()
    models = str(data.get("models_dir") or data.get("modelsDir") or "").strip()
    if models:
        return normalize_path(models)
    default = os.path.join(_ltx_app_data_dir(), "models")
    return default if os.path.isdir(default) else ""


def resolve_comfyui_root(settings: Dict[str, Any] | None = None) -> str:
    env = os.environ.get("COMFYUI_ROOT", "").strip()
    if env:
        return normalize_path(env)
    if settings is None:
        settings = load_settings()
    return str(settings.get("comfyui_root") or "").strip()


def sammie_launcher(root: str | None = None) -> str:
    """Return the launcher script path for the current platform."""
    if root is None:
        root = resolve_sammie_root()
    if os.name == "nt":
        candidate = os.path.join(root, "run_sammie.bat")
    elif sys_platform() == "darwin":
        candidate = os.path.join(root, "run_sammie.command")
        if not os.path.isfile(candidate):
            candidate = os.path.join(root, "run_sammie.sh")
    else:
        candidate = os.path.join(root, "run_sammie.sh")
        if not os.path.isfile(candidate):
            candidate = os.path.join(root, "run_sammie.command")
    return candidate


def _ltx_binary_candidates(root: str) -> List[str]:
    """Ordered platform candidate paths for the built LTX Desktop binary."""
    if os.name == "nt":
        return [os.path.join(root, "release", "win-unpacked", "LTX Desktop.exe")]
    if sys_platform() == "darwin":
        # mac-arm64 (Apple Silicon) first, then intel mac.
        app = ["LTX Desktop.app", "Contents", "MacOS", "LTX Desktop"]
        return [os.path.join(root, "release", "mac-arm64", *app),
                os.path.join(root, "release", "mac", *app)]
    return [os.path.join(root, "release", "linux-unpacked", "ltx-desktop")]


def _ltx_binary(root: str) -> str:
    """First existing built binary, else the first candidate (may not exist)."""
    cands = _ltx_binary_candidates(root)
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0]


def _looks_like_ltx_root(root: str) -> bool:
    """True if *root* looks like an LTX Desktop checkout/release.

    Accepts: a built binary, OR a package.json whose name is ltx-desktop, OR the
    Electron+backend source layout (electron/main.ts + backend/pyproject.toml).
    """
    if not os.path.isdir(root):
        return False
    if os.path.isfile(_ltx_binary(root)):
        return True
    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and str(data.get("name", "")).lower() == "ltx-desktop":
                return True
        except (OSError, ValueError):
            pass
    return (
        os.path.isfile(os.path.join(root, "electron", "main.ts"))
        and os.path.isfile(os.path.join(root, "backend", "pyproject.toml"))
    )


def ltx_command(root: str | None = None) -> List[str] | None:
    """Return [cmd...] to launch LTX Desktop, or None if not runnable.

    Built binary wins. Dev mode requires an LTX-looking root with package.json
    AND pnpm on PATH — never OKs an arbitrary directory.
    """
    if root is None:
        root = resolve_ltx_root()
    binary = _ltx_binary(root)
    if os.path.isfile(binary):
        return [binary]
    if (_looks_like_ltx_root(root)
            and os.path.isfile(os.path.join(root, "package.json"))
            and shutil.which("pnpm")):
        return ["pnpm", "dev"]
    return None


def sys_platform() -> str:
    import sys
    return sys.platform


def tool_env(base: Dict[str, str] | None = None) -> Dict[str, str]:
    """Build an env dict with all resolved tool paths for subprocesses.

    base={} is respected (subprocess gets only the resolved overlay);
    base=None falls back to os.environ.
    """
    settings = load_settings()
    env = dict(os.environ if base is None else base)
    sammie = resolve_sammie_root(settings)
    ltx = resolve_ltx_root(settings)
    comfyui = resolve_comfyui_root(settings)
    ltx_models = resolve_ltx_models_dir(settings)
    if sammie:
        env["SAMMIE_ROOT"] = sammie
    if ltx:
        env["LTX_ROOT"] = ltx
    if comfyui:
        env["COMFYUI_ROOT"] = comfyui
    if ltx_models:
        env["LTX_MODELS_DIR"] = ltx_models
    return env


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


def _omnipaint_model_report() -> List[Dict[str, str]]:
    """Lazy wrapper around omnipaint_models.model_report() (avoids circular import)."""
    import omnipaint_models  # local: omnipaint_models imports ai_config at load
    return omnipaint_models.model_report()


def _ltx_active_ic_lora(models_dir: str) -> tuple[str, str]:
    """IC-LoRA inpaint adapter path + provenance label.

    Reads the LTX active model profile's ``ic_lora_in_outpainting`` component
    when available; otherwise falls back to the canonical adapter filename.
    Returns (path, "active profile"|"canonical"). path may be "".
    """
    canonical = ""
    if models_dir:
        canonical = os.path.join(
            models_dir, "adapters",
            "ltx-2.3-22b-ic-lora-in-outpainting-0.9.safetensors")
    try:
        with open(os.path.join(_ltx_app_data_dir(), "model_profiles.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            active = data.get("active_model_profile_id")
            for p in data.get("profiles") or []:
                if isinstance(p, dict) and p.get("id") == active:
                    comps = p.get("components")
                    if isinstance(comps, dict):
                        path = comps.get("ic_lora_in_outpainting")
                        if path:
                            return str(path), "active profile"
    except (OSError, ValueError):
        pass
    return canonical, "canonical"


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
    cr = comfyui_root or resolve_comfyui_root(settings)
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

    # OmniPaint backend assets/FLUX/NF4 — delegated to the model manager
    # (mirrors the adapter's required-file checks; never false-OKs).
    report.extend(_omnipaint_model_report())

    # Sammie — release-style install needs launcher.py + a platform launcher.
    sr = resolve_sammie_root(settings)
    sl = sammie_launcher(sr)
    has_root = os.path.isdir(sr)
    has_launcher_py = os.path.isfile(os.path.join(sr, "launcher.py"))
    if os.path.isfile(sl) and has_launcher_py:
        report.append({"name": "Sammie-Roto", "status": "ok", "detail": sl})
    elif has_root:
        report.append({"name": "Sammie-Roto", "status": "missing",
                       "detail": f"root exists but launcher missing: {sl}"})
    else:
        report.append({"name": "Sammie-Roto", "status": "missing", "detail": sl})

    # LTX Desktop — discovery/config only; never installs or builds.
    lr = resolve_ltx_root(settings)
    lcmd = ltx_command(lr)
    if lcmd:
        report.append({"name": "LTX Desktop", "status": "ok", "detail": lcmd[0]})
    elif _looks_like_ltx_root(lr):
        report.append({"name": "LTX Desktop", "status": "partial",
                       "detail": f"root looks like LTX but no runnable command: {lr}"})
    else:
        report.append({"name": "LTX Desktop", "status": "missing",
                       "detail": f"not found / not LTX: {lr}"})

    # LTX models dir — ok if configured/discovered dir exists.
    lmd = resolve_ltx_models_dir(settings)
    if lmd and os.path.isdir(lmd):
        report.append({"name": "LTX models dir", "status": "ok", "detail": lmd})
    else:
        report.append({"name": "LTX models dir", "status": "missing",
                       "detail": lmd or "not configured/discovered"})

    # LTX IC-LoRA inpaint adapter — discovery only; never downloads.
    # Honor the LTX active model profile's ic_lora_in_outpainting path when set;
    # fall back to the canonical adapter filename.
    ic_path, ic_source = _ltx_active_ic_lora(lmd)
    if ic_path and os.path.isfile(ic_path):
        report.append({"name": "LTX IC-LoRA inpaint", "status": "ok",
                       "detail": f"[{ic_source}] {ic_path}"})
    else:
        report.append({"name": "LTX IC-LoRA inpaint", "status": "missing",
                       "detail": f"[{ic_source}] {ic_path or 'no models dir/path configured'}"})

    return report
