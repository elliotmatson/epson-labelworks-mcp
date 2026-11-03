# epson-labelworks-mcp

MCP server for Epson LabelWorks label printers, starting with the **LW-PX400**.
It speaks the reverse-engineered ESC/P-L2 raster protocol directly over Bluetooth
Classic (RFCOMM), USB, or serial. It exposes preview and print tools over
streamable HTTP, so it can sit behind an MCP gateway such as Obot.

> Not affiliated with Epson. **Hardware-verified:** LW-PX400 over USB (12 mm tape,
> 2026-11-02). Other models and tape widths are profile data from the published
> research; printing on unverified profiles is off until you enable it (see *Bring-up*).

## Tools

| Tool | Effect | Notes |
|---|---|---|
| `printer_info` | read-only | Profile, dot table, transport, whether printing is enabled |
| `printer_status` | read-only | State, error, loaded tape width |
| `probe_printer` | read-only | USB descriptors and IEEE-1284 ID, raw status frame. Never feeds or cuts |
| `preview_label` | read-only | Returns a PNG of exactly what would print, plus its length |
| `print_label` | prints | Text (multi-line, auto-fit), QR code, and/or image. `copies`, `cut`, `density`, `dry_run` |
| `print_test_pattern` | prints | Calibration wedge for dot count and orientation |

If `tape_mm` is left out, the server uses the loaded cartridge's width when the
printer reports it, falling back to `LW_DEFAULT_TAPE_MM`. Before each real print it
checks status and refuses to print if the printer is busy, reports an error, or has a
different tape width loaded.

## What the PX400 profile is based on

Epson's Android SDK (`LWPrintProductInformation`, per tyalie's analysis) lists the
LW-PX400 as **capability level 3, 180 dpi, tapes 4–24 mm, no half-cutter, 8-byte
(protocol v1) status**. So the job is:

```
!  Q(on)  @ { C D G      job environment   (cutter = 01 "fast" variant)
Q(on)  L T  H(05) s(1)    page environment  (H and s are the level-3 additions)
ESC . records…  0C  @     raster, form feed, print end
```

Remaining uncertainties, all handled by bring-up:

* printable dots per tape (nospero's LW-600P table, with 12 mm = 72 dots confirmed on an LW-700)
* which tape edge is raster bit 0 (`flip_across`, default matches nospero/HA)
* whether USB exposes the textual status or only the binary `GetLWStatus` block

## Bring-up

1. **Pair on the host** (no PIN needed): `bluetoothctl` → `scan on` → `pair <MAC>` → `trust <MAC>`.
2. **Probe**, which is read-only:
   ```sh
   pip install -e .
   labelworks probe --bt AA:BB:CC:DD:EE:FF        # or: --transport usb
   ```
   You want a `status` block with `"tape_width_mm"` that matches the cartridge. Over USB,
   check `ieee1284_id` contains `MDL:LW-PX400` and note the PID.
3. **Dry run** and inspect the bytes: `labelworks print "Hello" --tape 12 --dry-run ./jobs`
4. **Calibrate:** `labelworks pattern --bt <MAC> --yes`. On the printed wedge:
   * the wedge's long edge should reach the far side of the printable area. If it is
     clipped, lower that tape's entry in `profiles.py`.
   * the `row0` text should read upright. If it is mirrored or upside down, flip `flip_across`.
5. Print a real label with `labelworks print "Rack A" --bt <MAC> --yes`, then set
   `verified=True` on the profile (or `LW_ALLOW_UNVERIFIED=1` in `.env`).

## Deploy

Every push to `main` runs the tests and publishes a multi-arch (amd64/arm64) image to
`ghcr.io/elliotmatson/epson-labelworks-mcp` (`latest`, `sha-<short>`, and `X.Y.Z` for
`vX.Y.Z` tags).

```sh
cp .env.example .env        # set LW_MCP_TOKEN (and LW_BT_ADDRESS for Bluetooth)
docker compose pull && docker compose up -d     # or: up -d --build to build locally
curl localhost:8765/healthz
```

* USB is the default: the container gets `/dev/bus/usb` and cgroup rule `c 189:* rwm`,
  and detaches the host's `usblp` driver while it prints. On Proxmox, pass the printer
  (`04b8:0705`) through to the VM by vendor/device ID.
* For Bluetooth, set `LW_TRANSPORT=rfcomm` and use `network_mode: host` (RFCOMM uses the
  host's BlueZ stack). The image uses Debian's `python3` because the official
  `python:*-slim` images are built without `AF_BLUETOOTH`.
* **Obot:** add a remote MCP server at `http://<host>:8765/mcp` with header
  `X-API-Key: <LW_MCP_TOKEN>`. Use this rather than `Authorization`, because a gateway
  may replace the `Authorization` header with its own credentials. Other clients can
  send `Authorization: Bearer <LW_MCP_TOKEN>`. Rejected requests are logged with the
  names (never values) of the auth headers that arrived. Set `LW_ALLOWED_HOSTS` to the
  hostname Obot uses to turn on DNS-rebinding protection.
* For local use you can run over stdio: `labelworks serve --stdio`.

## Environment

See `.env.example`. Main settings: `LW_PROFILE`, `LW_TRANSPORT`
(`rfcomm|usb|serial|file`), `LW_BT_ADDRESS`, `LW_BT_CHANNEL`, `LW_USB_PID`,
`LW_DEFAULT_TAPE_MM`, `LW_ALLOW_UNVERIFIED`, `LW_MCP_TOKEN`, `LW_ALLOWED_HOSTS`,
`LW_FONT` / `LW_FONT_BOLD`.

## Development

```sh
pip install -e '.[test]' && pytest
```

The tests check every fixed frame byte-for-byte against the published research, and
verify the PX400 level-3 command order, raster packing, status parsing, and rendering
on all six tape widths.

## Credits

This is a clean implementation from published protocol research:
[tyalie/RE-epson-label-printer](https://github.com/tyalie/RE-epson-label-printer),
[oxplot/nospero](https://github.com/oxplot/nospero),
[igrbtn/LW700Print](https://github.com/igrbtn/LW700Print),
[stoeppke/ha-epson-labelworks](https://github.com/stoeppke/ha-epson-labelworks).
