"""Анализ памяти — чистые функции без обращений к ОС (полностью покрываются тестами).

Принцип: высокий процент «занятой» RAM сам по себе не проблема. Проблема — это
  * Commit Charge у предела (риск ошибок «недостаточно памяти»),
  * устойчивые чтения страниц с диска при малом объёме доступной памяти,
  * процессы, у которых долго и ровно растут private bytes (вероятная утечка).
Если ничего этого нет — честно говорим, что вмешательство не требуется.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.memory.history import SystemPoint
from app.memory.models import (
    MB,
    ActionKind,
    Assessment,
    Finding,
    LeakSuspect,
    PagefileConfig,
    ProcessMemory,
    Recommendation,
    Severity,
    SpikeEvent,
    SystemMemory,
)


@dataclass(frozen=True, slots=True)
class Thresholds:
    commit_warning: float = 0.85
    commit_critical: float = 0.95
    available_low_pct: float = 10.0
    available_critical_pct: float = 5.0
    # Чтения страниц с диска (hard faults), устойчиво за окно. 20 МБ/с ≈ 5000 страниц/с.
    paging_bytes_per_sec: float = 20 * MB
    paging_window_sec: float = 30.0
    cache_share_info: float = 0.25
    pagefile_usage_watch: float = 80.0
    # утечки
    leak_min_points: int = 20
    leak_min_window_sec: float = 600.0
    leak_window_sec: float = 1800.0
    leak_min_rate_per_hour: float = 50 * MB
    leak_min_rate_share: float = 0.05  # доля от текущего объёма в час
    leak_min_growth: float = 64 * MB
    leak_min_r2: float = 0.8
    leak_min_monotonic: float = 0.7
    # скачки
    spike_system_min: float = 512 * MB
    spike_system_share: float = 0.05
    spike_process_min: float = 300 * MB
    spike_process_share: float = 0.5
    spike_baseline_points: int = 15
    # рекомендации
    heavy_background: int = 300 * MB
    heavy_app: int = 1024 * MB
    heavy_startup: int = 150 * MB
    standby_purge_free_max_share: float = 0.005
    standby_purge_free_max: int = 64 * MB
    standby_purge_standby_share: float = 0.30


DEFAULT = Thresholds()


# ------------------------------------------------------------------ system


def recent(points: Sequence[SystemPoint], window_sec: float, now: float | None = None) -> list[SystemPoint]:
    if not points:
        return []
    end = now if now is not None else points[-1].t
    return [p for p in points if end - p.t <= window_sec]


def sustained_hard_faults(points: Sequence[SystemPoint], window_sec: float) -> float | None:
    """Медиана hard faults (страниц/с) за окно. Медиана, а не среднее: один всплеск
    (запуск программы) не должен выглядеть как постоянная нехватка памяти."""
    vals = [p.hard_faults for p in recent(points, window_sec) if not math.isnan(p.hard_faults)]
    if len(vals) < 3:
        return None
    return statistics.median(vals)


# Утилиты «очистки RAM»: принудительно выгружают рабочие наборы и/или сбрасывают кэш по таймеру.
# Это мешает Windows кэшировать данные и обычно добавляет чтения с диска.
RAM_CLEANERS = frozenset({
    "memreduct.exe", "wisememoryoptimizer.exe", "cleanmem.exe", "mini_monitor.exe", "ramrush.exe",
    "mzrambooster.exe", "memorycleaner.exe", "rambooster.exe", "ramsaverpro.exe", "ramsaver.exe",
})


def assess_system(snap: SystemMemory, points: Sequence[SystemPoint] = (), cfg: Thresholds = DEFAULT,
                  pagefile: PagefileConfig | None = None) -> Assessment:
    """pagefile — конфигурация файла подкачки: если размер «по выбору системы», Windows сама
    увеличит файл (и лимит commit) при нехватке, поэтому 85–95% — повод наблюдать, а не тревога."""
    findings: list[Finding] = []
    auto_grow = bool(pagefile and pagefile.known and pagefile.system_managed)
    avail_pct = 100.0 * snap.available / snap.total if snap.total else 100.0
    commit_pct = 100.0 * snap.commit_ratio

    if snap.commit_ratio >= cfg.commit_critical:
        # даже при авторасширении файл может не успеть вырасти — но это уже не «критично»
        sev = Severity.WARNING if auto_grow else Severity.CRITICAL
        findings.append(Finding("commit_critical", sev, {"pct": commit_pct}))
    elif snap.commit_ratio >= cfg.commit_warning:
        if auto_grow:
            findings.append(Finding("commit_high_auto", Severity.WATCH, {"pct": commit_pct}))
        else:
            findings.append(Finding("commit_high", Severity.WARNING, {"pct": commit_pct}))

    hf = sustained_hard_faults(points, cfg.paging_window_sec)
    hf_bytes = hf * 4096 if hf is not None else None
    paging = hf_bytes is not None and hf_bytes >= cfg.paging_bytes_per_sec
    if avail_pct < cfg.available_low_pct and paging:
        sev = Severity.CRITICAL if avail_pct < cfg.available_critical_pct else Severity.WARNING
        findings.append(Finding("paging_pressure", sev, {"avail_pct": avail_pct, "rate": hf_bytes}))
    elif avail_pct < cfg.available_low_pct:
        code = "low_available_ok" if hf_bytes is not None else "low_available"
        findings.append(Finding(code, Severity.WATCH, {"avail_pct": avail_pct}))
    elif paging:
        # Чтения с диска при достаточной памяти — обычно это загрузка программ/файлов, не нехватка RAM.
        findings.append(Finding("paging_not_memory", Severity.INFO, {"rate": hf_bytes}))

    if snap.pagefile_usage_percent is not None and snap.pagefile_usage_percent >= cfg.pagefile_usage_watch:
        findings.append(Finding("pagefile_busy", Severity.WATCH, {"pct": snap.pagefile_usage_percent}))

    if snap.has_lists and snap.total and snap.standby / snap.total >= cfg.cache_share_info:  # type: ignore[operator]
        findings.append(Finding("cache_large", Severity.INFO, {"standby": snap.standby}))
    if hf is None:
        findings.append(Finding("no_fault_data", Severity.INFO))

    worst = max((f.severity for f in findings), default=Severity.OK)
    findings.sort(key=lambda f: f.severity, reverse=True)
    if worst < Severity.WATCH:
        # главный вывод — первым; INFO-пояснения (кэш и т. п.) после него
        findings.insert(0, Finding("no_action_needed", Severity.OK, {"avail_pct": avail_pct}))
        worst = Severity.OK
    return Assessment(worst, findings)


# ------------------------------------------------------------------ leaks


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """(наклон, R²) методом наименьших квадратов. Для вырожденных данных — (0, 0)."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return slope, r2


