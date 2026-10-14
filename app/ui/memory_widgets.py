"""Виджеты страницы «Память»: кольцевой индикатор, состав памяти, график истории.

Цвета серий — категориальная палитра из treemap в фиксированном порядке
(проверена валидатором на CVD для светлой и тёмной темы): «используется» — синий,
«изменённая» — оранжевый, «ожидание (кэш)» — бирюзовый, «свободно» — нейтральный.
Подписи и значения — цветом текста; каждая серия подписана (не только цвет).
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from app.ui import theme
from app.ui.treemap import OTHER_DARK, OTHER_LIGHT, PALETTE_DARK, PALETTE_LIGHT
from app.utils.format_size import format_size
from app.utils.i18n import tr


def series_colors() -> list[QColor]:
    pal = PALETTE_DARK if theme.is_dark() else PALETTE_LIGHT
    return [QColor(pal[0]), QColor(pal[1]), QColor(pal[2])]


def neutral_color() -> QColor:
    return QColor(OTHER_DARK if theme.is_dark() else OTHER_LIGHT)


def status_color(level: int) -> QColor:
    c = theme.colors()
    return QColor({0: c["success"], 1: c["success"], 2: c["warning"], 3: c["warning"]}.get(level, c["danger"]))


class RingGauge(QWidget):
    """Большой индикатор: доля памяти «используется» (без кэша)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(190, 190))
        self._value = 0.0
        self._caption = ""
        self._sub = ""

    def set_value(self, percent: float, caption: str, sub: str = "") -> None:
        self._value = max(0.0, min(100.0, percent))
        self._caption = caption
        self._sub = sub
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        c = theme.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        thickness = 14
        rect = QRectF(thickness, thickness, self.width() - 2 * thickness, self.height() - 2 * thickness)
        pen = QPen(QColor(c["track"]), thickness, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 90 * 16, -360 * 16)
        if self._value > 0:
            pen.setColor(series_colors()[0])
            p.setPen(pen)
            p.drawArc(rect, 90 * 16, int(-360 * 16 * self._value / 100))
        p.setPen(QColor(c["text"]))
        f = QFont(self.font())
        f.setPointSizeF(26)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRectF(0, self.height() / 2 - 34, self.width(), 40), Qt.AlignmentFlag.AlignCenter,
                   f"{self._value:.0f}%")
        f.setPointSizeF(9.5)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QColor(c["muted"]))
        p.drawText(QRectF(10, self.height() / 2 + 6, self.width() - 20, 18), Qt.AlignmentFlag.AlignCenter,
                   self._caption)
        if self._sub:
            p.drawText(QRectF(10, self.height() / 2 + 24, self.width() - 20, 18), Qt.AlignmentFlag.AlignCenter,
                       self._sub)
        p.end()


