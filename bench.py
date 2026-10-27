"""Measure what RGB Sync costs the rest of the PC.

    python bench.py                 capture cost at several rates + system latency
    python bench.py --engine        ... and the full engine driving your devices
    python bench.py --seconds 20    longer phases, steadier numbers

Close the GUI first (two copies would fight over the devices) and leave the
desktop alone while it runs. Every phase reports:

  app cpu     CPU the app used, as % of ONE core (all its threads together)
  grab ms     how long one screen capture takes; mean and 95th percentile
  latency     how late a normal-priority thread in ANOTHER process wakes from
              a 1 ms sleep, mean / 99th percentile / worst. This stands in for
              a game's threads: if the app makes them wait, it shows up here.

The "idle" phase is the baseline with nothing running. Compare each phase to
it. The game-side check - FPS and frametimes with the app on and off - is in
the README under "Performance".

Results are also written to logs/bench-<time>.txt.
"""
import os, sys, time, threading, subprocess, statistics, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Runs in its own interpreter so it never queues behind our GIL - it measures
# the OS scheduler, the way a game thread would experience it.
PROBE = r"""
import time, sys
end = time.perf_counter() + float(sys.argv[1])
late = []
while time.perf_counter() < end:
    t = time.perf_counter()
    time.sleep(0.001)
    late.append((time.perf_counter() - t - 0.001) * 1000)
late.sort()
n = len(late)
print(f"{sum(late)/n:.3f} {late[int(n*0.99)]:.3f} {late[-1]:.3f} {n}")
"""


def probe(seconds):
    """Start the latency probe in a child process; returns a 'collect' fn."""
    p = subprocess.Popen([sys.executable, "-c", PROBE, str(seconds)],
                         stdout=subprocess.PIPE, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def collect():
        out, _ = p.communicate(timeout=seconds + 15)
        mean, p99, worst, n = out.split()
        return float(mean), float(p99), float(worst)
    return collect


def pct(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * q))] if vals else 0.0


def phase_idle(seconds):
    collect = probe(seconds)
    c0, t0 = time.process_time(), time.perf_counter()
    time.sleep(seconds)
    cpu = 100 * (time.process_time() - c0) / (time.perf_counter() - t0)
    return {"cpu": cpu, "grab": None, "lat": collect()}


def phase_capture(seconds, fps, stretch_name="halftone"):
    """The capture loop alone, as CaptureThread runs it, plus sampling."""
    import screen
    stretch = screen.HALFTONE if stretch_name == "halftone" else screen.COLORONCOLOR
    cols, rows = 48, 27
    g = screen.ScreenGrabber(cols, rows, stretch=stretch)
    sampler = screen.Sampler(cols, rows, "vivid", 4.0)
    grabs = []
    collect = probe(seconds)
    c0, t0 = time.process_time(), time.perf_counter()
    end = t0 + seconds
    try:
        while time.perf_counter() < end:
            t = time.perf_counter()
            raw = g.grab()
            grabs.append((time.perf_counter() - t) * 1000)
            sampler.prepare(raw)
            for i in range(48):                      # roughly one frame's regions
                sampler.region(raw, i, 20, i + 1, 27)
            time.sleep(max(0.0, 1.0 / fps - (time.perf_counter() - t)))
    finally:
        g.close()
    cpu = 100 * (time.process_time() - c0) / (time.perf_counter() - t0)
    return {"cpu": cpu, "grab": (statistics.mean(grabs), pct(grabs, 0.95)),
            "lat": collect(), "rate": len(grabs) / seconds}


def phase_engine(seconds):
    import ambient
    ambient.load_config()
    lines = []
    eng = ambient.Engine(log=lines.append)
    th = threading.Thread(target=eng.run, daemon=True)
    collect = probe(seconds)
    c0, t0 = time.process_time(), time.perf_counter()
    th.start()
    grabs = []
    end = t0 + seconds
    while time.perf_counter() < end:
        time.sleep(0.5)
        if eng.cap.grab_ms:
            grabs.append(eng.cap.grab_ms)
    cpu = 100 * (time.process_time() - c0) / (time.perf_counter() - t0)
    fps = eng.fps_actual
    eng.stop()
    th.join(3)
    eng.close()
    return {"cpu": cpu, "grab": (statistics.mean(grabs), pct(grabs, 0.95)) if grabs else None,
            "lat": collect(), "rate": fps, "devices": [k for k, v in eng.found.items() if v]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--engine", action="store_true",
                    help="also run the real engine against your devices")
    a = ap.parse_args()
    s = a.seconds

    plan = [("idle (baseline)", lambda: phase_idle(s)),
            ("capture 30/s  (new default)", lambda: phase_capture(s, 30)),
            ("capture 60/s", lambda: phase_capture(s, 60)),
            ("capture 120/s (old, always on)", lambda: phase_capture(s, 120)),
            ("capture 30/s  fast filter", lambda: phase_capture(s, 30, "fast"))]
    if a.engine:
        plan.append(("full engine, your config", lambda: phase_engine(s)))

    out = [f"RGB Sync benchmark  {time.strftime('%Y-%m-%d %H:%M:%S')}  "
           f"{s:.0f}s per phase", ""]
    head = f"{'phase':32} {'rate':>6} {'app cpu':>8} {'grab ms':>13} {'latency ms':>22}"
    out += [head, f"{'':32} {'/s':>6} {'% core':>8} {'mean/p95':>13} {'mean/p99/worst':>22}"]
    print("\n".join(out), flush=True)
    for name, fn in plan:
        try:
            r = fn()
        except Exception as e:
            line = f"{name:32} failed: {type(e).__name__}: {e}"
        else:
            rate = f"{r['rate']:.0f}" if r.get("rate") else "-"
            grab = f"{r['grab'][0]:.1f}/{r['grab'][1]:.1f}" if r["grab"] else "-"
            lat = "{:.2f}/{:.2f}/{:.1f}".format(*r["lat"])
            line = f"{name:32} {rate:>6} {r['cpu']:7.1f}% {grab:>13} {lat:>22}"
            if r.get("devices") is not None:
                line += "   devices: " + (", ".join(r["devices"]) or "none")
        print(line, flush=True)
        out.append(line)

    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    path = os.path.join(HERE, "logs", time.strftime("bench-%Y%m%d-%H%M%S.txt"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"\nsaved to {path}")


if __name__ == "__main__":
    main()
