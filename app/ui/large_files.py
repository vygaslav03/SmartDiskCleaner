"""Страница «Большие файлы»: поиск и (по отдельному подтверждению) удаление."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.history import KIND_LARGE, get_history, make_entry
from app.core.cleaner import MODE_RECYCLE, CleanReport, Cleaner
from app.core.disk_analyzer import LargeFileFinder, LargeFilesResult
from app.core.safety import SafetyPolicy
from app.utils import winpaths
from app.utils.format_size import format_size
from app.utils.i18n import tr
from app.utils.settings_store import Settings
from app.ui.cleaner import format_failures
from app.ui.widgets import (
    ConfirmDeleteDialog,
    FileTableModel,
    make_file_table,
    open_in_explorer,
    page_header,
)
from app.ui.workers import Task

THRESHOLDS_MB = (100, 500, 1024, 5 * 1024, 10 * 1024)


class LargeFilesPage(QWidget):
    files_deleted = Signal(object)  # CleanReport

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.result: LargeFilesResult | None = None
        self._task: Task | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        header, _ = page_header(tr("large.title"), tr("large.subtitle"))
        root.addWidget(header)

        bar = QHBoxLayout()
        self.root_combo = QComboBox()
        self.root_combo.setMinimumWidth(220)
        for d in winpaths.list_drives():
            self.root_combo.addItem(d, d)
        self.root_combo.addItem(tr("large.choose_folder"), "__browse__")
        self.root_combo.activated.connect(self._on_root_activated)
        self._last_root_index = 0

        self.threshold_combo = QComboBox()
        for mb in THRESHOLDS_MB:
            self.threshold_combo.addItem(f"> {format_size(mb * 1024 * 1024, 0)}", mb)
        self._select_threshold(settings.large_file_threshold_mb)

        self.scan_btn = QPushButton(tr("btn.find"))
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_btn = QPushButton(tr("btn.cancel"))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)

        lbl_where = QLabel(tr("large.where"))
        lbl_where.setObjectName("Muted")
        lbl_size = QLabel(tr("large.min_size"))
        lbl_size.setObjectName("Muted")
        for w in (lbl_where, self.root_combo, lbl_size, self.threshold_combo, self.scan_btn, self.cancel_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        root.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.status = QLabel(tr("large.hint"))
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        root.addWidget(self.progress)
        root.addWidget(self.status)

        self.model = FileTableModel(checkable=True, parent=self)
        self.model.checked_changed.connect(self._update_buttons)
        self.view = make_file_table(self.model, sort_column=2)
        self.view.setColumnWidth(0, 32)
        self.view.setColumnWidth(1, 260)
        self.view.setColumnWidth(2, 100)
        self.view.setColumnWidth(3, 140)
        self.view.doubleClicked.connect(self._open_current)
        root.addWidget(self.view, 1)

        actions = QHBoxLayout()
        self.open_btn = QPushButton(tr("btn.open_location"))
        self.open_btn.clicked.connect(lambda: self._open_current(self.view.currentIndex()))
        self.uncheck_btn = QPushButton(tr("btn.deselect_all"))
        self.uncheck_btn.clicked.connect(lambda: self.model.set_all_checked(False))
        self.selected_label = QLabel("")
        self.selected_label.setObjectName("Muted")
        self.delete_btn = QPushButton(tr("btn.delete_selected"))
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self.delete_selected)
        actions.addWidget(self.open_btn)
        actions.addWidget(self.uncheck_btn)
        actions.addStretch(1)
        actions.addWidget(self.selected_label)
        actions.addWidget(self.delete_btn)
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

    def set_root(self, path: str) -> None:
        """Выбрать папку для поиска (например, из анализатора диска)."""
        if self.busy or not path:
            return
        idx = self.root_combo.findData(path)
        if idx < 0:
            idx = self.root_combo.count() - 1  # перед пунктом «Выбрать папку…»
            self.root_combo.insertItem(idx, path, path)
        self.root_combo.setCurrentIndex(idx)
        self._last_root_index = idx

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.shutdown()

    # -------------------------------------------------------------- private
    def _select_threshold(self, mb: int) -> None:
        idx = self.threshold_combo.findData(mb)
        if idx < 0:
            self.threshold_combo.addItem(f"> {format_size(mb * 1024 * 1024, 0)}", mb)
            idx = self.threshold_combo.count() - 1
        self.threshold_combo.setCurrentIndex(idx)

    def _on_root_activated(self, index: int) -> None:
        if self.root_combo.itemData(index) != "__browse__":
            self._last_root_index = index
            return
        folder = QFileDialog.getExistingDirectory(self, tr("large.choose_folder"))
        if folder:
            folder = winpaths.strip_long_prefix(folder.replace("/", "\\") if winpaths.IS_WINDOWS else folder)
            pos = self.root_combo.count() - 1
            self.root_combo.insertItem(pos, folder, folder)
            self.root_combo.setCurrentIndex(pos)
            self._last_root_index = pos
        else:
            self.root_combo.setCurrentIndex(self._last_root_index)

    def _current_root(self) -> str:
        data = self.root_combo.currentData()
        return data if data and data != "__browse__" else ""

    def start_scan(self) -> None:
        if self.busy:
            return
        root = self._current_root()
        if not root:
            return
        policy = SafetyPolicy.for_user_files(self.settings.allow_program_files, self.settings.exclusions)
        if policy.validate_rule_root(root):
            QMessageBox.warning(self, tr("large.title"), tr("large.root_protected"))
            return
        min_size = int(self.threshold_combo.currentData()) * 1024 * 1024
        finder = LargeFileFinder(policy)
        self.model.set_items([])
        self.result = None
        self.progress.setVisible(True)
        self.status.setText(tr("large.scanning_start"))
        self._task = Task(
            self,
            finder.find,
            root,
            min_size,
            self.settings.large_files_max_results,
            on_progress=self._on_progress,
            on_finished=self._on_finished,
            on_failed=self._on_failed,
        )
        self._update_buttons()

    def _on_progress(self, p: dict) -> None:
        current = p.get("current", "")
        if len(current) > 90:
            current = "…" + current[-88:]
        self.status.setText(
            tr("large.scanning", files=p.get("files", 0), found=p.get("found", 0), current=current)
        )

    def _on_finished(self, result: LargeFilesResult) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.result = result
        self.model.set_items(result.items)
        msg = tr(
            "large.done",
            found=len(result.items),
            size=format_size(sum(i.size for i in result.items)),
            files=result.scanned_files,
        )
        if result.truncated:
            msg += " " + tr("large.truncated", max=len(result.items))
        if result.errors:
            msg += " " + tr("large.errors", count=result.errors)
        if result.cancelled:
            msg = tr("clean.scan_cancelled") + " " + msg
        self.status.setText(msg)
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

    def _open_current(self, index) -> None:
        item = self.model.item_at(index.row()) if index is not None and index.isValid() else None
        if item:
            open_in_explorer(item.path)

    def delete_selected(self) -> None:
        if self.busy or not self.result:
            return
        selected = self.model.checked_items()
        if not selected:
            return
        mode = self.settings.user_files_delete_mode
        size = sum(i.size for i in selected)
        warning = tr("confirm.to_recycle") if mode == MODE_RECYCLE else tr("confirm.permanent")
        dlg = ConfirmDeleteDialog(
            tr("large.confirm_title"),
            tr("confirm.user_summary", files=len(selected), size=format_size(size)),
            items=selected,
            warning=warning,
            require_ack=True,
            parent=self,
        )
        if not dlg.ask():
            return
        policy = SafetyPolicy.for_user_files(self.settings.allow_program_files, self.settings.exclusions)
        cleaner = Cleaner(policy)
        self.progress.setVisible(True)
        self.status.setText(tr("clean.cleaning_start"))
        self._task = Task(
            self,
            cleaner.delete_user_files,
            selected,
            self.result.items,
            [self.result.root],
            mode,
            on_finished=self._on_deleted,
            on_failed=self._on_failed,
        )
        self._update_buttons()

    def _on_deleted(self, report: CleanReport) -> None:
        self._task = None
        get_history().add(make_entry(KIND_LARGE, report, drive=self.result.root if self.result else "", mode=self.settings.user_files_delete_mode))
        self.progress.setVisible(False)
        if self.result:
            self.result.items = [i for i in self.result.items if i.path not in report.deleted_paths]
        self.model.remove_paths(report.deleted_paths)
        self.status.setText(
            tr(
                "clean.done_status",
                deleted=report.deleted_files,
                freed=format_size(report.freed_bytes),
                failed=report.failed_count,
            )
        )
        if report.failed_count:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr("clean.done_title"))
            box.setText(tr("clean.failed_files", count=report.failed_count))
            box.setDetailedText(format_failures(report))
            box.exec()
        self._update_buttons()
        self.files_deleted.emit(report)

    def _update_buttons(self) -> None:
        busy = self.busy
        checked = self.model.checked_items()
        self.scan_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.root_combo.setEnabled(not busy)
        self.threshold_combo.setEnabled(not busy)
        self.delete_btn.setEnabled(not busy and bool(checked))
        self.uncheck_btn.setEnabled(not busy and bool(checked))
        self.open_btn.setEnabled(self.model.rowCount() > 0)
        self.selected_label.setText(
            tr("large.selected", count=len(checked), size=format_size(sum(i.size for i in checked)))
            if checked
            else ""
        )
