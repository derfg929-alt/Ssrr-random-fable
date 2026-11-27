"""Капча с картинками для участия в розыгрыше.

Генерирует PNG с четырьмя фигурами и хранит правильный ответ в памяти.
Нужна, чтобы кнопку «Участвовать» не дёргали боты: человек смотрит на
картинку и нажимает номер нужной фигуры.
"""

import math
import random
import secrets
import struct
import threading
import time
import zlib

TTL = 180            # сколько секунд живёт капча
MAX_TRIES = 3        # сколько раз можно ошибиться
PENDING_TTL = 900    # сколько помним попытку участия, если бот не смог написать первым
MAX_ITEMS = 2000     # сколько капч максимум держим в памяти
MAX_ISSUE = 10       # сколько капч можно запросить за окно
ISSUE_WINDOW = 600   # окно лимита выдачи, сек

_rng = random.SystemRandom()
_lock = threading.Lock()
_items = {}          # user_id -> капча
_pending = {}        # user_id -> (draw_id, expires)
_issued = {}         # user_id -> [время выдачи, ...]

COLORS = {
    "red": (226, 58, 58),
    "blue": (52, 110, 224),
    "green": (44, 168, 92),
    "yellow": (240, 190, 40),
    "purple": (148, 72, 204),
    "orange": (240, 140, 44),
}
SHAPES = ("circle", "square", "triangle", "star", "diamond", "heart")

TILE = 280
GAP = 20
PAD = 20
SIZE = PAD * 2 + TILE * 2 + GAP   # 620x620
SUPERSAMPLE = 2                    # сглаживание краёв фигур

_DIGITS = {
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
}


class _Canvas:
    """Простой RGB-холст с суперсэмплингом. Координаты — логические пиксели."""

    def __init__(self, size, scale=SUPERSAMPLE, bg=(255, 255, 255)):
        self.size = size
        self.scale = scale
        self.w = size * scale
        self.buf = bytearray(bytes(bg) * (self.w * self.w))

    def rect(self, x0, y0, x1, y1, color):
        s, w = self.scale, self.w
        x0 = max(0, int(x0 * s))
        x1 = min(w, int(x1 * s))
        y0 = max(0, int(y0 * s))
        y1 = min(w, int(y1 * s))
        if x1 <= x0:
            return
        row = bytes(color) * (x1 - x0)
        for y in range(y0, y1):
            i = (y * w + x0) * 3
            self.buf[i:i + len(row)] = row

    def circle(self, cx, cy, r, color):
        s, w = self.scale, self.w
        cxd, cyd, rd = cx * s, cy * s, r * s
        paint = bytes(color)
        y0 = max(0, int(cyd - rd))
        y1 = min(w, int(cyd + rd) + 1)
        for y in range(y0, y1):
            dy = y + 0.5 - cyd
            if abs(dy) > rd:
                continue
            dx = math.sqrt(rd * rd - dy * dy)
            xa = max(0, int(cxd - dx))
            xb = min(w, int(cxd + dx) + 1)
            if xb > xa:
                i = (y * w + xa) * 3
                self.buf[i:i + (xb - xa) * 3] = paint * (xb - xa)

    def poly(self, points, color):
        s, w = self.scale, self.w
        pts = [(x * s, y * s) for x, y in points]
        paint = bytes(color)
        ys = [p[1] for p in pts]
        y0 = max(0, int(min(ys)))
        y1 = min(w, int(max(ys)) + 1)
        n = len(pts)
        for y in range(y0, y1):
            yc = y + 0.5
            xs = []
            for i in range(n):
                x1, y1p = pts[i]
                x2, y2p = pts[(i + 1) % n]
                if (y1p <= yc < y2p) or (y2p <= yc < y1p):
                    xs.append(x1 + (yc - y1p) / (y2p - y1p) * (x2 - x1))
            xs.sort()
            for j in range(0, len(xs) - 1, 2):
                xa = max(0, int(xs[j]))
                xb = min(w, int(xs[j + 1]) + 1)
                if xb > xa:
                    i = (y * w + xa) * 3
                    self.buf[i:i + (xb - xa) * 3] = paint * (xb - xa)

    def to_png(self):
        s, w, size = self.scale, self.w, self.size
        out = bytearray(size * size * 3)
        n = s * s
        buf = self.buf
        for y in range(size):
            for x in range(size):
                r = g = b = 0
                for dy in range(s):
                    base = ((y * s + dy) * w + x * s) * 3
                    for dx in range(s):
                        i = base + dx * 3
                        r += buf[i]
                        g += buf[i + 1]
                        b += buf[i + 2]
                o = (y * size + x) * 3
                out[o] = r // n
                out[o + 1] = g // n
                out[o + 2] = b // n
        return _png(size, size, out)


