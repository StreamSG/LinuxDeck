"""High-contrast anti-aliased tile and dial-strip renderer for LinuxDeck (480x270)."""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_REG_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
FONT_MONO_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]


@lru_cache(maxsize=64)
def get_font(kind: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = (
        FONT_BOLD_PATHS
        if kind == "bold"
        else FONT_MONO_PATHS
        if kind == "mono"
        else FONT_REG_PATHS
    )
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def hex_to_rgb(color: str | Sequence[int], default: tuple[int, int, int] = (0, 255, 102)) -> tuple[int, int, int]:
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        return (int(color[0]) & 0xFF, int(color[1]) & 0xFF, int(color[2]) & 0xFF)
    if isinstance(color, str):
        s = color.strip().lstrip("#")
        if len(s) == 6:
            try:
                return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
            except ValueError:
                pass
    return default


def blend_rgb(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _draw_vector_icon(
    draw: ImageDraw.ImageDraw,
    icon: str,
    cx: int,
    cy: int,
    color: tuple[int, int, int],
    active: bool = False,
) -> None:
    """Draw crisp vector icons centered at (cx, cy) within roughly a 28x28 box."""
    ic = icon.lower().strip()

    if ic in ("mic", "mic_mute"):
        draw.rounded_rectangle([cx - 5, cy - 11, cx + 5, cy + 2], radius=5, outline=color, width=2)
        draw.arc([cx - 9, cy - 6, cx + 9, cy + 6], start=0, end=180, fill=color, width=2)
        draw.line([cx, cy + 6, cx, cy + 11], fill=color, width=2)
        draw.line([cx - 5, cy + 11, cx + 5, cy + 11], fill=color, width=2)
        if ic == "mic_mute" or active:
            draw.line([cx - 11, cy - 10, cx + 11, cy + 10], fill=(255, 65, 85), width=3)

    elif ic in ("speaker", "headset", "audio", "mute"):
        pts = [
            (cx - 10, cy - 4),
            (cx - 5, cy - 4),
            (cx + 1, cy - 10),
            (cx + 1, cy + 10),
            (cx - 5, cy + 4),
            (cx - 10, cy + 4),
        ]
        draw.polygon(pts, fill=color)
        if ic == "mute":
            draw.line([cx + 4, cy - 5, cx + 11, cy + 5], fill=(255, 65, 85), width=2)
            draw.line([cx + 11, cy - 5, cx + 4, cy + 5], fill=(255, 65, 85), width=2)
        else:
            draw.arc([cx - 1, cy - 6, cx + 7, cy + 6], start=-55, end=55, fill=color, width=2)
            draw.arc([cx - 3, cy - 10, cx + 11, cy + 10], start=-55, end=55, fill=color, width=2)

    elif ic in ("obs", "scene", "camera"):
        draw.rounded_rectangle([cx - 12, cy - 8, cx + 5, cy + 8], radius=3, outline=color, width=2)
        draw.polygon([(cx + 6, cy - 4), (cx + 12, cy - 8), (cx + 12, cy + 8), (cx + 6, cy + 4)], fill=color)
        if active:
            draw.ellipse([cx - 6, cy - 3, cx, cy + 3], fill=color)

    elif ic in ("record", "rec"):
        draw.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], outline=color, width=2)
        r = 7 if active else 5
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 50, 70) if active else color)

    elif ic in ("stream", "broadcast", "live"):
        draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)
        draw.arc([cx - 8, cy - 8, cx + 8, cy + 8], start=135, end=225, fill=color, width=2)
        draw.arc([cx - 8, cy - 8, cx + 8, cy + 8], start=-45, end=45, fill=color, width=2)
        draw.arc([cx - 13, cy - 13, cx + 13, cy + 13], start=135, end=225, fill=color, width=2)
        draw.arc([cx - 13, cy - 13, cx + 13, cy + 13], start=-45, end=45, fill=color, width=2)

    elif ic in ("play", "play_pause"):
        draw.polygon([(cx - 9, cy - 9), (cx - 9, cy + 9), (cx + 3, cy)], fill=color)
        draw.rectangle([cx + 5, cy - 8, cx + 7, cy + 8], fill=color)
        draw.rectangle([cx + 10, cy - 8, cx + 12, cy + 8], fill=color)

    elif ic in ("next", "skip_next"):
        draw.polygon([(cx - 9, cy - 8), (cx - 9, cy + 8), (cx + 3, cy)], fill=color)
        draw.polygon([(cx - 1, cy - 8), (cx - 1, cy + 8), (cx + 9, cy)], fill=color)
        draw.rectangle([cx + 9, cy - 8, cx + 11, cy + 8], fill=color)

    elif ic in ("prev", "skip_prev"):
        draw.rectangle([cx - 11, cy - 8, cx - 9, cy + 8], fill=color)
        draw.polygon([(cx + 1, cy - 8), (cx + 1, cy + 8), (cx - 9, cy)], fill=color)
        draw.polygon([(cx + 9, cy - 8), (cx + 9, cy + 8), (cx - 1, cy)], fill=color)

    elif ic in ("eq", "easyeffects", "mixer"):
        for dx, knob_y in [(-8, cy - 4), (0, cy + 3), (8, cy - 2)]:
            draw.line([cx + dx, cy - 10, cx + dx, cy + 10], fill=color, width=2)
            draw.ellipse([cx + dx - 3, knob_y - 3, cx + dx + 3, knob_y + 3], fill=color)

    elif ic == "cpu":
        draw.rounded_rectangle([cx - 8, cy - 8, cx + 8, cy + 8], radius=2, outline=color, width=2)
        draw.rectangle([cx - 4, cy - 4, cx + 4, cy + 4], fill=color)
        for d in (-5, 0, 5):
            draw.line([cx + d, cy - 12, cx + d, cy - 9], fill=color, width=2)
            draw.line([cx + d, cy + 9, cx + d, cy + 12], fill=color, width=2)
            draw.line([cx - 12, cy + d, cx - 9, cy + d], fill=color, width=2)
            draw.line([cx + 9, cy + d, cx + 12, cy + d], fill=color, width=2)

    elif ic == "gpu":
        draw.rounded_rectangle([cx - 12, cy - 7, cx + 12, cy + 7], radius=2, outline=color, width=2)
        draw.ellipse([cx - 8, cy - 4, cx, cy + 4], outline=color, width=2)
        draw.ellipse([cx + 2, cy - 4, cx + 10, cy + 4], outline=color, width=2)

    elif ic == "ram":
        draw.rounded_rectangle([cx - 12, cy - 6, cx + 12, cy + 6], radius=2, outline=color, width=2)
        for dx in (-7, -1, 5):
            draw.rectangle([cx + dx, cy - 3, cx + dx + 3, cy + 3], fill=color)

    elif ic in ("net", "network"):
        draw.line([cx - 5, cy - 9, cx - 5, cy + 9], fill=color, width=2)
        draw.polygon([(cx - 9, cy - 4), (cx - 1, cy - 4), (cx - 5, cy - 10)], fill=color)
        draw.line([cx + 5, cy - 9, cx + 5, cy + 9], fill=color, width=2)
        draw.polygon([(cx + 1, cy + 4), (cx + 9, cy + 4), (cx + 5, cy + 10)], fill=color)

    elif ic in ("temp", "thermometer", "psu", "battery"):
        draw.rounded_rectangle([cx - 3, cy - 11, cx + 3, cy + 5], radius=3, outline=color, width=2)
        draw.ellipse([cx - 6, cy + 2, cx + 6, cy + 12], fill=color)

    elif ic in ("terminal", "cmd", "script"):
        draw.rounded_rectangle([cx - 12, cy - 9, cx + 12, cy + 9], radius=3, outline=color, width=2)
        draw.line([cx - 8, cy - 4, cx - 3, cy], fill=color, width=2)
        draw.line([cx - 3, cy, cx - 8, cy + 4], fill=color, width=2)
        draw.line([cx - 1, cy + 4, cx + 6, cy + 4], fill=color, width=2)

    elif ic in ("discord", "chat", "vesktop"):
        draw.rounded_rectangle([cx - 11, cy - 8, cx + 11, cy + 6], radius=5, outline=color, width=2)
        draw.ellipse([cx - 6, cy - 3, cx - 3, cy], fill=color)
        draw.ellipse([cx + 3, cy - 3, cx + 6, cy], fill=color)

    elif ic in ("steam", "game"):
        draw.rounded_rectangle([cx - 12, cy - 6, cx + 12, cy + 7], radius=5, outline=color, width=2)
        draw.line([cx - 8, cy, cx - 2, cy], fill=color, width=2)
        draw.line([cx - 5, cy - 3, cx - 5, cy + 3], fill=color, width=2)
        draw.ellipse([cx + 4, cy - 3, cx + 8, cy + 1], fill=color)

    elif ic in ("browser", "web", "chrome"):
        draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], outline=color, width=2)
        draw.ellipse([cx - 4, cy - 10, cx + 4, cy + 10], outline=color, width=1)
        draw.line([cx - 10, cy, cx + 10, cy], fill=color, width=1)

    elif ic in ("brightness", "sun"):
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=color, width=2)
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            x1 = cx + int(math.cos(rad) * 7)
            y1 = cy + int(math.sin(rad) * 7)
            x2 = cx + int(math.cos(rad) * 11)
            y2 = cy + int(math.sin(rad) * 11)
            draw.line([x1, y1, x2, y2], fill=color, width=2)

    elif ic in ("clock", "time"):
        draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], outline=color, width=2)
        draw.line([cx, cy, cx, cy - 6], fill=color, width=2)
        draw.line([cx, cy, cx + 5, cy + 2], fill=color, width=2)

    elif ic in ("lock", "power"):
        draw.rounded_rectangle([cx - 8, cy - 2, cx + 8, cy + 10], radius=2, outline=color, width=2)
        draw.arc([cx - 5, cy - 10, cx + 5, cy], start=180, end=360, fill=color, width=2)

    else:
        draw.polygon([(cx, cy - 10), (cx + 10, cy), (cx, cy + 10), (cx - 10, cy)], outline=color, width=2)
        draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)


