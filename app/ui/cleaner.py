"""Страница «Очистка»: результаты сканирования по категориям, выбор и удаление."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.categories import GROUP_ORDER, all_categories, category_by_id, KIND_FILES, KIND_RECYCLE, KIND_SYSTEM, PROCESSES
from app.core.history import KIND_JUNK, get_history, make_entry
from app.core.processes import running_apps
from app.core.cleaner import CleanReport, Cleaner
from app.core.safety import SafetyPolicy
from app.core.scanner import JunkScanner
from app.models.scan_result import CategoryResult, ScanResult
from app.utils.format_size import format_size
from app.utils.i18n import tr
from app.utils.logger import get_logger
from app.utils.permissions import is_admin
from app.utils.settings_store import Settings
from app.ui import theme
from app.ui.widgets import Card, ConfirmDeleteDialog, FileListDialog, page_header
from app.ui.workers import Task

log = get_logger("ui.cleaner")

ROLE_CAT = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1
KIND_GROUP, KIND_CATEGORY, KIND_PLACEHOLDER, KIND_FILE = "group", "category", "placeholder", "file"
PREVIEW_LIMIT = 200


def cat_name(cat_id: str) -> str:
    cat = category_by_id(cat_id)
    if cat is not None and cat.title:  # импортированные правила (Winapp2.ini)
        return cat.title
    if cat_id.startswith("wa2:"):  # правило из отключённого импорта (например, в истории)
        return cat_id[4:].replace("-", " ").capitalize()
    return tr(f"cat.{cat_id}")


def format_failures(report: CleanReport, limit: int = 300) -> str:
    lines = []
    summary = report.reasons_summary()
    for reason, count in sorted(summary.items(), key=lambda kv: -kv[1]):
        lines.append(f"{tr('reason.' + reason)}: {count}")
    if report.failed:
        lines.append("")
        for path, reason in report.failed[:limit]:
            label = cat_name(path) if "\\" not in path and "/" not in path else path
            lines.append(f"[{tr('reason.' + reason)}] {label}")
            detail = report.tool_details.get(path)
            if detail:
                lines.append(detail)
        if len(report.failed) > limit:
            lines.append(tr("details.more", count=len(report.failed) - limit))
    return "\n".join(lines)


class CleanerPage(QWidget):
    scan_started = Signal()
    scan_finished = Signal(object)  # ScanResult
    clean_finished = Signal(object)  # CleanReport
    restart_requested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.drive = ""
        self.result: ScanResult | None = None
        self._task: Task | None = None
        self._pending_smart = False
        self._populating = False
        self._running: dict[str, list[str]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        header, self.subtitle = page_header(tr("clean.title"), tr("clean.subtitle"))
        root.addWidget(header)

        # ---- панель сканирования
        bar = QHBoxLayout()
        self.scan_btn = QPushButton(tr("btn.scan"))
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.clicked.connect(lambda: self.start_scan(self.drive))
        self.smart_btn = QPushButton(tr("btn.smart_clean"))
        self.smart_btn.clicked.connect(lambda: self.start_scan(self.drive, smart=True))
        self.cancel_btn = QPushButton(tr("btn.cancel"))
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        bar.addWidget(self.scan_btn)
        bar.addWidget(self.smart_btn)
        bar.addWidget(self.cancel_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.status = QLabel(tr("clean.not_scanned"))
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        root.addWidget(self.progress)
        root.addWidget(self.status)

        # ---- итог
        summary = Card()
        srow = QHBoxLayout()
        left = QVBoxLayout()
        lt = QLabel(tr("clean.found"))
        lt.setObjectName("CardTitle")
        self.found_label = QLabel("—")
        self.found_label.setObjectName("BigValue")
        self.found_files_label = QLabel("")
        self.found_files_label.setObjectName("Muted")
        left.addWidget(lt)
        left.addWidget(self.found_label)
        left.addWidget(self.found_files_label)
        right = QVBoxLayout()
        rt = QLabel(tr("clean.selected"))
        rt.setObjectName("CardTitle")
        self.selected_label = QLabel("—")
        self.selected_label.setObjectName("CardValue")
        right.addWidget(rt)
        right.addWidget(self.selected_label)
        right.addStretch(1)
        srow.addLayout(left, 1)
        srow.addLayout(right, 1)
        summary.layout_.addLayout(srow)
        root.addWidget(summary)

        # ---- дерево категорий
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([tr("col.category"), tr("col.files"), tr("col.size")])
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 120)
        self.tree.setColumnWidth(2, 120)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemDoubleClicked.connect(lambda *_: self.show_details())
        root.addWidget(self.tree, 1)

        # ---- кнопки
        actions = QHBoxLayout()
        self.select_all_btn = QPushButton(tr("btn.select_all"))
        self.select_all_btn.clicked.connect(lambda: self._set_all(True))
        self.deselect_btn = QPushButton(tr("btn.deselect_all"))
        self.deselect_btn.clicked.connect(lambda: self._set_all(False))
        self.details_btn = QPushButton(tr("btn.details"))
        self.details_btn.clicked.connect(self.show_details)
        self.clean_btn = QPushButton(tr("btn.clean_selected"))
        self.clean_btn.setObjectName("DangerButton")
        self.clean_btn.setMinimumHeight(38)
        self.clean_btn.clicked.connect(self.clean_selected)
        for b in (self.select_all_btn, self.deselect_btn, self.details_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.clean_btn)
        root.addLayout(actions)
        self._update_buttons()

    # ================================================================ public
    def set_drive(self, drive: str) -> None:
        if self.busy:
            return
        if drive != self.drive:
            self.drive = drive
            self.result = None
            self.tree.clear()
            self.found_label.setText("—")
            self.found_files_label.setText("")
            self.selected_label.setText("—")
            self.status.setText(tr("clean.not_scanned"))
        self.subtitle.setText(tr("clean.subtitle_drive", drive=drive.rstrip("\\")))
        self._update_buttons()

    @property
    def busy(self) -> bool:
        return self._task is not None and self._task.running

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.shutdown()

    def start_scan(self, drive: str, smart: bool = False) -> None:
        if self.busy:
            return
        self.set_drive(drive)
        self._pending_smart = smart
        policy = SafetyPolicy.for_junk(self.settings.allow_program_files, self.settings.exclusions)
        categories = all_categories()
        scanner = JunkScanner(
            policy,
            categories=categories,
            temp_min_age_hours=self.settings.temp_min_age_hours,
            log_min_age_days=self.settings.log_min_age_days,
        )
        self.tree.clear()
        self.result = None
        self.progress.setRange(0, len(categories))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status.setText(tr("clean.scanning_start"))
        self._task = Task(
            self,
            scanner.scan,
            drive,
            on_progress=self._on_scan_progress,
            on_finished=self._on_scan_finished,
            on_failed=self._on_task_failed,
        )
        self.scan_started.emit()
        self._update_buttons()

    # ============================================================ scanning
    def _on_scan_progress(self, p: dict) -> None:
        self.progress.setMaximum(max(1, p.get("total", 1)))
        self.progress.setValue(p.get("index", 0))
        cat = p.get("category")
        self.status.setText(
            tr(
                "clean.scanning",
                category=cat_name(cat) if cat else "…",
                files=p.get("files", 0),
                size=format_size(p.get("bytes", 0)),
            )
        )

    def _on_scan_finished(self, result: ScanResult) -> None:
        self._task = None
        self.progress.setVisible(False)
        self.result = result
        self._running = running_apps(c.category.id for c in result.categories if not c.is_empty)
        admin = is_admin()
        for cres in result.categories:
            accessible = not (cres.category.requires_admin and not admin)
            cres.selected = cres.category.smart and accessible and not cres.is_empty
        self._populate()
        msg = tr("clean.scan_done", files=result.total_files, size=format_size(result.total_size))
        if result.cancelled:
            msg = tr("clean.scan_cancelled") + " " + msg
        self.status.setText(msg)
        self._update_buttons()
        self.scan_finished.emit(result)
        if self._pending_smart and not result.cancelled:
            self._pending_smart = False
            self._apply_smart_selection()
            self.clean_selected(smart=True)
        self._pending_smart = False

    def _apply_smart_selection(self) -> None:
        if not self.result:
            return
        admin = is_admin()
        for cres in self.result.categories:
            accessible = not (cres.category.requires_admin and not admin)
            cres.selected = cres.category.smart and accessible and not cres.is_empty
        self._populate()

    # ============================================================ tree
    def _populate(self) -> None:
        self._populating = True
        self.tree.clear()
        if not self.result:
            self._populating = False
            return
        admin = is_admin()
        shield = self.style().standardIcon(QStyle.StandardPixmap.SP_VistaShield)
        by_group: dict[str, list[CategoryResult]] = {}
        for cres in self.result.categories:
            needs_admin_hint = cres.category.requires_admin and not admin and cres.access_denied
            if cres.is_empty and not needs_admin_hint:
                continue
            by_group.setdefault(cres.category.group, []).append(cres)

        for group in GROUP_ORDER:
            members = by_group.get(group)
            if not members:
                continue
            g_size = sum(c.total_size for c in members)
            g_count = sum(c.file_count for c in members)
            gitem = QTreeWidgetItem([tr(f"group.{group}"), str(g_count), format_size(g_size)])
            gitem.setData(0, ROLE_KIND, KIND_GROUP)
            # Состояние группы считаем сами (_recompute_group): автоматический tristate Qt
            # перестаёт работать, если в группе есть неактивная категория без чекбокса.
            gitem.setFlags(gitem.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            font = gitem.font(0)
            font.setBold(True)
            for col in range(3):
                gitem.setFont(col, font)
            self._align(gitem)
            self.tree.addTopLevelItem(gitem)

            for cres in members:
                cat = cres.category
                count_text = str(cres.file_count)
                size_text = format_size(cres.total_size)
                if cres.excluded:
                    count_text = f"{cres.effective_count} / {cres.file_count}"
                    size_text = format_size(cres.effective_size)
                if cres.is_empty:
                    count_text = tr("clean.needs_admin_short")
                if cat.kind == KIND_SYSTEM:
                    count_text = str(cres.file_count) if cres.file_count else "—"
                    if not cres.size_known:
                        size_text = "—"
                    elif cres.size_partial:
                        size_text = "≥ " + size_text
                name = cat_name(cat.id)
                running = self._running.get(cat.id)
                if running:
                    name += "  " + tr("clean.running_suffix")
                citem = QTreeWidgetItem([name, count_text, size_text])
                if running:
                    citem.setForeground(0, QColor(theme.colors()["warning"]))
                citem.setData(0, ROLE_CAT, cat.id)
                citem.setData(0, ROLE_KIND, KIND_CATEGORY)
                if cat.title:
                    tip = [cat.description or tr("wa2.desc"), tr("wa2.source_tip")]
                else:
                    tip = [tr(f"cat.{cat.id}.desc")]
                if cat.note:
                    tip.append(tr(cat.note))
                if cat.requires_admin:
                    tip.append(tr("clean.admin_tip"))
                    if not admin:
                        citem.setIcon(0, shield)
                if cres.errors:
                    tip.append(tr("clean.errors_tip", count=cres.errors))
                if cres.skipped_recent:
                    tip.append(tr("clean.recent_tip", count=cres.skipped_recent))
                if running:
                    tip.append(tr("clean.running_tip", names=", ".join(running)))
                if cres.excluded:
                    tip.append(tr("clean.excluded_tip", count=len(cres.excluded)))
                if cat.kind == KIND_SYSTEM and not cres.size_known:
                    tip.append(tr("clean.size_after_tip"))
                citem.setToolTip(0, "\n".join(tip))
                if cres.is_empty:
                    citem.setFlags(citem.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    citem.setDisabled(True)
                else:
                    citem.setFlags(citem.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    citem.setCheckState(
                        0, Qt.CheckState.Checked if cres.selected else Qt.CheckState.Unchecked
                    )
                    if cres.items:
                        ph = QTreeWidgetItem([tr("clean.loading")])
                        ph.setData(0, ROLE_KIND, KIND_PLACEHOLDER)
                        citem.addChild(ph)
                self._align(citem)
                gitem.addChild(citem)
            self._recompute_group(gitem)
            gitem.setExpanded(True)

        if self.tree.topLevelItemCount() == 0:
            empty = QTreeWidgetItem([tr("clean.nothing_found")])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)
        self._populating = False
        self._refresh_summary()

    @staticmethod
    def _align(item: QTreeWidgetItem) -> None:
        for col in (1, 2):
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, ROLE_KIND) != KIND_CATEGORY or not self.result:
            return
        if item.childCount() != 1 or item.child(0).data(0, ROLE_KIND) != KIND_PLACEHOLDER:
            return
        cres = self.result.get(item.data(0, ROLE_CAT))
        if not cres:
            return
        self._populating = True
        item.takeChild(0)
        children = []
        for fi in cres.items[:PREVIEW_LIMIT]:
            child = QTreeWidgetItem([fi.path, "", format_size(fi.size)])
            child.setData(0, ROLE_KIND, KIND_FILE)
            child.setToolTip(0, fi.path)
            child.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._align(child)
            children.append(child)
        if len(cres.items) > PREVIEW_LIMIT:
            more = QTreeWidgetItem([tr("clean.more_items", count=len(cres.items) - PREVIEW_LIMIT)])
            more.setData(0, ROLE_KIND, KIND_FILE)
            more.setFlags(Qt.ItemFlag.ItemIsEnabled)
            children.append(more)
        item.addChildren(children)
        self._populating = False

    def _checkable_children(self, group: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        """Категории, которыми управляет чекбокс группы.

        Системные операции (Windows.old, DISM) сюда не входят: их можно выбрать только
        отдельным кликом, групповой чекбокс и «Выбрать всё» их не отмечают.
        """
        return [
            group.child(j)
            for j in range(group.childCount())
            if group.child(j).data(0, ROLE_KIND) == KIND_CATEGORY
            and not group.child(j).isDisabled()
            and not self._is_system(group.child(j).data(0, ROLE_CAT))
        ]

    def _recompute_group(self, group: QTreeWidgetItem) -> None:
        children = self._checkable_children(group)
        if not children:
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            return
        checked = sum(1 for c in children if c.checkState(0) == Qt.CheckState.Checked)
        if checked == 0:
            state = Qt.CheckState.Unchecked
        elif checked == len(children):
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked
        was = self._populating
        self._populating = True
        group.setCheckState(0, state)
        self._populating = was

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._populating or column != 0 or not self.result:
            return
        kind = item.data(0, ROLE_KIND)
        self._populating = True
        if kind == KIND_GROUP:
            state = item.checkState(0)
            if state == Qt.CheckState.PartiallyChecked:
                state = Qt.CheckState.Checked
            for child in self._checkable_children(item):
                child.setCheckState(0, state)
        self._populating = False
        if kind == KIND_GROUP:
            self._recompute_group(item)
        elif kind == KIND_CATEGORY and item.parent() is not None:
            self._recompute_group(item.parent())
        self._sync_selection_from_tree()

    def _sync_selection_from_tree(self) -> None:
        if not self.result:
            return
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                if c.data(0, ROLE_KIND) != KIND_CATEGORY:
                    continue
                cres = self.result.get(c.data(0, ROLE_CAT))
                if cres:
                    cres.selected = (
                        not c.isDisabled() and c.checkState(0) == Qt.CheckState.Checked
                    )
        self._refresh_summary()

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._populating = True
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                c = g.child(j)
                if c.data(0, ROLE_KIND) == KIND_CATEGORY and not c.isDisabled():
                    if checked and self._is_system(c.data(0, ROLE_CAT)):
                        continue  # тяжёлые системные операции — только явным выбором
                    c.setCheckState(0, state)
            self._recompute_group(g)
        self._populating = False
        self._sync_selection_from_tree()

    def _is_system(self, cat_id: str) -> bool:
        cres = self.result.get(cat_id) if self.result else None
        return bool(cres and cres.category.kind == KIND_SYSTEM)

    def _refresh_summary(self) -> None:
        if not self.result:
            return
        self.found_label.setText(format_size(self.result.total_size))
        self.found_files_label.setText(tr("clean.files_count", count=self.result.total_files))
        self.selected_label.setText(
            tr(
                "clean.selected_value",
                size=format_size(self.result.selected_size),
                count=self.result.selected_files,
            )
        )
        self._update_buttons()

    def show_details(self) -> None:
        if not self.result:
            return
        item = self.tree.currentItem()
        while item is not None and item.data(0, ROLE_KIND) not in (KIND_CATEGORY, KIND_GROUP):
            item = item.parent()
        if item is None:
            QMessageBox.information(self, tr("btn.details"), tr("clean.pick_category"))
            return
        if item.data(0, ROLE_KIND) == KIND_GROUP:
            cat_ids = [item.child(j).data(0, ROLE_CAT) for j in range(item.childCount())]
            title = item.text(0)
        else:
            cat_ids = [item.data(0, ROLE_CAT)]
            title = item.text(0)
        items = []
        notes = []
        cats = [c for c in (self.result.get(cid) for cid in cat_ids) if c is not None]
        excluded: set[str] = set()
        for cres in cats:
            items.extend(cres.items)
            excluded |= cres.excluded
            if cres.category.kind == KIND_RECYCLE:
                notes.append(tr("clean.recycle_note"))
        # Снимать галочки можно только с обычных файлов (не корзина, не системные операции).
        editable = (
            not self.busy and bool(items) and all(c.category.kind == KIND_FILES for c in cats)
        )
        dlg = FileListDialog(title, items, "\n".join(notes), self, checkable=editable, excluded=excluded)
        dlg.exec()
        if dlg.applied and editable:
            new_excluded = dlg.excluded_paths()
            for cres in cats:
                cres.excluded = {i.path for i in cres.items if i.path in new_excluded}
            log.info("Исключено вручную файлов: %d", len(new_excluded))
            self._populate()

    # ============================================================ cleaning
    def clean_selected(self, smart: bool = False) -> None:
        if self.busy or not self.result:
            return
        selected = self.result.selected()
        if not selected:
            QMessageBox.information(self, tr("clean.title"), tr("clean.nothing_selected"))
            return

        needs_admin = [c for c in selected if c.category.requires_admin]
        if needs_admin and not is_admin():
            names = ", ".join(cat_name(c.category.id) for c in needs_admin)
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr("admin.title"))
            box.setText(tr("admin.needed", names=names))
            restart = box.addButton(tr("admin.restart"), QMessageBox.ButtonRole.AcceptRole)
            skip = box.addButton(tr("admin.skip"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(tr("btn.cancel"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked == restart:
                self.restart_requested.emit()
                return
            if clicked != skip:
                return
            for c in needs_admin:
                c.selected = False
            self._populate()
            selected = self.result.selected()
            if not selected:
                return

        selected = self._handle_running_apps(selected)
        if not selected:
            return

        files = sum(c.effective_count for c in selected)
        size = sum(c.effective_size for c in selected)
        rows = [
            (
                cat_name(c.category.id),
                str(c.effective_count) if c.file_count or c.category.kind != KIND_SYSTEM else "—",
                format_size(c.effective_size) if c.size_known else "—",
            )
            for c in selected
        ]
        notes = sorted({tr(c.category.note) for c in selected if c.category.note})
        if any(c.category.kind == KIND_SYSTEM for c in selected):
            notes.insert(0, tr("confirm.system_tools"))
        if any(c.category.kind == KIND_FILES for c in selected):
            if self.settings.quarantine_enabled:
                notes.insert(0, tr("confirm.quarantine_on", days=self.settings.quarantine_days))
            else:
                notes.insert(0, tr("confirm.quarantine_off"))
        title = tr("btn.smart_clean") if smart else tr("confirm.title")
        dlg = ConfirmDeleteDialog(
            title,
            tr("confirm.junk_summary", files=files, size=format_size(size)),
            rows=rows,
            warning="\n".join(notes),
            ok_text=tr("btn.clean"),
            parent=self,
        )
        if not dlg.ask():
            log.info("Удаление отменено пользователем")
            return

        policy = SafetyPolicy.for_junk(self.settings.allow_program_files, self.settings.exclusions)
        cleaner = Cleaner(policy)
        total = sum(len(c.effective_items) if c.category.kind == KIND_FILES else 1 for c in selected)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status.setText(tr("clean.cleaning_start"))
        self._cleaning_ids = [c.category.id for c in selected]
        quarantine = None
        if self.settings.quarantine_enabled:
            from app.core.quarantine import get_quarantine

            quarantine = get_quarantine().new_session(self._cleaning_ids)
        self._task = Task(
            self,
            cleaner.clean_junk,
            self.result,
            [c.category.id for c in selected],
            quarantine=quarantine,
            on_progress=self._on_clean_progress,
            on_finished=self._on_clean_finished,
            on_failed=self._on_task_failed,
        )
        self._update_buttons()

    def _handle_running_apps(self, selected: list[CategoryResult]) -> list[CategoryResult]:
        """Предупреждает о запущенных программах. Процессы НЕ закрываются принудительно."""
        while True:
            running = running_apps(c.category.id for c in selected)
            self._running = {**{k: v for k, v in self._running.items() if k not in PROCESSES}, **running}
            if not running:
                return selected
            names = "\n".join(f"• {cat_name(cid)} ({', '.join(procs)})" for cid, procs in running.items())
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr("running.title"))
            box.setText(tr("running.text", names=names))
            recheck = box.addButton(tr("running.recheck"), QMessageBox.ButtonRole.AcceptRole)
            skip = box.addButton(tr("running.skip"), QMessageBox.ButtonRole.ActionRole)
            anyway = box.addButton(tr("running.anyway"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(tr("btn.cancel"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked == recheck:
                continue
            if clicked == skip:
                for c in selected:
                    if c.category.id in running:
                        c.selected = False
                self._populate()
                return [c for c in selected if c.category.id not in running]
            if clicked == anyway:
                return selected
            return []

    def _on_clean_progress(self, p: dict) -> None:
        cat = p.get("category") or ""
        if "tool_percent" in p:
            pct = p.get("tool_percent")
            if pct is None:
                self.progress.setRange(0, 0)  # инструмент Windows не сообщает прогресс
                self.status.setText(tr("clean.tool_running", category=cat_name(cat)))
            else:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(pct * 10))
                self.status.setText(tr("clean.tool_percent", category=cat_name(cat), percent=f"{pct:.1f}"))
            return
        self.progress.setMaximum(max(1, p.get("total", 1)))
        self.progress.setValue(p.get("processed", 0))
        self.status.setText(
            tr(
                "clean.cleaning",
                category=cat_name(cat) if cat else "…",
                deleted=p.get("deleted", 0),
                freed=format_size(p.get("freed", 0)),
                failed=p.get("failed", 0),
            )
        )

    def _on_clean_finished(self, report: CleanReport) -> None:
        self._task = None
        get_history().add(
            make_entry(KIND_JUNK, report, drive=self.drive, categories=getattr(self, "_cleaning_ids", []))
        )
        self.progress.setVisible(False)
        if self.result:
            for cres in self.result.categories:
                cres.remove_paths(report.deleted_paths)
                if cres.category.kind == KIND_SYSTEM and cres.category.id in report.tools_done:
                    cres.tool_available = False
                    cres.reported_size = 0
                    cres.reported_count = 0
                if cres.category.kind == KIND_RECYCLE and report.recycle_emptied:
                    cres.items = []
                    cres.reported_size = 0
                    cres.reported_count = 0
                cres.selected = False
        self._populate()
        self.status.setText(
            tr(
                "clean.done_status",
                deleted=report.deleted_files,
                freed=format_size(report.freed_bytes),
                failed=report.failed_count,
            )
        )
        self._update_buttons()

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(tr("clean.done_title"))
        text = tr("clean.done_title") + "\n\n" + tr("clean.freed", size=format_size(report.freed_bytes))
        if report.quarantined_files:
            text += "\n" + tr(
                "clean.quarantined",
                count=report.quarantined_files,
                size=format_size(report.quarantined_bytes),
                days=self.settings.quarantine_days,
            )
        text += "\n" + tr("clean.deleted_files", count=report.deleted_files)
        if report.failed_count:
            text += "\n" + tr("clean.failed_files", count=report.failed_count)
            box.setDetailedText(format_failures(report))
        if report.cancelled:
            text += "\n" + tr("clean.was_cancelled")
        box.setText(text)
        box.exec()
        self.clean_finished.emit(report)

    # ============================================================ common
    def _on_task_failed(self, message: str) -> None:
        self._task = None
        self._pending_smart = False
        self.progress.setVisible(False)
        self.status.setText(tr("error.task_failed", error=message))
        self._update_buttons()
        QMessageBox.critical(self, tr("error.title"), tr("error.task_failed", error=message))

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status.setText(tr("clean.cancelling"))

    def _update_buttons(self) -> None:
        busy = self.busy
        has_result = self.result is not None
        has_selection = bool(self.result and self.result.selected())
        self.scan_btn.setEnabled(not busy and bool(self.drive))
        self.smart_btn.setEnabled(not busy and bool(self.drive))
        self.cancel_btn.setEnabled(busy)
        self.select_all_btn.setEnabled(not busy and has_result)
        self.deselect_btn.setEnabled(not busy and has_result)
        self.details_btn.setEnabled(not busy and has_result)
        self.clean_btn.setEnabled(not busy and has_selection)
