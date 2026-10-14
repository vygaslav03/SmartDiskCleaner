"""Страница «Память»: мониторинг, рекомендации и ручная оптимизация RAM.

Три режима (сохраняются в настройках):
  monitor   — только статистика; никаких советов и действий;
  recommend — анализ и предложения, система не меняется;
  manual    — выбранные действия выполняются после подтверждения.

Никакое действие не выполняется автоматически. Каждое — с подтверждением,
записью в журнал и замером «до/после» через 30 секунд.
"""

from __future__ import annotations

import math
import time
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QShowEvent, QHideEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.memory import actions as mem_actions
from app.memory import analysis
from app.memory.collector import private_working_set, read_pagefile_config
from app.memory.journal import JournalEntry, get_journal
from app.memory.models import ActionKind, Recommendation, Severity, SpikeEvent
from app.memory.monitor import MemoryMonitor, MonitorState
from app.memory.startup import StartupItem, list_system_startup
from app.ui.memory_widgets import CompositionBar, HistoryChart, RingGauge, neutral_color, series_colors, status_color
from app.ui.widgets import Card, StatCard, page_header
from app.ui.workers import Task
from app.utils.format_size import format_size, format_timestamp
from app.utils.i18n import tr
from app.utils.logger import get_logger
from app.utils.permissions import is_admin
from app.utils.settings_store import Settings, save_settings

log = get_logger("ui.memory")

MODES = ("monitor", "recommend", "manual")
AFTER_DELAY_MS = 30_000
SIZE_KEYS = {"rate", "growth", "size", "standby", "free", "peak", "limit"}
PCT_KEYS = {"pct", "avail_pct"}


