"""RGB Screen Sync - control panel.

Tkinter (ships with Python, nothing to install). The engine runs on a worker
thread; this window only reads its stats and mutates the shared CFG dict, which
the engine re-reads every frame - so every slider takes effect live.
"""
import threading, traceback, sys
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
    s.configure("TCheckbutton", background=PANEL, foreground=FG,
                indicatorcolor=PANEL, focuscolor=PANEL)
    s.map("TCheckbutton", background=[("active", PANEL)],
          indicatorcolor=[("selected", ACCENT), ("!selected", "#39404f")])
    s.configure("TRadiobutton", background=PANEL, foreground=FG,
                indicatorcolor=PANEL, focuscolor=PANEL)
    s.map("TRadiobutton", background=[("active", PANEL)],
          indicatorcolor=[("selected", ACCENT), ("!selected", "#39404f")])
    s.configure("TScale", background=PANEL, troughcolor="#2a2f3a")
    s.configure("Accent.TButton", background=ACCENT, foreground="#0b1020",
                borderwidth=0, focusthickness=0, font=("Segoe UI", 10, "bold"))
    s.map("Accent.TButton", background=[("active", "#7aa2ff"), ("disabled", "#3a4257")])
    s.configure("Ghost.TButton", background="#2a2f3a", foreground=FG, borderwidth=0)
    s.map("Ghost.TButton", background=[("active", "#39404f"), ("disabled", "#1a1d23")])


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
        root.geometry("580x810")
        root.minsize(520, 640)

        apply_theme(root)
        self._build()
        self._tick()

    def _card(self, parent, title, expand=False):
        ttk.Label(parent, text=title.upper(), style="Head.TLabel").pack(
            anchor="w", pady=(14, 4))
        f = ttk.Frame(parent, style="Card.TFrame", padding=12)
        f.pack(fill="both" if expand else "x", expand=expand)
        return f

    # -------------------------------------------------------------- layout --
    def _build(self):
        outer = ttk.Frame(self.root, padding=16)
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
        ttk.Button(btns, text="Save settings", style="Ghost.TButton",
                   command=self.save).pack(side="right", ipadx=10, ipady=4)
        self.btn_rescan = ttk.Button(btns, text="Rescan", style="Ghost.TButton",
                                     command=self.rescan, state="disabled")
        self.btn_rescan.pack(side="right", padx=8, ipadx=10, ipady=4)

        # devices
        card = self._card(outer, "Devices")
        self.dev_vars, self.dev_labels, self.swatches = {}, {}, {}
        for key, label, col in DEVICES:
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(fill="x", pady=2)
            tk.Canvas(row, width=10, height=10, bg=col, highlightthickness=0,
                      bd=0).pack(side="left", padx=(0, 7))
            v = tk.BooleanVar(value=CFG["enabled"].get(key, True))
            self.dev_vars[key] = v
            tk.Checkbutton(row, text=label, variable=v,
                           command=lambda k=key: self._toggle(k),
                           bg=PANEL, fg=FG, selectcolor=ACCENT,
                           activebackground=PANEL, activeforeground=FG,
                           highlightthickness=0, bd=0,
                           font=("Segoe UI", 9)).pack(side="left")
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

        # colour quality
        card = self._card(outer, "Colour quality")
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Sampling", style="Card.TLabel", width=11).pack(side="left")
        self.quality = tk.StringVar(value=CFG.get("quality", "vivid"))
        for val, txt in (("mean", "Flat"), ("linear", "Accurate"), ("vivid", "Vivid")):
            tk.Radiobutton(row, text=txt, value=val, variable=self.quality,
                           command=self._set_quality,
                           bg=PANEL, fg=FG, selectcolor=ACCENT,
                           activebackground=PANEL, activeforeground=FG,
                           highlightthickness=0, bd=0,
                           font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        self._slider(card, "vividness", "Vividness", 0.0, 10.0, is_int=False)
        self.q_hint = ttk.Label(card, justify="left", style="Card.TLabel",
                                foreground=MUTED, font=("Segoe UI", 8), text="")
        self.q_hint.pack(anchor="w", pady=(6, 0))
        self._set_quality(log=False)

        # appearance
        card = self._card(outer, "Master appearance")
        for key, label, lo, hi in SLIDERS:
            self._slider(card, key, label, lo, hi, is_int=False)
        self._slider(card, "fps", "Frame rate", 5, 60, is_int=True)
        ttk.Label(card, justify="left", style="Card.TLabel", foreground=MUTED,
                  font=("Segoe UI", 8),
                  text=("Lower gamma = brighter. Higher smoothing = slower to react.\n"
                        "Devices given their own values in Device tuning ignore these."
                        )).pack(anchor="w", pady=(8, 0))

        # behaviour
        card = self._card(outer, "Behaviour")
        self.behaviour = {}

        def check(parent, text, getter, setter, hint):
            v = tk.BooleanVar(value=getter())
            tk.Checkbutton(parent, text=text, variable=v,
                           command=lambda: setter(v.get()),
                           bg=PANEL, fg=FG, selectcolor=ACCENT,
                           activebackground=PANEL, activeforeground=FG,
                           highlightthickness=0, bd=0,
                           font=("Segoe UI", 9)).pack(anchor="w")
            ttk.Label(parent, text=hint, style="Card.TLabel", foreground=MUTED,
                      font=("Segoe UI", 8), justify="left").pack(anchor="w",
                                                                 padx=(22, 0), pady=(0, 6))
            return v

        self.var_autostart = check(
            card, "Start with Windows", autostart.is_enabled, self._set_autostart,
            "Launches this app silently at login and starts syncing.")
        self.var_lock = check(
            card, "Pause while locked", lambda: CFG.get("pause_on_lock", True),
            lambda on: CFG.__setitem__("pause_on_lock", on),
            "Lights off on the lock screen, back on when you sign in.")
        self.var_letterbox = check(
            card, "Crop black bars in films", lambda: CFG.get("letterbox", False),
            lambda on: CFG.__setitem__("letterbox", on),
            "Ignores letterbox bars in films. Best-guess detection - it holds off unless confident.")

        # log
        card = self._card(outer, "Log", expand=True)
        bar = ttk.Frame(card, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Also saved to logs\\rgbsync.log", style="Card.TLabel",
                  foreground=MUTED, font=("Segoe UI", 8)).pack(side="left")
        ttk.Button(bar, text="Open log folder", style="Ghost.TButton",
                   command=self.open_logs).pack(side="right", ipadx=6)
        self.log = tk.Text(card, height=6, bg="#12141a", fg=MUTED, bd=0,
                           insertbackground=FG, font=("Consolas", 8), wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

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
        "mean":   "Plain average of screen pixels. Mixes colours muddier and\n"
                  "darker than real light - small bright things wash out.",
        "linear": "Averages in linear light, the way light physically mixes.\n"
                  "A fire or HUD in a dark scene stays visible.",
        "vivid":  "Linear, but saturated pixels count for more, so a colourful\n"
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
                gained = eng.rescan()
                if not gained:
                    missing = [k for k in ambient.DEVICE_KEYS if not eng.found.get(k)]
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
            try:
                eng.blackout_key(key)
            except Exception:
                pass

    def start(self):
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
        if self.engine:
            self.engine.stop()
        self.btn_stop.config(state="disabled")

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

    def save(self):
        save_config()
        self.write(f"settings saved -> {ambient.CONFIG_PATH}")

    # ------------------------------------------------------------- refresh --
    def _tick(self):
        eng = self.engine
        alive = bool(self.thread and self.thread.is_alive())
        running = bool(eng and eng.running)

        self.btn_start.config(state="disabled" if (alive or self.starting) else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        self.btn_rescan.config(state="normal" if running else "disabled")

        if running and getattr(eng, "paused", False):
            self.stat.config(text=f"paused ({eng.pause_reason})", foreground=MUTED)
        elif running:
            cap = getattr(eng.cap, "fps", 0.0)
            extra = ""
            lb = getattr(eng, "letterbox", None)
            if lb is not None and lb.active:
                extra = "   cropped"
            self.stat.config(text=f"running   {eng.fps_actual:4.1f} fps   "
                                  f"capture {cap:4.1f}   worst {eng.worst_ms:.0f} ms{extra}",
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
                if found:
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
        if self.engine:
            self.engine.stop()
        if self.thread:
            self.thread.join(timeout=2.0)
        self.root.destroy()


if __name__ == "__main__":
    load_config()
    logbook.session_header("gui")
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    if "--autostart" in sys.argv:
        # launched at login: start syncing straight away, out of the way
        root.after(400, app.start)
        root.iconify()
    root.mainloop()