def _png(width, height, rgb):
    stride = width * 3
    raw = bytearray((stride + 1) * height)
    for y in range(height):
        src = y * stride
        dst = y * (stride + 1)
        raw[dst + 1:dst + 1 + stride] = rgb[src:src + stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _shape(c, shape, cx, cy, r, color):
    if shape == "circle":
        c.circle(cx, cy, r, color)
    elif shape == "square":
        k = r * 0.86
        c.rect(cx - k, cy - k, cx + k, cy + k, color)
    elif shape == "triangle":
        c.poly([(cx, cy - r), (cx - r * 0.92, cy + r * 0.72),
                (cx + r * 0.92, cy + r * 0.72)], color)
    elif shape == "star":
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rad = r if i % 2 == 0 else r * 0.45
            pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
        c.poly(pts, color)
    elif shape == "diamond":
        c.poly([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], color)
    elif shape == "heart":
        c.circle(cx - r * 0.42, cy - r * 0.22, r * 0.52, color)
        c.circle(cx + r * 0.42, cy - r * 0.22, r * 0.52, color)
        c.poly([(cx - r * 0.92, cy - r * 0.04), (cx + r * 0.92, cy - r * 0.04),
                (cx, cy + r * 0.95)], color)


def _digit(c, x, y, ch, color, scale=4):
    for row_i, row in enumerate(_DIGITS[ch]):
        for col_i, bit in enumerate(row):
            if bit == "1":
                px = x + col_i * scale
                py = y + row_i * scale
                c.rect(px, py, px + scale, py + scale, color)


def _render(combos):
    c = _Canvas(SIZE)
    c.rect(0, 0, SIZE, SIZE, (255, 255, 255))
    positions = (
        (PAD, PAD),
        (PAD + TILE + GAP, PAD),
        (PAD, PAD + TILE + GAP),
        (PAD + TILE + GAP, PAD + TILE + GAP),
    )
    border = (226, 230, 238)
    for i, ((color_key, shape), (tx, ty)) in enumerate(zip(combos, positions)):
        c.rect(tx, ty, tx + TILE, ty + TILE, (248, 249, 252))
        c.rect(tx, ty, tx + TILE, ty + 3, border)
        c.rect(tx, ty + TILE - 3, tx + TILE, ty + TILE, border)
        c.rect(tx, ty, tx + 3, ty + TILE, border)
        c.rect(tx + TILE - 3, ty, tx + TILE, ty + TILE, border)

        cx = tx + TILE / 2
        cy = ty + TILE / 2 + TILE * 0.03
        r = TILE * 0.30
        shadow = [(cx + r * 0.78 * math.cos(a), cy + r * 1.02 + r * 0.18 * math.sin(a))
                  for a in (i * math.pi / 20 for i in range(40))]
        c.poly(shadow, (234, 237, 244))
        _shape(c, shape, cx, cy, r, COLORS[color_key])

        bx, by = tx + 38, ty + 38
        c.circle(bx, by, 26, (28, 32, 42))
        _digit(c, bx - 10, by - 14, str(i + 1), (255, 255, 255))
    return c.to_png()


def make(draw_id):
    """Собирает новую капчу. Возвращает dict с png, ответом и nonce."""
    colors = list(COLORS)
    target_color = _rng.choice(colors)
    target_shape = _rng.choice(SHAPES)
    target = (target_color, target_shape)
    # Отвлекающие картинки отличаются и цветом, и формой: цель видно однозначно.
    other_colors = [c for c in colors if c != target_color]
    other_shapes = [s for s in SHAPES if s != target_shape]
    combos = [target]
    while len(combos) < 4:
        combo = (_rng.choice(other_colors), _rng.choice(other_shapes))
        if combo not in combos:
            combos.append(combo)
    _rng.shuffle(combos)
    return {
        "png": _render(combos),
        "answer": combos.index(target),
        "target": target,
        "nonce": secrets.token_hex(4),
        "draw_id": int(draw_id),
        "tries": 0,
    }


def _sweep_locked(now=None):
    """Убирает просроченные капчи и попытки. Без этого память росла бы вечно:
    записи удалялись только при повторном обращении того же пользователя."""
    now = now or time.time()
    for key, item in list(_items.items()):
        if item.get("expires", 0) < now:
            _items.pop(key, None)
    if len(_items) > MAX_ITEMS:
        stale = sorted(_items.items(), key=lambda kv: kv[1].get("expires", 0))
        for key, _ in stale[:len(_items) - MAX_ITEMS]:
            _items.pop(key, None)
    for key, (_, expires) in list(_pending.items()):
        if expires < now:
            _pending.pop(key, None)
    for key, stamps in list(_issued.items()):
        fresh = [stamp for stamp in stamps if stamp > now - ISSUE_WINDOW]
        if fresh:
            _issued[key] = fresh
        else:
            _issued.pop(key, None)


def sweep():
    with _lock:
        _sweep_locked()


def allow(user_id):
    """Лимит на выдачу капч: перебор вариантов новыми капчами не помогает."""
    key = str(user_id)
    now = time.time()
    with _lock:
        _sweep_locked(now)
        stamps = _issued.setdefault(key, [])
        if len(stamps) >= MAX_ISSUE:
            return False
        stamps.append(now)
        return True


def save(user_id, item, chat_id, message_id):
    item["chat_id"] = chat_id
    item["message_id"] = message_id
    item["expires"] = time.time() + TTL
    with _lock:
        _items[str(user_id)] = item
        _sweep_locked()


def get(user_id):
    key = str(user_id)
    with _lock:
        item = _items.get(key)
        if item and item.get("expires", 0) < time.time():
            _items.pop(key, None)
            item = None
        return item


def drop(user_id):
    with _lock:
        _items.pop(str(user_id), None)


def set_pending(user_id, draw_id):
    with _lock:
        _sweep_locked()
        _pending[str(user_id)] = (int(draw_id), time.time() + PENDING_TTL)


def pop_pending(user_id):
    with _lock:
        value = _pending.pop(str(user_id), None)
    if not value:
        return None
    draw_id, expires = value
    return draw_id if expires > time.time() else None


def forget(user_id):
    """Удаляет всё, что связано с пользователем (запрос на удаление данных)."""
    key = str(user_id)
    with _lock:
        _items.pop(key, None)
        _pending.pop(key, None)
        _issued.pop(key, None)