class CompositionBar(QWidget):
    """Состав физической памяти: сегменты с зазором 2 px и легенда со значениями."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(76)  # полоса + до двух строк легенды
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._parts: list[tuple[str, int, QColor, str]] = []  # (подпись, байты, цвет, подсказка)
        self._total = 0
        self.setMouseTracking(True)

    def set_parts(self, parts: list[tuple[str, int, QColor, str]], total: int) -> None:
        self._parts = [p for p in parts if p[1] > 0]
        self._total = total
        self._fit_height()
        self.update()

    def _legend_items(self) -> list[tuple[str, float]]:
        f = QFont(self.font())
        f.setPointSizeF(9.5)
        fm = QFontMetrics(f)
        return [(f"{label}  {format_size(value, 1)}", 14 + fm.horizontalAdvance(f"{label}  {format_size(value, 1)}") + 22)
                for label, value, _c, _t in self._parts]

    def _legend_rows(self, width: float) -> int:
        rows, x = 1, 0.0
        for _text, item_w in self._legend_items():
            if x + item_w > width and x > 0:
                rows += 1
                x = 0.0
            x += item_w
        return rows

    def _fit_height(self) -> None:
        """Высота = полоса + столько строк легенды, сколько нужно при текущей ширине."""
        h = 30 + 20 * self._legend_rows(max(1, self.width())) + 4
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit_height()

    def _segments(self) -> list[tuple[QRectF, int]]:
        segs = []
        if not self._total:
            return segs
        x, w, gap = 0.0, float(self.width()), 2.0
        usable = w - gap * max(0, len(self._parts) - 1)
        for i, (_label, value, _c, _tip) in enumerate(self._parts):
            seg_w = max(2.0, usable * value / self._total)
            segs.append((QRectF(x, 0, seg_w, 16), i))
            x += seg_w + gap
        return segs

    def paintEvent(self, _e: QPaintEvent) -> None:
        c = theme.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for rect, i in self._segments():
            path = QPainterPath()
            path.addRoundedRect(rect, 4, 4)
            p.fillPath(path, self._parts[i][2])
        # легенда: квадрат цвета + подпись + значение (текст — цветом текста)
        x = 0.0
        y = 30.0
        f = QFont(self.font())
        f.setPointSizeF(9.5)
        p.setFont(f)
        for (label, value, color, _tip), (text, item_w) in zip(self._parts, self._legend_items()):
            if x + item_w > self.width() and x > 0:
                x = 0.0
                y += 20
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, y + 3, 10, 10), 2, 2)
            p.fillPath(path, color)
            p.setPen(QColor(c["text"]))
            p.drawText(QPointF(x + 16, y + 12), text)
            x += item_w
        p.end()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        for rect, i in self._segments():
            if rect.contains(e.position()):
                label, value, _c, tip = self._parts[i]
                pct = 100 * value / self._total if self._total else 0
                QToolTip.showText(e.globalPosition().toPoint(),
                                  f"<b>{label}</b>: {format_size(value)} ({pct:.0f}%)<br>{tip}", self)
                return
        QToolTip.hideText()


class HistoryChart(QWidget):
    """Линии «используется %» и «выделено (commit) %» на одной шкале 0–100%.

    Наведение — перекрестие и подсказка со значениями; скачки — отметки сверху.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._t: list[float] = []
        self._series: list[tuple[str, list[float]]] = []
        self._spikes: list[float] = []
        self._window = 1800.0
        self._hover_x: float | None = None

    def set_window(self, seconds: float) -> None:
        self._window = seconds
        self.update()

    def set_data(self, times: list[float], series: list[tuple[str, list[float]]], spikes: list[float]) -> None:
        self._t = times
        self._series = series
        self._spikes = spikes
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(40, 26, max(10, self.width() - 52), max(10, self.height() - 50))

    def _x(self, t: float, now: float, r: QRectF) -> float:
        return r.left() + r.width() * (1 - (now - t) / self._window)

    def paintEvent(self, _e: QPaintEvent) -> None:
        c = theme.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._plot_rect()
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        # сетка и подписи оси Y — приглушённые
        grid = QColor(c["border"])
        for pct in (0, 25, 50, 75, 100):
            y = r.bottom() - r.height() * pct / 100
            p.setPen(QPen(grid, 1, Qt.PenStyle.SolidLine if pct == 0 else Qt.PenStyle.DotLine))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(c["muted"]))
            p.drawText(QRectF(0, y - 8, r.left() - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{pct}%")
        # подписи оси X
        minutes = int(self._window / 60)
        for frac, label in ((0.0, tr("mem.chart.ago", m=minutes)), (0.5, tr("mem.chart.ago", m=minutes // 2)),
                            (1.0, tr("mem.chart.now"))):
            x = r.left() + r.width() * frac
            align = Qt.AlignmentFlag.AlignLeft if frac == 0 else (
                Qt.AlignmentFlag.AlignRight if frac == 1 else Qt.AlignmentFlag.AlignHCenter)
            box = QRectF(x - (0 if frac == 0 else (120 if frac == 1 else 60)), r.bottom() + 4, 120, 16)
            p.drawText(box, align, label)

        colors = series_colors()
        now = self._t[-1] if self._t else time.time()
        # легенда сверху (всегда при 2 сериях)
        lx = r.left()
        for i, (name, values) in enumerate(self._series):
            path = QPainterPath()
            path.addRoundedRect(QRectF(lx, 8, 10, 10), 2, 2)
            p.fillPath(path, colors[i])
            p.setPen(QColor(c["text"]))
            last = next((v for v in reversed(values) if not math.isnan(v)), None)
            text = f"{name}" + (f"  {last:.0f}%" if last is not None else "")
            p.drawText(QPointF(lx + 15, 17), text)
            lx += 15 + p.fontMetrics().horizontalAdvance(text) + 24

        p.save()
        p.setClipRect(r.adjusted(-2, -2, 2, 2))
        for i, (_name, values) in enumerate(self._series):
            path = QPainterPath()
            started = False
            for t, v in zip(self._t, values):
                if now - t > self._window or math.isnan(v):
                    started = False
                    continue
                pt = QPointF(self._x(t, now, r), r.bottom() - r.height() * max(0, min(100, v)) / 100)
                if started:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    started = True
            p.setPen(QPen(colors[i], 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
        # скачки
        warn = QColor(c["warning"])
        for t in self._spikes:
            if now - t <= self._window:
                x = self._x(t, now, r)
                p.setPen(QPen(warn, 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                tri = QPainterPath()
                tri.moveTo(x - 5, r.top())
                tri.lineTo(x + 5, r.top())
                tri.lineTo(x, r.top() + 7)
                tri.closeSubpath()
                p.fillPath(tri, warn)
        p.restore()

        # перекрестие при наведении
        if self._hover_x is not None and self._t:
            idx = self._nearest(self._hover_x, now, r)
            if idx is not None:
                x = self._x(self._t[idx], now, r)
                p.setPen(QPen(QColor(c["muted"]), 1))
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                for i, (_n, values) in enumerate(self._series):
                    v = values[idx]
                    if math.isnan(v):
                        continue
                    y = r.bottom() - r.height() * v / 100
                    p.setBrush(colors[i])
                    p.setPen(QPen(QColor(c["surface"]), 2))
                    p.drawEllipse(QPointF(x, y), 4.5, 4.5)
        if not self._t:
            p.setPen(QColor(c["muted"]))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, tr("mem.chart.collecting"))
        p.end()

    def _nearest(self, x: float, now: float, r: QRectF) -> int | None:
        best, best_d = None, 1e9
        for i, t in enumerate(self._t):
            if now - t > self._window:
                continue
            d = abs(self._x(t, now, r) - x)
            if d < best_d:
                best, best_d = i, d
        return best if best_d < 30 else None

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        self._hover_x = e.position().x()
        self.update()
        r = self._plot_rect()
        now = self._t[-1] if self._t else 0
        idx = self._nearest(self._hover_x, now, r) if self._t else None
        if idx is None:
            QToolTip.hideText()
            return
        when = time.strftime("%H:%M:%S", time.localtime(self._t[idx]))
        rows = [f"<b>{when}</b>"]
        for name, values in self._series:
            v = values[idx]
            rows.append(f"{name}: {'—' if math.isnan(v) else f'{v:.1f}%'}")
        QToolTip.showText(e.globalPosition().toPoint(), "<br>".join(rows), self)

    def leaveEvent(self, _e) -> None:
        self._hover_x = None
        self.update()
