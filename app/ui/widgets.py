"""Общие виджеты: карточки, ленивая таблица файлов, диалоги подтверждения."""

from __future__ import annotations

import os
import subprocess
from typing import Any, Callable, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.models.file_item import FileItem
from app.utils.format_size import format_size, format_timestamp
from app.utils.i18n import tr
from app.utils.winpaths import IS_WINDOWS


# ------------------------------------------------------------------ layout
class Card(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(18, 16, 18, 16)
        self.layout_.setSpacing(8)


class StatCard(Card):
    def __init__(self, title: str, value: str = "—", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("CardValue")
        self.layout_.addWidget(self.title_label)
        self.layout_.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


def page_header(title: str, subtitle: str = "") -> tuple[QWidget, QLabel]:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 8)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    s.setWordWrap(True)
    lay.addWidget(t)
    lay.addWidget(s)
    return w, s


def open_in_explorer(path: str) -> None:
    """Показывает файл в Проводнике (выделяя его) или открывает папку."""
    try:
        if IS_WINDOWS and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])  # noqa: S603,S607
            return
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
    except OSError:
        pass


# --------------------------------------------------------- file table model
Column = tuple[str, Callable[[FileItem], Any], Callable[[FileItem], Any]]  # заголовок, текст, ключ сортировки


def default_columns() -> list[Column]:
    return [
        (tr("col.name"), lambda i: i.name, lambda i: i.name.lower()),
        (tr("col.size"), lambda i: format_size(i.size), lambda i: i.size),
        (tr("col.modified"), lambda i: format_timestamp(i.mtime), lambda i: i.mtime),
        (tr("col.path"), lambda i: i.path, lambda i: i.path.lower()),
    ]


