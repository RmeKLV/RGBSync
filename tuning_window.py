"""Per-device tuning - brightness/saturation/gamma and white balance per device."""
import tkinter as tk
from tkinter import ttk

from ambient import CFG, settings_for, reset_tuning, _dev_defaults
from layout_editor import DEVICES, NAME

BG, PANEL, FG, MUTED = "#16181d", "#1e2128", "#e6e8ec", "#8b93a1"
ACCENT = "#5b8cff"

SLIDERS = [
    ("brightness", "Brightness", 0.0, 1.0),
    ("saturation", "Saturation", 0.0, 3.0),
    ("gamma",      "Gamma",      0.4, 2.0),
]
CHANNELS = [("R", 0, "#ff6b6b"), ("G", 1, "#3ddc84"), ("B", 2, "#5b8cff")]


class TuningWindow(tk.Toplevel):
    def __init__(self, master, get_engine):
        super().__init__(master)
        self.get_engine = get_engine
        self.title("Device tuning")
        self.configure(bg=BG)
        self.geometry("660x560")
        self.minsize(600, 500)
        self.selected = "bm38"
        self._loading = False

        wrap = ttk.Frame(self, padding=16)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Device tuning", style="Title.TLabel").pack(anchor="w")
        ttk.Label(wrap, style="Muted.TLabel", font=("Segoe UI", 8),
                  text="Each device can follow the master sliders or have its own look.").pack(
                      anchor="w", pady=(2, 10))

        body = ttk.Frame(wrap)
        body.pack(fill="both", expand=True)

        # ---- device list ----
        left = ttk.Frame(body, width=210)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ttk.Label(left, text="DEVICE", style="Head.TLabel").pack(anchor="w")
        box = ttk.Frame(left, style="Card.TFrame", padding=8)
        box.pack(fill="x", pady=(4, 0))
        self.dev_var = tk.StringVar(value=self.selected)
        self.swatches = {}
        for key, name, col in DEVICES:
            row = ttk.Frame(box, style="Card.TFrame")
            row.pack(fill="x", pady=1)
            tk.Canvas(row, width=10, height=10, bg=col, highlightthickness=0,
                      bd=0).pack(side="left", padx=(0, 6))
            tk.Radiobutton(row, text=name, value=key, variable=self.dev_var,
                           command=self._select, bg=PANEL, fg=FG, selectcolor=col,
                           activebackground=PANEL, activeforeground=FG,
                           highlightthickness=0, bd=0,
                           font=("Segoe UI", 9)).pack(side="left")
            cv = tk.Canvas(box, height=10, bg=PANEL, highlightthickness=0, bd=0)
            cv.pack(fill="x", pady=(0, 4))
            self.swatches[key] = cv

        # ---- settings ----
        right = ttk.Frame(body)
        right.pack(side="right", fill="both", expand=True, padx=(14, 0))
        self.title_lbl = ttk.Label(right, text="", style="Head.TLabel")
        self.title_lbl.pack(anchor="w")

        card = ttk.Frame(right, style="Card.TFrame", padding=12)
        card.pack(fill="x", pady=(4, 0))
        self.link_var = tk.BooleanVar(value=True)
        tk.Checkbutton(card, text="Follow the master sliders", variable=self.link_var,
                       command=self._link, bg=PANEL, fg=FG, selectcolor=ACCENT,
                       activebackground=PANEL, activeforeground=FG,
                       highlightthickness=0, bd=0,
                       font=("Segoe UI", 9)).pack(anchor="w")

        self.rows, self.vars, self.labels = {}, {}, {}
        for key, label, lo, hi in SLIDERS:
            self.rows[key] = self._slider(card, key, label, lo, hi)

        ttk.Label(right, text="WHITE BALANCE", style="Head.TLabel").pack(anchor="w", pady=(14, 0))
        card2 = ttk.Frame(right, style="Card.TFrame", padding=12)
        card2.pack(fill="x", pady=(4, 0))
        ttk.Label(card2, style="Card.TLabel", foreground=MUTED, font=("Segoe UI", 8),
                  text="Trim a channel if a device's white looks tinted.").pack(anchor="w",
                                                                               pady=(0, 6))
        for label, idx, col in CHANNELS:
            self.rows[f"gain{idx}"] = self._gain_slider(card2, label, idx, col)

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Copy master here", style="Ghost.TButton",
                   command=self._copy_master).pack(side="left", fill="x",
                                                   expand=True, padx=(0, 4), ipady=3)
        ttk.Button(btns, text="Reset device", style="Ghost.TButton",
                   command=self._reset_one).pack(side="left", fill="x",
                                                 expand=True, padx=4, ipady=3)
        ttk.Button(btns, text="Reset all", style="Ghost.TButton",
                   command=self._reset_all).pack(side="left", fill="x",
                                                 expand=True, padx=(4, 0), ipady=3)

        self._select()
        self._tick()

    # ------------------------------------------------------------ builders --
    def _slider(self, parent, key, label, lo, hi):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, style="Card.TLabel", width=10).pack(side="left")
        val = ttk.Label(row, text="", style="Card.TLabel", foreground=MUTED, width=5)
        val.pack(side="right")
        var = tk.DoubleVar()
        sc = ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal",
                       command=lambda _e, k=key: self._set(k))
        sc.pack(side="left", fill="x", expand=True, padx=8)
        self.vars[key] = var
        self.labels[key] = val
        return sc

    def _gain_slider(self, parent, label, idx, col):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=3)
        tk.Canvas(row, width=10, height=10, bg=col, highlightthickness=0,
                  bd=0).pack(side="left", padx=(0, 6))
        ttk.Label(row, text=label, style="Card.TLabel", width=3).pack(side="left")
        val = ttk.Label(row, text="", style="Card.TLabel", foreground=MUTED, width=5)
        val.pack(side="right")
        var = tk.DoubleVar()
        sc = ttk.Scale(row, from_=0.0, to=1.5, variable=var, orient="horizontal",
                       command=lambda _e, i=idx: self._set_gain(i))
        sc.pack(side="left", fill="x", expand=True, padx=8)
        self.vars[f"gain{idx}"] = var
        self.labels[f"gain{idx}"] = val
        return sc

    # -------------------------------------------------------------- state --
    def cfg(self):
        return CFG["device"].setdefault(self.selected, _dev_defaults())

    def _select(self):
        self.selected = self.dev_var.get()
        self.title_lbl.config(text=NAME[self.selected].upper())
        self._load()

    def _load(self):
        self._loading = True
        d = self.cfg()
        self.link_var.set(bool(d.get("link", True)))
        s, gm, br, gain = settings_for(self.selected)
        shown = {"brightness": br, "saturation": s, "gamma": gm}
        for key, _l, _lo, _hi in SLIDERS:
            self.vars[key].set(shown[key])
            self.labels[key].config(text=f"{shown[key]:.2f}")
        for _l, idx, _c in CHANNELS:
            g = gain[idx] if idx < len(gain) else 1.0
            self.vars[f"gain{idx}"].set(g)
            self.labels[f"gain{idx}"].config(text=f"{g:.2f}")
        self._loading = False
        self._enable()

    def _enable(self):
        state = "disabled" if self.link_var.get() else "normal"
        for key, _l, _lo, _hi in SLIDERS:
            self.rows[key].config(state=state)

    def _link(self):
        d = self.cfg()
        # Read the values it is showing BEFORE flipping the flag - settings_for()
        # switches source the moment link changes, so unlinking would otherwise
        # snap to stale per-device values instead of continuing from the master.
        s, gm, br, _g = settings_for(self.selected)
        d["link"] = self.link_var.get()
        if not d["link"]:
            d.update(saturation=s, gamma=gm, brightness=br)
        self._load()

    def _set(self, key):
        if self._loading or self.link_var.get():
            return
        v = round(self.vars[key].get(), 3)
        self.cfg()[key] = v
        self.labels[key].config(text=f"{v:.2f}")

    def _set_gain(self, idx):
        if self._loading:
            return
        v = round(self.vars[f"gain{idx}"].get(), 3)
        g = list(self.cfg().get("gain", [1.0, 1.0, 1.0]))
        while len(g) < 3:
            g.append(1.0)
        g[idx] = v
        self.cfg()["gain"] = g
        self.labels[f"gain{idx}"].config(text=f"{v:.2f}")

    def _copy_master(self):
        self.cfg().update(link=False, saturation=CFG["saturation"],
                          gamma=CFG["gamma"], brightness=CFG["brightness"])
        self._load()

    def _reset_one(self):
        CFG["device"][self.selected] = _dev_defaults()
        self._load()

    def _reset_all(self):
        reset_tuning()
        self._load()

    # ------------------------------------------------------------ previews --
    def _tick(self):
        eng = self.get_engine()
        for key, _n, _c in DEVICES:
            cv = self.swatches[key]
            cv.delete("all")
            cols = (eng.preview.get(key) if eng else None) or []
            if cols:
                w = max(1.0, cv.winfo_width())
                n = min(len(cols), 24)
                step = max(1, len(cols) // n)
                shown = cols[::step][:n]
                bw = w / len(shown)
                for i, (r, g, b) in enumerate(shown):
                    cv.create_rectangle(i * bw, 0, (i + 1) * bw, 10,
                                        fill=f"#{r:02x}{g:02x}{b:02x}", outline="")
        self.after(150, self._tick)
