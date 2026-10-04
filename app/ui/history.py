"""Страница «История»: итоги прошлых очисток (без путей файлов)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.history import KIND_DUPLICATES, KIND_JUNK, KIND_LARGE, get_history, totals
from app.utils.format_size import format_size, format_timestamp
from app.utils.i18n import tr
from app.ui.cleaner import cat_name
from app.ui.widgets import StatCard, page_header

KIND_KEYS = {KIND_JUNK: "hist.kind.junk", KIND_LARGE: "hist.kind.large", KIND_DUPLICATES: "hist.kind.duplicates"}


class HistoryPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, _ = page_header(tr("hist.title"), tr("hist.subtitle"))
        root.addWidget(header)

        cards = QGridLayout()
        cards.setHorizontalSpacing(16)
        self.freed_card = StatCard(tr("hist.total_freed"))
        self.files_card = StatCard(tr("hist.total_files"))
        self.count_card = StatCard(tr("hist.total_runs"))
        cards.addWidget(self.freed_card, 0, 0)
        cards.addWidget(self.files_card, 0, 1)
        cards.addWidget(self.count_card, 0, 2)
        root.addLayout(cards)

        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setHeaderLabels(
            [tr("hist.col.date"), tr("hist.col.kind"), tr("hist.col.drive"), tr("hist.col.deleted"),
             tr("hist.col.freed"), tr("hist.col.failed"), tr("hist.col.details")]
        )
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate((140, 150, 120, 90, 110, 100)):
            self.tree.setColumnWidth(col, width)
        root.addWidget(self.tree, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.clear_btn = QPushButton(tr("hist.clear"))
        self.clear_btn.clicked.connect(self._clear)
        row.addWidget(self.clear_btn)
        root.addLayout(row)
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.refresh()
        super().showEvent(event)

    def refresh(self) -> None:
        entries = get_history().load()
        freed, files, runs = totals(entries)
        self.freed_card.set_value(format_size(freed))
        self.files_card.set_value(f"{files:,}".replace(",", " "))
        self.count_card.set_value(str(runs))
        self.tree.clear()
        if not entries:
            empty = QTreeWidgetItem([tr("hist.empty")])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)
            self.clear_btn.setEnabled(False)
            return
        self.clear_btn.setEnabled(True)
        items = []
        for e in entries:
            details = ", ".join(cat_name(c) for c in e.categories)
            if e.mode:
                details = tr(f"set.mode.{e.mode}") if not details else f"{details} • {tr(f'set.mode.{e.mode}')}"
            if e.cancelled:
                details = (details + " • " if details else "") + tr("hist.cancelled")
            item = QTreeWidgetItem(
                [
                    format_timestamp(e.timestamp),
                    tr(KIND_KEYS.get(e.kind, e.kind)),
                    e.drive.rstrip("\\") if len(e.drive) <= 3 else e.drive,
                    str(e.deleted),
                    format_size(e.freed),
                    str(e.failed) if e.failed else "—",
                    details,
                ]
            )
            item.setToolTip(6, details)
            item.setToolTip(2, e.drive)
            for col in (3, 4, 5):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            items.append(item)
        self.tree.addTopLevelItems(items)

    def _clear(self) -> None:
        answer = QMessageBox.question(
            self,
            tr("hist.title"),
            tr("hist.clear_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            get_history().clear()
            self.refresh()