def leak_score(times: Sequence[float], values: Sequence[float], cfg: Thresholds = DEFAULT):
    """Возвращает (slope_per_hour, r2, growth, window) или None, если признаков утечки нет."""
    if len(times) < cfg.leak_min_points:
        return None
    end = times[-1]
    pairs = [(t, v) for t, v in zip(times, values) if end - t <= cfg.leak_window_sec]
    if len(pairs) < cfg.leak_min_points:
        return None
    window = pairs[-1][0] - pairs[0][0]
    if window < cfg.leak_min_window_sec:
        return None
    ts = [t - pairs[0][0] for t, _ in pairs]
    vs = [v for _, v in pairs]
    slope, r2 = linear_fit(ts, vs)
    per_hour = slope * 3600
    head = statistics.median(vs[:3])
    tail = statistics.median(vs[-3:])
    growth = tail - head
    steps = [b - a for a, b in zip(vs, vs[1:])]
    tolerance = 0.01 * max(vs)
    monotonic = sum(1 for d in steps if d >= -tolerance) / len(steps)
    if (
        per_hour >= max(cfg.leak_min_rate_per_hour, cfg.leak_min_rate_share * tail)
        and growth >= cfg.leak_min_growth
        and r2 >= cfg.leak_min_r2
        and monotonic >= cfg.leak_min_monotonic
    ):
        return per_hour, r2, int(growth), window
    return None


