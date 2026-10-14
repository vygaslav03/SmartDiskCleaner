"""Фоновый мониторинг памяти: один долгоживущий поток, без Qt (тестируется отдельно).

Нагрузка самой программы держится низкой:
  * снимок системы — раз в 2 с (несколько системных вызовов, < 1 мс);
  * обход процессов — раз в 5 с, пока страница «Память» открыта, и раз в 15 с в фоне;
  * время, затраченное на сбор, измеряется и показывается пользователю.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.memory import analysis
from app.memory.collector import ProcessCollector, SystemCollector
from app.memory.history import EventLog, ProcessHistory, SystemHistory
from app.memory.models import Assessment, LeakSuspect, ProcessMemory, SpikeEvent, SystemMemory
from app.utils.logger import get_logger

log = get_logger("memory.monitor")


@dataclass(slots=True)
class MonitorState:
    system: SystemMemory | None = None
    assessment: Assessment | None = None
    processes: list[ProcessMemory] = field(default_factory=list)
    processes_at: float = 0.0
    leaks: list[LeakSuspect] = field(default_factory=list)
    idle: set = field(default_factory=set)
    cost_system_ms: float = 0.0
    cost_process_ms: float = 0.0


class MemoryMonitor:
    def __init__(
        self,
        system_collector: SystemCollector | None = None,
        process_collector: ProcessCollector | None = None,
        system_interval: float = 2.0,
        process_interval_fast: float = 5.0,
        process_interval_slow: float = 15.0,
        thresholds: analysis.Thresholds = analysis.DEFAULT,
    ) -> None:
        self._sys_collector = system_collector
        self._proc_collector = process_collector
        self.system_interval = system_interval
        self.process_interval_fast = process_interval_fast
        self.process_interval_slow = process_interval_slow
        self.cfg = thresholds
        self.system_history = SystemHistory(capacity=int(3600 / system_interval))  # 1 час
        self.process_history = ProcessHistory(capacity=int(1800 / process_interval_fast) + 1)
        self.events = EventLog()
        self._state = MonitorState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._fast = False
        self._force_processes = False
        self._thread: threading.Thread | None = None
        self._listeners: list[Callable[[MonitorState], None]] = []
        self._prev_footprint: dict = {}
        self._prev_cpu: dict = {}
        self._prev_proc_time = 0.0
        self._last_proc_scan = 0.0

    # ----------------------------------------------------------------- control
    def add_listener(self, fn: Callable[[MonitorState], None]) -> None:
        self._listeners.append(fn)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="memory-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        if self._sys_collector is not None:
            self._sys_collector.close()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def set_fast(self, fast: bool) -> None:
        """Быстрый опрос процессов — пока страница видна."""
        self._fast = fast
        if fast:
            self._wake.set()

    def request_processes(self) -> None:
        """Немедленный обход процессов (кнопка «Быстрый анализ»)."""
        self._force_processes = True
        self._wake.set()

    def state(self) -> MonitorState:
        with self._lock:
            s = self._state
            return MonitorState(s.system, s.assessment, list(s.processes), s.processes_at, list(s.leaks),
                                set(s.idle), s.cost_system_ms, s.cost_process_ms)

    # ----------------------------------------------------------------- loop
    def _run(self) -> None:
        try:
            if self._sys_collector is None:
                self._sys_collector = SystemCollector()
            if self._proc_collector is None:
                self._proc_collector = ProcessCollector()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось инициализировать сбор памяти")
            return
        while not self._stop.is_set():
            self.tick()
            self._wake.wait(self.system_interval)
            self._wake.clear()

    def tick(self, now: float | None = None) -> MonitorState:
        """Один шаг мониторинга (вызывается из потока; в тестах — напрямую)."""
        mono = time.monotonic() if now is None else now
        try:
            self._sample_system()
            interval = self.process_interval_fast if self._fast else self.process_interval_slow
            if self._force_processes or mono - self._last_proc_scan >= interval:
                self._force_processes = False
                self._last_proc_scan = mono
                self._sample_processes()
        except Exception:  # noqa: BLE001 - мониторинг не должен падать
            log.exception("Ошибка шага мониторинга")
        state = self.state()
        for fn in list(self._listeners):
            try:
                fn(state)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработчика мониторинга")
        return state

    def _sample_system(self) -> None:
        t0 = time.perf_counter()
        snap = self._sys_collector.snapshot()  # type: ignore[union-attr]
        self.system_history.add(snap)
        points = self.system_history.points()
        assessment = analysis.assess_system(snap, points, self.cfg)
        spike = analysis.detect_system_spike(points, snap.total, self.cfg)
        if spike is not None:
            self.events.add(spike)
        cost = (time.perf_counter() - t0) * 1000
        with self._lock:
            self._state.system = snap
            self._state.assessment = assessment
            self._state.cost_system_ms = _ema(self._state.cost_system_ms, cost)

    def _sample_processes(self) -> None:
        t0 = time.perf_counter()
        procs = self._proc_collector.scan()  # type: ignore[union-attr]
        ts = time.time()
        self.process_history.add(ts, procs)
        leaks = analysis.detect_leaks(self.process_history.items(), self.cfg)
        for ev in analysis.detect_process_spikes(self._prev_footprint, procs, ts, self.cfg):
            self.events.add(ev)
        mono = time.monotonic()
        idle = analysis.idle_processes(self._prev_cpu, procs, mono - self._prev_proc_time) \
            if self._prev_proc_time else set()
        self._prev_footprint = {p.key: p.footprint for p in procs}
        self._prev_cpu = {p.key: p.cpu_time for p in procs if p.cpu_time is not None}
        self._prev_proc_time = mono
        cost = (time.perf_counter() - t0) * 1000
        with self._lock:
            self._state.processes = procs
            self._state.processes_at = ts
            self._state.leaks = leaks
            self._state.idle = idle
            self._state.cost_process_ms = _ema(self._state.cost_process_ms, cost)

    def recent_spikes(self, limit: int = 50) -> list[SpikeEvent]:
        return self.events.items()[-limit:]


def _ema(prev: float, value: float, alpha: float = 0.3) -> float:
    return value if prev == 0 else prev + alpha * (value - prev)
