"""Screen layout editor - drag a rectangle per device to pick where it samples.

Live thumbnail of the screen is drawn behind the boxes while the engine runs,
so you can see exactly what each device is looking at.
"""
import tkinter as tk
from tkinter import ttk

from ambient import CFG, DEFAULT_REGIONS, reset_regions

BG, PANEL, FG, MUTED = "#16181d", "#1e2128", "#e6e8ec", "#8b93a1"
HANDLE = 10          # px grab area for the resize corner
MIN_FRAC = 0.04      # smallest allowed region, as a fraction of the screen

DEVICES = [
    ("mobo",    "Motherboard",      "#ff7a45"),
    ("kbd",     "Keyboard",         "#5b8cff"),
    ("mouse",   "Mouse",            "#3ddc84"),
    ("monitor", "Monitor backlight", "#ffd93d"),
    ("bm38",    "BM38 boom arm",    "#ff6bd6"),
    ("am8",     "AM8 Pro mic",      "#4ee0e0"),
]
COLOR = {k: c for k, _n, c in DEVICES}
NAME = {k: n for k, n, _c in DEVICES}

PRESETS = {
    "Full screen":  dict(x=0.00, y=0.00, w=1.00, h=1.00),
    "Top edge":     dict(x=0.00, y=0.00, w=1.00, h=0.25),
    "Bottom edge":  dict(x=0.00, y=0.75, w=1.00, h=0.25),
    "Left edge":    dict(x=0.00, y=0.00, w=0.25, h=1.00),
    "Right edge":   dict(x=0.75, y=0.00, w=0.25, h=1.00),
    "Centre":       dict(x=0.25, y=0.25, w=0.50, h=0.50),
}

AXES = [("x", "Left to right"), ("y", "Top to bottom"),
        ("avg", "One flat colour"), ("grid", "6x3 grid (mic ring)")]
AXIS_LABEL = {a: t for a, t in AXES}


