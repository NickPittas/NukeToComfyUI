# Installer and central settings

Use Python 3.10+ with Nuke and ComfyUI already installed. Run from the repo root
(`python3` may be needed instead of `python` on Linux/macOS):

```bash
python tools/install.py
```

For a new installation, choose **Configure ComfyUI path**, then **Save
settings**. You can then preview the proposed installation:

```bash
python tools/install.py --dry-run --yes
```

Apply the installation with **Run all**, or:

```bash
python tools/install.py --yes
```

Write a log:

```bash
python tools/install.py --log install.log
```

These are the supported options: `--dry-run`, `--yes`, `--log PATH`, and
`--help`. The installer does not install applications, models, or Python/OS
dependencies. Restart Nuke and ComfyUI after installation or updating plugin
files; keep this repository at its installed path when using symlinks.

## What it changes

- User Nuke init only: adds a marked `nuke.pluginAddPath(".../nuke")` block to
  `~/.nuke/init.py` after backing it up.
- ComfyUI custom node: symlinks `comfyui/nuke_bridge` into
  `<ComfyUI>/custom_nodes/nuke_bridge`. If symlink fails, it informs you and
  copies the node instead.
- Central settings: writes `~/.nuke/comfyui_bridge/settings.json`.

It does **not** edit user `~/.nuke/menu.py`.

## Path selection

`Configure ComfyUI path` opens a **Tk file dialog** (a separate dialog window
via `tkinter.filedialog`) to pick the ComfyUI root. It is not the OS-native
file manager, and the installer itself is a plain terminal menu, not a
full-screen TUI. Headless/SSH sessions or hosts without working tkinter fall
back to a typed path. Preflight reports whether tkinter is importable;
actual dialog availability is unverified until a dialog opens. Cancelling the
dialog keeps the current value; a dialog error or missing Tk falls back to
typing the path. **Tk is optional: type or paste the ComfyUI folder at the
terminal prompt to complete installation without it.** Pressing Enter at a
prompt with a default keeps that default.

### Missing Tk on a new installation

The installer checks for Tk before showing the terminal menu and during
preflight. If it is missing, it asks you to install it and shows instructions
for your OS and the Python executable running the installer:

| OS / Python | Install in your own terminal |
| --- | --- |
| Omarchy | `omarchy pkg add tk` |
| Arch-based Linux | `sudo pacman -S --needed tk` |
| Debian / Ubuntu | `sudo apt install python3-tk` |
| Fedora / RHEL | `sudo dnf install python3-tkinter` |
| openSUSE | `sudo zypper install python3-tk` |
| Alpine | `sudo apk add py3-tkinter` |
| Windows python.org Python | Modify the matching Python installation; enable **tcl/tk and IDLE** |
| macOS Homebrew Python | `brew install python-tk@X.Y`, matching the running Python version |
| Other macOS Python | Use a matching python.org distribution with Tcl/Tk support |

Restart the installer afterward. For custom-built/pyenv Python, OS packages
alone may not supply its tkinter binding: rebuild that Python with Tcl/Tk
development libraries or use the OS Python with tkinter support. Missing a
graphical display is different from missing Tk; package installation does
not provide a display for an SSH/headless session.

The installer never runs package managers or requests administrator privileges.
Neither `--yes` nor `--dry-run` installs OS packages. On Arch/Omarchy, the
`tk` package supplies the Tk runtime and pulls in Tcl as a dependency.

### Selecting the correct folder

The selected directory must be the **ComfyUI engine directory** — the folder
containing `main.py` and a `comfy/` subfolder. For portable installs select
the inner `ComfyUI` folder (e.g. `ComfyUI_windows_portable/ComfyUI`), not the
wrapper. Anything else is rejected before any files are written; the
installer never scans the disk for candidate installs.

If `COMFYUI_ROOT` is set in the environment it overrides the stored setting
and is used for installing and checking. The installer prints a NOTE whenever
that override differs from the stored selection, so it never silently
installs/checks one root while the stored setting names another. An empty
answer keeps the default, or leaves the root unset if no default exists.
Unset `COMFYUI_ROOT` if you want a newly selected path to take precedence.

To preview without first saving a path, set the environment variable:

Linux/macOS:

```bash
COMFYUI_ROOT="/path/to/ComfyUI" python tools/install.py --dry-run --yes
```

Windows PowerShell:

```powershell
$env:COMFYUI_ROOT = 'C:\ComfyUI_windows_portable\ComfyUI'
python tools/install.py --dry-run --yes
```

An environment override is not copied into the saved path automatically.

## Run all and exit codes

`Run all` (and `--yes` / `--dry-run --yes`) validates the effective ComfyUI
root (environment `COMFYUI_ROOT` first, else the stored setting) before
changing `init.py`, settings, or the custom-node link. If the root is absent
or invalid, those targets are left untouched.

Noninteractive runs return `0` when all installation steps succeed (or can
be previewed), `1` for an incomplete install or an I/O failure, and `130` for
an interrupted operation. An invalid root is an incomplete run, including in
dry-run mode. Interactive **Run all** reports errors and returns to the menu;
quitting the menu is not proof that installation succeeded.

Dry-run reports intended changes without applying them. Health checks show
current on-disk installation state, not the state that would exist after
installation. If `--log PATH` is supplied, that log file is still written,
even with `--dry-run`.

Nuke-only installs (e.g. remote ComfyUI) remain possible through **Install
Nuke plugin**. Install the ComfyUI custom node on the machine running ComfyUI.
The health check covers plugin files, Nuke registration, and the custom-node
link/copy; it does not verify a live image round-trip.

## Central settings

Path:

```text
~/.nuke/comfyui_bridge/settings.json
```

Important keys:

```json
{
  "comfyui_root": "",
  "comfyui_host": "127.0.0.1",
  "comfyui_port": 8188,
  "output_directory": "~/comfyui_bridge_results"
}
```

Environment overrides:

```text
NUKE_HOME_DIR
COMFYUI_ROOT
```

## Upgrading from the combined toolchain

OmniPaintRemove, Sammie-Roto, and LTX Desktop integration files and Nuke menu
entries have been removed. Their external installations, models, and user
project files are not uninstalled. Existing Nuke scripts containing
`OmniPaintRemove` still need that plugin separately; the bridge does not
replace it.

Legacy `sammie_root`, `ltx_root`, and `ltx_models_dir` settings are ignored on
load and removed on the next explicit settings save. Bridge settings and
unrelated keys are preserved; loading settings does not rewrite the file.
The Nuke **AI Setup** menu retains **Open Settings** and **Health Check**.

## Installer menu

```text
Preflight checks
Configure ComfyUI path
Install Nuke plugin (init.py)
Link ComfyUI custom node
Health check
Save settings
Run all (install everything)
Quit
```
