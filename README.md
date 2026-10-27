# RGB Screen Sync

Real-time ambient lighting for Windows. Samples what's on your screen and pushes the colour to every
RGB device you own — each one reading its **own** region of the display, so the keyboard follows the
bottom of the screen, the boom arm follows the side of the monitor it sits on, and the mic lights up
with whatever is happening there.

No pip install. No runtime. **Zero third-party Python packages** — the whole thing is the standard
library, `ctypes`, and one bundled `hidapi.dll`.

---

## What it drives

| Device | Path |
|---|---|
| Motherboard / RAM / anything OpenRGB sees | OpenRGB SDK |
| An RGB mouse (off by default — the Razer Viper here has no LEDs, only a battery light) | OpenRGB SDK |
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

## Dark when the PC is off

Off has to be an active state for some of this hardware. The Fifine mic and arm show our colours only
while a stream of frames keeps arriving; once it stops they go back to their onboard rainbow. So:

- **Locked, display asleep, or Stop pressed:** the engine keeps streaming black. They stay dark.
  (Stop holds the lights dark by default; untick *Keep lights dark when stopped* to let go instead.)
- **Shutdown, restart, sign-out and sleep:** `power.py` catches them before Windows goes ahead,
  and everything is blacked out first.
- **After the PC is off:** no software is running to hold anything, so what you see is whatever each
  device does on its own with standby power. Change these once:

| Device | Setting |
|---|---|
| Fifine mic | Handled by the app: it switches the AM8 Pro's own lighting off whenever it lets go (exit, shutdown, sleep) and back on when it takes over (`am8_light_off`). The command comes from Fifine Genie's plugin. |
| Fifine arm (and mic, as a fallback) | The BM38 has no lighting-off command; Genie's own plugin doesn't have one either, and holding its button switches it off in a way the app can't override. So only a running app keeps it dark: closing the window hides it and keeps holding the lights black (`close_to_background`; launching the app again brings the window back, **Quit** exits). With the PC off: **BIOS → Advanced → APM Configuration → ErP Ready → Enabled (S4+S5)**. This cuts USB standby power when the PC is off, so they have nothing to light up with. Keyboard lights go dark too, and USB charging stops while the PC is off. Alternatively, switch their own lighting off with the RGB touch button or Fifine Genie. The device remembers that setting, and the app overrides it while it runs. |
| Monitor backlight | Nothing to change: leave **Aura Sync on** in the monitor's OSD. With Aura Sync on, the XG248Q keeps the last colour it was sent, and the app sends black before Windows shuts down, so the backlight stays dark with the PC off. (With Aura Sync off the app can't drive the backlight at all.) Confirmed on hardware. |
| Motherboard | BIOS → Advanced → Onboard Devices Configuration → **LED lighting → Stealth mode** for the sleep/off states. |

Sleep (S3) keeps USB powered and ErP does not apply, so the arm shows its onboard effect
while the PC sleeps unless its own lighting is switched off on the arm.

## Performance

Screen capture is the only part a game can feel. The old GDI capture made Windows copy the whole
composited desktop back from the GPU and scale it down on the CPU: about 12-14 ms and 20% of a core
at 30 grabs a second on a 1440p screen. Older versions also grabbed **120 times a second, always**,
even when the engine ran at 30-60 fps and even while paused. Now:

- capture uses **DXGI Desktop Duplication** (`dxgi_capture.py`): the GPU shrinks the desktop with
  its own mipmaps, and only a ~80x45 image (a few KB) is read back. When nothing on screen has
  changed, the last frame is reused at no cost. Frames that arrive in 16-bit colour (Windows 11
  Auto Color Management or HDR) are handled too, and converted after the GPU shrinks them. If it
  can't start (rotated display, remote session) or keeps failing, it falls back to GDI on its own. The status line shows which
  one is running (`dxgi` or `gdi`), and `capture` in the config can force either;
- capture runs at the engine's frame rate (default **30**; no device accepts colour faster), and
  stops entirely while paused or held dark;
- the process runs at **below-normal priority** (`low_priority`), so a game's threads always go first;
- the status line shows the live cost: capture rate, ms per grab, and the app's CPU use.

To measure it on your machine, close the GUI and run:

```
python bench.py --engine
```

That compares a baseline with GDI capture at 30/60/120 per second, DXGI capture, and the full
engine. Keep something moving on screen during the DXGI phases (a video, or drag a window), because
on a still desktop it costs almost nothing and the numbers would flatter it. For each one it
reports CPU use, grab time, and how late an unrelated normal-priority thread wakes up (a stand-in for
a game). For the in-game number, run the same scene with an FPS/frametime overlay (Steam, NVIDIA,
AMD, or CapFrameX/PresentMon for 1% lows), once with the app syncing and once after pressing Stop.
Stop now halts capture too, so the difference between the two runs is the app's cost.

## Colour accuracy

- **Linear-light sampling**: screen colours are averaged as light, not as sRGB bytes.
- **True-to-screen hues** (`hue_correct`): LEDs are linear and screens are not, so sending the
  screen's bytes straight through makes every mixed colour pale and shifted (orange reads yellow,
  teal reads cyan). Channel ratios now come from linear light, and the brightest channel keeps its
  level, so brightness is unchanged. This needs much less saturation than before. Configs from older
  versions are reduced automatically, and the original is kept as `config.v1.json`.
- **Vivid** mode no longer lets near-black capture noise count as "saturated" colour in dark scenes.
- Per-device gain (Device tuning) is white balance: put the screen on white and trim each device
  until it matches.

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
`power.py` (shutdown / sleep / display-off notifications), `bench.py` (performance measurement),
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
| `hue_correct` | mix LED colours in linear light (see Colour accuracy) |
| `pause_on_display_off` | black out while Windows has the monitor off |
| `dark_when_stopped` | Stop keeps the devices black instead of letting go |
| `capture` | `auto` (DXGI, falling back to GDI), `dxgi`, or `gdi` |
| `am8_light_off` | switch the mic's own lighting off when the app lets go |
| `close_to_background` | closing the GUI window hides it and holds the lights dark instead of exiting |
| `low_priority` | run below normal priority so games always win the CPU |

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
