"""RGB Screen Sync - control panel.

Tkinter (ships with Python, nothing to install). The engine runs on a worker
thread; this window only reads its stats and mutates the shared CFG dict, which
the engine re-reads every frame - so every slider takes effect live.
"""
import threading, traceback, sys, socket, json
import autostart
import tkinter as tk
from tkinter import ttk

import ambient
import logbook
from ambient import CFG, Engine, load_config, save_config
from layout_editor import LayoutEditor, DEVICES, COLOR
from prereq_window import PrereqWindow
from tuning_window import TuningWindow

BG      = "#16181d"
PANEL   = "#1e2128"
FG      = "#e6e8ec"
MUTED   = "#8b93a1"
ACCENT  = "#5b8cff"
OK      = "#3ddc84"
BAD     = "#ff6b6b"

SLIDERS = [
    ("saturation", "Saturation", 0.0, 3.0),
    ("gamma",      "Gamma",      0.4, 2.0),
    ("brightness", "Brightness", 0.0, 1.0),
    ("smoothing",  "Smoothing",  0.0, 0.95),
]


def apply_theme(widget):
    s = ttk.Style(widget)
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass
    s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=PANEL)
    s.configure("TLabel", background=BG, foreground=FG)
    s.configure("Card.TLabel", background=PANEL, foreground=FG)
    s.configure("Muted.TLabel", background=BG, foreground=MUTED)
    s.configure("Head.TLabel", background=BG, foreground=MUTED,
                font=("Segoe UI", 9, "bold"))
    s.configure("Title.TLabel", background=BG, foreground=FG,
                font=("Segoe UI", 15, "bold"))
    # readable check/radio indicators - the clam defaults render nearly invisible
    s.configure("TCheckbutton", background=PANEL, foreground=FG, font=("Segoe UI", 9),
                indicatorcolor=PANEL, focuscolor=PANEL)
    # clam reads indicatorbackground/-foreground; indicatorcolor is what the
    # other themes use. Set both so the box is blue when on, grey when off.
    ind = dict(indicatorcolor=[("selected", ACCENT), ("!selected", "#39404f")],
               indicatorbackground=[("selected", ACCENT), ("!selected", "#39404f")],
               indicatorforeground=[("selected", "#ffffff"), ("!selected", "#39404f")],
               upperbordercolor=[("selected", ACCENT), ("!selected", "#4a5263")],
               lowerbordercolor=[("selected", ACCENT), ("!selected", "#4a5263")])
    s.map("TCheckbutton", background=[("active", PANEL)], **ind)
    s.configure("TRadiobutton", background=PANEL, foreground=FG, font=("Segoe UI", 9),
                indicatorcolor=PANEL, focuscolor=PANEL)
    s.map("TRadiobutton", background=[("active", PANEL)], **ind)
    s.configure("TScale", background=PANEL, troughcolor="#2a2f3a")
    s.configure("Accent.TButton", background=ACCENT, foreground="#0b1020",
                borderwidth=0, focusthickness=0, font=("Segoe UI", 10, "bold"))
    s.map("Accent.TButton", background=[("active", "#7aa2ff"), ("disabled", "#3a4257")])
    s.configure("Ghost.TButton", background="#2a2f3a", foreground=FG, borderwidth=0)
    s.map("Ghost.TButton", background=[("active", "#39404f"), ("disabled", "#1a1d23")])
    s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0),
                bordercolor=BG, lightcolor=BG, darkcolor=BG)
    s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(16, 6),
                borderwidth=0, bordercolor=BG, lightcolor=BG, darkcolor=BG,
                focuscolor=BG, font=("Segoe UI", 9, "bold"))
    s.map("TNotebook.Tab",
          background=[("selected", PANEL), ("active", "#23262e")],
          foreground=[("selected", FG), ("active", FG)],
          lightcolor=[("selected", PANEL)], bordercolor=[("selected", PANEL)])


