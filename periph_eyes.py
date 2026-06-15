#!/usr/bin/env python3
"""
PeriphEyes — Peripheral Vision Training Overlay
Тренировка аккомодации и снятие спазма через периферийные визуальные паттерны.
Центр экрана остаётся свободным для работы.
"""

import tkinter as tk
from tkinter import ttk
import json
import os
import ctypes
import math
import random
import threading
import winreg
from dataclasses import dataclass, field
from typing import List

import pystray
from PIL import Image as PILImage, ImageDraw as PILDraw, ImageTk as PILImageTk
import keyboard as kb

# ─── DPI Awareness (до создания Tk) ──────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ─── Константы ────────────────────────────────────────────────────────────────
VERSION     = "0.3"
DATA_PATH   = os.path.join(os.path.expanduser("~"), ".periph_eyes.json")
SCRIPT_PATH = os.path.abspath(__file__)
HOTKEY      = "ctrl+shift+f12"        # глобальная горячая клавиша

# Цвет фона оверлея, который становится прозрачным через transparentcolor
TRANSPARENT_BG = "#010101"

DEFAULTS: dict = {
    "edge_cm": 3.0,
    "session_min": 5,
    "interval_min": 45,
    "intensity": 60,       # 0–100
    "scheme": "green",
    "sides": {"top": True, "bottom": True, "left": True, "right": True},
    "pattern": "mix",      # mix | bloom | depth | drift | gabor
}

# (r, g, b) основной цвет и цвет свечения для каждой схемы
SCHEMES = {
    "green":  {"main": (0,   220, 110), "glow": (60,  255, 160)},
    "amber":  {"main": (255, 160,   0), "glow": (255, 200,  60)},
    "blue":   {"main": (0,   160, 255), "glow": ( 60, 200, 255)},
    "ghost":  {"main": (170, 170, 190), "glow": (210, 210, 240)},
}

# UI-цвета
UI_BG     = "#0D0D1A"
UI_CARD   = "#16162A"
UI_ACCENT = "#00E57A"
UI_FG     = "#DDE0F0"
UI_DIM    = "#52527A"
UI_GREEN  = "#00CC66"
UI_ORANGE = "#FF9900"

# Раздел реестра для автозапуска
_REG_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_APP = "PeriphEyes"


# ─── Автозапуск (реестр) ─────────────────────────────────────────────────────

def autostart_is_on() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_RUN, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _REG_APP)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def autostart_enable():
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_RUN, 0, winreg.KEY_SET_VALUE)
    # pythonw — без консольного окна
    winreg.SetValueEx(key, _REG_APP, 0, winreg.REG_SZ, f'pythonw "{SCRIPT_PATH}"')
    winreg.CloseKey(key)


def autostart_disable():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_RUN, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, _REG_APP)
        winreg.CloseKey(key)
    except OSError:
        pass


# ─── Персистентность ─────────────────────────────────────────────────────────

def load_settings() -> dict:
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, encoding="utf-8") as f:
                s = json.load(f)
            for k, v in DEFAULTS.items():
                s.setdefault(k, v)
            return s
        except Exception:
            pass
    return dict(DEFAULTS)


def save_settings(s: dict):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def cm_to_px(cm: float, root: tk.Tk) -> int:
    ppi = root.winfo_fpixels("1i")
    return max(40, int(cm * ppi / 2.54))


def apply_click_through(hwnd: int):
    """WS_EX_LAYERED | WS_EX_TRANSPARENT: весь overlay кликнасквозь."""
    try:
        GWL_EXSTYLE       = -20
        WS_EX_LAYERED     = 0x80000
        WS_EX_TRANSPARENT = 0x20
        cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, cur | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    except Exception:
        pass


def rgb_hex(r: int, g: int, b: int) -> str:
    """Конвертирует RGB в hex, избегая цвет прозрачности #010101."""
    return f"#{max(2, min(255, r)):02X}{max(2, min(255, g)):02X}{max(2, min(255, b)):02X}"


def dim_rgb(rgb: tuple, factor: float) -> tuple:
    return tuple(max(2, int(c * factor)) for c in rgb)


