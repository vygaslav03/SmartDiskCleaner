"""Страница «Анализ диска»: дерево папок по размеру. Только просмотр — ничего не удаляет."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.space_analyzer import DirNode, SpaceAnalyzer, SpaceResult
from app.utils import winpaths
from app.utils.format_size import format_size, format_timestamp
from app.utils.i18n import tr
from app.ui import theme
from app.ui.treemap import TreemapWidget
from app.ui.widgets import Card, open_in_explorer, page_header
from app.ui.workers import Task

ROLE_NODE = Qt.ItemDataRole.UserRole
ROLE_PATH = Qt.ItemDataRole.UserRole + 1
ROLE_SHARE = Qt.ItemDataRole.UserRole + 2
ROLE_KIND = Qt.ItemDataRole.UserRole + 3
KIND_DIR, KIND_FILE, KIND_PLACEHOLDER, KIND_INFO = "dir", "file", "placeholder", "info"
COL_NAME, COL_SIZE, COL_SHARE, COL_FILES, COL_MODIFIED = range(5)
MAX_CHILD_DIRS = 500


def _num(n: int) -> str:
    """511212 -> '511 212'."""
    return f"{int(n):,}".replace(",", "\u00a0")


class ShareDelegate(QStyledItemDelegate):
    """Рисует полоску доли от размера родительской папки."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        share = index.data(ROLE_SHARE)
        if share is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        c = theme.colors()
        r = option.rect.adjusted(6, 7, -54, -7)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["track"]))
        painter.drawRoundedRect(r, 3, 3)
        width = max(0, min(r.width(), int(r.width() * float(share))))
        if width:
            painter.setBrush(QColor(c["accent"]))
            painter.drawRoundedRect(QRect(r.left(), r.top(), width, r.height()), 3, 3)
        painter.setPen(QColor(c["muted"]))
        text_rect = QRect(r.right() + 4, option.rect.top(), 48, option.rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         f"{float(share) * 100:.1f}%")
        painter.restore()