class App:
    def __init__(self, root):
        self.root = root
        self.engine = None
        self.thread = None
        self.starting = False
        self.editor = None
        self.prereqs = None
        self.tuning = None

        root.title("RGB Screen Sync")
        root.configure(bg=BG)

        apply_theme(root)
        self._build()
        self._fit_window()
        self._repair_autostart()
        self._saved = self._disk_snapshot()
        self._pending = None
        self._tick()
        self.root.after(1000, self._autosave)

    def _fit_window(self):
        """Size the window to its content, capped to the screen."""
        self.root.update_idletasks()
        w = max(580, self.root.winfo_reqwidth())
        h = min(self.root.winfo_reqheight(), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(min(w, 560), min(h, 560))

    # ------------------------------------------------------------ autosave --
    @staticmethod
    def _snapshot():
        return json.dumps(CFG, sort_keys=True)

    @staticmethod
    def _disk_snapshot():
        try:
            with open(ambient.CONFIG_PATH, encoding="utf-8") as f:
                return json.dumps(json.load(f), sort_keys=True)
        except (OSError, ValueError):
            return None

    def _autosave(self):
        """Save once settings have stopped changing for a moment.

        There used to be only a Save button: a slider moved and then Quit,
        or a shutdown, and the change was gone. Waiting for two equal
        snapshots a second apart keeps a slider drag from writing the file
        dozens of times.
        """
        self._save_if_changed(settle=True)
        self.root.after(1000, self._autosave)

    def _save_if_changed(self, settle=False):
        snap = self._snapshot()
        if snap == self._saved:
            self._pending = None
            return
        if settle and snap != self._pending:
            self._pending = snap            # still changing - look again next second
            return
        try:
            save_config()
            self._saved = snap
        except OSError as e:
            if self._pending != "failed":
                self.write(f"could not save settings: {e}")
            self._pending = "failed"

    def _card(self, parent, title, expand=False):
        ttk.Label(parent, text=title.upper(), style="Head.TLabel").pack(
            anchor="w", pady=(14, 4))
        f = ttk.Frame(parent, style="Card.TFrame", padding=12)
        f.pack(fill="both" if expand else "x", expand=expand)
        return f

    WRAP = 470          # px; hints wrap to the card instead of hard line breaks

    def _hint(self, parent, text, padx=0, pady=(0, 6)):
        lab = ttk.Label(parent, text=text, style="Card.TLabel", foreground=MUTED,
                        font=("Segoe UI", 8), justify="left", wraplength=self.WRAP - padx)
        lab.pack(anchor="w", padx=(padx, 0), pady=pady)
        return lab

    def _check(self, parent, text, getter, setter, hint=None):
        v = tk.BooleanVar(value=getter())
        ttk.Checkbutton(parent, text=text, variable=v,
                        command=lambda: setter(v.get())).pack(anchor="w")
        if hint:
            self._hint(parent, hint, padx=24)
        return v

    # -------------------------------------------------------------- layout --
    def _build(self):
        """Header and controls on top, the rest in three tabs.

        It used to be one long column: about 1350px of content in a 920px
        window, so on a 1080p screen the Behaviour options and the log were
        cut off, and the top row was too narrow to show the Rescan button.
        """
        outer = ttk.Frame(self.root, padding=(16, 14, 16, 14))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="RGB Screen Sync", style="Title.TLabel").pack(side="left")
        self.stat = ttk.Label(head, text="stopped", style="Muted.TLabel")
        self.stat.pack(side="right")

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(12, 0))
        self.btn_start = ttk.Button(btns, text="Start", style="Accent.TButton",
                                    command=self.start)
        self.btn_start.pack(side="left", ipadx=18, ipady=4)
        self.btn_stop = ttk.Button(btns, text="Stop", style="Ghost.TButton",
                                   command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8, ipadx=18, ipady=4)
        self.btn_rescan = ttk.Button(btns, text="Rescan", style="Ghost.TButton",
                                     command=self.rescan, state="disabled")
        self.btn_rescan.pack(side="left", ipadx=10, ipady=4)
        ttk.Button(btns, text="Quit", style="Ghost.TButton",
                   command=self.quit).pack(side="right", ipadx=14, ipady=4)

        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True, pady=(14, 0))
        tab_dev, tab_col, tab_beh = (ttk.Frame(nb) for _ in range(3))
        nb.add(tab_dev, text="Devices")
        nb.add(tab_col, text="Colour")
        nb.add(tab_beh, text="Behaviour")

        # ---- devices + log
        card = self._card(tab_dev, "Devices")
        self.dev_vars, self.dev_labels, self.swatches = {}, {}, {}
        for key, label, col in DEVICES:
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(fill="x", pady=2)
            tk.Canvas(row, width=10, height=10, bg=col, highlightthickness=0,
                      bd=0).pack(side="left", padx=(0, 7))
            v = tk.BooleanVar(value=CFG["enabled"].get(key, True))
            self.dev_vars[key] = v
            ttk.Checkbutton(row, text=label, variable=v,
                            command=lambda k=key: self._toggle(k)).pack(side="left")
            cv = tk.Canvas(row, width=132, height=14, bg=PANEL,
                           highlightthickness=0, bd=0)
            cv.pack(side="right")
            self.swatches[key] = cv
            lab = ttk.Label(row, text="", style="Card.TLabel",
                            foreground=MUTED, font=("Segoe UI", 8))
            lab.pack(side="right", padx=8)
            self.dev_labels[key] = lab

        tools = ttk.Frame(card, style="Card.TFrame")
        tools.pack(fill="x", pady=(10, 0))
        ttk.Button(tools, text="Screen layout...", style="Ghost.TButton",
                   command=self.open_editor).pack(side="left", fill="x",
                                                  expand=True, padx=(0, 4), ipady=4)
        ttk.Button(tools, text="Device tuning...", style="Ghost.TButton",
                   command=self.open_tuning).pack(side="left", fill="x",
                                                  expand=True, padx=4, ipady=4)
        ttk.Button(tools, text="Prerequisites...", style="Ghost.TButton",
                   command=self.open_prereqs).pack(side="left", fill="x",
                                                   expand=True, padx=(4, 0), ipady=4)

        card = self._card(tab_dev, "Log", expand=True)
        bar = ttk.Frame(card, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Also saved to logs\\rgbsync.log", style="Card.TLabel",
                  foreground=MUTED, font=("Segoe UI", 8)).pack(side="left")
        ttk.Button(bar, text="Open log folder", style="Ghost.TButton",
                   command=self.open_logs).pack(side="right", ipadx=6)
        self.log = tk.Text(card, height=8, bg="#12141a", fg=MUTED, bd=0,
                           insertbackground=FG, font=("Consolas", 8), wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

        # ---- colour
        card = self._card(tab_col, "Colour quality")
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Sampling", style="Card.TLabel", width=11).pack(side="left")
        self.quality = tk.StringVar(value=CFG.get("quality", "vivid"))
        for val, txt in (("mean", "Flat"), ("linear", "Accurate"), ("vivid", "Vivid")):
            ttk.Radiobutton(row, text=txt, value=val, variable=self.quality,
                            command=self._set_quality).pack(side="left", padx=(0, 10))
        self._slider(card, "vividness", "Vividness", 0.0, 10.0, is_int=False)
        self.q_hint = self._hint(card, "", pady=(2, 8))
        self._check(card, "True-to-screen hues",
                    lambda: CFG.get("hue_correct", True),
                    lambda on: CFG.__setitem__("hue_correct", on),
                    "LEDs are linear, screens are not: without this, mixed colours come "
                    "out pale and shifted (orange reads yellow). Needs less saturation "
                    "than before - about 1.0-1.3 looks right.")
        self._set_quality(log=False)

        card = self._card(tab_col, "Master appearance")
        for key, label, lo, hi in SLIDERS:
            self._slider(card, key, label, lo, hi, is_int=False)
        self._slider(card, "fps", "Frame rate", 5, 60, is_int=True)
        self._hint(card, "Lower gamma = brighter. Higher smoothing = slower to react. "
                         "Devices given their own values in Device tuning ignore these.",
                   pady=(8, 0))

        # ---- behaviour
        card = self._card(tab_beh, "Behaviour")
        self.var_autostart = self._check(
            card, "Start with Windows", autostart.is_enabled, self._set_autostart,
            "Launches this app silently at login and starts syncing.")
        self._check(card, "Pause while locked",
                    lambda: CFG.get("pause_on_lock", True),
                    lambda on: CFG.__setitem__("pause_on_lock", on),
                    "Lights off on the lock screen, back on when you sign in.")
        self._check(card, "Pause while the display is off",
                    lambda: CFG.get("pause_on_display_off", True),
                    lambda on: CFG.__setitem__("pause_on_display_off", on),
                    "Lights off when Windows turns the monitor off. Shutdown and sleep "
                    "always black everything out first.")
        self._check(card, "Keep lights dark when stopped",
                    lambda: CFG.get("dark_when_stopped", True),
                    lambda on: CFG.__setitem__("dark_when_stopped", on),
                    "Stop holds every device black. Off: Stop lets go, and the mic and "
                    "arm go back to their own rainbow effect.")
        self._check(card, "Closing the window keeps lights dark",
                    lambda: CFG.get("close_to_background", True),
                    lambda on: CFG.__setitem__("close_to_background", on),
                    "Closing hides the window and holds every device dark in the "
                    "background - the arm has no off command, so it lights up whenever "
                    "the app isn't running. Open RGB Sync again to get the window back; "
                    "Quit exits for real.")
        self._check(card, "Crop black bars in films",
                    lambda: CFG.get("letterbox", False),
                    lambda on: CFG.__setitem__("letterbox", on),
                    "Ignores letterbox bars in films. Best-guess detection - it holds "
                    "off unless confident.")
        ttk.Label(tab_beh, text="Settings are saved automatically.",
                  style="Muted.TLabel", font=("Segoe UI", 8)).pack(anchor="w", pady=(10, 0))

    def _slider(self, parent, key, label, lo, hi, is_int):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, style="Card.TLabel", width=11).pack(side="left")
        txt = str(CFG[key]) if is_int else f"{CFG[key]:.2f}"
        val = ttk.Label(row, text=txt, style="Card.TLabel",
                        foreground=MUTED, width=5)
        val.pack(side="right")
        var = tk.DoubleVar(value=CFG[key])

        def cb(_e=None):
            CFG[key] = int(round(var.get())) if is_int else round(var.get(), 3)
            val.config(text=str(CFG[key]) if is_int else f"{CFG[key]:.2f}")
        ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal",
                  command=lambda _e: cb()).pack(side="left", fill="x",
                                                expand=True, padx=8)

    HINTS = {
        "mean":   "Plain average of screen pixels. Mixes colours muddier and darker "
                  "than real light - small bright things wash out.",
        "linear": "Averages in linear light, the way light physically mixes. "
                  "A fire or HUD in a dark scene stays visible.",
        "vivid":  "Linear, but saturated pixels count for more, so a colourful "
                  "object drives the colour instead of averaging into grey.",
    }

    def _set_quality(self, log=True):
        CFG["quality"] = self.quality.get()
        self.q_hint.config(text=self.HINTS.get(CFG["quality"], ""))
        if log:
            self.write(f"colour sampling -> {CFG['quality']}")

    # ------------------------------------------------------------- actions --
    def write(self, msg):
        logbook.write(msg)

        def do():
            self.log.configure(state="normal")
            self.log.insert("end", str(msg) + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, do)

    def open_editor(self):
        if self.editor and self.editor.winfo_exists():
            self.editor.lift()
            self.editor.focus_force()
            return
        self.editor = LayoutEditor(self.root, lambda: self.engine)

    def open_tuning(self):
        if self.tuning and self.tuning.winfo_exists():
            self.tuning.lift(); self.tuning.focus_force(); return
        self.tuning = TuningWindow(self.root, lambda: self.engine)

    def open_logs(self):
        try:
            logbook.open_folder()
        except Exception as e:
            self.write(f"could not open the log folder: {e}")

    def rescan(self):
        """Re-look for every device that is currently missing."""
        eng = self.engine
        if not eng:
            return
        self.write("rescanning for missing devices...")

        def work():
            try:
                gained = eng.call(eng.rescan).result(timeout=60)
                if not gained:
                    missing = eng.missing()
                    self.write("rescan: nothing new"
                               + (f" - still missing: {', '.join(missing)}" if missing
                                  else " - everything is already connected"))
            except Exception as e:
                self.write(f"rescan failed: {type(e).__name__}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def open_prereqs(self):
        if self.prereqs and self.prereqs.winfo_exists():
            self.prereqs.lift(); self.prereqs.focus_force(); return
        self.prereqs = PrereqWindow(self.root)

    def _toggle(self, key):
        CFG["enabled"][key] = self.dev_vars[key].get()
        eng = self.engine
        if eng and not CFG["enabled"][key]:
            eng.call(lambda: eng.blackout_key(key))     # on the engine thread
        elif eng and not eng.found.get(key):
            # disabled devices are never looked for, so look now
            self.rescan()

    def start(self):
        eng = self.engine
        if eng and eng.held:
            eng.hold(False)
            self.write("--- resumed ---")
            return
        if self.thread and self.thread.is_alive():
            return
        self.starting = True
        self.btn_start.config(state="disabled")
        self.write("--- starting ---")

        def worker():
            try:
                logbook.session_header("engine")
                logbook.snapshot()
                self.engine = Engine(log=self.write)
            except Exception as e:
                self.write(f"FAILED to start: {e}")
                self.write("Is OpenRGB running with its SDK server enabled?")
                for line in traceback.format_exc().splitlines()[-3:]:
                    self.write("  " + line)
                self.engine = None
                self.starting = False
                return
            self.starting = False
            try:
                self.engine.run()
            except Exception as e:
                self.write(f"engine stopped: {e}")
            finally:
                try:
                    self.engine.close()
                except Exception:
                    pass
                self.engine = None
                self.write("--- stopped ---")

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def stop(self):
        eng = self.engine
        if eng and CFG.get("dark_when_stopped", True):
            eng.hold(True)
            self.write("--- stopped (holding lights dark) ---")
        elif eng:
            eng.stop()
        self.btn_stop.config(state="disabled")

    def _repair_autostart(self):
        """Folder moved since autostart was switched on? Follow it."""
        try:
            old = autostart.repair()
        except OSError as e:
            self.write(f"could not check the start-with-Windows entry: {e}")
            return
        if old:
            self.write("start with Windows: pointed at the app's new location")
            self.write(f"  was: {old}")
            self.write(f"  now: {autostart.command()}")

    def _set_autostart(self, on):
        try:
            autostart.set_enabled(on)
            if on:
                self.write(f"start with Windows: ON")
                self.write(f"  {autostart.command()}")
            else:
                self.write("start with Windows: OFF")
        except Exception as e:
            self.write(f"could not change autostart: {e}")
            self.var_autostart.set(autostart.is_enabled())

    # ------------------------------------------------------------- refresh --
    def _tick(self):
        eng = self.engine
        alive = bool(self.thread and self.thread.is_alive())
        held = bool(eng and eng.running and eng.held)
        running = bool(eng and eng.running) and not held

        self.btn_start.config(state="normal" if held or not (alive or self.starting)
                              else "disabled")
        self.btn_stop.config(state="normal" if running else "disabled")
        self.btn_rescan.config(state="normal" if running else "disabled")

        if held:
            self.stat.config(text="stopped - holding lights dark", foreground=MUTED)
        elif running and getattr(eng, "paused", False):
            self.stat.config(text=f"paused ({eng.pause_reason})", foreground=MUTED)
        elif running:
            cap = getattr(eng.cap, "fps", 0.0)
            extra = ""
            lb = getattr(eng, "letterbox", None)
            if lb is not None and lb.active:
                extra = "   cropped"
            grab = getattr(eng.cap, "grab_ms", 0.0)
            self.stat.config(text=f"running  {eng.fps_actual:4.1f} fps  "
                                  f"{eng.cap.backend or 'capture'} {cap:4.1f}/s {grab:.1f} ms  "
                                  f"cpu {eng.cpu_pct:.0f}%{extra}",
                             foreground=OK)
        elif self.starting:
            self.stat.config(text="starting...", foreground=MUTED)
        else:
            self.stat.config(text="stopped", foreground=MUTED)

        for key, _label, _col in DEVICES:
            cv = self.swatches[key]
            cv.delete("all")
            lab = self.dev_labels[key]
            if eng:
                found = eng.found.get(key)
                if not CFG["enabled"].get(key, True):
                    lab.config(text="off", foreground=MUTED)
                elif found:
                    txt = found if len(found) <= 30 else found[:29] + "\u2026"
                    lab.config(text=txt, foreground=MUTED)
                else:
                    lab.config(text="not found", foreground=BAD)
            else:
                lab.config(text="")
            cols = (eng.preview.get(key) if eng else None) or []
            if cols and running and CFG["enabled"].get(key, True):
                n = min(len(cols), 24)
                step = max(1, len(cols) // n)
                shown = cols[::step][:n]
                w = 132 / len(shown)
                for i, (r, g, b) in enumerate(shown):
                    cv.create_rectangle(i * w, 0, (i + 1) * w, 14,
                                        fill=f"#{r:02x}{g:02x}{b:02x}", outline="")
        self.root.after(120, self._tick)

    def on_close(self):
        """Window X. Hide and hold dark rather than exit, when that is on.

        Exiting is the one state nothing can keep the BM38 arm dark in: it has
        no lighting-off command, so its onboard effect runs the moment our
        stream stops. A running app can hold it black indefinitely.
        """
        eng = self.engine
        if CFG.get("close_to_background", True) and eng and eng.running:
            if not eng.held:
                eng.hold(True)
                self.write("--- window closed: running in background, lights dark ---")
            self.root.withdraw()
            return
        self.quit()

    def show(self):
        """Bring the window back - another launch of the app asked for it."""
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def quit(self):
        self._save_if_changed()
        if self.engine:
            self.engine.stop()
        if self.thread:
            self.thread.join(timeout=2.0)
        self.root.destroy()


# One copy at a time: a second one would fight the first over every device.
# The first copy listens on a loopback port; a later launch finds it taken,
# asks the first to show its window, and exits. Loopback-only, so no firewall
# prompt and nothing reachable from outside the PC.
INSTANCE_PORT = 47391


def claim_instance():
    """-> (listening socket or None, already_running).

    already_running is True only when another copy actually answered. If the
    port is held by something else entirely, we run anyway, just without the
    hand-off - refusing to start over a port clash would be worse.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen(2)
        return srv, False
    except OSError:
        srv.close()
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1.0) as c:
            c.sendall(b"show")
            c.shutdown(socket.SHUT_WR)
            c.settimeout(1.0)
            ok = c.recv(16).startswith(b"ok")
    except OSError:
        ok = False
    return None, ok


def serve_instance(srv, on_show):
    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2.0)
                    if conn.recv(16).startswith(b"show"):
                        conn.sendall(b"ok")
                        on_show()
                    # Let the caller hang up first: whichever side closes
                    # first holds the port in TIME_WAIT, and that must not be
                    # the listening port, or a quick relaunch can't bind it.
                    while conn.recv(16):
                        pass
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    srv, running = claim_instance()
    if running:
        sys.exit(0)             # already running - it has been asked to show itself
    load_config()
    logbook.session_header("gui")
    root = tk.Tk()
    app = App(root)
    if srv is not None:
        serve_instance(srv, lambda: root.after(0, app.show))
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    if "--autostart" in sys.argv:
        # launched at login: start syncing straight away, out of the way
        root.after(400, app.start)
        root.iconify()
    root.mainloop()
