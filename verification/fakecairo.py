"""Минимальная имитация cairo.Context на PIL — только для превью раскладки без GTK.

Покрывает примитивы, которые использует ValveBench: прямоугольники, линии, дуги, заливка,
штрих, текст, save/restore, clip по прямоугольнику, translate/rotate (только для текста).
Шрифты и сглаживание отличаются от настоящего Cairo — это проверка геометрии, не пикселей."""
from __future__ import annotations
import math
from PIL import Image, ImageDraw, ImageFont

_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"


class _Ext:
    def __init__(self, adv, h):
        self.x_advance, self.height, self.x_bearing, self.y_bearing = adv, h, 0, -h


class FakeContext:
    def __init__(self, w, h, scale=2, bg=(30, 30, 30, 255)):
        self.S = scale
        self.img = Image.new("RGBA", (int(w * scale), int(h * scale)), bg)
        self.rgba = (1, 1, 1, 1)
        self.lw = 1.0
        self.path, self.cur = [], None
        self.fsize = 10.0
        self.mono = False
        self.stack = []
        self.clip_rect = None
        self.tx = (0.0, 0.0)
        self.rot = 0.0
        self.dash = []

    # --- состояние
    def select_font_face(self, name, *a):
        self.mono = "mono" in str(name).lower()
    def set_font_size(self, s): self.fsize = float(s)
    def set_source_rgba(self, r, g, b, a=1.0): self.rgba = (r, g, b, a)
    def set_line_width(self, w): self.lw = float(w)
    def set_dash(self, d, *a): self.dash = list(d)
    def save(self): self.stack.append((self.clip_rect, self.tx, self.rot, self.rgba, self.lw, self.fsize))
    def restore(self):
        if self.stack: self.clip_rect, self.tx, self.rot, self.rgba, self.lw, self.fsize = self.stack.pop()
    def translate(self, x, y): self.tx = (self.tx[0] + x, self.tx[1] + y)
    def rotate(self, a): self.rot = a

    # --- путь
    def move_to(self, x, y): self.cur = (x, y); self.path.append([(x, y)])
    def line_to(self, x, y):
        if not self.path: self.path.append([(x, y)])
        else: self.path[-1].append((x, y))
        self.cur = (x, y)
    def rectangle(self, x, y, w, h): self.path.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]); self._last_rect = (x, y, w, h)
    def close_path(self):
        if self.path and len(self.path[-1]) > 2: self.path[-1].append(self.path[-1][0])
    def arc(self, x, y, r, a0, a1):
        n = 24
        self.path.append([(x + r * math.cos(a0 + (a1 - a0) * i / n), y + r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)])
    def new_path(self): self.path = []
    def clip(self):
        if self.path:
            xs = [p[0] for sp in self.path for p in sp]; ys = [p[1] for sp in self.path for p in sp]
            r = (min(xs), min(ys), max(xs), max(ys))
            if self.clip_rect:
                c = self.clip_rect; r = (max(r[0], c[0]), max(r[1], c[1]), min(r[2], c[2]), min(r[3], c[3]))
            self.clip_rect = r
        self.path = []

    # --- рисование
    def _col(self): return tuple(int(round(c * 255)) for c in self.rgba[:3]) + (int(round(self.rgba[3] * 255)),)
    def _layer(self): return Image.new("RGBA", self.img.size, (0, 0, 0, 0))
    def _commit(self, layer):
        if self.clip_rect:
            S = self.S; r = self.clip_rect
            mask = Image.new("L", self.img.size, 0)
            ImageDraw.Draw(mask).rectangle([r[0] * S, r[1] * S, r[2] * S, r[3] * S], fill=255)
            a = layer.getchannel("A"); from PIL import ImageChops
            layer.putalpha(ImageChops.multiply(a, mask))
        self.img = Image.alpha_composite(self.img, layer)

    def fill(self):
        L = self._layer(); d = ImageDraw.Draw(L); S = self.S
        for sp in self.path:
            if len(sp) >= 3: d.polygon([(x * S, y * S) for x, y in sp], fill=self._col())
        self._commit(L); self.path = []
    def stroke(self):
        L = self._layer(); d = ImageDraw.Draw(L); S = self.S
        for sp in self.path:
            if len(sp) >= 2:
                pts = [(x * S, y * S) for x, y in sp]
                if self.dash:
                    # штрих: рисуем сегментами
                    on, off = self.dash[0] * S, self.dash[-1] * S
                    for a, b in zip(pts, pts[1:]):
                        dist = math.hypot(b[0] - a[0], b[1] - a[1]); pos = 0.0
                        while pos < dist:
                            e = min(dist, pos + on)
                            d.line([(a[0] + (b[0] - a[0]) * pos / dist, a[1] + (b[1] - a[1]) * pos / dist),
                                    (a[0] + (b[0] - a[0]) * e / dist, a[1] + (b[1] - a[1]) * e / dist)],
                                   fill=self._col(), width=max(1, int(self.lw * S)))
                            pos += on + off
                else:
                    d.line(pts, fill=self._col(), width=max(1, int(round(self.lw * S))), joint="curve")
        self._commit(L); self.path = []
    def _font(self): return ImageFont.truetype(_MONO if self.mono else _FONT, max(6, int(round(self.fsize * self.S))))
    def text_extents(self, s):
        f = self._font(); return _Ext(f.getlength(s) / self.S, self.fsize)
    def show_text(self, s):
        if self.cur is None: return
        L = self._layer(); d = ImageDraw.Draw(L); S = self.S
        x, y = self.cur[0] + self.tx[0], self.cur[1] + self.tx[1]
        f = self._font()
        if self.rot:
            tmp = Image.new("RGBA", (int(f.getlength(s)) + 8, int(self.fsize * S * 1.6)), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((2, 2), s, font=f, fill=self._col())
            tmp = tmp.rotate(math.degrees(-self.rot), expand=True)
            L.paste(tmp, (int(x * S - tmp.size[0] / 2), int(y * S - tmp.size[1] / 2)))
        else:
            d.text((x * S, y * S), s, font=f, fill=self._col(), anchor="ls")
        self._commit(L)
        self.cur = (self.cur[0] + f.getlength(s) / S, self.cur[1])
    def save_png(self, path): self.img.convert("RGB").save(path)