def lerp_rgb(a: tuple, b: tuple, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


# ─── Частицы ─────────────────────────────────────────────────────────────────

@dataclass
class Particle:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    r: float = 1.0
    r_min: float = 1.0
    r_max: float = 24.0
    life: float = 0.0      # 0 → 1
    d_life: float = 0.014  # шаг жизни за тик
    kind: str = "bloom"    # bloom | depth | drift | gabor
    color: tuple = (0, 220, 110)
    glow: tuple = (60, 255, 160)
    angle: float = 0.0     # для gabor-линий

    @property
    def alive(self) -> bool:
        return self.life < 1.0

    @property
    def size_t(self) -> float:
        """0 → 1 → 0 по синусоиде (birth → peak → death)."""
        return math.sin(self.life * math.pi)

    def tick(self) -> bool:
        self.life += self.d_life
        self.r = lerp(self.r_min, self.r_max, self.size_t)
        self.x += self.vx
        self.y += self.vy
        return self.life < 1.0


# ─── Windows Layered Window (UpdateLayeredWindow) ────────────────────────────

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

class _BLEND(ctypes.Structure):
    _fields_ = [
        ("BlendOp",             ctypes.c_byte),
        ("BlendFlags",          ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat",         ctypes.c_byte),
    ]

class _BMPINFO(ctypes.Structure):
    _fields_ = [
        ("biSize",          ctypes.c_uint32), ("biWidth",         ctypes.c_int32),
        ("biHeight",        ctypes.c_int32),  ("biPlanes",        ctypes.c_uint16),
        ("biBitCount",      ctypes.c_uint16), ("biCompression",   ctypes.c_uint32),
        ("biSizeImage",     ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),  ("biClrUsed",       ctypes.c_uint32),
        ("biClrImportant",  ctypes.c_uint32),
    ]

_u32 = ctypes.windll.user32
_g32 = ctypes.windll.gdi32
_k32 = ctypes.windll.kernel32

# Явные restype: без них 64-битные хендлы урезаются до c_int (32 бит)
_u32.GetDC.restype                = ctypes.c_void_p
_u32.UpdateLayeredWindow.restype  = ctypes.c_bool
_g32.CreateCompatibleDC.restype   = ctypes.c_void_p
_g32.CreateDIBSection.restype     = ctypes.c_void_p
_g32.SelectObject.restype         = ctypes.c_void_p
_g32.DeleteObject.restype         = ctypes.c_bool
_g32.DeleteDC.restype             = ctypes.c_bool


def _show_layered(hwnd: int, img: PILImage.Image):
    """UpdateLayeredWindow — настоящая попиксельная прозрачность без transparentcolor."""
    from ctypes import wintypes
    rect = wintypes.RECT()
    _u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))

    w, h = img.size
    if w <= 0 or h <= 0:
        return

    r_ch, g_ch, b_ch, a_ch = img.split()
    data = PILImage.merge("RGBA", (b_ch, g_ch, r_ch, a_ch)).tobytes()

    sdc = _u32.GetDC(None)
    mdc = _g32.CreateCompatibleDC(ctypes.c_void_p(sdc))

    bmi = _BMPINFO()
    bmi.biSize = ctypes.sizeof(_BMPINFO)
    bmi.biWidth, bmi.biHeight = w, -h   # -h → top-down
    bmi.biPlanes, bmi.biBitCount = 1, 32

    ppv = ctypes.c_void_p()
    hbm = _g32.CreateDIBSection(ctypes.c_void_p(mdc), ctypes.byref(bmi), 0,
                                 ctypes.byref(ppv), None, 0)
    if not hbm or not ppv.value:
        _g32.DeleteDC(ctypes.c_void_p(mdc))
        _u32.ReleaseDC(None, ctypes.c_void_p(sdc))
        print(f"[ULW] CreateDIBSection FAIL hwnd={hwnd:#x} sz={w}x{h}")
        return

    ctypes.memmove(ppv.value, data, len(data))
    old = _g32.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(hbm))

    blend  = _BLEND(0, 0, 255, 1)
    pt_dst = _POINT(rect.left, rect.top)
    pt_src = _POINT(0, 0)
    sz     = _SIZE(w, h)
    ok = _u32.UpdateLayeredWindow(ctypes.c_void_p(hwnd), ctypes.c_void_p(sdc),
        ctypes.byref(pt_dst), ctypes.byref(sz),
        ctypes.c_void_p(mdc), ctypes.byref(pt_src), 0, ctypes.byref(blend), 2)
    if not ok:
        err = _k32.GetLastError()
        print(f"[ULW] FAIL hwnd={hwnd:#x} err={err} sz={w}x{h} dst=({rect.left},{rect.top})")

    _g32.SelectObject(ctypes.c_void_p(mdc), ctypes.c_void_p(old))
    _g32.DeleteObject(ctypes.c_void_p(hbm))
    _g32.DeleteDC(ctypes.c_void_p(mdc))
    _u32.ReleaseDC(None, ctypes.c_void_p(sdc))


def _pil_draw_particle(draw: PILDraw.ImageDraw, p: "Particle",
                       ox: int = 0, oy: int = 0):
    r  = int(p.r)
    if r < 1:
        return
    st = p.size_t
    cx, cy = int(p.x) + ox, int(p.y) + oy

    if p.kind in ("bloom", "depth"):
        if r > 4:
            gr = int(r * 1.85)
            gc = tuple(max(2, int(c * 0.28 * st)) for c in p.glow) + (255,)
            draw.ellipse([cx-gr, cy-gr, cx+gr, cy+gr], fill=gc)
        bright = tuple(max(2, int(c * (0.55 + 0.45 * st))) for c in p.color) + (255,)
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=bright)

    elif p.kind == "drift":
        angle  = math.atan2(p.vy, p.vx) if (p.vx or p.vy) else p.angle
        rx, ry = r * 1.9, r * 0.55
        bright = tuple(max(2, int(c * (0.65 + 0.35 * st))) for c in p.color) + (255,)
        pts = [
            (cx + (rx * math.cos(a)) * math.cos(angle) - (ry * math.sin(a)) * math.sin(angle),
             cy + (rx * math.cos(a)) * math.sin(angle) + (ry * math.sin(a)) * math.cos(angle))
            for a in (2 * math.pi * i / 20 for i in range(20))
        ]
        draw.polygon(pts, fill=bright)

    elif p.kind == "gabor":
        _pil_draw_gabor(draw, p, r, st, cx, cy)


