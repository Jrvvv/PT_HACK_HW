"""Color of one fixed lamp, from pixels only. No model."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

# Fallback spot. The live camera locks onto the bright core once, then stays there.
SPOT_XY = (520, 223)
SPOT_R = 120

CSS = {
    "red": "#ff2d2d",
    "green": "#3dff4a",
    "blue": "#2f7bff",
    "cyan": "#3de4ff",
    "yellow": "#ffe14a",
    "white": "#f4f4f4",
    "other": "#f2f2f2",
    "off": "#2a2a2a",
}


def name_rgb(rgb: list[int]) -> str:
    """Name a color from one RGB triple. Cyan is not called blue when green is close."""
    r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    mx, mn = max(r, g, b), min(r, g, b)
    if mx < 40:
        return "off"
    if mx - mn < 28 and mx > 160:
        return "white"
    # Warm lamp: both red and green high, blue almost off. Not red.
    if b < 90 and r > 140 and g > 120:
        return "yellow"
    if r > 140 and g < r * 0.45 and b < r * 0.45:
        return "red"
    if g >= r + 30 and g >= b + 25:
        return "green"
    if b >= r + 40 and b >= g + 50:
        return "blue"
    if g >= r + 30 and b >= r + 30 and abs(int(b) - int(g)) <= 55:
        return "cyan"
    return "other"


def _halo_rgb(sel: np.ndarray) -> tuple[list[int], float]:
    """Median of the brightest saturated pixels. The white core is clipped and drops out."""
    if sel.size == 0:
        return [0, 0, 0], 0.0
    chan = sel.astype(np.float32)
    mx = chan.max(axis=1)
    mn = chan.min(axis=1)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    score = np.where(mx >= 80, sat * mx, 0.0)
    keep = score > 0
    if int(keep.sum()) < 20:
        rgb = [int(v) for v in np.median(sel, axis=0)]
        return rgb, float(score.sum())
    positive = score[keep]
    thr = float(np.quantile(positive, 0.75))
    top = sel[score >= thr]
    rgb = [int(v) for v in np.median(top, axis=0)]
    return rgb, float(score[score >= thr].sum())


def red_peak(
    im: Image.Image,
    xy: tuple[int, int] = SPOT_XY,
    radius: int = SPOT_R,
    min_pixels: int = 40,
) -> dict | None:
    """A wrong password flashes red. It does not stay on, so count red pixels, do not average them away."""
    a = np.asarray(im.convert("RGB"))
    h, w, _ = a.shape
    cx, cy = int(xy[0]), int(xy[1])
    yy, xx = np.ogrid[:h, :w]
    disk = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius * radius
    sel = a[disk]
    if sel.size == 0:
        return None
    r = sel[:, 0].astype(np.int16)
    g = sel[:, 1].astype(np.int16)
    b = sel[:, 2].astype(np.int16)
    # Real red, not the warm yellow lamp (that one keeps G high).
    mask = (r >= 170) & (r > g + 55) & (r > b + 55) & (g < 150) & (b < 150)
    n = int(mask.sum())
    if n < min_pixels:
        return None
    rgb = [int(v) for v in np.median(sel[mask], axis=0)]
    return {"color": "red", "rgb": rgb, "pixels": n, "css": CSS["red"], "peak": True, "xy": [cx, cy], "radius": int(radius)}


def classify_image(
    im: Image.Image,
    xy: tuple[int, int] = SPOT_XY,
    radius: int = SPOT_R,
) -> dict:
    """Majority color inside the lamp circle. White cores lose to the colored ring."""
    rgb_im = im.convert("RGB")
    a = np.asarray(rgb_im)
    h, w, _ = a.shape
    cx, cy = int(xy[0]), int(xy[1])
    yy, xx = np.ogrid[:h, :w]
    dist2 = (yy - cy) ** 2 + (xx - cx) ** 2
    # Annulus: the core is often clipped to white, the color sits in the halo.
    inner = max(8, int(radius * 0.35))
    disk = (dist2 >= inner * inner) & (dist2 <= radius * radius)
    sel = a[disk]
    if sel.size == 0:
        return _result("off", [0, 0, 0], {}, xy, radius, 0.0)
    rgb, weight = _halo_rgb(sel)
    color = name_rgb(rgb)
    return _result(color, rgb, {"halo": round(weight, 1)}, xy, radius, weight)


def _result(color: str, rgb: list[int], weights: dict, xy, radius: int, weight: float) -> dict:
    return {
        "color": color,
        "rgb": rgb,
        "css": CSS.get(color, CSS["other"]),
        "weights": {k: round(v, 1) for k, v in weights.items()},
        "xy": [int(xy[0]), int(xy[1])],
        "radius": int(radius),
        "weight": round(float(weight), 1),
        "red": color == "red",
    }


def lock_blue_spot(im: Image.Image, xy: tuple[int, int] = SPOT_XY, radius: int = SPOT_R) -> tuple[int, int]:
    """Center on the clipped core of the blue lamp (white-cyan), not on the dim halo."""
    a = np.asarray(im.convert("RGB"))
    r = a[:, :, 0].astype(np.int16)
    g = a[:, :, 1].astype(np.int16)
    b = a[:, :, 2].astype(np.int16)
    sel = (b >= 245) & (g >= 200) & (r < 190) & (b + 15 >= g)
    if int(sel.sum()) < 40:
        sel = (b >= 210) & (b > r + 50) & (b + 20 >= g) & (g > r)
    if int(sel.sum()) < 40:
        return xy
    ys, xs = np.where(sel)
    return int(np.median(xs)), int(np.median(ys))


def annotate(im: Image.Image, xy: tuple[int, int], radius: int, color: str) -> Image.Image:
    out = im.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    x, y = xy
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline=CSS.get(color, "#fff"), width=3)
    draw.text((x + radius + 6, y - 8), color, fill=CSS.get(color, "#fff"))
    return out
