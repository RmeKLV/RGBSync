"""Prerequisites dialog - runs the checks off-thread and offers the safe fixes."""
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import prereqs
from prereqs import OK, WARN, FAIL, run_checks, summarise, DOWNLOAD_URL

BG, PANEL, FG, MUTED = "#16181d", "#1e2128", "#e6e8ec", "#8b93a1"
DOT = {OK: "#3ddc84", WARN: "#ffd93d", FAIL: "#ff6b6b", "info": "#5b8cff"}
WORD = {OK: "OK", WARN: "WARN", FAIL: "FAIL", "info": "INFO"}

CONFIRM_FIX = {"Close it"}          # fixes that kill someone else's app


class PrereqWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Prerequisites")
        self.configure(bg=BG)
        self.geometry("720x600")
        self.minsize(620, 460)
        self.busy = False

        wrap = ttk.Frame(self, padding=16)
        wrap.pack(fill="both", expand=True)

        head = ttk.Frame(wrap)
        head.pack(fill="x")
        ttk.Label(head, text="Prerequisites", style="Title.TLabel").pack(side="left")
        self.summary = ttk.Label(head, text="checking...", style="Muted.TLabel")
        self.summary.pack(side="right")

        bar = ttk.Frame(wrap)
        bar.pack(fill="x", pady=(12, 0))
        self.btn_recheck = ttk.Button(bar, text="Re-check", style="Accent.TButton",
                                      command=self.recheck)
        self.btn_recheck.pack(side="left", ipadx=14, ipady=4)
        ttk.Button(bar, text="OpenRGB download page", style="Ghost.TButton",
                   command=lambda: prereqs.fix_open_download_page()).pack(
                       side="right", ipadx=8, ipady=4)

        # scrollable list
        holder = ttk.Frame(wrap, style="Card.TFrame", padding=2)
        holder.pack(fill="both", expand=True, pady=(12, 0))
        self.canvas = tk.Canvas(holder, bg=PANEL, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.rows = ttk.Frame(self.canvas, style="Card.TFrame")
        self.rows.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.win = self.canvas.create_window((0, 0), window=self.rows, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._wheel)

        self.note = ttk.Label(
            wrap, style="Muted.TLabel", justify="left", font=("Segoe UI", 8),
            text=("OpenRGB is optional - only the motherboard, keyboard and mouse need it.\n"
                  "Installing goes through winget in a console window so you can see and "
                  "approve it; nothing is downloaded or run silently."))
        self.note.pack(anchor="w", pady=(10, 0))

        self.recheck()

    def _wheel(self, e):
        try:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    def destroy(self):
        try:
            self.canvas.unbind_all("<MouseWheel>")
        except Exception:
            pass
        super().destroy()

    # -------------------------------------------------------------- checks --
    def recheck(self):
        if self.busy:
            return
        self.busy = True
        self.btn_recheck.config(state="disabled")
        self.summary.config(text="checking...", foreground=MUTED)
        for w in self.rows.winfo_children():
            w.destroy()

        # The worker never touches Tk. It drops its result in _result and the
        # main thread picks it up - calling .after() off-thread is not safe.
        self._result = None
        self._error = None

        def work():
            try:
                self._result = run_checks()
            except Exception as e:
                self._error = f"{type(e).__name__}: {e}"

        threading.Thread(target=work, daemon=True).start()
        self._poll()

    def _poll(self):
        if self._error is not None:
            self.busy = False
            self.btn_recheck.config(state="normal")
            self.summary.config(text=f"check failed: {self._error}", foreground=DOT[FAIL])
            return
        if self._result is not None:
            self._render(self._result)
            return
        self.after(80, self._poll)

    def _render(self, checks):
        self.busy = False
        self.btn_recheck.config(state="normal")
        if not checks:
            return
        for c in checks:
            row = ttk.Frame(self.rows, style="Card.TFrame", padding=(10, 7))
            row.pack(fill="x")
            tk.Canvas(row, width=9, height=9, bg=DOT[c.status],
                      highlightthickness=0, bd=0).pack(side="left", padx=(0, 9))
            txt = ttk.Frame(row, style="Card.TFrame")
            txt.pack(side="left", fill="x", expand=True)
            ttk.Label(txt, text=c.name, style="Card.TLabel",
                      font=("Segoe UI", 9, "bold")).pack(anchor="w")
            ttk.Label(txt, text=c.detail, style="Card.TLabel", foreground=MUTED,
                      font=("Segoe UI", 8), wraplength=430,
                      justify="left").pack(anchor="w")
            ttk.Label(row, text=WORD[c.status], style="Card.TLabel",
                      foreground=DOT[c.status],
                      font=("Consolas", 8, "bold")).pack(side="right", padx=(8, 0))
            if c.fix:
                ttk.Button(row, text=c.fix_label or "Fix", style="Ghost.TButton",
                           command=lambda ck=c: self._apply(ck)).pack(side="right", ipadx=4)
            tk.Frame(self.rows, height=1, bg="#2a2f3a").pack(fill="x")

        status, text = summarise(checks)
        self.summary.config(text=text, foreground=DOT[status])

    def _apply(self, check):
        if check.fix_label in CONFIRM_FIX:
            if not messagebox.askyesno(
                    "Confirm", f"Close {check.name.replace('Conflict: ', '')}?\n\n"
                               "This force-closes the application.", parent=self):
                return
        try:
            ok, msg = check.fix()
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        messagebox.showinfo("Prerequisites" if ok else "Didn't work", msg, parent=self)
        if ok:
            self.after(600, self.recheck)