class AnalyzerPage(QWidget):
    find_large_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.result: SpaceResult | None = None
        self._task: Task | None = None
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        header, _ = page_header(tr("an.title"), tr("an.subtitle"))
        root.addWidget(header)

        bar = QHBoxLayout()
        lbl = QLabel(tr("large.where"))
        lbl.setObjectName("Muted")
        self.root_combo = QComboBox()
        self.root_combo.setMinimumWidth(240)
        for d in winpaths.list_drives():
            self.root_combo.addItem(d, d)
        self.root_combo.addItem(tr("large.choose_folder"), "__browse__")
        self.root_combo.activated.connect(self._on_root_activated)
        self._last_root_index = 0
        self.scan_btn = QPushButton(tr("an.analyze"))
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton(tr("btn.cancel"))
        self.cancel_btn.clicked.connect(self._cancel)
        for w in (lbl, self.root_combo, self.scan_btn, self.cancel_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        root.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.status = QLabel(tr("an.hint"))
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        root.addWidget(self.progress)
        root.addWidget(self.status)

        summary = Card()
        srow = QHBoxLayout()
        self.total_label = QLabel("—")
        self.total_label.setObjectName("BigValue")
        self.details_label = QLabel("")
        self.details_label.setObjectName("Muted")
        self.details_label.setWordWrap(True)
        left = QVBoxLayout()
        t = QLabel(tr("an.total"))
        t.setObjectName("CardTitle")
        left.addWidget(t)
        left.addWidget(self.total_label)
        srow.addLayout(left)
        srow.addWidget(self.details_label, 1)
        summary.layout_.addLayout(srow)
        root.addWidget(summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [tr("col.name"), tr("col.size"), tr("an.share"), tr("col.files"), tr("col.modified")]
        )
        self.tree.setUniformRowHeights(True)
        self.tree.setItemDelegateForColumn(COL_SHARE, ShareDelegate(self.tree))
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col, width in ((COL_SIZE, 110), (COL_SHARE, 190), (COL_FILES, 90), (COL_MODIFIED, 140)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.tree.setColumnWidth(col, width)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self._open(item))
        self.tree.currentItemChanged.connect(self._on_tree_current)

        # ---- treemap над деревом (в разделителе — пользователь сам выбирает пропорции)
        map_card = Card()
        mrow = QHBoxLayout()
        self.up_btn = QPushButton(tr("tm.up"))
        self.up_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.up_btn.clicked.connect(self._treemap_up)
        self.crumb = QLabel("")
        self.crumb.setObjectName("Muted")
        self.crumb.setMinimumWidth(10)
        hint = QLabel(tr("tm.hint"))
        hint.setObjectName("Muted")
        mrow.addWidget(self.up_btn)
        mrow.addWidget(self.crumb, 1)
        mrow.addWidget(hint)
        map_card.layout_.addLayout(mrow)
        self.treemap = TreemapWidget()
        self.treemap.node_selected.connect(self._on_map_selected)
        self.treemap.node_activated.connect(self._on_map_activated)
        map_card.layout_.addWidget(self.treemap, 1)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(map_card)
        self.splitter.addWidget(self.tree)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 300])
        root.addWidget(self.splitter, 1)

        actions = QHBoxLayout()
        self.open_btn = QPushButton(tr("btn.open_location"))
        self.open_btn.clicked.connect(lambda: self._open(self.tree.currentItem()))
        self.large_btn = QPushButton(tr("an.find_large_here"))
        self.large_btn.clicked.connect(self._find_large_here)
        note = QLabel(tr("an.readonly_note"))
        note.setObjectName("Muted")
        actions.addWidget(self.open_btn)
        actions.addWidget(self.large_btn)
        actions.addStretch(1)
        actions.addWidget(note)
        root.addLayout(actions)
        self._update_buttons()

    # ------------------------------------------------------------------ API
    @property
    def busy(self) -> bool:
        return self._task is not None and self._task.running

    def set_drive(self, drive: str) -> None:
        if self.busy:
            return
        idx = self.root_combo.findData(drive)
        if idx >= 0:
            self.root_combo.setCurrentIndex(idx)
            self._last_root_index = idx

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.shutdown()

    def start(self) -> None:
        if self.busy:
            return
        root = self._current_root()
        if not root:
            return
        self.tree.clear()
        self.treemap.clear()
        self._update_crumb()
        self.result = None
        self.total_label.setText("—")
        self.details_label.setText("")
        self.progress.setVisible(True)
        self.status.setText(tr("large.scanning_start"))
        self._task = Task(
            self,
            SpaceAnalyzer().analyze,
            root,
            on_progress=self._on_progress,
            on_finished=self._on_finished,
            on_failed=self._on_failed,
        )
        self._update_buttons()

    # -------------------------------------------------------------- private
    def _on_root_activated(self, index: int) -> None:
        if self.root_combo.itemData(index) != "__browse__":
            self._last_root_index = index
            return
        folder = QFileDialog.getExistingDirectory(self, tr("large.choose_folder"))
        if folder:
            folder = folder.replace("/", "\\") if winpaths.IS_WINDOWS else folder
            pos = self.root_combo.count() - 1
            self.root_combo.insertItem(pos, folder, folder)
            self.root_combo.setCurrentIndex(pos)
            self._last_root_index = pos
        else:
            self.root_combo.setCurrentIndex(self._last_root_index)

    def _current_root(self) -> str:
        data = self.root_combo.currentData()
        return data if data and data != "__browse__" else ""

    def _on_progress(self, p: dict) -> None:
        current = p.get("current", "")
        if len(current) > 90:
            current = "…" + current[-88:]
        self.status.setText(
            tr("an.scanning", dirs=_num(p.get("dirs", 0)), files=_num(p.get("files", 0)),
               size=format_size(p.get("bytes", 0)), current=current)
        )

    def _on_finished(self, result: SpaceResult) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.result = result
        root = result.root
        self.total_label.setText(format_size(root.size))
        details = tr("an.details", files=_num(root.files), dirs=_num(root.dirs), seconds=f"{result.duration:.1f}")
        if result.errors:
            details += "\n" + tr("an.errors", count=_num(result.errors))
        if result.cloud_only_files:
            details += "\n" + tr("an.cloud", count=result.cloud_only_files)
        self.details_label.setText(details)
        msg = tr("an.done")
        if result.cancelled:
            msg = tr("an.cancelled")
        self.status.setText(msg)

        top = self._make_dir_item(root, root.size)
        top.setText(COL_NAME, root.path)
        self.tree.addTopLevelItem(top)
        top.setExpanded(True)
        self.treemap.set_view(root)
        self._update_crumb()
        self._update_buttons()

    def _on_failed(self, message: str) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.status.setText(tr("error.task_failed", error=message))
        self._update_buttons()
        QMessageBox.critical(self, tr("error.title"), tr("error.task_failed", error=message))

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _make_dir_item(self, node: DirNode, parent_size: int) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.name, format_size(node.size), "", _num(node.files),
                                format_timestamp(node.mtime) if node.mtime else ""])
        item.setIcon(COL_NAME, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        item.setData(COL_NAME, ROLE_NODE, node)
        item.setData(COL_NAME, ROLE_PATH, node.path)
        item.setData(COL_NAME, ROLE_KIND, KIND_DIR)
        item.setData(COL_SHARE, ROLE_SHARE, (node.size / parent_size) if parent_size else 0.0)
        tip = node.path
        if node.access_denied:
            tip += "\n" + tr("an.denied_tip")
        item.setToolTip(COL_NAME, tip)
        self._align(item)
        if node.children or node.own_files:
            ph = QTreeWidgetItem([tr("clean.loading")])
            ph.setData(COL_NAME, ROLE_KIND, KIND_PLACEHOLDER)
            item.addChild(ph)
        return item

    @staticmethod
    def _align(item: QTreeWidgetItem) -> None:
        for col in (COL_SIZE, COL_FILES):
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        if item.childCount() != 1 or item.child(0).data(COL_NAME, ROLE_KIND) != KIND_PLACEHOLDER:
            return
        node: DirNode | None = item.data(COL_NAME, ROLE_NODE)
        if node is None:
            return
        item.takeChild(0)
        children: list[QTreeWidgetItem] = []
        dirs = node.sorted_children()
        for child in dirs[:MAX_CHILD_DIRS]:
            children.append(self._make_dir_item(child, node.size))
        if len(dirs) > MAX_CHILD_DIRS:
            rest = dirs[MAX_CHILD_DIRS:]
            info = QTreeWidgetItem([tr("an.more_dirs", count=len(rest)), format_size(sum(d.size for d in rest))])
            info.setData(COL_NAME, ROLE_KIND, KIND_INFO)
            self._align(info)
            children.append(info)
        file_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        top_files = node.top_files
        for f in top_files:
            fi = QTreeWidgetItem([f.name, format_size(f.size), "", "", format_timestamp(f.mtime)])
            fi.setIcon(COL_NAME, file_icon)
            fi.setData(COL_NAME, ROLE_PATH, f.path)
            fi.setData(COL_NAME, ROLE_KIND, KIND_FILE)
            fi.setData(COL_SHARE, ROLE_SHARE, (f.size / node.size) if node.size else 0.0)
            fi.setToolTip(COL_NAME, f.path)
            self._align(fi)
            children.append(fi)
        hidden = node.own_files - len(top_files)
        if hidden > 0:
            hidden_size = node.own_size - sum(f.size for f in top_files)
            info = QTreeWidgetItem([tr("an.more_files", count=hidden), format_size(hidden_size)])
            info.setData(COL_NAME, ROLE_KIND, KIND_INFO)
            self._align(info)
            children.append(info)
        item.addChildren(children)

    # ------------------------------------------------------------- treemap
    def _update_crumb(self) -> None:
        view = self.treemap.view
        text = view.path if view is not None else ""
        fm = self.crumb.fontMetrics()
        self.crumb.setText(fm.elidedText(text, Qt.TextElideMode.ElideMiddle, max(80, self.crumb.width() - 4)))
        self.crumb.setToolTip(text)
        self.up_btn.setEnabled(view is not None and view.parent is not None)

    def _treemap_up(self) -> None:
        view = self.treemap.view
        if view is None or view.parent is None:
            return
        self.treemap.set_view(view.parent)
        self.treemap.set_selected(view)
        self._update_crumb()
        self._select_in_tree(view)

    def _on_map_selected(self, node: DirNode) -> None:
        self._select_in_tree(node)

    def _on_map_activated(self, node: DirNode) -> None:
        self.treemap.set_view(node)
        self._update_crumb()
        self._select_in_tree(node)

    def _on_tree_current(self, item: QTreeWidgetItem | None, _prev=None) -> None:
        self._update_buttons()
        if self._syncing or item is None or item.data(COL_NAME, ROLE_KIND) != KIND_DIR:
            return
        node: DirNode | None = item.data(COL_NAME, ROLE_NODE)
        if node is None:
            return
        if node.children or node.parent is None:
            self.treemap.set_view(node)
        else:
            self.treemap.set_view(node.parent)
            self.treemap.set_selected(node)
        self._update_crumb()

    def _find_item(self, node: DirNode) -> QTreeWidgetItem | None:
        """Находит (раскрывая по пути) элемент дерева для папки."""
        chain: list[DirNode] = []
        n: DirNode | None = node
        while n is not None:
            chain.append(n)
            n = n.parent
        chain.reverse()
        if self.tree.topLevelItemCount() == 0:
            return None
        item = self.tree.topLevelItem(0)
        if item.data(COL_NAME, ROLE_PATH) != chain[0].path:
            return None
        for target in chain[1:]:
            item.setExpanded(True)  # подгружает детей лениво
            found = None
            for i in range(item.childCount()):
                child = item.child(i)
                if child.data(COL_NAME, ROLE_KIND) == KIND_DIR and child.data(COL_NAME, ROLE_PATH) == target.path:
                    found = child
                    break
            if found is None:
                return item  # за пределами MAX_CHILD_DIRS — ближайший предок
            item = found
        return item

    def _select_in_tree(self, node: DirNode) -> None:
        item = self._find_item(node)
        if item is None:
            return
        self._syncing = True
        try:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        finally:
            self._syncing = False
        self._update_buttons()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_crumb()

    def _open(self, item: QTreeWidgetItem | None) -> None:
        if item is None:
            return
        path = item.data(COL_NAME, ROLE_PATH)
        if path:
            open_in_explorer(path)

    def _find_large_here(self) -> None:
        item = self.tree.currentItem()
        if item is None or item.data(COL_NAME, ROLE_KIND) != KIND_DIR:
            return
        self.find_large_requested.emit(item.data(COL_NAME, ROLE_PATH))

    def _update_buttons(self) -> None:
        busy = self.busy
        item = self.tree.currentItem()
        has_path = bool(item is not None and item.data(COL_NAME, ROLE_PATH))
        is_dir = bool(item is not None and item.data(COL_NAME, ROLE_KIND) == KIND_DIR)
        self.scan_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.root_combo.setEnabled(not busy)
        self.open_btn.setEnabled(has_path)
        self.large_btn.setEnabled(is_dir and not busy)