def detect_leaks(series: Iterable[tuple[tuple[int, float], str, list[float], list[float]]],
                 cfg: Thresholds = DEFAULT) -> list[LeakSuspect]:
    suspects: list[LeakSuspect] = []
    for (pid, ctime), name, times, values in series:
        score = leak_score(times, values, cfg)
        if score is None:
            continue
        per_hour, r2, growth, window = score
        suspects.append(LeakSuspect(pid, name, ctime, per_hour, r2, growth, window, int(values[-1])))
    suspects.sort(key=lambda s: s.slope_bytes_per_hour, reverse=True)
    return suspects


# ------------------------------------------------------------------ spikes


def detect_system_spike(points: Sequence[SystemPoint], total: int, cfg: Thresholds = DEFAULT) -> SpikeEvent | None:
    """Скачок Commit Charge относительно медианы предыдущих точек."""
    if len(points) < cfg.spike_baseline_points + 1:
        return None
    base = statistics.median(p.commit for p in points[-cfg.spike_baseline_points - 1:-1])
    delta = points[-1].commit - base
    if delta >= max(cfg.spike_system_min, cfg.spike_system_share * total):
        return SpikeEvent(points[-1].t, "system", "commit", int(delta))
    return None


def detect_process_spikes(previous: dict[tuple[int, float], int], current: Sequence[ProcessMemory],
                          timestamp: float, cfg: Thresholds = DEFAULT) -> list[SpikeEvent]:
    events: list[SpikeEvent] = []
    for p in current:
        before = previous.get(p.key)
        if before is None:
            continue
        delta = p.footprint - before
        if delta >= max(cfg.spike_process_min, cfg.spike_process_share * before):
            events.append(SpikeEvent(timestamp, "process", p.name, int(delta), p.pid))
    return events


# ------------------------------------------------------------------ recommendations