class FileTableModel(QAbstractTableModel):
    """Модель списка файлов с ленивой подгрузкой (fetchMore) и опциональными чекбоксами.

    В представление добавляется по BATCH строк — десятки тысяч элементов не
    создают десятки тысяч строк в GUI сразу.
    """

    BATCH = 500
    checked_changed = Signal()

    def __init__(
        self,
        items: Sequence[FileItem] = (),
        columns: list[Column] | None = None,
        checkable: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items: list[FileItem] = list(items)
        self._columns = columns or default_columns()
        self._checkable = checkable
        self._checked: set[str] = set()
        self._loaded = min(self.BATCH, len(self._items))
        self._offset = 1 if checkable else 0

    # ----- данные
    def set_items(self, items: Sequence[FileItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._checked &= {i.path for i in self._items}
        self._loaded = min(self.BATCH, len(self._items))
        self.endResetModel()
        self.checked_changed.emit()

    def items(self) -> list[FileItem]:
        return self._items

    def item_at(self, row: int) -> FileItem | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def checked_items(self) -> list[FileItem]:
        return [i for i in self._items if i.path in self._checked]

    def set_checked_paths(self, paths) -> None:
        self._checked = set(paths) & {i.path for i in self._items}
        if self._loaded:
            self.dataChanged.emit(self.index(0, 0), self.index(self._loaded - 1, 0))
        self.checked_changed.emit()

    def set_all_checked(self, checked: bool) -> None:
        self._checked = {i.path for i in self._items} if checked else set()
        if self._loaded:
            self.dataChanged.emit(self.index(0, 0), self.index(self._loaded - 1, 0))
        self.checked_changed.emit()

    def remove_paths(self, paths: set[str]) -> None:
        self.set_items([i for i in self._items if i.path not in paths])

    # ----- Qt API
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else self._loaded

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._columns) + self._offset

    def canFetchMore(self, parent: QModelIndex) -> bool:
        return not parent.isValid() and self._loaded < len(self._items)

    def fetchMore(self, parent: QModelIndex) -> None:
        if parent.isValid():
            return
        remaining = len(self._items) - self._loaded
        n = min(self.BATCH, remaining)
        if n <= 0:
            return
        self.beginInsertRows(QModelIndex(), self._loaded, self._loaded + n - 1)
        self._loaded += n
        self.endInsertRows()

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if self._checkable and section == 0:
                return ""
            return self._columns[section - self._offset][0]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.item_at(index.row())
        if item is None:
            return None
        col = index.column()
        if self._checkable and col == 0:
            if role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if item.path in self._checked else Qt.CheckState.Unchecked
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._columns[col - self._offset][1](item)
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.path
        if role == Qt.ItemDataRole.TextAlignmentRole and self._columns[col - self._offset][0] == tr("col.size"):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if not (self._checkable and index.isValid() and index.column() == 0):
            return False
        if role != Qt.ItemDataRole.CheckStateRole:
            return False
        item = self.item_at(index.row())
        if item is None:
            return False
        state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
        if state == Qt.CheckState.Checked:
            self._checked.add(item.path)
        else:
            self._checked.discard(item.path)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.checked_changed.emit()
        return True

    def flags(self, index: QModelIndex):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self._checkable and index.column() == 0:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        if self._checkable and column == 0:
            return
        key = self._columns[column - self._offset][2]
        self.layoutAboutToBeChanged.emit()
        self._items.sort(key=key, reverse=order == Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()


def make_file_table(model: FileTableModel, sort_column: int | None = None) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setSortingEnabled(True)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(28)
    view.setWordWrap(False)
    header = view.horizontalHeader()
    header.setStretchLastSection(True)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setHighlightSections(False)
    view.setShowGrid(False)
    if sort_column is not None:
        view.sortByColumn(sort_column, Qt.SortOrder.DescendingOrder)
    return view


# ----------------------------------------------------------------- dialogs
class FileListDialog(QDialog):
    """«Подробнее»: полный список найденных элементов (с ленивой подгрузкой).

    checkable=True — пользователь может снять галочки с отдельных файлов, чтобы
    они НЕ удалялись. Результат — excluded_paths() после accept().
    """

    def __init__(
        self,
        title: str,
        items: Sequence[FileItem],
        note: str = "",
        parent=None,
        checkable: bool = False,
        excluded: set[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 620)
        self._items = list(items)
        self._checkable = checkable
        lay = QVBoxLayout(self)
        self.header = QLabel()
        self.header.setObjectName("Muted")
        lay.addWidget(self.header)
        if note:
            n = QLabel(note)
            n.setObjectName("Warning")
            n.setWordWrap(True)
            lay.addWidget(n)
        if checkable:
            hint = QLabel(tr("details.uncheck_hint"))
            hint.setObjectName("Muted")
            hint.setWordWrap(True)
            lay.addWidget(hint)
        self.model = FileTableModel(items, checkable=checkable, parent=self)
        off = 1 if checkable else 0
        self.view = make_file_table(self.model, sort_column=1 + off)
        if checkable:
            self.view.setColumnWidth(0, 32)
            excluded = excluded or set()
            self.model.set_checked_paths(i.path for i in self._items if i.path not in excluded)
            self.model.checked_changed.connect(self._update_header)
        self.view.setColumnWidth(0 + off, 260)
        self.view.setColumnWidth(1 + off, 100)
        self.view.setColumnWidth(2 + off, 140)
        self.view.doubleClicked.connect(self._open)
        lay.addWidget(self.view, 1)

        row = QHBoxLayout()
        open_btn = QPushButton(tr("btn.open_location"))
        open_btn.clicked.connect(lambda: self._open(self.view.currentIndex()))
        row.addWidget(open_btn)
        if checkable:
            all_btn = QPushButton(tr("btn.select_all"))
            all_btn.clicked.connect(lambda: self.model.set_all_checked(True))
            none_btn = QPushButton(tr("btn.deselect_all"))
            none_btn.clicked.connect(lambda: self.model.set_all_checked(False))
            row.addWidget(all_btn)
            row.addWidget(none_btn)
        row.addStretch(1)
        if checkable:
            cancel = QPushButton(tr("btn.cancel"))
            cancel.clicked.connect(self.reject)
            apply_btn = QPushButton(tr("btn.apply"))
            apply_btn.setObjectName("Primary")
            apply_btn.clicked.connect(self.accept)
            row.addWidget(cancel)
            row.addWidget(apply_btn)
        else:
            close = QPushButton(tr("btn.close"))
            close.clicked.connect(self.reject)
            row.addWidget(close)
        lay.addLayout(row)
        self.applied = False
        self._update_header()

    def accept(self) -> None:
        self.applied = True
        super().accept()

    def _update_header(self) -> None:
        total = len(self._items)
        total_size = sum(i.size for i in self._items)
        if not self._checkable:
            self.header.setText(tr("details.summary", count=total, size=format_size(total_size)))
            return
        chosen = self.model.checked_items()
        self.header.setText(
            tr(
                "details.selected",
                chosen=len(chosen),
                count=total,
                size=format_size(sum(i.size for i in chosen)),
            )
        )

    def excluded_paths(self) -> set[str]:
        if not self._checkable:
            return set()
        chosen = {i.path for i in self.model.checked_items()}
        return {i.path for i in self._items if i.path not in chosen}

    def _open(self, index) -> None:
        item = self.model.item_at(index.row()) if index.isValid() else None
        if item and not item.virtual:
            open_in_explorer(item.path)


class ConfirmDeleteDialog(QDialog):
    """Подтверждение удаления: итог, полный список и (опционально) обязательная галочка."""

    def __init__(
        self,
        title: str,
        summary: str,
        items: Sequence[FileItem] | None = None,
        rows: Sequence[tuple[str, str, str]] | None = None,
        warning: str = "",
        require_ack: bool = False,
        ok_text: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 560 if (items or rows) else 240)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        s = QLabel(summary)
        s.setTextFormat(Qt.TextFormat.RichText)
        s.setWordWrap(True)
        lay.addWidget(s)

        if rows:
            from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

            tree = QTreeWidget()
            tree.setRootIsDecorated(False)
            tree.setHeaderLabels([tr("col.category"), tr("col.files"), tr("col.size")])
            for name, count, size in rows:
                it = QTreeWidgetItem([name, count, size])
                it.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                it.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                tree.addTopLevelItem(it)
            tree.header().setStretchLastSection(False)
            tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            lay.addWidget(tree, 1)

        if items:
            model = FileTableModel(
                items,
                columns=[
                    (tr("col.size"), lambda i: format_size(i.size), lambda i: i.size),
                    (tr("col.path"), lambda i: i.path, lambda i: i.path.lower()),
                ],
                parent=self,
            )
            view = make_file_table(model, sort_column=0)
            view.setColumnWidth(0, 110)
            lay.addWidget(view, 1)

        if warning:
            w = QLabel(warning)
            w.setObjectName("Warning")
            w.setWordWrap(True)
            lay.addWidget(w)

        self.ack: QCheckBox | None = None
        if require_ack:
            self.ack = QCheckBox(tr("confirm.ack"))
            lay.addWidget(self.ack)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton(tr("btn.cancel"))
        cancel.clicked.connect(self.reject)
        self.ok = QPushButton(ok_text or tr("btn.delete"))
        self.ok.setObjectName("DangerButton")
        self.ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        lay.addLayout(row)
        cancel.setDefault(True)  # Enter не должен случайно запускать удаление
        cancel.setFocus()
        if self.ack is not None:
            self.ok.setEnabled(False)
            self.ack.toggled.connect(self.ok.setEnabled)
        self.confirmed = False

    def accept(self) -> None:
        if self.ack is not None and not self.ack.isChecked():
            return
        self.confirmed = True
        super().accept()

    def ask(self) -> bool:
        """Показывает диалог модально. True — пользователь явно подтвердил удаление."""
        self.exec()
        return self.confirmed
