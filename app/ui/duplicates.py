"""Страница «Дубликаты»: поиск по размеру и хешу, выбор копий, удаление с подтверждением."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.history import KIND_DUPLICATES, get_history, make_entry
from app.core.cleaner import MODE_RECYCLE, CleanReport, Cleaner
from app.core.duplicate_finder import DuplicateFinder, DuplicatesResult
from app.core.safety import SafetyPolicy
from app.models.file_item import DuplicateGroup, FileItem
from app.utils import winpaths
from app.utils.format_size import format_size, format_timestamp
from app.utils.i18n import tr
from app.utils.settings_store import Settings, save_settings
from app.ui.cleaner import format_failures
from app.ui.widgets import Card, ConfirmDeleteDialog, open_in_explorer, page_header
from app.ui.workers import Task

GROUPS_PAGE = 300
MIN_SIZES_KB = (100, 1024, 10 * 1024, 100 * 1024)
ROLE_GROUP = Qt.ItemDataRole.UserRole
ROLE_FILE = Qt.ItemDataRole.UserRole + 1


def default_duplicate_folders() -> list[str]:
    folders = []
    for name in ("Downloads", "Desktop", "Documents", "Pictures", "Videos"):
        p = winpaths.known_folder(name)
        if p and os.path.isdir(p) and p not in folders:
            folders.append(p)
    return folders


class DuplicatesPage(QWidget):
    files_deleted = Signal(object)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.result: DuplicatesResult | None = None
        self._task: Task | None = None
        self._shown_groups = 0
        self._populating = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        header, _ = page_header(tr("dup.title"), tr("dup.subtitle"))
        root.addWidget(header)

        # ---- папки поиска
        folders_card = Card()
        ft = QLabel(tr("dup.folders"))
        ft.setObjectName("CardTitle")
        folders_card.layout_.addWidget(ft)
        frow = QHBoxLayout()
        self.folders = QListWidget()
        self.folders.setMaximumHeight(110)
        for f in settings.duplicate_folders or default_duplicate_folders():
            self.folders.addItem(f)
        fbtns = QVBoxLayout()
        add_btn = QPushButton(tr("btn.add_folder"))
        add_btn.clicked.connect(self._add_folder)
        rm_btn = QPushButton(tr("btn.remove"))
        rm_btn.clicked.connect(self._remove_folder)
        reset_btn = QPushButton(tr("btn.reset_defaults"))
        reset_btn.clicked.connect(self._reset_folders)
        self._folder_buttons = (add_btn, rm_btn, reset_btn)
        for b in self._folder_buttons:
            fbtns.addWidget(b)
        fbtns.addStretch(1)
        frow.addWidget(self.folders, 1)
        frow.addLayout(fbtns)
        folders_card.layout_.addLayout(frow)
        root.addWidget(folders_card)

        bar = QHBoxLayout()
        lbl = QLabel(tr("dup.min_size"))
        lbl.setObjectName("Muted")
        self.min_size = QComboBox()
        for kb in MIN_SIZES_KB:
            self.min_size.addItem(f"> {format_size(kb * 1024, 0)}", kb)
        idx = self.min_size.findData(settings.duplicates_min_size_kb)
        if idx < 0:
            self.min_size.addItem(f"> {format_size(settings.duplicates_min_size_kb * 1024, 0)}",
                                  settings.duplicates_min_size_kb)
            idx = self.min_size.count() - 1
        self.min_size.setCurrentIndex(idx)
        self.scan_btn = QPushButton(tr("btn.find_duplicates"))
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_btn = QPushButton(tr("btn.cancel"))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        for w in (lbl, self.min_size, self.scan_btn, self.cancel_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        root.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel(tr("dup.hint"))
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        root.addWidget(self.progress)
        root.addWidget(self.status)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("col.name"), tr("col.size"), tr("col.modified"), tr("col.folder")])
        self.tree.setUniformRowHeights(True)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 140)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(self._open_item)
        root.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.keep_oldest_btn = QPushButton(tr("dup.keep_oldest"))
        self.keep_oldest_btn.clicked.connect(lambda: self._auto_select(keep_oldest=True))
        self.keep_newest_btn = QPushButton(tr("dup.keep_newest"))
        self.keep_newest_btn.clicked.connect(lambda: self._auto_select(keep_oldest=False))
        self.uncheck_btn = QPushButton(tr("btn.deselect_all"))
        self.uncheck_btn.clicked.connect(self._uncheck_all)
        self.more_btn = QPushButton(tr("dup.show_more"))
        self.more_btn.clicked.connect(self._show_more_groups)
        self.selected_label = QLabel("")
        self.selected_label.setObjectName("Muted")
        self.delete_btn = QPushButton(tr("btn.delete_selected"))
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self.delete_selected)
        for b in (self.keep_oldest_btn, self.keep_newest_btn, self.uncheck_btn, self.more_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.selected_label)
        actions.addWidget(self.delete_btn)
        root.addLayout(actions)
        self._checked: set[str] = set()
        self._update_buttons()

    # ------------------------------------------------------------------ API
    @property
    def busy(self) -> bool:
        return self._task is not None and self._task.running

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.shutdown()

    def folder_list(self) -> list[str]:
        return [self.folders.item(i).text() for i in range(self.folders.count())]

    # -------------------------------------------------------------- folders
    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("btn.add_folder"))
        if folder:
            folder = os.path.normpath(folder)
            if folder not in self.folder_list():
                self.folders.addItem(folder)

    def _remove_folder(self) -> None:
        for it in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(it))

    def _reset_folders(self) -> None:
        self.folders.clear()
        for f in default_duplicate_folders():
            self.folders.addItem(f)

    # ------------------------------------------------------------- scanning
    def start_scan(self) -> None:
        if self.busy:
            return
        roots = self.folder_list()
        if not roots:
            QMessageBox.information(self, tr("dup.title"), tr("dup.no_folders"))
            return
        if roots != self.settings.duplicate_folders:
            self.settings.duplicate_folders = list(roots)
            save_settings(self.settings)
        policy = SafetyPolicy.for_user_files(self.settings.allow_program_files, self.settings.exclusions)
        finder = DuplicateFinder(policy)
        self.tree.clear()
        self._checked.clear()
        self.result = None
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.status.setText(tr("dup.collecting", files=0))
        self._task = Task(
            self,
            finder.find,
            roots,
            int(self.min_size.currentData()) * 1024,
            on_progress=self._on_progress,
            on_finished=self._on_finished,
            on_failed=self._on_failed,
        )
        self._update_buttons()

    def _on_progress(self, p: dict) -> None:
        phase = p.get("phase")
        if phase == "collect":
            self.progress.setRange(0, 0)
            self.status.setText(tr("dup.collecting", files=p.get("files", 0)))
        elif phase == "partial":
            self.progress.setRange(0, max(1, p.get("total", 1)))
            self.progress.setValue(p.get("done", 0))
            self.status.setText(tr("dup.partial", done=p.get("done", 0), total=p.get("total", 0)))
        elif phase == "full":
            total = max(1, p.get("total_bytes", 1))
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(p.get("bytes", 0) / total * 1000))
            self.status.setText(
                tr("dup.hashing", done=format_size(p.get("bytes", 0)), total=format_size(total))
            )

    def _on_finished(self, result: DuplicatesResult) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.result = result
        self._shown_groups = 0
        self.tree.clear()
        self._checked.clear()
        self._show_more_groups()
        msg = tr(
            "dup.done",
            groups=len(result.groups),
            wasted=format_size(result.wasted_bytes),
            files=result.scanned_files,
        )
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

    # ----------------------------------------------------------------- tree
    def _show_more_groups(self) -> None:
        if not self.result:
            return
        self._populating = True
        start = self._shown_groups
        end = min(len(self.result.groups), start + GROUPS_PAGE)
        for gi in range(start, end):
            group = self.result.groups[gi]
            gitem = QTreeWidgetItem(
                [
                    tr("dup.group", n=gi + 1, count=group.count, size=format_size(group.size),
                       wasted=format_size(group.wasted_bytes)),
                    "",
                    "",
                    "",
                ]
            )
            gitem.setData(0, ROLE_GROUP, gi)
            gitem.setToolTip(0, tr("dup.group_tip", digest=group.digest[:16]))
            font = gitem.font(0)
            font.setBold(True)
            gitem.setFont(0, font)
            gitem.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for item in group.items:
                child = QTreeWidgetItem(
                    [item.name, format_size(item.size), format_timestamp(item.mtime), item.folder]
                )
                child.setData(0, ROLE_FILE, item.path)
                child.setToolTip(0, item.path)
                child.setToolTip(3, item.path)
                child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                child.setCheckState(
                    0, Qt.CheckState.Checked if item.path in self._checked else Qt.CheckState.Unchecked
                )
                child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                gitem.addChild(child)
            gitem.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tree.addTopLevelItem(gitem)
            gitem.setExpanded(True)
        self._shown_groups = end
        self._populating = False
        self._update_buttons()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._populating or column != 0:
            return
        path = item.data(0, ROLE_FILE)
        if not path:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._checked.add(path)
        else:
            self._checked.discard(path)
        self._update_buttons()

    def _apply_checks_to_tree(self) -> None:
        self._populating = True
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                path = c.data(0, ROLE_FILE)
                c.setCheckState(0, Qt.CheckState.Checked if path in self._checked else Qt.CheckState.Unchecked)
        self._populating = False
        self._update_buttons()

    def _auto_select(self, keep_oldest: bool) -> None:
        """Отмечает все копии, кроме одной (самой старой/новой) — во ВСЕХ группах."""
        if not self.result:
            return
        self._checked.clear()
        for group in self.result.groups:
            ordered = sorted(group.items, key=lambda i: i.mtime, reverse=not keep_oldest)
            for item in ordered[1:]:
                self._checked.add(item.path)
        self._apply_checks_to_tree()

    def _uncheck_all(self) -> None:
        self._checked.clear()
        self._apply_checks_to_tree()

    def _open_item(self, item: QTreeWidgetItem, _column: int) -> None:
        path = item.data(0, ROLE_FILE)
        if path:
            open_in_explorer(path)

    # -------------------------------------------------------------- delete
    def _selected_items(self) -> list[FileItem]:
        if not self.result:
            return []
        return [i for g in self.result.groups for i in g.items if i.path in self._checked]

    def _fully_selected_groups(self) -> list[DuplicateGroup]:
        if not self.result:
            return []
        return [g for g in self.result.groups if all(i.path in self._checked for i in g.items)]

    def delete_selected(self) -> None:
        if self.busy or not self.result:
            return
        selected = self._selected_items()
        if not selected:
            return
        full = self._fully_selected_groups()
        if full:
            QMessageBox.warning(self, tr("dup.title"), tr("dup.all_copies_selected", count=len(full)))
            return
        mode = self.settings.user_files_delete_mode
        size = sum(i.size for i in selected)
        dlg = ConfirmDeleteDialog(
            tr("dup.confirm_title"),
            tr("confirm.user_summary", files=len(selected), size=format_size(size)),
            items=selected,
            warning=(tr("confirm.to_recycle") if mode == MODE_RECYCLE else tr("confirm.permanent"))
            + "\n" + tr("dup.keep_note"),
            require_ack=True,
            parent=self,
        )
        if not dlg.ask():
            return
        policy = SafetyPolicy.for_user_files(self.settings.allow_program_files, self.settings.exclusions)
        cleaner = Cleaner(policy)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self._task = Task(
            self,
            cleaner.delete_user_files,
            selected,
            self.result.all_items,
            self.result.roots,
            mode,
            self.result.groups,
            on_finished=self._on_deleted,
            on_failed=self._on_failed,
        )
        self._update_buttons()

    def _on_deleted(self, report: CleanReport) -> None:
        self._task = None
        get_history().add(make_entry(KIND_DUPLICATES, report, drive="", mode=self.settings.user_files_delete_mode))
        self.progress.setVisible(False)
        if self.result:
            new_groups = []
            for g in self.result.groups:
                g.items = [i for i in g.items if i.path not in report.deleted_paths]
                if len(g.items) > 1:
                    new_groups.append(g)
            self.result.groups = new_groups
        self._checked -= report.deleted_paths
        self.tree.clear()
        self._shown_groups = 0
        self._show_more_groups()
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
        has = bool(self.result and self.result.groups)
        selected = self._selected_items() if self.result else []
        self.scan_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.min_size.setEnabled(not busy)
        for b in self._folder_buttons:
            b.setEnabled(not busy)
        self.keep_oldest_btn.setEnabled(not busy and has)
        self.keep_newest_btn.setEnabled(not busy and has)
        self.uncheck_btn.setEnabled(not busy and bool(selected))
        self.more_btn.setVisible(bool(self.result and self._shown_groups < len(self.result.groups)))
        self.delete_btn.setEnabled(not busy and bool(selected))
        self.selected_label.setText(
            tr("large.selected", count=len(selected), size=format_size(sum(i.size for i in selected)))
            if selected
            else ""
        )
