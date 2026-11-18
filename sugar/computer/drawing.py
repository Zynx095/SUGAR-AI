"""Drawing plans: what strokes to draw, in canvas-relative coordinates (0–1 on both axes).

* Simple subjects (circle, square, star, heart, house, sun, smiley, tree, flower…) have vector plans: fast, clean,
  no download needed.
* Anything else is traced from a reference image: OpenCV separates the subject from its background, finds its
  outline and main inner lines, simplifies them to a few hundred points, and scales them into the target box.
  :func:`drawability` scores candidate references so a clean silhouette or line drawing beats a busy photo.

A plan is a list of strokes; each stroke is a list of (x, y) points drawn with the mouse button held.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

Point = tuple[float, float]
Stroke = list[Point]


@dataclass
class Plan:
    subject: str
    strokes: list[Stroke]
    source: str = "vector"  # vector | reference
    fill_point: Point | None = None  # a point inside the main outline, for "add colour"
    attribution: dict = field(default_factory=dict)

    @property
    def points(self) -> int:
        return sum(len(s) for s in self.strokes)

    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for s in self.strokes for p in s] or [0.0]
        ys = [p[1] for s in self.strokes for p in s] or [0.0]
        return min(xs), min(ys), max(xs), max(ys)

    def placed(self, box: tuple[float, float, float, float]) -> Plan:
        """The plan scaled (aspect kept) and centred into ``box`` = (left, top, right, bottom), all 0–1."""
        x0, y0, x1, y1 = self.bbox()
        width, height = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        bl, bt, br, bb = box
        scale = min((br - bl) / width, (bb - bt) / height)
        ox = bl + ((br - bl) - width * scale) / 2 - x0 * scale
        oy = bt + ((bb - bt) - height * scale) / 2 - y0 * scale

        def move(p: Point) -> Point:
            return (ox + p[0] * scale, oy + p[1] * scale)

        fill = move(self.fill_point) if self.fill_point else None
        return Plan(self.subject, [[move(p) for p in s] for s in self.strokes], self.source, fill, self.attribution)


# ---------------------------------------------------------------------------------------------- vector shapes


def _ellipse(cx: float, cy: float, rx: float, ry: float, n: int = 48, start: float = 0.0,
             end: float = 2 * math.pi) -> Stroke:
    return [(cx + rx * math.cos(start + (end - start) * i / n), cy + ry * math.sin(start + (end - start) * i / n))
            for i in range(n + 1)]


def _polygon(points: list[Point]) -> Stroke:
    return [*points, points[0]]


def _star(cx: float, cy: float, r: float) -> Stroke:
    points = []
    for i in range(10):
        radius = r if i % 2 == 0 else r * 0.42
        angle = -math.pi / 2 + i * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return _polygon(points)


def _heart(cx: float, cy: float, s: float) -> Stroke:
    pts = []
    for i in range(61):
        t = 2 * math.pi * i / 60
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * s / 17, cy - y * s / 17))
    return pts


def vector_plan(subject: str) -> Plan | None:
    """A ready-made plan for simple subjects, or None."""
    s = subject.lower().strip()
    s = s.removeprefix("a ").removeprefix("an ").removeprefix("the ").strip()
    if s in ("circle", "round", "ring"):
        return Plan(s, [_ellipse(0.5, 0.5, 0.4, 0.4)], fill_point=(0.5, 0.5))
    if s in ("oval", "ellipse"):
        return Plan(s, [_ellipse(0.5, 0.5, 0.45, 0.3)], fill_point=(0.5, 0.5))
    if s in ("square", "box"):
        return Plan(s, [_polygon([(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)])], fill_point=(0.5, 0.5))
    if s in ("rectangle",):
        return Plan(s, [_polygon([(0.05, 0.25), (0.95, 0.25), (0.95, 0.75), (0.05, 0.75)])], fill_point=(0.5, 0.5))
    if s in ("triangle",):
        return Plan(s, [_polygon([(0.5, 0.08), (0.92, 0.88), (0.08, 0.88)])], fill_point=(0.5, 0.6))
    if s in ("line", "straight line"):
        return Plan(s, [[(0.05, 0.5), (0.95, 0.5)]])
    if s in ("star",):
        return Plan(s, [_star(0.5, 0.52, 0.45)], fill_point=(0.5, 0.52))
    if s in ("heart",):
        return Plan(s, [_heart(0.5, 0.45, 0.42)], fill_point=(0.5, 0.45))
    if s in ("house", "home", "simple house"):
        body = _polygon([(0.2, 0.45), (0.8, 0.45), (0.8, 0.95), (0.2, 0.95)])
        roof = [(0.12, 0.47), (0.5, 0.08), (0.88, 0.47)]
        door = [(0.43, 0.95), (0.43, 0.7), (0.57, 0.7), (0.57, 0.95)]
        window = _polygon([(0.27, 0.55), (0.38, 0.55), (0.38, 0.66), (0.27, 0.66)])
        return Plan(s, [body, roof, door, window], fill_point=(0.68, 0.8))
    if s in ("sun",):
        rays = []
        for i in range(12):
            a = 2 * math.pi * i / 12
            rays.append([(0.5 + 0.3 * math.cos(a), 0.5 + 0.3 * math.sin(a)),
                         (0.5 + 0.45 * math.cos(a), 0.5 + 0.45 * math.sin(a))])
        return Plan(s, [_ellipse(0.5, 0.5, 0.22, 0.22), *rays], fill_point=(0.5, 0.5))
    if s in ("smiley", "smiley face", "smile", "happy face", "face"):
        return Plan(s, [_ellipse(0.5, 0.5, 0.42, 0.42), _ellipse(0.36, 0.38, 0.05, 0.06, 16),
                        _ellipse(0.64, 0.38, 0.05, 0.06, 16),
                        _ellipse(0.5, 0.52, 0.25, 0.2, 24, 0.15 * math.pi, 0.85 * math.pi)], fill_point=(0.5, 0.75))
    if s in ("tree", "simple tree"):
        trunk = _polygon([(0.44, 0.95), (0.44, 0.62), (0.56, 0.62), (0.56, 0.95)])
        crown = _ellipse(0.5, 0.38, 0.3, 0.28)
        return Plan(s, [crown, trunk], fill_point=(0.5, 0.38))
    if s in ("flower",):
        petals = [_ellipse(0.5 + 0.16 * math.cos(a), 0.35 + 0.16 * math.sin(a), 0.1, 0.1, 24)
                  for a in (2 * math.pi * i / 6 for i in range(6))]
        stem = [(0.5, 0.48), (0.5, 0.95)]
        leaf = _ellipse(0.58, 0.72, 0.08, 0.04, 20)
        return Plan(s, [_ellipse(0.5, 0.35, 0.07, 0.07, 20), *petals, stem, leaf], fill_point=(0.5, 0.35))
    if s in ("arrow", "right arrow"):
        return Plan(s, [[(0.08, 0.5), (0.88, 0.5)], [(0.68, 0.3), (0.9, 0.5), (0.68, 0.7)]])
    return None


# ---------------------------------------------------------------------------------------------- references


def _foreground(gray: np.ndarray) -> np.ndarray:
    """A binary mask of the subject: dark (or light) things on a plain background."""
    import cv2

    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    border = np.concatenate([mask[0, :], mask[-1, :], mask[:, 0], mask[:, -1]])
    if border.mean() < 127:  # background came out black: the subject is the white part already
        return mask
    return 255 - mask


def drawability(image) -> float:
    """0–1: how well an image traces into a clean drawing (plain background, one clear subject, few parts)."""
    import cv2

    rgb = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = _foreground(gray)
    fraction = float((mask > 0).mean())
    if not 0.03 < fraction < 0.7:
        return 0.0
    border = np.concatenate([mask[0, :], mask[-1, :], mask[:, 0], mask[:, -1]])
    border_penalty = float((border > 0).mean())  # photos fill the frame; a clean subject sits inside it
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    big = [c for c in contours if cv2.contourArea(c) > 0.002 * mask.size]
    if not big:
        return 0.0
    main = max(big, key=cv2.contourArea)
    dominance = cv2.contourArea(main) / max(1.0, sum(cv2.contourArea(c) for c in big))
    spread = float(np.std(gray[gray < 250])) if (gray < 250).any() else 0.0
    background = float((gray > 235).mean())  # plain white background
    colourful = float(np.std(rgb.astype(np.float32), axis=2).mean()) / 60.0
    score = (0.35 * dominance + 0.3 * background + 0.2 * (1 - border_penalty) + 0.15 * min(1.0, spread / 40)
             - 0.2 * min(1.0, colourful) - 0.05 * max(0, len(big) - 12))
    return max(0.0, min(1.0, score))


def reference_plan(image, subject: str, *, detail: float = 1.0, max_points: int = 1400) -> Plan:
    """Trace a reference image into strokes: the outline plus the main inner lines (``detail`` 0.5–1.5)."""
    import cv2

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    scale = 600 / max(height, width)
    if scale < 1:
        rgb = cv2.resize(rgb, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
        height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = _foreground(gray)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    minimum = 0.0006 * width * height / max(detail, 0.3)
    kept = [c for c in contours if cv2.contourArea(c) > minimum or cv2.arcLength(c, True) > 0.15 * (width + height)]
    kept.sort(key=lambda c: -cv2.arcLength(c, True))
    kept = kept[: int(24 * detail) + 1]
    strokes: list[Stroke] = []
    for contour in kept:
        epsilon = 0.0035 / max(detail, 0.3) * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
        if len(approx) < 3:
            continue
        stroke = [(float(x) / width, float(y) / height) for x, y in approx]
        stroke.append(stroke[0])
        strokes.append(stroke)
    total = sum(len(s) for s in strokes)
    while total > max_points and len(strokes) > 1:
        strokes.pop()  # drop the smallest details first (sorted longest first)
        total = sum(len(s) for s in strokes)
    fill = None
    if kept:
        main = kept[0]
        moments = cv2.moments(main)
        if moments["m00"]:
            cx, cy = moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]
            if cv2.pointPolygonTest(main, (cx, cy), False) > 0:
                fill = (cx / width, cy / height)
    plan = Plan(subject, strokes, "reference", fill)
    # keep the subject's proportions: normalise so the larger side spans 0–1
    aspect = width / height
    if aspect > 1:
        plan = Plan(subject, [[(x, y / aspect + (1 - 1 / aspect) / 2) for x, y in s] for s in strokes], "reference",
                    (fill[0], fill[1] / aspect + (1 - 1 / aspect) / 2) if fill else None)
    elif aspect < 1:
        plan = Plan(subject, [[(x * aspect + (1 - aspect) / 2, y) for x, y in s] for s in strokes], "reference",
                    (fill[0] * aspect + (1 - aspect) / 2, fill[1]) if fill else None)
    return plan


def densify(stroke: list[tuple[int, int]], step: float = 5.0) -> list[tuple[int, int]]:
    """Insert points so consecutive points are at most ``step`` pixels apart (smooth mouse drags)."""
    if len(stroke) < 2:
        return list(stroke)
    out = [stroke[0]]
    for (x0, y0), (x1, y1) in zip(stroke, stroke[1:], strict=False):
        distance = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(distance / step))
        for i in range(1, steps + 1):
            out.append((round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps)))
    return out