def _pil_draw_gabor(draw: PILDraw.ImageDraw, p: "Particle", r: int, st: float, cx: int, cy: int):
    spacing = max(3, r / 4.5)
    n       = max(3, int(r * 2 / spacing))
    bright  = tuple(max(2, int(c * (0.45 + 0.55 * st))) for c in p.color)
    lw      = max(1, r // 7)
    for i in range(-n // 2, n // 2 + 1):
        d2 = (i * spacing) ** 2
        if d2 >= r * r:
            continue
        hc  = math.sqrt(r * r - d2)
        ox2  =  i * spacing * math.sin(p.angle)
        oy2  = -i * spacing * math.cos(p.angle)
        x1  = cx + ox2 - hc * math.cos(p.angle)
        y1  = cy + oy2 - hc * math.sin(p.angle)
        x2  = cx + ox2 + hc * math.cos(p.angle)
        y2  = cy + oy2 + hc * math.sin(p.angle)
        shade = 0.3 + 0.7 * abs(math.sin(i * math.pi / max(1, n / 2)))
        lc = tuple(max(2, int(c * shade)) for c in bright) + (255,)
        draw.line([(x1, y1), (x2, y2)], fill=lc, width=lw)


# ─── Полоса по одному краю экрана ────────────────────────────────────────────

class Strip:
    """Полоса-окно с colorkey-прозрачностью (SetLayeredWindowAttributes).
    WS_EX_LAYERED устанавливается ДО вызова SLA — это главное условие.
    Фон #010203 не совпадает ни с одним цветом частиц (min comp = 2)."""

    _KEY_BGR = 0x030201   # Windows COLORREF (B,G,R): R=1,G=2,B=3
    _KEY_HEX = "#010203"
    _KEY_RGB = (1, 2, 3)

    def __init__(self, root: tk.Tk, side: str, x: int, y: int, w: int, h: int):
        self.side = side
        self.sw, self.sh = w, h
        self.particles: List[Particle] = []

        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.configure(bg=self._KEY_HEX)
        win.wm_attributes("-topmost", True)
        win.update()

        hwnd = win.winfo_id()

        # 1. Сначала устанавливаем WS_EX_LAYERED | WS_EX_TRANSPARENT
        GWL_EXSTYLE       = -20
        WS_EX_LAYERED     = 0x80000
        WS_EX_TRANSPARENT = 0x20
        cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, cur | WS_EX_LAYERED | WS_EX_TRANSPARENT)

        # 2. Только после WS_EX_LAYERED вызываем SetLayeredWindowAttributes
        ctypes.windll.user32.SetLayeredWindowAttributes(
            ctypes.c_void_p(hwnd), self._KEY_BGR, 0, 0x1)  # LWA_COLORKEY

        cv = tk.Canvas(win, width=w, height=h,
                       bg=self._KEY_HEX, highlightthickness=0)
        cv.pack()

        self.win    = win
        self._cv    = cv
        self._photo = None
        self._img_id: int | None = None

    def render(self):
        frame = PILImage.new("RGBA", (self.sw, self.sh), (0, 0, 0, 0))
        draw  = PILDraw.Draw(frame)
        for p in self.particles:
            _pil_draw_particle(draw, p)
        bg     = PILImage.new("RGBA", (self.sw, self.sh), (*self._KEY_RGB, 255))
        result = PILImage.alpha_composite(bg, frame).convert("RGB")
        self._photo = PILImageTk.PhotoImage(image=result)
        if self._img_id is None:
            self._img_id = self._cv.create_image(0, 0, anchor="nw",
                                                  image=self._photo)
        else:
            self._cv.itemconfigure(self._img_id, image=self._photo)


# ─── Overlay-движок (4 полосы по краям) ──────────────────────────────────────

class EdgeOverlay:
    FPS      = 30
    FRAME_MS = 1000 // FPS

    def __init__(self, root: tk.Tk, settings: dict):
        self.root = root
        self.s    = settings
        self.running = False
        self._after_spawn  = None
        self._after_render = None
        self.sw = root.winfo_screenwidth()
        self.sh = root.winfo_screenheight()
        self.strips: List[Strip] = []

    # ── Публичное API ─────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return
        self.strips = self._build_strips()
        if not self.strips:
            return
        self.running = True
        self._schedule_spawn(0)
        self._schedule_render(0)

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self._after_spawn:
            self.root.after_cancel(self._after_spawn)
            self._after_spawn = None
        if self._after_render:
            self.root.after_cancel(self._after_render)
            self._after_render = None
        for s in self.strips:
            try:
                s.win.destroy()
            except Exception:
                pass
        self.strips = []

    def destroy(self):
        self.stop()

    def apply_settings(self, settings: dict):
        self.s = settings

    # ── Построение полос ─────────────────────────────────────────────────────

    def _edge_px(self) -> int:
        return cm_to_px(self.s.get("edge_cm", 3.0), self.root)

    def _build_strips(self) -> List[Strip]:
        ep    = self._edge_px()
        sw, sh = self.sw, self.sh
        sides  = self.s.get("sides", {k: True for k in ("top","bottom","left","right")})
        out    = []
        if sides.get("top"):    out.append(Strip(self.root, "top",     0,    0,   sw,   ep))
        if sides.get("bottom"): out.append(Strip(self.root, "bottom",  0, sh-ep,  sw,   ep))
        if sides.get("left"):   out.append(Strip(self.root, "left",    0,    0,   ep,   sh))
        if sides.get("right"):  out.append(Strip(self.root, "right", sw-ep,  0,   ep,   sh))
        return out

    # ── Спавн ─────────────────────────────────────────────────────────────────

    def _schedule_spawn(self, ms: int):
        self._after_spawn = self.root.after(ms, self._do_spawn)

    def _do_spawn(self):
        if not self.running:
            return
        intensity     = self.s.get("intensity", 60) / 100
        max_per_strip = int(4 + intensity * 7)

        for strip in self.strips:
            if len(strip.particles) < max_per_strip:
                for _ in range(random.randint(1, 2)):
                    self._spawn_on(strip)

        interval = int(400 - intensity * 290)
        self._schedule_spawn(max(80, interval))

    def _spawn_on(self, strip: Strip):
        intensity = self.s.get("intensity", 60) / 100
        scheme    = SCHEMES.get(self.s.get("scheme", "green"), SCHEMES["green"])
        jitter    = random.randint(-18, 18)
        color     = tuple(max(2, min(255, c + jitter)) for c in scheme["main"])
        glow      = scheme["glow"]

        thin  = strip.sh if strip.side in ("top", "bottom") else strip.sw
        r_max = thin * random.uniform(0.15, 0.48)
        d_life = 0.004 + intensity * 0.013

        pat  = self.s.get("pattern", "mix")
        kind = random.choice(["bloom", "depth", "drift", "gabor"]) if pat == "mix" else pat

        x = random.uniform(0, strip.sw)
        y = random.uniform(0, strip.sh)
        vx = vy = 0.0
        spd = intensity * 0.8

        if kind == "depth":
            if strip.side == "top":      vy =  spd
            elif strip.side == "bottom": vy = -spd
            elif strip.side == "left":   vx =  spd
            else:                        vx = -spd
        elif kind == "drift":
            if strip.side in ("top", "bottom"):
                vx = random.choice([-1, 1]) * spd * random.uniform(0.4, 1.0)
            else:
                vy = random.choice([-1, 1]) * spd * random.uniform(0.4, 1.0)

        strip.particles.append(Particle(
            x=x, y=y, vx=vx, vy=vy,
            r_min=1.0, r_max=r_max,
            life=0.0, d_life=d_life,
            kind=kind, color=color, glow=glow,
            angle=random.uniform(0, math.pi),
        ))

    # ── Рендер ───────────────────────────────────────────────────────────────

    def _schedule_render(self, ms: int):
        self._after_render = self.root.after(ms, self._do_render)

    def _do_render(self):
        if not self.running:
            return
        for strip in self.strips:
            strip.particles = [
                p for p in strip.particles
                if p.tick()
                and -p.r_max <= p.x <= strip.sw + p.r_max
                and -p.r_max <= p.y <= strip.sh + p.r_max
            ]
            strip.render()
        self._schedule_render(self.FRAME_MS)


# ─── Главное окно ─────────────────────────────────────────────────────────────

class App:

    TIPS = [
        "💡 Паттерн «Глубина»: объекты приближаются/удаляются — хрусталик тренирует аккомодацию рефлекторно.",
        "💡 Оптимальная ширина краёв: 2.5–4 см — периферия видит движение, центр полностью свободен.",
        "💡 Интенсивность 30–50% не мешает работе, но даёт заметный эффект уже через 5 минут.",
        "💡 Паттерн «Габор» активирует нейроны зрительной коры и улучшает контрастную чувствительность.",
        "💡 Регулярность важнее интенсивности: 5 мин через каждые 45–60 мин работы.",
        f"💡 Горячая клавиша {HOTKEY} — старт/стоп сеанса из любого приложения.",
    ]

    def __init__(self):
        self.s = load_settings()

        self.root = tk.Tk()
        self.root.title(f"PeriphEyes v{VERSION}")
        self.root.geometry("480x640")
        self.root.configure(bg=UI_BG)
        self.root.resizable(False, False)

        self.overlay = EdgeOverlay(self.root, self.s)

        self._session_active   = False
        self._session_total    = 0
        self._session_remain   = 0
        self._countdown_remain = 0
        self._tip_idx          = 0
        self._after_session    = None
        self._after_countdown  = None
        self._tray: pystray.Icon | None = None

        self._build_ui()
        self._setup_tray()
        self._setup_hotkey()
        self._schedule_interval()

        # Закрытие окна → сворачивание в трей
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.root.mainloop()

    # ── Системный трей ────────────────────────────────────────────────────────

    def _make_tray_image(self) -> PILImage.Image:
        """Иконка трея: глаз из кругов, 64×64 RGBA."""
        sz = 64
        img = PILImage.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d   = PILDraw.Draw(img)
        d.ellipse([2, 2, sz-2, sz-2],  fill=(13, 13, 26, 240))       # фон
        d.ellipse([8, 20, 56, 44],  outline=(0, 229, 122, 255), width=5)  # склера
        d.ellipse([24, 24, 40, 40], fill=(0, 229, 122, 255))           # зрачок
        d.ellipse([29, 29, 34, 34], fill=(13, 13, 26, 255))            # блик
        return img

    def _setup_tray(self):
        img = self._make_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem(
                "Запустить сеанс",
                lambda icon, item: self.root.after(0, self._start_now),
            ),
            pystray.MenuItem(
                "Остановить",
                lambda icon, item: self.root.after(0, self._stop_session),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Показать окно",
                lambda icon, item: self.root.after(0, self._show_from_tray),
                default=True,   # двойной клик по иконке
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Автозапуск с Windows",
                lambda icon, item: self.root.after(0, self._tray_toggle_autostart),
                checked=lambda item: autostart_is_on(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Выход",
                lambda icon, item: self.root.after(0, self._quit),
            ),
        )
        self._tray = pystray.Icon("PeriphEyes", img, "PeriphEyes", menu=menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _hide_to_tray(self):
        self.root.withdraw()

    def _show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_toggle_autostart(self):
        if autostart_is_on():
            autostart_disable()
        else:
            autostart_enable()
        # Синхронизируем чекбокс в Settings
        if hasattr(self, "_autostart_var"):
            self._autostart_var.set(autostart_is_on())

    # ── Горячая клавиша ───────────────────────────────────────────────────────

    def _setup_hotkey(self):
        # Запускаем в отдельном потоке с паузой — tkinter должен стартовать раньше
        def register():
            import time as _t
            _t.sleep(0.5)
            try:
                kb.add_hotkey(HOTKEY, lambda: self.root.after(0, self._toggle_session))
            except Exception as e:
                print(f"[PeriphEyes] hotkey failed: {e}")
        threading.Thread(target=register, daemon=True).start()

    def _toggle_session(self):
        if self._session_active:
            self._stop_session()
        else:
            self._start_now()

    # ── Построение UI ─────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self.root, bg=UI_CARD, pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text="◉  PeriphEyes",
                 font=("Segoe UI", 18, "bold"), bg=UI_CARD, fg=UI_ACCENT).pack()
        tk.Label(hdr, text="Периферийная тренировка зрения  |  " + HOTKEY,
                 font=("Segoe UI", 9), bg=UI_CARD, fg=UI_DIM).pack()

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook",     background=UI_BG,   borderwidth=0)
        style.configure("TNotebook.Tab", background=UI_CARD, foreground=UI_DIM,
                        padding=[18, 8], font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", UI_ACCENT)],
                  foreground=[("selected", UI_BG)])
        style.configure("Accent.Horizontal.TProgressbar",
                        troughcolor=UI_BG, background=UI_ACCENT, thickness=8)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)

        t_main = tk.Frame(nb, bg=UI_BG); nb.add(t_main, text="  Главная  ")
        t_set  = tk.Frame(nb, bg=UI_BG); nb.add(t_set,  text=" Настройки ")
        t_info = tk.Frame(nb, bg=UI_BG); nb.add(t_info,  text="  Принцип  ")

        self._tab_main(t_main)
        self._tab_settings(t_set)
        self._tab_info(t_info)

    # ── Вкладка «Главная» ─────────────────────────────────────────────────────

    def _tab_main(self, parent):
        sc = tk.Frame(parent, bg=UI_CARD, padx=20, pady=18)
        sc.pack(fill="x", padx=16, pady=(16, 8))

        self._lbl_status = tk.Label(sc, text="● Ожидание",
                                    font=("Segoe UI", 12, "bold"), bg=UI_CARD, fg=UI_DIM)
        self._lbl_status.pack()

        self._lbl_countdown = tk.Label(sc, text="—",
                                       font=("Segoe UI", 44, "bold"), bg=UI_CARD, fg=UI_FG)
        self._lbl_countdown.pack()

        self._lbl_cd_label = tk.Label(sc, text="до следующего сеанса",
                                      font=("Segoe UI", 9), bg=UI_CARD, fg=UI_DIM)
        self._lbl_cd_label.pack()

        br = tk.Frame(sc, bg=UI_CARD)
        br.pack(pady=(14, 0))
        self._btn_start = self._btn(br, "  Запустить сейчас",
                                    self._start_now, bg=UI_ACCENT, fg=UI_BG)
        self._eye_icon = self._make_eye_icon()
        self._btn_start.configure(image=self._eye_icon, compound="left")
        self._btn_start.pack(side="left", padx=4)

        self._btn_stop = self._btn(br, "■  Стоп", self._stop_session,
                                   bg=UI_CARD, fg=UI_FG)
        self._btn_stop.config(state="disabled")
        self._btn_stop.pack(side="left", padx=4)

        pc = tk.Frame(parent, bg=UI_CARD, padx=20, pady=12)
        pc.pack(fill="x", padx=16, pady=4)
        tk.Label(pc, text="Прогресс сеанса",
                 font=("Segoe UI", 10, "bold"), bg=UI_CARD, fg=UI_FG).pack(anchor="w")
        self._progress = ttk.Progressbar(pc, orient="horizontal", mode="determinate",
                                         style="Accent.Horizontal.TProgressbar")
        self._progress.pack(fill="x", pady=(6, 4))
        self._lbl_prog = tk.Label(pc, text="Сеанс не активен",
                                  font=("Segoe UI", 9), bg=UI_CARD, fg=UI_DIM)
        self._lbl_prog.pack(anchor="w")

        qc = tk.Frame(parent, bg=UI_CARD, padx=20, pady=10)
        qc.pack(fill="x", padx=16, pady=4)
        tk.Label(qc, text="Цветовая схема",
                 font=("Segoe UI", 10, "bold"), bg=UI_CARD, fg=UI_FG).pack(anchor="w")
        sr = tk.Frame(qc, bg=UI_CARD)
        sr.pack(anchor="w", pady=(6, 0))
        scheme_labels = {"green": "Зелёный", "amber": "Янтарь",
                         "blue": "Синий", "ghost": "Призрак"}
        for name, sc_data in SCHEMES.items():
            color = rgb_hex(*sc_data["main"])
            col = tk.Frame(sr, bg=UI_CARD)
            col.pack(side="left", padx=4)
            ind = tk.Frame(col, bg=color, width=28, height=28, cursor="hand2")
            ind.pack()
            ind.bind("<Button-1>", lambda e, n=name: self._set_scheme(n))
            tk.Label(col, text=scheme_labels[name], font=("Segoe UI", 8),
                     bg=UI_CARD, fg=UI_DIM).pack()

        tc = tk.Frame(parent, bg="#08080F", padx=14, pady=10)
        tc.pack(fill="x", padx=16, pady=(4, 16))
        self._lbl_tip = tk.Label(tc, text="",
                                 font=("Segoe UI", 9), bg="#08080F", fg=UI_DIM,
                                 wraplength=390, justify="left")
        self._lbl_tip.pack(anchor="w")
        self._next_tip()

    # ── Вкладка «Настройки» ───────────────────────────────────────────────────

    def _tab_settings(self, parent):
        outer = tk.Frame(parent, bg=UI_BG)
        outer.pack(fill="both", expand=True, padx=16, pady=12)

        self._svars: dict = {}

        def scale_row(label: str, key: str, lo: float, hi: float, unit: str,
                      is_float: bool = False):
            f = tk.Frame(outer, bg=UI_CARD, padx=14, pady=10)
            f.pack(fill="x", pady=3)
            row = tk.Frame(f, bg=UI_CARD)
            row.pack(fill="x")
            tk.Label(row, text=label, font=("Segoe UI", 10),
                     bg=UI_CARD, fg=UI_FG).pack(side="left")
            val_lbl = tk.Label(row, font=("Segoe UI", 10, "bold"),
                               bg=UI_CARD, fg=UI_ACCENT)
            val_lbl.pack(side="right")

            cur = self.s.get(key, DEFAULTS.get(key, 0))
            val_lbl.config(text=f"{cur:.1f}{unit}" if is_float else f"{cur}{unit}")

            var = tk.DoubleVar(value=cur) if is_float else tk.IntVar(value=int(cur))
            self._svars[key] = var

            def on_change(v, k=key, lbl=val_lbl, u=unit, fl=is_float):
                val = float(v)
                val = (round(val * 2) / 2) if fl else int(val)
                self.s[k] = val
                lbl.config(text=f"{val:.1f}{u}" if fl else f"{val}{u}")
                save_settings(self.s)
                self.overlay.apply_settings(self.s)

            ttk.Scale(f, from_=lo, to=hi, variable=var,
                      command=on_change, orient="horizontal").pack(fill="x", pady=(4, 0))

        tk.Label(outer, text="Таймер", font=("Segoe UI", 11, "bold"),
                 bg=UI_BG, fg=UI_DIM).pack(anchor="w", pady=(0, 4))
        scale_row("Интервал между сеансами", "interval_min", 10, 120, " мин")
        scale_row("Длительность сеанса",     "session_min",   1,  30, " мин")

        tk.Label(outer, text="Внешний вид", font=("Segoe UI", 11, "bold"),
                 bg=UI_BG, fg=UI_DIM).pack(anchor="w", pady=(10, 4))
        scale_row("Ширина краёв",  "edge_cm",   1.0, 6.0, " см", is_float=True)
        scale_row("Интенсивность", "intensity", 10, 100, "%")

        tk.Label(outer, text="Паттерн анимации", font=("Segoe UI", 11, "bold"),
                 bg=UI_BG, fg=UI_DIM).pack(anchor="w", pady=(10, 4))
        pf = tk.Frame(outer, bg=UI_CARD, padx=14, pady=10)
        pf.pack(fill="x", pady=3)
        self._pat_var = tk.StringVar(value=self.s.get("pattern", "mix"))
        patterns = [
            ("mix",   "Микс — случайное чередование"),
            ("bloom", "Bloom — пульсирующие круги"),
            ("depth", "Глубина — имитация приближения"),
            ("drift", "Дрейф — скольжение вдоль края"),
            ("gabor", "Габор — синусоидальная решётка"),
        ]
        for val, lbl in patterns:
            tk.Radiobutton(pf, text=lbl, variable=self._pat_var, value=val,
                           command=lambda v=val: self._set_pattern(v),
                           bg=UI_CARD, fg=UI_FG, selectcolor=UI_BG,
                           activebackground=UI_CARD, font=("Segoe UI", 9)).pack(anchor="w")

        tk.Label(outer, text="Активные стороны", font=("Segoe UI", 11, "bold"),
                 bg=UI_BG, fg=UI_DIM).pack(anchor="w", pady=(10, 4))
        sf = tk.Frame(outer, bg=UI_CARD, padx=14, pady=10)
        sf.pack(fill="x", pady=3)
        self._side_vars: dict = {}
        sides  = self.s.get("sides", {k: True for k in ("top", "bottom", "left", "right")})
        labels = {"top": "Верх ↑", "bottom": "Низ ↓", "left": "← Лево", "right": "Право →"}
        for row_sides in [("top", "bottom"), ("left", "right")]:
            row = tk.Frame(sf, bg=UI_CARD)
            row.pack(fill="x")
            for side in row_sides:
                var = tk.BooleanVar(value=sides.get(side, True))
                self._side_vars[side] = var
                tk.Checkbutton(row, text=labels[side], variable=var,
                               command=self._update_sides,
                               bg=UI_CARD, fg=UI_FG, selectcolor=UI_BG,
                               activebackground=UI_CARD,
                               font=("Segoe UI", 9)).pack(side="left", padx=12, pady=2)

        # ── Система ───────────────────────────────────────────────────────────
        tk.Label(outer, text="Система", font=("Segoe UI", 11, "bold"),
                 bg=UI_BG, fg=UI_DIM).pack(anchor="w", pady=(10, 4))
        sys_f = tk.Frame(outer, bg=UI_CARD, padx=14, pady=10)
        sys_f.pack(fill="x", pady=3)

        self._autostart_var = tk.BooleanVar(value=autostart_is_on())
        tk.Checkbutton(sys_f,
                       text="Автозапуск с Windows (через реестр HKCU)",
                       variable=self._autostart_var,
                       command=self._checkbox_autostart,
                       bg=UI_CARD, fg=UI_FG, selectcolor=UI_BG,
                       activebackground=UI_CARD,
                       font=("Segoe UI", 9)).pack(anchor="w")

        tk.Label(sys_f,
                 text=f"Горячая клавиша: {HOTKEY}  (старт / стоп из любого приложения)",
                 font=("Segoe UI", 9), bg=UI_CARD, fg=UI_DIM).pack(anchor="w", pady=(6, 0))

        tk.Label(sys_f,
                 text="Закрыть окно = свернуть в трей. Выход — через меню трея.",
                 font=("Segoe UI", 9, "italic"), bg=UI_CARD, fg=UI_DIM).pack(anchor="w", pady=(2, 0))

    # ── Вкладка «Принцип» ─────────────────────────────────────────────────────

    def _tab_info(self, parent):
        cv = tk.Canvas(parent, bg=UI_BG, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=cv.yview)
        frame = tk.Frame(cv, bg=UI_BG)
        frame.bind("<Configure>",
                   lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=frame, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True, padx=8)
        sb.pack(side="right", fill="y")
        cv.bind("<MouseWheel>",
                lambda e: cv.yview_scroll(-1 * (e.delta // 120), "units"))

        sections = [
            ("Идея", [
                "PeriphEyes занимает только края экрана (1–6 см с каждой стороны). "
                "Центр остаётся полностью свободным — работай в редакторе, браузере, "
                "любом приложении.",
                "Периферийное зрение автоматически реагирует на движение. Глаза "
                "совершают микродвижения, не отвлекая сознательного внимания.",
            ]),
            ("Тренировка аккомодации", [
                "Паттерн «Глубина»: объекты на краях увеличиваются (имитация приближения) "
                "и уменьшаются (удаление). Хрусталик рефлекторно меняет кривизну.",
                "Цилиарная мышца попеременно напрягается и расслабляется — снимается "
                "спазм аккомодации, который копится при долгой работе за монитором.",
                "Эффект аналогичен упражнению «метка на стекле», но автоматизирован.",
            ]),
            ("Паттерн Bloom — снятие спазма", [
                "Мягкие круги с ореолом появляются, достигают максимума и исчезают. "
                "Это заставляет зрительные мышцы попеременно фокусироваться и "
                "расслабляться без волевых усилий.",
            ]),
            ("Стимулы Габора", [
                "Синусоидальные решётки (паттерн «Габор») активируют нейроны "
                "первичной зрительной коры (V1). Исследования нейробиологов "
                "(Polat, Sagi) показывают, что такие стимулы повышают "
                "контрастную чувствительность.",
                "Движущиеся Габор-патчи вызывают саккады — быстрые скачки глаз, "
                "улучшающие кровоснабжение сетчатки.",
            ]),
            ("Рекомендуемый режим", [
                "• Интервал: 45–60 минут",
                "• Сеанс: 5–7 минут",
                "• Интенсивность: 40–65%",
                "• Ширина краёв: 2.5–4 см",
                "• Паттерн «Микс» — оптимальное чередование всех стимулов",
                "• Результат заметен через 1–2 недели регулярного использования",
            ]),
        ]

        for title, lines in sections:
            tk.Label(frame, text=title, font=("Segoe UI", 11, "bold"),
                     bg=UI_BG, fg=UI_ACCENT, anchor="w").pack(anchor="w", padx=16, pady=(14, 4))
            for line in lines:
                tk.Label(frame, text=line, font=("Segoe UI", 9),
                         bg=UI_BG, fg=UI_FG, wraplength=400,
                         justify="left", anchor="w").pack(anchor="w", padx=20, pady=2)

    # ── Логика управления сеансом ─────────────────────────────────────────────

    def _start_now(self):
        self._cancel_countdown()
        self._do_start()

    def _do_start(self):
        if self._session_active:
            return
        self._session_active = True

        total = self.s.get("session_min", 5) * 60
        self._session_total  = total
        self._session_remain = total

        self._lbl_status.config(text="● Сеанс активен", fg=UI_ACCENT)
        self._lbl_cd_label.config(text="осталось в сеансе")
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._progress.config(maximum=total, value=0)

        self.overlay.start()
        self._tick_session()

    def _tick_session(self):
        if not self._session_active:
            return
        elapsed = self._session_total - self._session_remain
        self._progress.config(value=elapsed)
        m, s = divmod(self._session_remain, 60)
        self._lbl_countdown.config(text=f"{m:02d}:{s:02d}")
        self._lbl_prog.config(
            text=f"Прошло: {elapsed // 60}:{elapsed % 60:02d} / "
                 f"{self._session_total // 60}:{self._session_total % 60:02d} мин")
        if self._session_remain > 0:
            self._session_remain -= 1
            self._after_session = self.root.after(1000, self._tick_session)
        else:
            self._stop_session()

    def _stop_session(self):
        if not self._session_active:
            return
        self._session_active = False
        if self._after_session:
            self.root.after_cancel(self._after_session)

        self.overlay.stop()
        self._lbl_status.config(text="● Ожидание", fg=UI_DIM)
        self._lbl_cd_label.config(text="до следующего сеанса")
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")
        self._progress.config(value=0)
        self._lbl_prog.config(text="Сеанс завершён")
        self._schedule_interval()

    def _schedule_interval(self):
        self._cancel_countdown()
        self._countdown_remain = self.s.get("interval_min", 45) * 60
        self._tick_countdown()

    def _cancel_countdown(self):
        if self._after_countdown:
            self.root.after_cancel(self._after_countdown)
            self._after_countdown = None

    def _tick_countdown(self):
        if self._session_active:
            return
        m, s = divmod(self._countdown_remain, 60)
        self._lbl_countdown.config(text=f"{m:02d}:{s:02d}")
        self._lbl_status.config(text="● Ожидание", fg=UI_DIM)
        if self._countdown_remain > 0:
            self._countdown_remain -= 1
            self._after_countdown = self.root.after(1000, self._tick_countdown)
        else:
            self._do_start()

    # ── Переключатели ─────────────────────────────────────────────────────────

    def _set_scheme(self, name: str):
        self.s["scheme"] = name
        save_settings(self.s)
        self.overlay.apply_settings(self.s)

    def _set_pattern(self, name: str):
        self.s["pattern"] = name
        save_settings(self.s)
        self.overlay.apply_settings(self.s)

    def _update_sides(self):
        self.s["sides"] = {k: v.get() for k, v in self._side_vars.items()}
        save_settings(self.s)
        self.overlay.apply_settings(self.s)

    def _checkbox_autostart(self):
        if self._autostart_var.get():
            autostart_enable()
        else:
            autostart_disable()

    # ── Подсказки ─────────────────────────────────────────────────────────────

    def _next_tip(self):
        self._lbl_tip.config(text=self.TIPS[self._tip_idx % len(self.TIPS)])
        self._tip_idx += 1
        self.root.after(11000, self._next_tip)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_eye_icon(self, size: int = 18) -> PILImageTk.PhotoImage:
        img = PILImage.new("RGBA", (size, size), (0, 0, 0, 0))
        d = PILDraw.Draw(img)
        m = size // 2
        # outer eye shape
        d.ellipse([1, m - size // 5, size - 2, m + size // 5], outline=UI_ACCENT, width=2)
        # pupil
        r = size // 6
        d.ellipse([m - r, m - r, m + r, m + r], fill=UI_ACCENT)
        return PILImageTk.PhotoImage(image=img)

    def _btn(self, parent, text, cmd, bg=UI_CARD, fg=UI_FG) -> tk.Button:
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         font=("Segoe UI", 10), relief="flat",
                         padx=14, pady=8, cursor="hand2",
                         activebackground=UI_BG, activeforeground=UI_FG)

    def _quit(self):
        try:
            kb.unhook_all()
        except Exception:
            pass
        if self._tray:
            self._tray.stop()
        self.overlay.destroy()
        save_settings(self.s)
        self.root.destroy()


# ─── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    App()
