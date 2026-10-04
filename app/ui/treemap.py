"""Treemap «Анализа диска»: прямоугольники, площадь которых пропорциональна размеру папки.

Только просмотр. Показывает содержимое одной папки (view): её подпапки и блок
«файлы в этой папке», внутри каждой подпапки — второй уровень вложенности.
Клик выделяет папку (и синхронизирует дерево), двойной клик — заходит внутрь.

Цвета — категориальная палитра в фиксированном порядке (8 оттенков, без циклов);
девятая и следующие папки — нейтральный «Прочее». Подписи — цветом текста, не серии.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from app.core.space_analyzer import DirNode
from app.core.treemap import Rect, squarify
from app.ui import theme
from app.utils.format_size import format_size
from app.utils.i18n import tr

# Категориальная палитра (порядок фиксирован): blue, orange, aqua, yellow, magenta, green, violet, red
PALETTE_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER_LIGHT, OTHER_DARK = "#a3a8b1", "#5b606a"
FILES_LIGHT, FILES_DARK = "#c9cdd4", "#464a52"

KEY_FILES = "__files__"
KEY_MORE = "__more__"
MAX_ITEMS = 250  # больше прямоугольников на экране не читаются — остальное одним блоком
MAX_INNER = 60
GAP = 2.0
LABEL_MIN_W, LABEL_MIN_H = 54, 30


def _luminance(c: QColor) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())


def _text_on(fill: QColor) -> QColor:
    """Тёмный или светлый текст — что контрастнее на этой заливке."""
    lum = _luminance(fill)
    return QColor("#ffffff") if (1.05 / (lum + 0.05)) >= ((lum + 0.05) / 0.05) else QColor("#15171c")


class TreemapWidget(QWidget):
    node_selected = Signal(object)  # DirNode — одиночный клик
    node_activated = Signal(object)  # DirNode — двойной клик (зайти внутрь)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.view: DirNode | None = None
        self.selected: DirNode | None = None
        self._hover: object | None = None
        self._rects: list[Rect] = []
        self._inner: dict[int, list[Rect]] = {}
        self._index: dict[int, int] = {}  # id(DirNode) -> номер цвета в палитре
        self._more_count = 0
        self._more_size = 0
        self._layout_size = (0, 0)

    # ------------------------------------------------------------------ API
    def set_view(self, node: DirNode | None) -> None:
        if node is self.view:
            return
        self.view = node
        self.selected = None
        self._hover = None
        self._relayout()
        self.update()

    def set_selected(self, node: DirNode | None) -> None:
        self.selected = node
        self.update()

    def clear(self) -> None:
        self.set_view(None)

    def visible_keys(self) -> list[object]:
        return [r.key for r in self._rects]

    def rect_for(self, key: object) -> Rect | None:
        for r in self._rects:
            if r.key is key:
                return r
        return None

    # -------------------------------------------------------------- layout
    def _items_for(self, node: DirNode, limit: int) -> tuple[list[tuple[object, float]], int, int]:
        dirs = [d for d in node.sorted_children() if d.size > 0]
        items: list[tuple[object, float]] = [(d, float(d.size)) for d in dirs[:limit]]
        rest = dirs[limit:]
        more_size = sum(d.size for d in rest)
        if more_size > 0:
            items.append((KEY_MORE, float(more_size)))
        if node.own_size > 0:
            items.append((KEY_FILES, float(node.own_size)))
        return items, len(rest), more_size

    def _relayout(self) -> None:
        self._rects, self._inner, self._index = [], {}, {}
        self._layout_size = (self.width(), self.height())
        node = self.view
        if node is None or node.size <= 0:
            return
        items, self._more_count, self._more_size = self._items_for(node, MAX_ITEMS)
        self._rects = squarify(items, 0, 0, float(self.width()), float(self.height()))
        # цвет закреплён за папкой (порядок по размеру внутри текущей view), не за позицией на экране
        for i, d in enumerate(d for d in node.sorted_children() if d.size > 0):
            if i >= len(PALETTE_LIGHT):
                break
            self._index[id(d)] = i
        fm = QFontMetrics(self._label_font())
        band = fm.height() + 6
        for r in self._rects:
            if not isinstance(r.key, DirNode) or not r.key.children:
                continue
            ix, iy, iw, ih = r.x + GAP, r.y + GAP, r.w - 2 * GAP, r.h - 2 * GAP
            if iw >= LABEL_MIN_W and ih >= LABEL_MIN_H + band:
                iy += band
                ih -= band
            if iw < 12 or ih < 12:
                continue
            sub, _, _ = self._items_for(r.key, MAX_INNER)
            self._inner[id(r.key)] = squarify(sub, ix, iy, iw, ih)

    def _label_font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(7.5, f.pointSizeF() * 0.9))
        return f

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    # --------------------------------------------------------------- paint
    def _fill(self, key: object) -> QColor:
        dark = theme.is_dark()
        if key == KEY_FILES:
            return QColor(FILES_DARK if dark else FILES_LIGHT)
        if key == KEY_MORE:
            return QColor(OTHER_DARK if dark else OTHER_LIGHT)
        i = self._index.get(id(key))
        if i is None:
            return QColor(OTHER_DARK if dark else OTHER_LIGHT)
        return QColor((PALETTE_DARK if dark else PALETTE_LIGHT)[i])

    def _label(self, key: object) -> str:
        if key == KEY_FILES:
            return tr("tm.files_here")
        if key == KEY_MORE:
            return tr("tm.more", count=self._more_count)
        return key.name if isinstance(key, DirNode) else str(key)

    def _size_of(self, key: object) -> int:
        if key == KEY_FILES:
            return self.view.own_size if self.view else 0
        if key == KEY_MORE:
            return self._more_size
        return key.size if isinstance(key, DirNode) else 0

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._layout_size != (self.width(), self.height()):
            self._relayout()
        c = theme.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(c["surface"]))
        if not self._rects:
            p.setPen(QColor(c["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("tm.empty"))
            p.end()
            return
        surface = QColor(c["surface"])
        font = self._label_font()
        p.setFont(font)
        fm = QFontMetrics(font)
        for r in self._rects:
            fill = self._fill(r.key)
            box = QRectF(r.x + GAP / 2, r.y + GAP / 2, r.w - GAP, r.h - GAP)
            if box.width() <= 0 or box.height() <= 0:
                continue
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(box, 3, 3)
            inner = self._inner.get(id(r.key)) if isinstance(r.key, DirNode) else None
            if inner:
                # второй уровень: та же заливка, чуть светлее, с тонкими зазорами цвета родителя
                sub_fill = QColor(fill).lighter(118) if theme.is_dark() else QColor(fill).lighter(112)
                p.setBrush(sub_fill)
                for s in inner:
                    sb = QRectF(s.x + 0.5, s.y + 0.5, s.w - 1, s.h - 1)
                    if sb.width() >= 1 and sb.height() >= 1:
                        p.drawRect(sb)
            if box.width() >= LABEL_MIN_W and box.height() >= LABEL_MIN_H:
                p.setPen(_text_on(fill))
                name = fm.elidedText(self._label(r.key), Qt.TextElideMode.ElideRight, int(box.width() - 10))
                size_txt = format_size(self._size_of(r.key))
                line = name
                if fm.horizontalAdvance(f"{name}  {size_txt}") <= box.width() - 10:
                    line = f"{name}  {size_txt}"
                p.drawText(QPointF(box.left() + 5, box.top() + fm.ascent() + 3), line)
            if r.key is self._hover or (self.selected is not None and r.key is self.selected):
                pen = QPen(QColor(c["text"]) if r.key is self.selected else surface)
                pen.setWidthF(2.0)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(box.adjusted(1, 1, -1, -1), 3, 3)
        p.end()

    # --------------------------------------------------------------- mouse
    def key_at(self, x: float, y: float) -> object | None:
        for r in self._rects:
            if r.contains(x, y):
                return r.key
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        key = self.key_at(pos.x(), pos.y())
        if key is not self._hover:
            self._hover = key
            self.update()
        if key is None or self.view is None:
            QToolTip.hideText()
            return
        size = self._size_of(key)
        share = (size / self.view.size * 100) if self.view.size else 0.0
        lines = [self._label(key), f"{format_size(size)} · {share:.1f}%"]
        if isinstance(key, DirNode):
            lines.append(tr("tm.tip_files", files=f"{key.files:,}".replace(",", " ")))
            if key.children:
                lines.append(tr("tm.tip_open"))
        QToolTip.showText(event.globalPosition().toPoint(), "\n".join(lines), self)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        key = self.key_at(event.position().x(), event.position().y())
        if isinstance(key, DirNode):
            self.selected = key
            self.update()
            self.node_selected.emit(key)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        key = self.key_at(event.position().x(), event.position().y())
        if isinstance(key, DirNode) and key.children:
            self.node_activated.emit(key)
