# RGB Screen Sync

Real-time ambient lighting for Windows. Samples what's on your screen and pushes the colour to every
RGB device you own — each one reading its **own** region of the display, so the keyboard follows the
bottom of the screen, the mouse follows the corner next to it, and the mic lights up with whatever is
happening on that side of the monitor.

No pip install. No runtime. **Zero third-party Python packages** — the whole thing is the standard
library, `ctypes`, and one bundled `hidapi.dll`.

---

## What it drives

| Device | Path |
|---|---|
| Motherboard / RAM / anything OpenRGB sees | OpenRGB SDK |
| Razer DeathAdder V2 | OpenRGB SDK |
| SteelSeries Apex 3 TKL (8-zone) | direct USB HID |
| ASUS XG248Q monitor backlight | direct USB HID |
| Fifine BM38 boom arm | direct USB HID |
| Fifine AM8 Pro microphone | direct USB HID |

The keyboard takes whichever path is available — OpenRGB if it has the device, otherwise
`apex3tkl.py` talks to it directly.

## Why it talks to hardware directly

OpenRGB is normally installed as a **LocalSystem service**, and a service runs one device-detection
pass at boot and never rescans. A device that enumerates late — plugged in after boot, or slow to
come up — is simply absent from OpenRGB's list until the next reboot, with no way to refresh it.
Driving those devices over HID ourselves removes that failure mode entirely.

## Two failure modes it handles that most ambient-light tools don't

Both of these look identical from software — the device reports success and sits there unlit:

- **Vendor software reclaims the device.** Fifine Genie, SignalRGB and friends pull hardware back
  onto their own profile. OpenRGB keeps accepting colour writes afterwards and returns success, so
  the device reads as perfectly healthy while doing nothing. Direct mode is therefore
  **checked every few seconds and re-asserted when it has been taken away**, rather than set once.
  Checked, not blindly re-sent: a mode switch is a real one on the hardware, and re-sending it on a
  timer shows up as a periodic blip on every device.
- **A device disappears or shows up late.** Missing devices are retried on a background timer, so
  plugging something in — or starting OpenRGB after the app — no longer means restarting anything.

## Architecture

```
screen.py     CaptureThread + Sampler — GDI screen capture on its own thread
   |            (decoupled so slow USB writes never stall the capture loop)
   v
ambient.py    Engine — per-device colour pipeline, timers, device lifecycle
   |            saturation / gamma / brightness / per-channel gain, per device
   +--> orgb.py       minimal OpenRGB SDK client (sockets, no dependency)
   +--> hidlib.py     shared hidapi plumbing: rate limit, skip-if-unchanged,
   |                  periodic forced refresh
   +--> apex3tkl.py   SteelSeries Apex 3 TKL 8-zone driver
   +--> xg248q.py     ASUS XG248Q backlight
   +--> fifine.py     Fifine BM38 / AM8 Pro

rgbsync_gui.py   Tkinter control panel. The engine runs on a worker thread; the
                 window mutates a shared CFG dict the engine re-reads every
                 frame, so every slider takes effect live.
   +--> layout_editor.py   drag the screen region each device samples
   +--> tuning_window.py   per-device colour tuning
   +--> prereq_window.py   dependency pre-flight checker
```

Supporting modules: `letterbox.py` (black-bar detection), `session.py` (lock/idle handling),
`autostart.py` (registry run-key), `logbook.py` (diagnostic logging), `prereqs.py` (environment
checks and the safe subset of fixes).

## Requirements

- Windows 10 or 11
- Python 3.11+ (`pythonw` for the GUI)
- [OpenRGB](https://openrgb.org/releases.html) — optional, only for devices on the SDK path.
  Enable **Server → Start Server**.

`prereqs.py` checks all of this for you and reports what's missing, what's optional, and which
known-conflicting vendor apps are running. It installs OpenRGB through `winget` (which does its own
trust checks and prompting) or points you at the official download page — it never silently
downloads or executes a binary.

## Running

```
Start RGB Sync.bat     GUI control panel
RGB Sync.bat           console engine, no window
```

Or directly:

```
pythonw rgbsync_gui.py
python -u ambient.py
```

## Configuration

Settings live in `config.json`, written next to the scripts. Copy `config.example.json` to
`config.json` to start from a sane tuned baseline, or just run the app — every key falls back to a
built-in default, and old config layouts are migrated automatically on load.

The interesting keys:

| Key | Meaning |
|---|---|
| `fps` | capture rate |
| `grid` | sampling grid resolution |
| `regions` | per-device screen rectangle + sampling axis (`x`, `y`, `avg`, `grid`) |
| `device` | per-device saturation, gamma, brightness, RGB gain |
| `orgb_hz` | cap on colour writes per second per OpenRGB device (the HID drivers have their own) |
| `letterbox` | ignore black bars when sampling |
| `pause_on_lock` | stop driving devices while the workstation is locked |
| `rescan_sec` | how often to retry missing devices |
| `mode_refresh_sec` | how often to check that devices are still in direct mode |
| `mode_verify` | read the current mode before re-asserting it; `false` re-asserts blind |

Edit regions visually with **Screen layout** in the GUI rather than by hand.

## Licence and attribution

Released under **GPL-2.0-or-later** — see [LICENSE](LICENSE).

The Apex 3 TKL protocol in `apex3tkl.py` is implemented from the reverse-engineering notes in
OpenRGB's `SteelSeriesApex8ZoneController` (GPL-2.0-or-later), which is why this project carries the
same licence.

`hidapi.dll` is bundled for convenience and is distributed under the BSD-3-Clause licence by the
[hidapi project](https://github.com/libusb/hidapi). It is the unmodified **0.14.0** Windows x64
release binary. Since you should not take a stranger's DLL on trust, verify it before running
anything:

```powershell
(Get-FileHash hidapi.dll -Algorithm SHA256).Hash
# EBEB835E2B4530ED68843F19D6A2604C51772E3C26E7F542FDE194075F82D9B4
```

If you would rather not use the bundled copy at all, delete it and drop in `hidapi.dll` from the
[official 0.14.0 release](https://github.com/libusb/hidapi/releases/tag/hidapi-0.14.0) — nothing
here depends on this particular build.

Device names and trademarks belong to their respective owners. This project is not affiliated with
or endorsed by ASUS, SteelSeries, Fifine, Razer, or OpenRGB.