def render_key_tile(
    title: str,
    subtitle: str = "",
    icon: str = "default",
    accent_color: str | tuple[int, int, int] = "#00FF66",
    active: bool = False,
    pressed: bool = False,
    progress: Optional[float] = None,
    sparkline: Optional[Sequence[float]] = None,
    badge: str = "",
) -> Image.Image:
    """Render a 90x90 touch tile for one of the 12 center keys."""
    accent = hex_to_rgb(accent_color)
    img = Image.new("RGB", (90, 90), (8, 10, 14))
    draw = ImageDraw.Draw(img)

    if pressed:
        bg = blend_rgb((18, 22, 30), accent, 0.35)
        border = (255, 255, 255)
    elif active:
        bg = blend_rgb((14, 18, 24), accent, 0.22)
        border = accent
    else:
        bg = (16, 20, 27)
        border = blend_rgb((36, 42, 54), accent, 0.45)

    draw.rounded_rectangle([3, 3, 86, 86], radius=10, fill=bg, outline=border, width=2 if active or pressed else 1)

    bar_color = (255, 255, 255) if pressed else accent
    draw.rounded_rectangle([18, 4, 71, 7], radius=2, fill=bar_color)

    if sparkline and len(sparkline) >= 2:
        pts = []
        n = len(sparkline)
        x0, x1 = 10, 79
        y0, y1 = 34, 60
        for i, val in enumerate(sparkline):
            clamped = max(0.0, min(100.0, float(val)))
            px = x0 + int((x1 - x0) * i / max(1, n - 1))
            py = y1 - int((y1 - y0) * (clamped / 100.0))
            pts.append((px, py))
        if len(pts) >= 2:
            fill_poly = [(pts[0][0], y1)] + pts + [(pts[-1][0], y1)]
            draw.polygon(fill_poly, fill=blend_rgb(bg, accent, 0.22))
            draw.line(pts, fill=accent, width=2)
    else:
        icon_y = 25 if subtitle else 31
        _draw_vector_icon(draw, icon, 45, icon_y, accent, active=active)

    if badge:
        f_badge = get_font("bold", 9)
        bbox = draw.textbbox((0, 0), badge, font=f_badge)
        bw = bbox[2] - bbox[0] + 6
        badge_bg = (220, 45, 65) if badge in ("MUTE", "REC", "LIVE") else blend_rgb((30, 35, 45), accent, 0.6)
        draw.rounded_rectangle([82 - bw, 8, 82, 20], radius=3, fill=badge_bg)
        draw.text((82 - bw // 2, 14), badge, fill=(255, 255, 255), font=f_badge, anchor="mm")

    if progress is not None:
        p = max(0.0, min(1.0, float(progress)))
        bar_y = 48 if subtitle else 54
        draw.rounded_rectangle([12, bar_y, 77, bar_y + 5], radius=2, fill=(32, 38, 48))
        if p > 0.01:
            fill_w = max(4, int(65 * p))
            draw.rounded_rectangle([12, bar_y, 12 + fill_w, bar_y + 5], radius=2, fill=accent)

    if subtitle:
        f_sub = get_font("bold", 13 if len(subtitle) <= 9 else 11)
        sub_y = 54 if progress is None and not sparkline else 43 if progress is not None else 21
        draw.text(
            (45, sub_y),
            subtitle[:12],
            fill=(255, 255, 255) if active or sparkline else blend_rgb((220, 230, 240), accent, 0.35),
            font=f_sub,
            anchor="mm",
        )

    f_title = get_font("bold", 11 if len(title) <= 10 else 10)
    draw.text((45, 74), title[:13], fill=(235, 240, 248), font=f_title, anchor="mm")

    return img


def render_strip_slot(
    label: str,
    value_text: str = "",
    level: Optional[float] = None,
    muted: bool = False,
    accent_color: str | tuple[int, int, int] = "#00FF66",
) -> Image.Image:
    """Render a 60x90 dial indicator slot for the left (0..2) or right (3..5) side strip."""
    accent = (255, 65, 85) if muted else hex_to_rgb(accent_color)
    img = Image.new("RGB", (60, 90), (8, 10, 14))
    draw = ImageDraw.Draw(img)

    bg = (22, 14, 18) if muted else (14, 18, 25)
    border = blend_rgb((34, 40, 52), accent, 0.5)
    draw.rounded_rectangle([2, 2, 57, 87], radius=7, fill=bg, outline=border, width=1)

    f_lbl = get_font("bold", 10)
    draw.text((30, 13), label[:8].upper(), fill=(210, 220, 232), font=f_lbl, anchor="mm")

    if level is not None:
        lv = max(0.0, min(1.0, float(level)))
        bbox = [11, 26, 49, 64]
        draw.arc(bbox, start=135, end=405, fill=(34, 40, 52), width=5)
        if lv > 0.01 and not muted:
            end_angle = 135 + int(270 * lv)
            draw.arc(bbox, start=135, end=end_angle, fill=accent, width=5)
        elif muted:
            draw.arc(bbox, start=135, end=405, fill=(140, 35, 45), width=4)

        f_val = get_font("bold", 11)
        center_txt = "MUTE" if muted else (value_text or f"{int(round(lv * 100))}%")
        draw.text((30, 45), center_txt[:6], fill=(255, 110, 120) if muted else (255, 255, 255), font=f_val, anchor="mm")
    else:
        f_val = get_font("bold", 12)
        draw.text((30, 46), value_text[:7], fill=accent, font=f_val, anchor="mm")

    draw.rounded_rectangle([14, 78, 46, 81], radius=1, fill=accent)

    return img
