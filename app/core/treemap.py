"""Squarified treemap (Bruls, Huizing, van Wijk) — раскладка прямоугольников по размеру.

Чистый алгоритм без Qt: на вход (ключ, вес), на выход (ключ, x, y, w, h).
Площадь каждого прямоугольника пропорциональна весу, а пропорции сторон
стремятся к квадрату — так подписи читаются лучше, чем в «полосатой» раскладке.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rect:
    key: Any
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h


def _worst(row: list[float], side: float) -> float:
    """Худшее соотношение сторон в ряду, уложенном вдоль стороны side."""
    s = sum(row)
    if s <= 0 or side <= 0:
        return float("inf")
    rmax, rmin = max(row), min(row)
    side2, s2 = side * side, s * s
    return max(side2 * rmax / s2, s2 / (side2 * rmin))


def squarify(
    items: Sequence[tuple[Any, float]], x: float, y: float, w: float, h: float
) -> list[Rect]:
    """Раскладывает элементы с положительным весом в прямоугольник (x, y, w, h).

    Элементы с нулевым/отрицательным весом пропускаются. Порядок результата —
    по убыванию веса.
    """
    data = sorted(((k, float(v)) for k, v in items if v and v > 0), key=lambda kv: kv[1], reverse=True)
    if not data or w <= 0 or h <= 0:
        return []
    total = sum(v for _, v in data)
    scale = (w * h) / total
    areas = [(k, v * scale) for k, v in data]

    out: list[Rect] = []
    cx, cy, cw, ch = x, y, w, h
    i = 0
    while i < len(areas):
        side = min(cw, ch)
        row: list[tuple[Any, float]] = [areas[i]]
        i += 1
        while i < len(areas):
            cur = [a for _, a in row]
            if _worst(cur + [areas[i][1]], side) <= _worst(cur, side):
                row.append(areas[i])
                i += 1
            else:
                break
        row_area = sum(a for _, a in row)
        if cw >= ch:  # ряд — вертикальная полоса слева
            col_w = row_area / ch if ch else 0
            yy = cy
            for k, a in row:
                hh = a / col_w if col_w else 0
                out.append(Rect(k, cx, yy, col_w, hh))
                yy += hh
            cx += col_w
            cw -= col_w
        else:  # ряд — горизонтальная полоса сверху
            row_h = row_area / cw if cw else 0
            xx = cx
            for k, a in row:
                ww = a / row_h if row_h else 0
                out.append(Rect(k, xx, cy, ww, row_h))
                xx += ww
            cy += row_h
            ch -= row_h
    return out
