"""Dashboard: информация о диске, кнопки Scan / Smart Clean, последнее сканирование."""

from __future__ import annotations

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.disk_analyzer import get_disk_info
from app.models.scan_result import ScanResult
from app.utils import winpaths
from app.utils.format_size import format_size, format_timestamp
from app.utils.i18n import tr
from app.utils.permissions import is_admin
from app.ui.widgets import Card, StatCard, page_header


class DashboardPage(QWidget):
    scan_requested = Signal(str)
    smart_clean_requested = Signal(str)
    drive_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        header, _ = page_header(tr("dash.title"), tr("dash.subtitle"))
        root.addWidget(header)

        # ---- выбор диска
        drive_row = QHBoxLayout()
        lbl = QLabel(tr("dash.drive"))
        lbl.setObjectName("Muted")
        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(160)
        for d in winpaths.list_drives():
            self.drive_combo.addItem(d, d)
        sys_drive = winpaths.system_drive()
        idx = self.drive_combo.findData(sys_drive)
        if idx >= 0:
            self.drive_combo.setCurrentIndex(idx)
        self.drive_combo.currentIndexChanged.connect(self._on_drive_changed)
        refresh = QPushButton(tr("btn.refresh"))
        refresh.clicked.connect(self.refresh_disk_info)
        drive_row.addWidget(lbl)
        drive_row.addWidget(self.drive_combo)
        drive_row.addWidget(refresh)
        drive_row.addStretch(1)
        root.addLayout(drive_row)

        # ---- диск
        disk_card = Card()
        self.disk_title = QLabel()
        self.disk_title.setObjectName("CardValue")
        self.usage_bar = QProgressBar()
        self.usage_bar.setObjectName("UsageBar")
        self.usage_bar.setRange(0, 1000)
        self.usage_bar.setTextVisible(False)
        self.usage_label = QLabel()
        self.usage_label.setObjectName("Muted")
        disk_card.layout_.addWidget(self.disk_title)
        disk_card.layout_.addWidget(self.usage_bar)
        disk_card.layout_.addWidget(self.usage_label)
        root.addWidget(disk_card)

        stats = QGridLayout()
        stats.setHorizontalSpacing(16)
        self.total_card = StatCard(tr("dash.total"))
        self.used_card = StatCard(tr("dash.used"))
        self.free_card = StatCard(tr("dash.free"))
        stats.addWidget(self.total_card, 0, 0)
        stats.addWidget(self.used_card, 0, 1)
        stats.addWidget(self.free_card, 0, 2)
        root.addLayout(stats)

        # ---- действия
        actions = QGridLayout()
        actions.setHorizontalSpacing(16)

        scan_card = Card()
        t1 = QLabel(tr("dash.scan_title"))
        t1.setObjectName("CardValue")
        d1 = QLabel(tr("dash.scan_desc"))
        d1.setObjectName("Muted")
        d1.setWordWrap(True)
        self.scan_btn = QPushButton(tr("btn.scan"))
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.setMinimumHeight(40)
        self.scan_btn.clicked.connect(lambda: self.scan_requested.emit(self.current_drive()))
        for w in (t1, d1):
            scan_card.layout_.addWidget(w)
        scan_card.layout_.addStretch(1)
        scan_card.layout_.addWidget(self.scan_btn)

        smart_card = Card()
        t2 = QLabel(tr("dash.smart_title"))
        t2.setObjectName("CardValue")
        d2 = QLabel(tr("dash.smart_desc"))
        d2.setObjectName("Muted")
        d2.setWordWrap(True)
        self.smart_btn = QPushButton(tr("btn.smart_clean"))
        self.smart_btn.setMinimumHeight(40)
        self.smart_btn.clicked.connect(lambda: self.smart_clean_requested.emit(self.current_drive()))
        for w in (t2, d2):
            smart_card.layout_.addWidget(w)
        smart_card.layout_.addStretch(1)
        smart_card.layout_.addWidget(self.smart_btn)

        actions.addWidget(scan_card, 0, 0)
        actions.addWidget(smart_card, 0, 1)
        root.addLayout(actions)

        # ---- последнее сканирование
        last_card = Card()
        lt = QLabel(tr("dash.last_scan"))
        lt.setObjectName("CardTitle")
        self.last_scan_label = QLabel(tr("dash.never_scanned"))
        self.last_scan_label.setWordWrap(True)
        self.last_clean_label = QLabel("")
        self.last_clean_label.setObjectName("Success")
        self.last_clean_label.setVisible(False)
        last_card.layout_.addWidget(lt)
        last_card.layout_.addWidget(self.last_scan_label)
        last_card.layout_.addWidget(self.last_clean_label)
        root.addWidget(last_card)

        self.admin_label = QLabel(tr("dash.admin_yes") if is_admin() else tr("dash.admin_no"))
        self.admin_label.setObjectName("Muted")
        self.admin_label.setWordWrap(True)
        root.addWidget(self.admin_label)
        root.addStretch(1)

        self.refresh_disk_info()
        self._show_last_from_history()

    # ------------------------------------------------------------------ API
    def current_drive(self) -> str:
        return self.drive_combo.currentData() or winpaths.system_drive()

    def set_busy(self, busy: bool) -> None:
        self.scan_btn.setEnabled(not busy)
        self.smart_btn.setEnabled(not busy)
        self.drive_combo.setEnabled(not busy)

    def refresh_disk_info(self) -> None:
        drive = self.current_drive()
        info = get_disk_info(drive)
        name = drive.rstrip("\\")
        title = tr("dash.disk", drive=name)
        if info.label:
            title += f"  ({info.label})"
        self.disk_title.setText(title)
        if not info.available:
            self.usage_bar.setValue(0)
            self.usage_label.setText(tr("dash.disk_unavailable"))
            for card in (self.total_card, self.used_card, self.free_card):
                card.set_value("—")
            return
        self.usage_bar.setValue(int(info.percent_used * 10))
        self.usage_label.setText(
            tr("dash.usage", percent=f"{info.percent_used:.1f}", free=format_size(info.free))
        )
        self.total_card.set_value(format_size(info.total))
        self.used_card.set_value(format_size(info.used))
        self.free_card.set_value(format_size(info.free))

    def on_scan_finished(self, result: ScanResult) -> None:
        when = format_timestamp(result.finished_at or time.time())
        self.last_scan_label.setText(
            tr(
                "dash.last_scan_value",
                when=when,
                drive=result.drive.rstrip("\\"),
                files=result.total_files,
                size=format_size(result.total_size),
            )
        )

    def _show_last_from_history(self) -> None:
        try:
            from app.core.history import get_history

            entries = get_history().load()
        except Exception:  # noqa: BLE001 - история не должна мешать запуску
            return
        if entries:
            last = entries[0]
            self.last_clean_label.setText(
                tr("dash.last_freed_at", size=format_size(last.freed), when=format_timestamp(last.timestamp))
            )
            self.last_clean_label.setVisible(True)

    def on_cleaned(self, freed: int) -> None:
        self.last_clean_label.setText(tr("dash.last_freed", size=format_size(freed)))
        self.last_clean_label.setVisible(True)
        self.refresh_disk_info()

    # -------------------------------------------------------------- private
    def _on_drive_changed(self) -> None:
        self.refresh_disk_info()
        self.drive_changed.emit(self.current_drive())

