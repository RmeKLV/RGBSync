"""Detect black bars (letterbox / pillarbox) and report the real content area.

This is a heuristic, not a certainty: a night scene with a dark sky above and
dark ground below looks exactly like letterboxing. Four guards keep it honest:

  symmetry  - real bars are equal on both sides; we use min(top, bottom)
  patience  - a candidate must repeat for `hold` frames before it is applied,
              so a passing dark frame cannot crop the picture
  cap       - never crop more than `max_frac` per side
  sanity    - if the middle is dark too, it is a dark scene, not a bar; bail out

Worst case when it guesses wrong is a slightly smaller sampling area, which is
mild. It releases quickly when content fills the frame again.
"""


class Letterbox:
    def __init__(self, cols, rows, threshold=8, max_frac=0.30, hold=8, release=3,
                 edge_step=24):
        self.cols, self.rows = cols, rows
        self.threshold = threshold      # 0-255 luma; below this a cell counts as black
        self.edge_step = edge_step      # picture must be this much brighter than the bar
        self.max_frac = max_frac
        self.hold = hold                # frames a candidate must persist to commit
        self.release = release          # frames before letting a crop go
        self.top = self.bottom = self.left = self.right = 0
        self._cand = (0, 0, 0, 0)
        self._count = 0
        self.active = False

    # --------------------------------------------------------------- probing --
    def _row_is_dark(self, raw, y):
        c = self.cols
        base = y * c * 4
        t = self.threshold
        for x in range(c):
            i = base + x * 4
            # cheap luma: max channel is enough to spot "not black"
            if raw[i] > t or raw[i + 1] > t or raw[i + 2] > t:
                return False
        return True

    def _col_is_dark(self, raw, x):
        c = self.cols
        t = self.threshold
        for y in range(self.rows):
            i = (y * c + x) * 4
            if raw[i] > t or raw[i + 1] > t or raw[i + 2] > t:
                return False
        return True

    def _row_max(self, raw, y):
        c = self.cols
        base = y * c * 4
        best = 0
        for x in range(c):
            i = base + x * 4
            m = max(raw[i], raw[i + 1], raw[i + 2])
            if m > best:
                best = m
        return best

    def _col_max(self, raw, x):
        c = self.cols
        best = 0
        for y in range(self.rows):
            i = (y * c + x) * 4
            m = max(raw[i], raw[i + 1], raw[i + 2])
            if m > best:
                best = m
        return best

    def _brightest(self, raw, y0, y1, x0, x1):
        c = self.cols
        best = 0
        for y in range(y0, y1):
            base = y * c
            for x in range(x0, x1):
                i = (base + x) * 4
                m = max(raw[i], raw[i + 1], raw[i + 2])
                if m > best:
                    best = m
        return best

    # ---------------------------------------------------------------- update --
    def update(self, raw):
        """Returns (x0, y0, x1, y1) content rect in grid cells."""
        if raw is None:
            return self.rect()

        max_v = int(self.rows * self.max_frac)
        max_h = int(self.cols * self.max_frac)

        top = 0
        while top < max_v and self._row_is_dark(raw, top):
            top += 1
        bottom = 0
        while bottom < max_v and self._row_is_dark(raw, self.rows - 1 - bottom):
            bottom += 1
        left = 0
        while left < max_h and self._col_is_dark(raw, left):
            left += 1
        right = 0
        while right < max_h and self._col_is_dark(raw, self.cols - 1 - right):
            right += 1

        # bars are symmetric; an uneven result means it is just dark content
        v = min(top, bottom) if abs(top - bottom) <= 1 else 0
        h = min(left, right) if abs(left - right) <= 1 else 0

        # a real bar ends in a hard edge. Dark scenery fades gradually, so if the
        # first "picture" line is barely brighter than the bar, it is not a bar.
        need = self.threshold + self.edge_step
        if v and not (self._row_max(raw, v) >= need and
                      self._row_max(raw, self.rows - 1 - v) >= need):
            v = 0
        if h and not (self._col_max(raw, h) >= need and
                      self._col_max(raw, self.cols - 1 - h) >= need):
            h = 0

        # sanity: if what is left is also dark, this is a dark scene, not a bar
        if v or h:
            y0, y1 = v, self.rows - v
            x0, x1 = h, self.cols - h
            if y1 - y0 < 2 or x1 - x0 < 2 or self._brightest(raw, y0, y1, x0, x1) <= self.threshold + 8:
                v = h = 0

        cand = (v, v, h, h)
        if cand == self._cand:
            self._count += 1
        else:
            self._cand = cand
            self._count = 1

        committed = (self.top, self.bottom, self.left, self.right)
        if cand != committed:
            # committing a crop takes patience; letting one go is quicker
            need = self.release if cand == (0, 0, 0, 0) else self.hold
            if self._count >= need:
                self.top, self.bottom, self.left, self.right = cand
                self.active = any(cand)
        return self.rect()

    def rect(self):
        return (self.left, self.top, self.cols - self.right, self.rows - self.bottom)

    def reset(self):
        self.top = self.bottom = self.left = self.right = 0
        self._cand = (0, 0, 0, 0)
        self._count = 0
        self.active = False

    def describe(self):
        if not self.active:
            return "none"
        parts = []
        if self.top:
            parts.append(f"{self.top}px bars top/bottom")
        if self.left:
            parts.append(f"{self.left}px bars left/right")
        return ", ".join(parts)