class LayoutEditor(tk.Toplevel):
    def __init__(self, master, get_engine):
        super().__init__(master)
        self.get_engine = get_engine
        self.title("Screen layout")
        self.configure(bg=BG)
        self.geometry("880x560")
        self.minsize(760, 480)

        self.selected = "bm38"
        self.drag = None
        self.cw, self.ch = 10, 10

        wrap = ttk.Frame(self, padding=12)
        wrap.pack(fill="both", expand=True)

        left = ttk.Frame(wrap)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Drag a box to move it, drag its bottom-right corner to resize.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        self.canvas = tk.Canvas(left, bg="#0d0f13", highlightthickness=1,
                                highlightbackground="#2a2f3a", bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))

        side = ttk.Frame(wrap, width=230)
        side.pack(side="right", fill="y", padx=(14, 0))
        side.pack_propagate(False)

        ttk.Label(side, text="DEVICE", style="Head.TLabel").pack(anchor="w")
        box = ttk.Frame(side, style="Card.TFrame", padding=8)
        box.pack(fill="x", pady=(4, 0))
        self.dev_var = tk.StringVar(value=self.selected)
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

        ttk.Label(side, text="DIRECTION", style="Head.TLabel").pack(anchor="w", pady=(14, 0))
        box = ttk.Frame(side, style="Card.TFrame", padding=8)
        box.pack(fill="x", pady=(4, 0))
        self.axis_var = tk.StringVar()
        for val, txt in AXES:
            tk.Radiobutton(box, text=txt, value=val, variable=self.axis_var,
                           command=self._axis, bg=PANEL, fg=FG, selectcolor="#5b8cff",
                           activebackground=PANEL, activeforeground=FG,
                           highlightthickness=0, bd=0,
                           font=("Segoe UI", 9)).pack(anchor="w")
        self.rev_var = tk.BooleanVar()
        tk.Checkbutton(box, text="Reverse order", variable=self.rev_var,
                       command=self._axis, bg=PANEL, fg=FG, selectcolor="#5b8cff",
                       activebackground=PANEL, activeforeground=FG,
                       highlightthickness=0, bd=0,
                       font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))

        ttk.Label(side, text="PRESETS", style="Head.TLabel").pack(anchor="w", pady=(14, 0))
        box = ttk.Frame(side, style="Card.TFrame", padding=8)
        box.pack(fill="x", pady=(4, 0))
        grid = ttk.Frame(box, style="Card.TFrame")
        grid.pack(fill="x")
        for i, name in enumerate(PRESETS):
            ttk.Button(grid, text=name, style="Ghost.TButton", width=13,
                       command=lambda n=name: self._preset(n)).grid(
                           row=i // 2, column=i % 2, padx=2, pady=2, sticky="ew")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        ttk.Button(side, text="Reset all to defaults", style="Ghost.TButton",
                   command=self._reset).pack(fill="x", pady=(14, 0), ipady=3)
        self.info = ttk.Label(side, text="", style="Muted.TLabel",
                              font=("Consolas", 8), justify="left")
        self.info.pack(anchor="w", pady=(10, 0))

        self._select()
        self._tick()

    # ------------------------------------------------------------ helpers --
    def reg(self, key=None):
        return CFG["regions"][key or self.selected]

    def _select(self):
        self.selected = self.dev_var.get()
        r = self.reg()
        self.axis_var.set(r.get("axis", "x"))
        self.rev_var.set(bool(r.get("reverse")))
        self._redraw()

    def _axis(self):
        r = self.reg()
        r["axis"] = self.axis_var.get()
        r["reverse"] = self.rev_var.get()
        self._redraw()

    def _preset(self, name):
        self.reg().update(PRESETS[name])
        self._redraw()

    def _reset(self):
        reset_regions()
        self._select()

    def _rect_px(self, key):
        r = self.reg(key)
        return (r["x"] * self.cw, r["y"] * self.ch,
                (r["x"] + r["w"]) * self.cw, (r["y"] + r["h"]) * self.ch)

    # -------------------------------------------------------------- mouse --
    def _press(self, e):
        # topmost first: selected device wins, then reverse draw order
        order = [self.selected] + [k for k, _n, _c in reversed(DEVICES) if k != self.selected]
        for key in order:
            x0, y0, x1, y1 = self._rect_px(key)
            if x1 - HANDLE <= e.x <= x1 + 4 and y1 - HANDLE <= e.y <= y1 + 4:
                self.dev_var.set(key); self._select()
                self.drag = ("resize", e.x, e.y, dict(self.reg(key)))
                return
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                self.dev_var.set(key); self._select()
                self.drag = ("move", e.x, e.y, dict(self.reg(key)))
                return
        self.drag = None

    def _motion(self, e):
        if not self.drag:
            return
        mode, sx, sy, start = self.drag
        dx = (e.x - sx) / max(1, self.cw)
        dy = (e.y - sy) / max(1, self.ch)
        r = self.reg()
        if mode == "move":
            r["x"] = min(max(0.0, start["x"] + dx), 1.0 - start["w"])
            r["y"] = min(max(0.0, start["y"] + dy), 1.0 - start["h"])
        else:
            r["w"] = min(max(MIN_FRAC, start["w"] + dx), 1.0 - start["x"])
            r["h"] = min(max(MIN_FRAC, start["h"] + dy), 1.0 - start["y"])
        self._redraw()

    # ------------------------------------------------------------- drawing --
    def _redraw(self):
        c = self.canvas
        c.delete("all")
        self.cw = max(10, c.winfo_width())
        self.ch = max(10, c.winfo_height())

        eng = self.get_engine()
        raw = getattr(eng, "last_raw", None) if eng else None

        # live screen thumbnail, or a flat backdrop when idle
        if raw:
            gc, gr = eng.C, eng.R
            tw, th = 24, 14
            cw, chh = self.cw / tw, self.ch / th
            for ty in range(th):
                for tx in range(tw):
                    col = eng.grab.region(raw,
                                          int(tx * gc / tw), int(ty * gr / th),
                                          int((tx + 1) * gc / tw), int((ty + 1) * gr / th))
                    c.create_rectangle(tx * cw, ty * chh, (tx + 1) * cw, (ty + 1) * chh,
                                       fill="#%02x%02x%02x" % col, outline="")
        else:
            c.create_rectangle(0, 0, self.cw, self.ch, fill="#0d0f13", outline="")
            c.create_text(self.cw / 2, self.ch / 2, fill=MUTED, font=("Segoe UI", 10),
                          text="start the engine to see a live screen preview")

        for key, name, col in DEVICES:
            if not CFG["enabled"].get(key, True):
                continue
            x0, y0, x1, y1 = self._rect_px(key)
            sel = key == self.selected
            c.create_rectangle(x0, y0, x1, y1, outline=col,
                               width=3 if sel else 1, dash=() if sel else (3, 3))
            c.create_text(x0 + 6, y0 + 5, anchor="nw", text=name, fill=col,
                          font=("Segoe UI", 9, "bold" if sel else "normal"))
            if sel:
                c.create_rectangle(x1 - HANDLE, y1 - HANDLE, x1, y1,
                                   fill=col, outline="")
                self._arrows(c, x0, y0, x1, y1, col, self.reg(key))

        r = self.reg()
        self.info.config(text=f"{NAME[self.selected]}\n"
                              f"x {r['x']:.2f}  y {r['y']:.2f}\n"
                              f"w {r['w']:.2f}  h {r['h']:.2f}\n"
                              f"{AXIS_LABEL.get(r.get('axis','x'),'')}"
                              f"{' (reversed)' if r.get('reverse') else ''}")

    def _arrows(self, c, x0, y0, x1, y1, col, r):
        """Show which way the gradient runs inside the selected box."""
        axis, rev = r.get("axis", "x"), r.get("reverse")
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        pad = 14
        if axis == "x":
            a, b = (x0 + pad, x1 - pad) if not rev else (x1 - pad, x0 + pad)
            if abs(b - a) > 12:
                c.create_line(a, my, b, my, fill=col, width=2, arrow="last")
        elif axis == "y":
            a, b = (y0 + pad, y1 - pad) if not rev else (y1 - pad, y0 + pad)
            if abs(b - a) > 12:
                c.create_line(mx, a, mx, b, fill=col, width=2, arrow="last")
        elif axis == "avg":
            c.create_oval(mx - 7, my - 7, mx + 7, my + 7, outline=col, width=2)
        else:
            for i in range(1, 6):
                c.create_line(x0 + i * (x1 - x0) / 6, y0, x0 + i * (x1 - x0) / 6, y1,
                              fill=col, dash=(2, 4))
            for j in range(1, 3):
                c.create_line(x0, y0 + j * (y1 - y0) / 3, x1, y0 + j * (y1 - y0) / 3,
                              fill=col, dash=(2, 4))

    def _tick(self):
        eng = self.get_engine()
        if eng and getattr(eng, "running", False) and not self.drag:
            self._redraw()
        self.after(160, self._tick)