def fmt_params(params: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k in SIZE_KEYS and isinstance(v, (int, float)):
            out[k] = format_size(v, 1)
        elif k in PCT_KEYS and isinstance(v, (int, float)):
            out[k] = f"{v:.0f}"
        elif k == "minutes":
            out[k] = f"{v:.0f}"
        else:
            out[k] = v
    return out


def severity_title(sev: Severity) -> str:
    return tr(f"mem.status.{int(sev)}")


def finding_text(code: str, params: dict) -> str:
    return tr(f"mem.f.{code}", **fmt_params(params))


def rec_text(rec: Recommendation) -> tuple[str, str]:
    """(заголовок, пояснение) рекомендации."""
    title = tr(f"mem.rec.{rec.code}.title", name=rec.target_name, **fmt_params(rec.params))
    body = tr(f"mem.rec.{rec.code}.body", name=rec.target_name, **fmt_params(rec.params))
    return title, body


class SizeItem(QTableWidgetItem):
    """Ячейка размера: показывает текст, сортируется по числу."""

    def __init__(self, value: float | None, text: str | None = None) -> None:
        super().__init__(text if text is not None else ("—" if value is None else format_size(value, 1)))
        self.value = -1.0 if value is None else float(value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, SizeItem):
            return self.value < other.value
        return super().__lt__(other)


class _Bridge(QObject):
    updated = Signal(object)


def _run_actions(recs: list[Recommendation], startup: dict[str, StartupItem], admin: bool, snapshot,
                 progress_cb=None, cancel_event=None) -> list[tuple[Recommendation, JournalEntry | None, str]]:
    """Выполняется в фоновом потоке. Возвращает [(рекомендация, запись журнала, ошибка)]."""
    results = []
    for rec in recs:
        if cancel_event is not None and cancel_event.is_set():
            break
        if progress_cb:
            progress_cb(rec.target_name or rec.kind)
        try:
            if rec.kind == ActionKind.CLOSE_APP:
                entry = mem_actions.close_app(rec.target_pid, rec.target_create_time)
            elif rec.kind == ActionKind.TRIM:
                entry = mem_actions.trim_working_set(rec.target_pid, rec.target_create_time)
            elif rec.kind == ActionKind.STARTUP_DISABLE:
                item = startup.get(rec.startup_id)
                if item is None:
                    raise mem_actions.ActionRefused("gone")
                entry = mem_actions.set_startup(item, False, admin)
            elif rec.kind == ActionKind.PURGE_STANDBY:
                entry = mem_actions.purge_standby(True, snapshot, admin)
            else:
                continue
            results.append((rec, entry, ""))
        except mem_actions.ActionRefused as exc:
            results.append((rec, None, tr(f"mem.refused.{exc.args[0]}")))
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка действия %s", rec.kind)
            results.append((rec, None, f"{type(exc).__name__}: {exc}"))
    return results


class MemoryPage(QWidget):
    def __init__(self, settings: Settings, monitor: MemoryMonitor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.monitor = monitor
        self.admin = is_admin()
        self._bridge = _Bridge(self)
        self._bridge.updated.connect(self._on_state)
        monitor.add_listener(self._bridge.updated.emit)
        self._state: MonitorState | None = None
        self._procs_at = 0.0
        self._startup: dict[str, StartupItem] = {}
        self._pagefile = None
        self._recs: list[Recommendation] = []
        self._analysis_requested_at: float | None = None
        self._task: Task | None = None
        self._before: tuple | None = None
        self.busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 16)
        root.setSpacing(12)
        header, _ = page_header(tr("mem.title"), tr("mem.subtitle"))
        root.addWidget(header)

        # ---- режимы и кнопки
        bar = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for i, mode in enumerate(MODES):
            b = QPushButton(tr(f"mem.mode.{mode}"))
            b.setObjectName("SegButton")
            b.setCheckable(True)
            b.setToolTip(tr(f"mem.mode.{mode}.tip"))
            self.mode_group.addButton(b, i)
            bar.addWidget(b)
        self.mode_group.idClicked.connect(lambda i: self.set_mode(MODES[i]))
        bar.addStretch(1)
        self.analyze_btn = QPushButton(tr("mem.btn.analyze"))
        self.analyze_btn.setObjectName("Primary")
        self.analyze_btn.clicked.connect(self.quick_analysis)
        self.optimize_btn = QPushButton(tr("mem.btn.optimize"))
        self.optimize_btn.clicked.connect(self.safe_optimization)
        bar.addWidget(self.analyze_btn)
        bar.addWidget(self.optimize_btn)
        root.addLayout(bar)

        # ---- статус
        status = Card()
        row = QHBoxLayout()
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(14, 14)
        self.status_title = QLabel(tr("mem.status.loading"))
        self.status_title.setObjectName("CardValue")
        row.addWidget(self.status_dot)
        row.addWidget(self.status_title, 1)
        status.layout_.addLayout(row)
        self.status_text = QLabel("")
        self.status_text.setWordWrap(True)
        self.status_text.setObjectName("Muted")
        status.layout_.addWidget(self.status_text)
        root.addWidget(status)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview(), tr("mem.tab.overview"))
        self.tabs.addTab(self._build_processes(), tr("mem.tab.processes"))
        self.tabs.addTab(self._build_startup(), tr("mem.tab.startup"))
        self.tabs.addTab(self._build_details(), tr("mem.tab.details"))
        self.tabs.addTab(self._build_journal(), tr("mem.tab.journal"))
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs, 1)

        self.set_mode(settings.memory_mode, save=False)
        self.refresh_journal()

    # ================================================================ build
    def _build_overview(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 12, 6, 6)
        lay.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(16)
        gauge_card = Card()
        self.gauge = RingGauge()
        gauge_card.layout_.addWidget(self.gauge, 0, Qt.AlignmentFlag.AlignCenter)
        top.addWidget(gauge_card)

        right = QVBoxLayout()
        right.setSpacing(12)
        stats = QGridLayout()
        stats.setHorizontalSpacing(12)
        self.total_card = StatCard(tr("mem.total"))
        self.used_card = StatCard(tr("mem.used"))
        self.avail_card = StatCard(tr("mem.available"))
        self.commit_card = StatCard(tr("mem.commit"))
        for i, c in enumerate((self.total_card, self.used_card, self.avail_card, self.commit_card)):
            stats.addWidget(c, 0, i)
        right.addLayout(stats)
        comp_card = Card()
        t = QLabel(tr("mem.composition"))
        t.setObjectName("CardTitle")
        comp_card.layout_.addWidget(t)
        self.composition = CompositionBar()
        comp_card.layout_.addWidget(self.composition)
        right.addWidget(comp_card)
        top.addLayout(right, 1)
        lay.addLayout(top)

        # график
        chart_card = Card()
        head = QHBoxLayout()
        ct = QLabel(tr("mem.chart.title"))
        ct.setObjectName("CardTitle")
        head.addWidget(ct)
        head.addStretch(1)
        self.range_group = QButtonGroup(self)
        for i, (label, sec) in enumerate(((tr("mem.range.5"), 300), (tr("mem.range.30"), 1800),
                                          (tr("mem.range.60"), 3600))):
            b = QPushButton(label)
            b.setObjectName("SegButton")
            b.setCheckable(True)
            b.setProperty("seconds", sec)
            self.range_group.addButton(b, i)
            head.addWidget(b)
        self.range_group.button(1).setChecked(True)
        self.range_group.idClicked.connect(self._on_range)
        chart_card.layout_.addLayout(head)
        self.chart = HistoryChart()
        self.chart.set_window(1800)
        chart_card.layout_.addWidget(self.chart)
        lay.addWidget(chart_card)

        # рекомендации
        self.recs_card = Card()
        rh = QHBoxLayout()
        self.recs_title = QLabel(tr("mem.recs.title"))
        self.recs_title.setObjectName("CardTitle")
        rh.addWidget(self.recs_title)
        rh.addStretch(1)
        self.execute_btn = QPushButton(tr("mem.btn.execute"))
        self.execute_btn.setObjectName("Primary")
        self.execute_btn.clicked.connect(self._execute_checked)
        rh.addWidget(self.execute_btn)
        self.recs_card.layout_.addLayout(rh)
        self.recs_hint = QLabel(tr("mem.recs.not_run"))
        self.recs_hint.setObjectName("Muted")
        self.recs_hint.setWordWrap(True)
        self.recs_card.layout_.addWidget(self.recs_hint)
        self.recs_list = QListWidget()
        self.recs_list.setWordWrap(True)
        self.recs_list.setMinimumHeight(240)
        self.recs_list.itemChanged.connect(lambda _i: self._update_execute_btn())
        self.recs_card.layout_.addWidget(self.recs_list)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setVisible(False)
        self.recs_card.layout_.addWidget(self.result_label)
        lay.addWidget(self.recs_card)

        # топ-10
        top_card = Card()
        tt = QLabel(tr("mem.top10"))
        tt.setObjectName("CardTitle")
        top_card.layout_.addWidget(tt)
        self.top_table = self._make_proc_table(top=True)
        self.top_table.setMinimumHeight(330)
        top_card.layout_.addWidget(self.top_table)
        lay.addWidget(top_card)
        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _make_proc_table(self, top: bool = False) -> QTableWidget:
        cols = [tr("mem.col.name"), tr("mem.col.private"), tr("mem.col.ws"), tr("mem.col.trend"),
                tr("mem.col.kind")]
        if not top:
            cols += [tr("mem.col.handles"), tr("mem.col.pid")]
        t = QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setSortingEnabled(not top)
        h = t.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(cols)):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos, tbl=t: self._proc_menu(tbl, pos))
        return t

    def _build_processes(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 12, 0, 0)
        row = QHBoxLayout()
        self.proc_filter = QLineEdit()
        self.proc_filter.setPlaceholderText(tr("mem.filter"))
        self.proc_filter.textChanged.connect(lambda _t: self._fill_processes(force=True))
        self.show_system = QCheckBox(tr("mem.show_system"))
        self.show_system.setChecked(True)
        self.show_system.toggled.connect(lambda _v: self._fill_processes(force=True))
        row.addWidget(self.proc_filter, 1)
        row.addWidget(self.show_system)
        lay.addLayout(row)
        self.proc_hint = QLabel(tr("mem.proc_hint"))
        self.proc_hint.setObjectName("Muted")
        self.proc_hint.setWordWrap(True)
        lay.addWidget(self.proc_hint)
        self.proc_table = self._make_proc_table()
        self.proc_table.horizontalHeader().setSortIndicator(1, Qt.SortOrder.DescendingOrder)
        lay.addWidget(self.proc_table, 1)
        return w

    def _build_startup(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 12, 0, 0)
        hint = QLabel(tr("mem.startup.hint"))
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        cols = [tr("mem.st.name"), tr("mem.st.state"), tr("mem.st.memory"), tr("mem.st.where"), tr("mem.st.command")]
        self.startup_table = QTableWidget(0, len(cols))
        self.startup_table.setHorizontalHeaderLabels(cols)
        self.startup_table.verticalHeader().setVisible(False)
        self.startup_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.startup_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.startup_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.startup_table.setAlternatingRowColors(True)
        self.startup_table.setWordWrap(False)
        self.startup_table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        h = self.startup_table.horizontalHeader()
        for i in range(4):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.startup_table.itemSelectionChanged.connect(self._update_startup_btn)
        lay.addWidget(self.startup_table, 1)
        row = QHBoxLayout()
        self.startup_refresh = QPushButton(tr("btn.refresh"))
        self.startup_refresh.clicked.connect(self.load_startup)
        self.startup_toggle = QPushButton(tr("mem.st.disable"))
        self.startup_toggle.clicked.connect(self._toggle_startup)
        row.addWidget(self.startup_refresh)
        row.addStretch(1)
        row.addWidget(self.startup_toggle)
        lay.addLayout(row)
        return w

    def _build_details(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 12, 6, 6)
        lay.setSpacing(12)
        self.detail_labels: dict[str, QLabel] = {}

        def section(title_key: str, keys: list[str]) -> None:
            card = Card()
            t = QLabel(tr(title_key))
            t.setObjectName("CardTitle")
            card.layout_.addWidget(t)
            form = QFormLayout()
            form.setHorizontalSpacing(24)
            for k in keys:
                lbl = QLabel("—")
                lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                name = QLabel(tr(f"mem.d.{k}"))
                name.setMinimumWidth(260)  # одинаковая колонка подписей во всех карточках
                name.setToolTip(tr(f"mem.d.{k}.tip"))
                lbl.setToolTip(tr(f"mem.d.{k}.tip"))
                form.addRow(name, lbl)
                self.detail_labels[k] = lbl
            card.layout_.addLayout(form)
            lay.addWidget(card)

        section("mem.d.physical", ["total", "available", "load", "in_use", "modified", "standby", "standby_core",
                                   "standby_normal", "standby_reserve", "free_zero", "system_cache"])
        section("mem.d.virtual", ["commit", "commit_peak", "kernel_paged", "kernel_nonpaged", "hard_faults",
                                  "page_reads", "process_count"])
        section("mem.d.pagefile", ["pf_files", "pf_config", "pf_usage"])
        section("mem.d.self", ["self_mem", "self_cost", "source", "admin"])

        ev_card = Card()
        et = QLabel(tr("mem.d.events"))
        et.setObjectName("CardTitle")
        ev_card.layout_.addWidget(et)
        self.events_label = QLabel(tr("mem.d.no_events"))
        self.events_label.setWordWrap(True)
        ev_card.layout_.addWidget(self.events_label)
        lay.addWidget(ev_card)

        leak_card = Card()
        lt = QLabel(tr("mem.d.leaks"))
        lt.setObjectName("CardTitle")
        leak_card.layout_.addWidget(lt)
        self.leaks_label = QLabel(tr("mem.d.no_leaks"))
        self.leaks_label.setWordWrap(True)
        leak_card.layout_.addWidget(self.leaks_label)
        lay.addWidget(leak_card)
        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _build_journal(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 12, 0, 0)
        hint = QLabel(tr("mem.j.hint"))
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        cols = [tr("mem.j.date"), tr("mem.j.action"), tr("mem.j.target"), tr("mem.j.result"), tr("mem.j.change"),
                tr("mem.j.undo_col")]
        self.journal_table = QTableWidget(0, len(cols))
        self.journal_table.setHorizontalHeaderLabels(cols)
        self.journal_table.verticalHeader().setVisible(False)
        self.journal_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.journal_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.journal_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.journal_table.setAlternatingRowColors(True)
        h = self.journal_table.horizontalHeader()
        for i in range(len(cols)):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.journal_table.itemSelectionChanged.connect(self._update_undo_btn)
        lay.addWidget(self.journal_table, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.undo_btn = QPushButton(tr("mem.j.undo"))
        self.undo_btn.clicked.connect(self._undo_selected)
        self.undo_btn.setEnabled(False)
        row.addWidget(self.undo_btn)
        lay.addLayout(row)
        return w

    # ================================================================ modes
    @property
    def mode(self) -> str:
        return self.settings.memory_mode

    def set_mode(self, mode: str, save: bool = True) -> None:
        if mode not in MODES:
            mode = "monitor"
        self.settings.memory_mode = mode
        self.mode_group.button(MODES.index(mode)).setChecked(True)
        self.recs_card.setVisible(mode != "monitor")
        self.execute_btn.setVisible(mode == "manual")
        self.startup_toggle.setVisible(mode == "manual")
        self.proc_hint.setText(tr("mem.proc_hint_manual" if mode == "manual" else "mem.proc_hint"))
        self._fill_recs()
        if save:
            save_settings(self.settings)
            log.info("Режим памяти: %s", mode)

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.set_mode(settings.memory_mode, save=False)

    # ================================================================ lifecycle
    def showEvent(self, e: QShowEvent) -> None:
        super().showEvent(e)
        self.monitor.set_fast(True)
        if not self.monitor.running:
            self.monitor.start()
        if self._state is not None:
            self._on_state(self._state)

    def hideEvent(self, e: QHideEvent) -> None:
        super().hideEvent(e)
        self.monitor.set_fast(False)
        if not self.settings.memory_background:
            self.monitor.stop()

    def shutdown(self) -> None:
        if self._task is not None and self._task.running:
            self._task.shutdown()

    # ================================================================ state
    def _on_state(self, state: MonitorState) -> None:
        self._state = state
        if not self.isVisible():
            return
        self._fill_status(state)
        self._fill_overview(state)
        if state.processes_at != self._procs_at:
            self._procs_at = state.processes_at
            self._fill_top(state)
            if self.tabs.currentIndex() == 1:
                self._fill_processes()
            if self._analysis_requested_at is not None and state.processes_at >= self._analysis_requested_at:
                self._analysis_requested_at = None
                self._finish_analysis()
        if self.tabs.currentIndex() == 3:
            self._fill_details(state)

    def _on_tab(self, index: int) -> None:
        if self._state is None:
            return
        if index == 1:
            self._fill_processes(force=True)
        elif index == 2 and not self._startup:
            self.load_startup()
        elif index == 3:
            self._fill_details(self._state)
        elif index == 4:
            self.refresh_journal()

    def _fill_status(self, state: MonitorState) -> None:
        a = state.assessment
        if a is None:
            return
        color = status_color(int(a.severity))
        self.status_dot.setStyleSheet(f"background-color: {color.name()}; border-radius: 7px;")
        self.status_title.setText(severity_title(a.severity))
        main = [f for f in a.findings if f.code not in ("no_fault_data",)]
        lines = [finding_text(f.code, f.params) for f in main[:3]]
        if any(f.code == "no_fault_data" for f in a.findings) and len(lines) < 3:
            lines.append(finding_text("no_fault_data", {}))
        self.status_text.setText("\n".join(lines))

    def _fill_overview(self, state: MonitorState) -> None:
        s = state.system
        if s is None:
            return
        in_use = s.in_use if s.in_use is not None else s.used
        self.gauge.set_value(100 * in_use / s.total if s.total else 0, tr("mem.gauge.in_use"),
                             format_size(in_use, 1))
        self.total_card.set_value(format_size(s.total, 1))
        self.used_card.set_value(format_size(s.used, 1))
        self.avail_card.set_value(format_size(s.available, 1))
        self.commit_card.set_value(f"{format_size(s.commit_total, 1)} / {format_size(s.commit_limit, 1)}")
        self.commit_card.setToolTip(tr("mem.d.commit.tip"))
        colors = series_colors()
        if s.has_lists:
            parts = [
                (tr("mem.part.in_use"), s.in_use or 0, colors[0], tr("mem.part.in_use.tip")),
                (tr("mem.part.modified"), s.modified or 0, colors[1], tr("mem.part.modified.tip")),
                (tr("mem.part.standby"), s.standby or 0, colors[2], tr("mem.part.standby.tip")),
                (tr("mem.part.free"), s.free_zero or 0, neutral_color(), tr("mem.part.free.tip")),
            ]
        else:
            parts = [
                (tr("mem.part.used"), s.used, colors[0], tr("mem.part.used.tip")),
                (tr("mem.part.available"), s.available, neutral_color(), tr("mem.part.available.tip")),
            ]
        self.composition.set_parts(parts, s.total)
        h = self.monitor.system_history
        spikes = [e.timestamp for e in self.monitor.recent_spikes() if e.scope == "system"]
        self.chart.set_data(h.series("t"), [(tr("mem.series.in_use"), h.series("in_use_pct")),
                                           (tr("mem.series.commit"), h.series("commit_pct"))], spikes)

    def _on_range(self, idx: int) -> None:
        self.chart.set_window(float(self.range_group.button(idx).property("seconds")))

    # ---------------------------------------------------------------- processes
    def _trend(self, key) -> float | None:
        data = self.monitor.process_history.series_for(key)
        if not data or len(data[0]) < 2:
            return None
        ts, vs = data
        cutoff = ts[-1] - 300
        first = next((v for t, v in zip(ts, vs) if t >= cutoff), vs[0])
        return vs[-1] - first

    def _kind_text(self, p, leak_keys: set) -> str:
        if p.key in leak_keys:
            return tr("mem.kind.leak")
        if p.protected and not p.owned:
            return tr("mem.kind.system")
        if p.protected:
            return tr("mem.kind.protected")
        return tr("mem.kind.app") if p.has_window else tr("mem.kind.background")

    def _fill_rows(self, table: QTableWidget, procs, leak_keys: set, extra: bool) -> None:
        selected = self._selected_key(table)
        sorting = table.isSortingEnabled()
        table.setSortingEnabled(False)
        table.setRowCount(len(procs))
        for row, p in enumerate(procs):
            name = QTableWidgetItem(p.name + (f"  [{', '.join(p.services[:3])}]" if p.services else ""))
            name.setData(Qt.ItemDataRole.UserRole, (p.pid, p.create_time))
            name.setToolTip(p.exe or p.name)
            table.setItem(row, 0, name)
            table.setItem(row, 1, SizeItem(p.private_bytes))
            table.setItem(row, 2, SizeItem(p.working_set))
            trend = self._trend(p.key)
            table.setItem(row, 3, SizeItem(trend, _signed_size(trend)))
            kind = QTableWidgetItem(self._kind_text(p, leak_keys))
            if p.key in leak_keys:
                kind.setForeground(QColor(status_color(3)))
            table.setItem(row, 4, kind)
            if extra:
                table.setItem(row, 5, SizeItem(p.handles, "—" if p.handles is None else str(p.handles)))
                table.setItem(row, 6, SizeItem(p.pid, str(p.pid)))
        table.setSortingEnabled(sorting)
        if selected is not None:
            for row in range(table.rowCount()):
                it = table.item(row, 0)
                if it and tuple(it.data(Qt.ItemDataRole.UserRole)) == selected:
                    table.selectRow(row)
                    break

    def _fill_top(self, state: MonitorState) -> None:
        procs = sorted(state.processes, key=lambda p: p.footprint, reverse=True)[:10]
        self._fill_rows(self.top_table, procs, {(l.pid, round(l.create_time, 3)) for l in state.leaks}, False)

    def _fill_processes(self, force: bool = False) -> None:
        if self._state is None:
            return
        text = self.proc_filter.text().strip().lower()
        procs = [p for p in self._state.processes
                 if (not text or text in p.name.lower() or any(text in s.lower() for s in p.services))
                 and (self.show_system.isChecked() or not p.protected)]
        self._fill_rows(self.proc_table, procs, {(l.pid, round(l.create_time, 3)) for l in self._state.leaks}, True)

    @staticmethod
    def _selected_key(table: QTableWidget):
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        if not rows:
            return None
        it = table.item(rows[0].row(), 0)
        return tuple(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def _proc_by_key(self, key):
        if self._state is None or key is None:
            return None
        return next((p for p in self._state.processes if (p.pid, p.create_time) == key), None)

    def _proc_menu(self, table: QTableWidget, pos) -> None:
        item = table.itemAt(pos)
        if item is None:
            return
        table.selectRow(item.row())
        p = self._proc_by_key(self._selected_key(table))
        if p is None:
            return
        menu = QMenu(self)
        pws = QAction(tr("mem.menu.private_ws"), menu)
        pws.triggered.connect(lambda: self._show_private_ws(p))
        menu.addAction(pws)
        if self.mode == "manual":
            menu.addSeparator()
            for key, handler in (("mem.menu.close", self._close_process), ("mem.menu.trim", self._trim_process),
                                 ("mem.menu.terminate", self._terminate_process)):
                act = QAction(tr(key), menu)
                act.setEnabled(not p.protected and not self.busy)
                act.triggered.connect(lambda _c=False, h=handler: h(p))
                menu.addAction(act)
            if p.protected:
                note = QAction(tr("mem.menu.protected"), menu)
                note.setEnabled(False)
                menu.addAction(note)
        else:
            note = QAction(tr("mem.menu.manual_only"), menu)
            note.setEnabled(False)
            menu.addAction(note)
        menu.exec(table.viewport().mapToGlobal(pos))

    def _show_private_ws(self, p) -> None:
        uss = private_working_set(p.pid)
        QMessageBox.information(self, p.name, tr("mem.private_ws_value", name=p.name,
                                                 value="—" if uss is None else format_size(uss)))

    def _rec_for(self, p, kind: str) -> Recommendation:
        return Recommendation(kind, "manual", {}, p.pid, p.create_time, p.name)

    def _close_process(self, p) -> None:
        if self._confirm(tr("mem.confirm.close", name=p.name)):
            self._run([self._rec_for(p, ActionKind.CLOSE_APP)])

    def _trim_process(self, p) -> None:
        if self._confirm(tr("mem.confirm.trim", name=p.name, size=format_size(p.working_set, 1))):
            self._run([self._rec_for(p, ActionKind.TRIM)])

    def _terminate_process(self, p) -> None:
        if not self._confirm(tr("mem.confirm.terminate", name=p.name)):
            return
        if not self._confirm(tr("mem.confirm.terminate2", name=p.name), danger=True):
            return
        self._set_busy(True)
        self._task = Task(self, lambda progress_cb=None, cancel_event=None: self._terminate_worker(p),
                          on_finished=self._on_actions_done, on_failed=self._on_actions_failed)

    @staticmethod
    def _terminate_worker(p):
        rec = Recommendation(ActionKind.TERMINATE, "manual", {}, p.pid, p.create_time, p.name)
        try:
            return [(rec, mem_actions.terminate(p.pid, p.create_time), "")]
        except mem_actions.ActionRefused as exc:
            return [(rec, None, tr(f"mem.refused.{exc.args[0]}"))]

    # ---------------------------------------------------------------- analysis / recs
    def quick_analysis(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.recs_hint.setText(tr("mem.recs.analyzing"))
        self._analysis_requested_at = time.time()
        self.monitor.request_processes()
        self._task = Task(self, _load_static, on_finished=self._on_static_loaded, on_failed=self._on_actions_failed)

    def _on_static_loaded(self, result) -> None:
        startup, pagefile = result
        self._startup = {i.id: i for i in startup}
        self._pagefile = pagefile
        if pagefile is not None:
            self.monitor.pagefile = pagefile
        self._fill_startup()
        if self._analysis_requested_at is None:
            self._finish_analysis()

    def _finish_analysis(self) -> None:
        if self._task is not None and self._task.running:
            return  # дождёмся загрузки автозагрузки/файла подкачки
        st = self.monitor.state()
        if st.system is None or st.assessment is None:
            self._set_busy(False)
            return
        self._recs = analysis.build_recommendations(
            st.system, st.assessment, st.processes, st.leaks, list(self._startup.values()), self._pagefile,
            st.idle, self.monitor.cfg)
        self._recs_at = time.time()
        log.info("Быстрый анализ: статус %s, рекомендаций %d", st.assessment.severity.name, len(self._recs))
        self._fill_recs()
        self._set_busy(False)
        if self.mode == "monitor":
            self.status_text.setText(self.status_text.text() + "\n" + tr("mem.recs.mode_hint", count=len(self._recs)))

    def _fill_recs(self) -> None:
        self.recs_list.blockSignals(True)
        self.recs_list.clear()
        if not hasattr(self, "_recs_at"):
            self.recs_hint.setText(tr("mem.recs.not_run"))
        elif not self._recs:
            self.recs_hint.setText(tr("mem.recs.none", when=time.strftime("%H:%M:%S", time.localtime(self._recs_at))))
        else:
            self.recs_hint.setText(tr("mem.recs.found", count=len(self._recs),
                                      when=time.strftime("%H:%M:%S", time.localtime(self._recs_at))))
        manual = self.mode == "manual"
        for rec in self._recs:
            title, body = rec_text(rec)
            notes = []
            if not rec.executable:
                notes.append(tr("mem.rec.advice_only"))
            if rec.needs_admin and not self.admin:
                notes.append(tr("mem.rec.needs_admin"))
            notes.append(tr("mem.rec.reversible") if rec.reversible else tr(f"mem.rec.effect.{rec.kind}"))
            item = QListWidgetItem(f"{title}\n{body}\n— " + " · ".join(notes))
            item.setData(Qt.ItemDataRole.UserRole, rec)
            can_run = manual and rec.executable and (self.admin or not rec.needs_admin)
            if can_run:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)  # ничего не отмечено заранее
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.recs_list.addItem(item)
        self.recs_list.setVisible(bool(self._recs))
        self.recs_list.blockSignals(False)
        self._update_execute_btn()

    def _checked_recs(self) -> list[Recommendation]:
        out = []
        for i in range(self.recs_list.count()):
            it = self.recs_list.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable and it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def _update_execute_btn(self) -> None:
        self.execute_btn.setEnabled(not self.busy and bool(self._checked_recs()))

    def safe_optimization(self) -> None:
        """Безопасная оптимизация = анализ + список предложений; ничего не выполняется без выбора."""
        if self.mode != "manual":
            if not self._confirm(tr("mem.confirm.switch_manual")):
                return
            self.set_mode("manual")
        self.tabs.setCurrentIndex(0)
        self.quick_analysis()

    def _execute_checked(self) -> None:
        recs = self._checked_recs()
        if not recs:
            return
        lines = []
        for r in recs:
            title, _ = rec_text(r)
            lines.append(f"• {title} — {tr(f'mem.rec.effect.{r.kind}') if not r.reversible else tr('mem.rec.reversible')}")
        if not self._confirm(tr("mem.confirm.execute", count=len(recs)) + "\n\n" + "\n".join(lines)):
            return
        self._run(recs)

    def _run(self, recs: list[Recommendation]) -> None:
        if self.busy:
            return
        st = self.monitor.state()
        faults = analysis.sustained_hard_faults(self.monitor.system_history.points(), 30)
        self._before = (st.system, faults)
        self._set_busy(True)
        snapshot = lambda: self.monitor.state().system  # noqa: E731
        self._task = Task(self, _run_actions, recs, dict(self._startup), self.admin, snapshot,
                          on_finished=self._on_actions_done, on_failed=self._on_actions_failed)

    def _on_actions_done(self, results) -> None:
        self._set_busy(False)
        ok = [e for _r, e, err in results if e is not None and e.ok]
        lines = []
        for rec, entry, err in results:
            name = rec.target_name or tr(f"mem.action.{rec.kind}")
            if entry is None:
                lines.append(f"✗ {name}: {err}")
            else:
                mark = "✓" if entry.ok else "✗"
                lines.append(f"{mark} {name}: {tr(f'mem.result.{entry.message}') if entry.message in RESULT_KEYS else entry.message or tr('mem.result.done')}")
        self._result_lines = lines
        if ok and self._before and self._before[0] is not None:
            QTimer.singleShot(AFTER_DELAY_MS, self._measure_after)
            lines = lines + [tr("mem.after.wait", sec=AFTER_DELAY_MS // 1000)]
        else:
            self._before = None
        self.result_label.setText("\n".join(lines))
        self.result_label.setVisible(True)
        self.refresh_journal()
        if any(r.kind in (ActionKind.STARTUP_DISABLE,) for r, *_ in results):
            self._fill_startup()
        # выполненные рекомендации убираем из списка
        done = {id(r) for r, e, _err in results if e is not None and e.ok}
        self._recs = [r for r in self._recs if id(r) not in done]
        self._fill_recs()
        self.monitor.request_processes()

    def _on_actions_failed(self, error: str) -> None:
        self._set_busy(False)
        QMessageBox.warning(self, tr("mem.title"), error)

    def _measure_after(self) -> None:
        if not self._before:
            return
        before, faults_before = self._before
        after = self.monitor.state().system
        faults_after = analysis.sustained_hard_faults(self.monitor.system_history.points(), 30)
        if after is None or before is None:
            return
        rows = [tr("mem.after.title")]
        for d in analysis.compare(before, after, faults_before, faults_after):
            if d.before is None or d.after is None:
                continue
            if d.key == "hard_faults":
                b = tr("mem.mbs", v=f"{d.before * 4096 / 1048576:.1f}")
                a = tr("mem.mbs", v=f"{d.after * 4096 / 1048576:.1f}")
            else:
                b, a = format_size(d.before, 1), format_size(d.after, 1)
            rows.append(f"{tr(f'mem.after.{d.key}')}: {b} → {a}")
        rows.append(tr("mem.after.note"))
        self.result_label.setText("\n".join(getattr(self, "_result_lines", [])) + "\n\n" + "\n".join(rows))
        self._before = None

    # ---------------------------------------------------------------- startup
    def load_startup(self) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._task = Task(self, _load_static, on_finished=self._on_startup_loaded, on_failed=self._on_actions_failed)

    def _on_startup_loaded(self, result) -> None:
        self._set_busy(False)
        startup, pagefile = result
        self._startup = {i.id: i for i in startup}
        self._pagefile = pagefile
        if pagefile is not None:
            self.monitor.pagefile = pagefile
        self._fill_startup()

    def _fill_startup(self) -> None:
        procs = self._state.processes if self._state else []
        by_name: dict[str, int] = {}
        for p in procs:
            by_name[p.name.lower()] = by_name.get(p.name.lower(), 0) + p.footprint
        items = sorted(self._startup.values(), key=lambda i: (not i.enabled, i.name.lower()))
        t = self.startup_table
        t.setRowCount(len(items))
        for row, it in enumerate(items):
            name = QTableWidgetItem(it.name)
            name.setData(Qt.ItemDataRole.UserRole, it.id)
            t.setItem(row, 0, name)
            t.setItem(row, 1, QTableWidgetItem(tr("mem.st.enabled") if it.enabled else tr("mem.st.disabled")))
            mem = by_name.get(it.exe_name.lower()) if it.exe_name else None
            t.setItem(row, 2, SizeItem(mem, "—" if not mem else format_size(mem, 1)))
            where = tr(f"mem.st.loc.{it.location}") + (f" ({tr('mem.st.admin')})" if it.needs_admin else "")
            t.setItem(row, 3, QTableWidgetItem(where))
            cmd = QTableWidgetItem(it.command)
            cmd.setToolTip(it.command)
            t.setItem(row, 4, cmd)
        self._update_startup_btn()

    def _selected_startup(self) -> StartupItem | None:
        rows = self.startup_table.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.startup_table.item(rows[0].row(), 0)
        return self._startup.get(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def _update_startup_btn(self) -> None:
        item = self._selected_startup()
        self.startup_toggle.setEnabled(item is not None and not self.busy and (self.admin or not item.needs_admin))
        self.startup_toggle.setText(tr("mem.st.enable") if item is not None and not item.enabled
                                    else tr("mem.st.disable"))

    def _toggle_startup(self) -> None:
        item = self._selected_startup()
        if item is None:
            return
        enable = not item.enabled
        if not self._confirm(tr("mem.confirm.startup_enable" if enable else "mem.confirm.startup_disable",
                                name=item.name)):
            return
        try:
            entry = mem_actions.set_startup(item, enable, self.admin)
        except mem_actions.ActionRefused as exc:
            QMessageBox.warning(self, tr("mem.title"), tr(f"mem.refused.{exc.args[0]}"))
            return
        if not entry.ok:
            QMessageBox.warning(self, tr("mem.title"), entry.message)
        self._fill_startup()
        self.refresh_journal()

    # ---------------------------------------------------------------- details
    def _fill_details(self, state: MonitorState) -> None:
        s = state.system
        if s is None:
            return
        L = self.detail_labels

        def size(v):
            return "—" if v is None else format_size(v)

        L["total"].setText(size(s.total))
        L["available"].setText(size(s.available))
        L["load"].setText(f"{s.load_percent:.0f}%")
        L["in_use"].setText(size(s.in_use))
        L["modified"].setText(size(s.modified))
        L["standby"].setText(size(s.standby))
        L["standby_core"].setText(size(s.standby_core))
        L["standby_normal"].setText(size(s.standby_normal))
        L["standby_reserve"].setText(size(s.standby_reserve))
        L["free_zero"].setText(size(s.free_zero))
        L["system_cache"].setText(size(s.system_cache))
        L["commit"].setText(f"{size(s.commit_total)} / {size(s.commit_limit)} ({100 * s.commit_ratio:.0f}%)")
        L["commit_peak"].setText(size(s.commit_peak))
        L["kernel_paged"].setText(size(s.kernel_paged))
        L["kernel_nonpaged"].setText(size(s.kernel_nonpaged))
        hf = analysis.sustained_hard_faults(self.monitor.system_history.points(), 30)
        L["hard_faults"].setText("—" if s.hard_faults_per_sec is None else
                                 tr("mem.d.hf_value", now=f"{s.hard_faults_per_sec:.0f}",
                                    mbs=f"{s.hard_faults_per_sec * 4096 / 1048576:.1f}",
                                    median="—" if hf is None else f"{hf:.0f}"))
        L["page_reads"].setText("—" if s.page_reads_per_sec is None else f"{s.page_reads_per_sec:.0f}/s")
        L["process_count"].setText("—" if s.process_count is None else str(s.process_count))
        pf = self._pagefile or self.monitor.pagefile
        if pf is None:
            L["pf_files"].setText(tr("mem.d.pf_not_loaded"))
            L["pf_config"].setText("—")
        else:
            L["pf_files"].setText("; ".join(f"{f.name}: {format_size(f.total, 1)} ({tr('mem.d.pf_used')} "
                                            f"{format_size(f.in_use, 1)}, {tr('mem.d.pf_peak')} {format_size(f.peak, 1)})"
                                            for f in pf.files) or tr("mem.d.pf_none"))
            if not pf.known:
                L["pf_config"].setText("—")
            elif pf.disabled:
                L["pf_config"].setText(tr("mem.d.pf_disabled"))
            else:
                L["pf_config"].setText((tr("mem.d.pf_system") if pf.system_managed else tr("mem.d.pf_manual"))
                                       + " — " + ", ".join(pf.configured))
        L["pf_usage"].setText("—" if s.pagefile_usage_percent is None else f"{s.pagefile_usage_percent:.1f}%")
        try:
            from app.memory.collector import own_process_memory

            ws, private = own_process_memory()
            L["self_mem"].setText(tr("mem.d.self_value", ws=format_size(ws, 1), private=size(private)))
        except Exception:  # noqa: BLE001
            L["self_mem"].setText("—")
        L["self_cost"].setText(tr("mem.d.cost_value", sys=f"{state.cost_system_ms:.1f}",
                                  proc=f"{state.cost_process_ms:.0f}",
                                  sys_i=f"{self.monitor.system_interval:.0f}",
                                  proc_i=f"{self.monitor.process_interval_fast:.0f}",
                                  proc_bg=f"{self.monitor.process_interval_slow:.0f}"))
        L["source"].setText(tr(f"mem.d.source.{s.source}"))
        L["admin"].setText(tr("mem.d.admin_yes") if self.admin else tr("mem.d.admin_no"))

        events: list[SpikeEvent] = self.monitor.recent_spikes(15)
        if events:
            self.events_label.setText("\n".join(
                f"{time.strftime('%H:%M:%S', time.localtime(e.timestamp))} — "
                + (tr("mem.ev.system", delta=format_size(e.delta, 1)) if e.scope == "system"
                   else tr("mem.ev.process", name=e.name, delta=format_size(e.delta, 1)))
                for e in reversed(events)))
        else:
            self.events_label.setText(tr("mem.d.no_events"))
        if state.leaks:
            self.leaks_label.setText("\n".join(
                tr("mem.d.leak_row", name=l.name, pid=l.pid, rate=format_size(l.slope_bytes_per_hour, 1),
                   growth=format_size(l.growth, 1), minutes=f"{l.window_sec / 60:.0f}", r2=f"{l.r2:.2f}")
                for l in state.leaks))
        else:
            self.leaks_label.setText(tr("mem.d.no_leaks"))

    # ---------------------------------------------------------------- journal
    def refresh_journal(self) -> None:
        entries = get_journal().load()
        t = self.journal_table
        t.setRowCount(len(entries))
        for row, e in enumerate(entries):
            d = QTableWidgetItem(format_timestamp(e.timestamp))
            d.setData(Qt.ItemDataRole.UserRole, e)
            t.setItem(row, 0, d)
            t.setItem(row, 1, QTableWidgetItem(tr(f"mem.action.{e.kind}")))
            t.setItem(row, 2, QTableWidgetItem(e.target))
            res = ("✓ " if e.ok else "✗ ") + (tr(f"mem.result.{e.message}") if e.message in RESULT_KEYS
                                               else e.message or tr("mem.result.done"))
            t.setItem(row, 3, QTableWidgetItem(res))
            t.setItem(row, 4, QTableWidgetItem(_change_text(e)))
            t.setItem(row, 5, QTableWidgetItem(tr("mem.j.undone") if e.undone else
                                               (tr("mem.j.can_undo") if e.can_undo else tr("mem.j.no_undo"))))
        self._update_undo_btn()

    def _selected_journal(self) -> JournalEntry | None:
        rows = self.journal_table.selectionModel().selectedRows()
        if not rows:
            return None
        it = self.journal_table.item(rows[0].row(), 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _update_undo_btn(self) -> None:
        e = self._selected_journal()
        self.undo_btn.setEnabled(e is not None and e.can_undo)

    def _undo_selected(self) -> None:
        e = self._selected_journal()
        if e is None or not e.can_undo:
            return
        if not self._confirm(tr("mem.confirm.undo", target=e.target)):
            return
        try:
            mem_actions.undo_entry(e)
        except (mem_actions.ActionRefused, OSError) as exc:
            QMessageBox.warning(self, tr("mem.title"), str(exc))
        self.refresh_journal()
        if self._startup:
            self.load_startup()

    # ---------------------------------------------------------------- helpers
    def _confirm(self, text: str, danger: bool = False) -> bool:
        answer = QMessageBox.question(
            self, tr("mem.title"), text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # по умолчанию — «Нет»
        )
        return answer == QMessageBox.StandardButton.Yes

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.analyze_btn.setEnabled(not busy)
        self.optimize_btn.setEnabled(not busy)
        self.startup_refresh.setEnabled(not busy)
        self._update_execute_btn()
        self._update_startup_btn()


def _signed_size(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) < 1024 * 1024:
        return "0"
    return ("+" if v > 0 else "−") + format_size(abs(v), 1)


RESULT_KEYS = {"closed", "still_running", "no_window", "terminated", "trimmed", "access_denied", "low_priority", "all"}


def _change_text(e: JournalEntry) -> str:
    b, a = e.before or {}, e.after or {}
    parts = []
    for key in ("working_set", "standby", "available"):
        if b.get(key) is not None and a.get(key) is not None:
            parts.append(f"{tr(f'mem.after.{key}')}: {format_size(b[key], 1)} → {format_size(a[key], 1)}")
    if not parts:
        # закрытие/завершение: «после» нет — показываем, сколько занимал процесс
        if b.get("private_bytes"):
            parts.append(f"{tr('mem.col.private')}: {format_size(b['private_bytes'], 1)}")
        if b.get("working_set"):
            parts.append(f"{tr('mem.col.ws')}: {format_size(b['working_set'], 1)}")
    return "; ".join(parts) or "—"


def _load_static(progress_cb=None, cancel_event=None):
    """Автозагрузка и конфигурация файла подкачки (чтение реестра) — в фоне."""
    try:
        startup = list_system_startup()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать автозагрузку")
        startup = []
    try:
        pagefile = read_pagefile_config()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось прочитать настройки файла подкачки")
        pagefile = None
    return startup, pagefile