def build_recommendations(
    snap: SystemMemory,
    assessment: Assessment,
    processes: Sequence[ProcessMemory],
    leaks: Sequence[LeakSuspect] = (),
    startup_items: Sequence = (),
    pagefile: PagefileConfig | None = None,
    idle_keys: set[tuple[int, float]] | None = None,
    cfg: Thresholds = DEFAULT,
) -> list[Recommendation]:
    """Список предложений. Ничего не выполняет. Под давлением памяти предлагает больше,
    без давления — только то, что полезно независимо от момента (утечки, автозагрузка,
    настройка файла подкачки)."""
    recs: list[Recommendation] = []
    under_pressure = assessment.severity >= Severity.WARNING
    by_key = {p.key: p for p in processes}
    used: set[tuple[int, float]] = set()

    # 1. Файл подкачки — только совет, ничего не меняем.
    if pagefile is not None and pagefile.known:
        if pagefile.disabled:
            recs.append(Recommendation(ActionKind.PAGEFILE_INFO, "pagefile_disabled", executable=False, priority=80))
        elif (pagefile.system_managed is False and snap.commit_peak
              and snap.commit_peak >= 0.9 * snap.commit_limit):
            recs.append(Recommendation(ActionKind.PAGEFILE_INFO, "pagefile_small", executable=False, priority=70,
                                       params={"peak": snap.commit_peak, "limit": snap.commit_limit}))

    # 2. Вероятные утечки.
    for leak in leaks:
        key = (leak.pid, round(leak.create_time, 3))
        p = by_key.get(key)
        params = {"rate": leak.slope_bytes_per_hour, "growth": leak.growth, "minutes": leak.window_sec / 60}
        if p is not None and not p.protected:
            recs.append(Recommendation(ActionKind.CLOSE_APP, "leak", params, p.pid, p.create_time, p.name,
                                       priority=90 if under_pressure else 60))
            used.add(key)
        else:
            recs.append(Recommendation(ActionKind.CLOSE_APP, "leak_system", params, leak.pid, leak.create_time,
                                       leak.name, executable=False, priority=50))

    # 3. Под давлением: тяжёлые программы пользователя.
    if under_pressure:
        candidates = sorted((p for p in processes if not p.protected and p.key not in used),
                            key=lambda p: p.footprint, reverse=True)
        for p in candidates[:8]:
            if not p.has_window and p.footprint >= cfg.heavy_background:
                is_idle = idle_keys is not None and p.key in idle_keys
                recs.append(Recommendation(ActionKind.CLOSE_APP, "heavy_background", {"size": p.footprint},
                                           p.pid, p.create_time, p.name, priority=75))
                if is_idle and p.working_set >= cfg.heavy_background // 2:
                    recs.append(Recommendation(ActionKind.TRIM, "idle_trim", {"size": p.working_set},
                                               p.pid, p.create_time, p.name, priority=30))
                used.add(p.key)
            elif p.has_window and p.footprint >= cfg.heavy_app:
                recs.append(Recommendation(ActionKind.CLOSE_APP, "heavy_app", {"size": p.footprint},
                                           p.pid, p.create_time, p.name, priority=65))
                used.add(p.key)

    # 4. Автозагрузка: включённые программы, которые сейчас занимают много памяти.
    names = {}
    for p in processes:
        names.setdefault(p.name.lower(), []).append(p)
    for item in startup_items:
        if not getattr(item, "enabled", False) or not getattr(item, "exe_name", ""):
            continue
        if item.exe_name.lower() in RAM_CLEANERS:
            recs.append(Recommendation(ActionKind.STARTUP_DISABLE, "ram_cleaner", {},
                                       target_name=item.name, startup_id=item.id, reversible=True,
                                       needs_admin=item.needs_admin, priority=55))
            continue
        running = names.get(item.exe_name.lower(), [])
        total = sum(p.footprint for p in running)
        if total >= cfg.heavy_startup:
            recs.append(Recommendation(ActionKind.STARTUP_DISABLE, "startup_heavy", {"size": total},
                                       target_name=item.name, startup_id=item.id, reversible=True,
                                       needs_admin=item.needs_admin, priority=40))

    # 5. Standby — только при объективном основании и с предупреждением.
    if snap.has_lists and snap.total:
        free_limit = max(cfg.standby_purge_free_max, cfg.standby_purge_free_max_share * snap.total)
        if snap.free_zero < free_limit and snap.standby >= cfg.standby_purge_standby_share * snap.total:  # type: ignore[operator]
            recs.append(Recommendation(ActionKind.PURGE_STANDBY, "standby_low_free",
                                       {"free": snap.free_zero, "standby": snap.standby},
                                       needs_admin=True, priority=10))

    recs.sort(key=lambda r: r.priority, reverse=True)
    return recs


# ------------------------------------------------------------------ before / after


@dataclass(slots=True)
class Delta:
    key: str
    before: float | None
    after: float | None

    @property
    def change(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before


def compare(before: SystemMemory, after: SystemMemory,
            faults_before: float | None = None, faults_after: float | None = None) -> list[Delta]:
    """Сравнение «до/после». Hard faults передаются медианой за окно, а не мгновенным значением."""
    return [
        Delta("available", before.available, after.available),
        Delta("in_use", before.in_use, after.in_use),
        Delta("standby", before.standby, after.standby),
        Delta("commit", before.commit_total, after.commit_total),
        Delta("hard_faults", faults_before, faults_after),
    ]


def idle_processes(prev_cpu: dict[tuple[int, float], float], processes: Sequence[ProcessMemory],
                   interval: float, max_share: float = 0.002) -> set[tuple[int, float]]:
    """Процессы, почти не использовавшие CPU за интервал (< 0.2% одного ядра)."""
    idle: set[tuple[int, float]] = set()
    if interval <= 0:
        return idle
    for p in processes:
        before = prev_cpu.get(p.key)
        if before is None or p.cpu_time is None:
            continue
        if (p.cpu_time - before) / interval <= max_share:
            idle.add(p.key)
    return idle
