# Bridge Protocol Draft

HTTP is local-only for the first implementation.

## Global server

`host` and `port` are global server settings saved in:

```text
~/.nuke/comfyui_bridge/settings.json
```

Bridge node knobs may display/edit those defaults, but one Nuke process owns one bound server. Host/port changes require server restart or Nuke restart.

## `GET /health`

Returns JSON:

```json
{"ok": true, "app": "nuke-comfyui-bridge"}
```

## `POST /bridge/{bridge_id}/frame`

ComfyUI `FromNuke` asks Nuke for one frame.

Use `_active` as `bridge_id` to target the selected bridge node in Nuke, or the only bridge node if there is just one. ComfyUI nodes leave `bridge_id` blank by default and send `_active`.

Request JSON:

```json
{
  "frame": -1,
  "format": "png8",
  "request_id": "optional"
}
```

`frame: -1` means use Nuke's current frame.

Phase 1 response is binary image bytes with headers:

```text
Content-Type: image/png
X-NukeBridge-Bridge-Id: <bridge_id>
X-NukeBridge-Frame: <frame>
X-NukeBridge-Format: png8
X-NukeBridge-Width: <width>
X-NukeBridge-Height: <height>
X-NukeBridge-Prompt: <url-quoted prompt>
X-NukeBridge-Mask-Source: source alpha | invert source alpha | mask input | invert mask input
X-NukeBridge-Colorspace: raw | sRGB
```

Phase 2 may add `Content-Type: image/exr` for `exr16`.

## `POST /bridge/{bridge_id}/result`

ComfyUI `ToNuke` sends one result image back.

Use `_active` as `bridge_id` for the same selected/only bridge fallback.

Request body is binary image bytes.

Required headers:

```text
Content-Type: image/png
X-NukeBridge-Filename-Prefix: comfy_result
X-NukeBridge-Frame: <frame or -1>
X-NukeBridge-Format: png8
X-NukeBridge-Colorspace: sRGB | raw
```

Response JSON:

```json
{
  "ok": true,
  "path": "/path/to/saved/result.png"
}
```

## Mask carrier and formulas

Phase 1 sends RGB plus the selected mask in alpha. The original source alpha is not preserved separately when another mask source is selected.

ComfyUI mask convention:

```text
0 = keep / opaque
1 = masked / transparent
```

Nuke alpha/mask convention exposed to the user:

```text
1 = keep / opaque
0 = transparent / masked
```

Formulas for the alpha channel sent to ComfyUI:

```text
source alpha:        carrier_alpha = source_alpha
invert source alpha: carrier_alpha = 1 - source_alpha
mask input:          carrier_alpha = mask_alpha_or_luma
invert mask input:   carrier_alpha = 1 - mask_alpha_or_luma
```

Then `FromNuke` converts carrier alpha to ComfyUI mask:

```text
comfy_mask = 1 - carrier_alpha
```

Disconnected mask input falls back to all-keep:

```text
carrier_alpha = 1
comfy_mask = 0
```

If mask input size differs from source, Nuke should render it through the same format/resolution as the bridge source.

## Run from Nuke

The bridge may optionally trigger a ComfyUI workflow from Nuke while preserving the main architecture.

Nuke only stores a workflow API JSON path and posts it unchanged except for values explicitly wired to bridge nodes/IDs if needed. It must not become a Nuke-side ComfyUI graph builder.

Minimum run-from-Nuke flow:

```text
Nuke Run button -> POST /prompt with selected API workflow -> ComfyUI FromNuke pulls from Nuke -> ToNuke returns result
```

Progress uses ComfyUI WebSocket `/ws?clientId=...` events and Nuke `ProgressTask`.
