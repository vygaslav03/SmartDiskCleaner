"""Страница «Карантин»: просмотр, восстановление и окончательное удаление очищенных файлов."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.quarantine import QSession, RestoreReport, get_quarantine
from app.models.file_item import FileItem
from app.utils.format_size import format_size, format_timestamp
from app.utils.i18n import tr
from app.utils.settings_store import Settings
from app.ui.cleaner import cat_name
from app.ui.widgets import FileListDialog, StatCard, page_header
from app.ui.workers import Task

ROLE_SESSION = Qt.ItemDataRole.UserRole


class QuarantinePage(QWidget):
    space_freed = Signal(int)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._task: Task | None = None
        self._sessions: list[QSession] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, self.subtitle = page_header(tr("q.title"), "")
        root.addWidget(header)

        cards = QGridLayout()
        cards.setHorizontalSpacing(16)
        self.size_card = StatCard(tr("q.total_size"))
        self.files_card = StatCard(tr("q.total_files"))
        self.sessions_card = StatCard(tr("q.total_sessions"))
        cards.addWidget(self.size_card, 0, 0)
        cards.addWidget(self.files_card, 0, 1)
        cards.addWidget(self.sessions_card, 0, 2)
        root.addLayout(cards)

        self.status = QLabel("")
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setHeaderLabels(
            [tr("q.col.date"), tr("q.col.files"), tr("q.col.size"), tr("q.col.expires"), tr("q.col.categories")]
        )
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate((150, 90, 110, 150)):
            self.tree.setColumnWidth(col, width)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.tree.itemDoubleClicked.connect(lambda *_: self._details())
        root.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.details_btn = QPushButton(tr("btn.details"))
        self.details_btn.clicked.connect(self._details)
        self.restore_btn = QPushButton(tr("q.restore"))
        self.restore_btn.setObjectName("Primary")
        self.restore_btn.clicked.connect(self._restore)
        self.delete_btn = QPushButton(tr("q.delete"))
        self.delete_btn.clicked.connect(self._delete_selected)
        self.delete_all_btn = QPushButton(tr("q.delete_all"))
        self.delete_all_btn.setObjectName("DangerButton")
        self.delete_all_btn.clicked.connect(self._delete_all)
        row.addWidget(self.details_btn)
        row.addWidget(self.restore_btn)
        row.addStretch(1)
        row.addWidget(self.delete_btn)
        row.addWidget(self.delete_all_btn)
        root.addLayout(row)
        self.refresh()

    # ------------------------------------------------------------------ API
    @property
    def busy(self) -> bool:
        return self._task is not None and self._task.running

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.refresh()

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.shutdown()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.busy:
            self.refresh()
        super().showEvent(event)

    def refresh(self) -> None:
        store = get_quarantine()
        self._sessions = store.sessions()
        days = self.settings.quarantine_days
        if self.settings.quarantine_enabled:
            self.subtitle.setText(tr("q.subtitle_on", days=days))
        else:
            self.subtitle.setText(tr("q.subtitle_off"))
        total = sum(s.size for s in self._sessions)
        self.size_card.set_value(format_size(total))
        self.files_card.set_value(f"{sum(s.count for s in self._sessions):,}".replace(",", " "))
        self.sessions_card.set_value(str(len(self._sessions)))
        self.status.setText(tr("q.space_note") if total else "")
        self.tree.clear()
        if not self._sessions:
            empty = QTreeWidgetItem([tr("q.empty")])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)
        for i, s in enumerate(self._sessions):
            cats = ", ".join(cat_name(c) for c in s.categories)
            item = QTreeWidgetItem(
                [
                    format_timestamp(s.created),
                    str(s.count),
                    format_size(s.size),
                    format_timestamp(get_quarantine().expires_at(s, days)),
                    cats,
                ]
            )
            item.setData(0, ROLE_SESSION, i)
            item.setToolTip(4, cats)
            for col in (1, 2):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tree.addTopLevelItem(item)
        self._update_buttons()

    # -------------------------------------------------------------- actions
    def _current(self) -> QSession | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        idx = item.data(0, ROLE_SESSION)
        if idx is None or not (0 <= idx < len(self._sessions)):
            return None
        return self._sessions[idx]

    def _details(self) -> None:
        s = self._current()
        if s is None:
            return
        items = [FileItem(e.original, e.size, e.mtime, e.category, virtual=True) for e in s.entries]
        FileListDialog(tr("q.details_title", date=format_timestamp(s.created)), items, tr("q.details_note"),
                       self).exec()

    def _restore(self) -> None:
        s = self._current()
        if s is None or self.busy:
            return
        answer = QMessageBox.question(
            self, tr("q.title"), tr("q.restore_confirm", count=s.count, size=format_size(s.size)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.status.setText(tr("q.restoring"))
        self._task = Task(self, get_quarantine().restore, s, on_finished=self._on_restored,
                          on_failed=self._on_failed)
        self._update_buttons()

    def _on_restored(self, report: RestoreReport) -> None:
        self._task = None
        self.refresh()
        text = tr("q.restored", count=report.restored, size=format_size(report.restored_bytes))
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information if not report.failed_count else QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("q.title"))
        if report.failed_count:
            text += "\n" + tr("q.restore_failed", count=report.failed_count)
            box.setDetailedText(
                "\n".join(f"[{tr('reason.' + r)}] {p}" for p, r in report.failed[:300])
            )
        box.setText(text)
        box.exec()

    def _on_failed(self, message: str) -> None:
        self._task = None
        self.refresh()
        QMessageBox.critical(self, tr("error.title"), tr("error.task_failed", error=message))

    def _delete_selected(self) -> None:
        s = self._current()
        if s is None or self.busy:
            return
        answer = QMessageBox.warning(
            self, tr("q.title"), tr("q.delete_confirm", count=s.count, size=format_size(s.size)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            freed = get_quarantine().delete(s)
            self.refresh()
            self.space_freed.emit(freed)

    def _delete_all(self) -> None:
        if not self._sessions or self.busy:
            return
        total = sum(s.size for s in self._sessions)
        answer = QMessageBox.warning(
            self, tr("q.title"), tr("q.delete_all_confirm", size=format_size(total)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            freed = sum(get_quarantine().delete(s) for s in list(self._sessions))
            self.refresh()
            self.space_freed.emit(freed)

    def _update_buttons(self) -> None:
        has = self._current() is not None and not self.busy
        self.details_btn.setEnabled(has)
        self.restore_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)
        self.delete_all_btn.setEnabled(bool(self._sessions) and not self.busy)
