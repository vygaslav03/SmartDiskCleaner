"""Диагностический отчёт: `python run.py --memory-report` (или DiskCleaner.exe --memory-report).

Только чтение. Нужен, чтобы проверить показатели на конкретном ПК:
  * сверить цифры с Диспетчером задач / Монитором ресурсов / RAMMap;
  * убедиться, что всё работает без прав администратора;
  * измерить стоимость сбора и память самой программы (без интерфейса).
Отчёт печатается и сохраняется в memory_report.txt рядом с программой.
"""

from __future__ import annotations

import platform
import statistics
import sys
import time

from app.memory import analysis
from app.memory.collector import (
    ProcessCollector,
    SystemCollector,
    own_process_memory,
    read_pagefile_config,
)
from app.memory.history import SystemHistory
from app.memory.startup import list_system_startup
from app.utils.format_size import format_size


def os_label() -> str:
    """platform.release() на Python < 3.12 называет Windows 11 «10» — определяем по номеру сборки."""
    version = platform.version()
    if platform.system() == "Windows":
        try:
            build = int(version.split(".")[2])
        except (IndexError, ValueError):
            build = 0
        name = "Windows 11" if build >= 22000 else f"Windows {platform.release()}"
        return f"{name} (build {version})"
    return f"{platform.system()} {platform.release()} ({version})"


def build_report(samples: int = 6, interval: float = 1.0) -> list[str]:
    from app.utils.permissions import is_admin

    lines: list[str] = []
    add = lines.append
    add(f"DiskCleaner memory report — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"OS: {os_label()}, Python {platform.python_version()}, admin={is_admin()}")
    ws0, pb0 = own_process_memory()
    add(f"Own memory at start: working set {format_size(ws0)}, private {format_size(pb0 or 0)}")
    add("")

    sc = SystemCollector()
    hist = SystemHistory(capacity=samples + 1)
    sys_costs = []
    snap = None
    for _ in range(samples):
        t0 = time.perf_counter()
        snap = sc.snapshot()
        sys_costs.append((time.perf_counter() - t0) * 1000)
        hist.add(snap)
        time.sleep(interval)
    sc.close()
    assert snap is not None
    s = snap
    add(f"[System]  source={s.source}, page lists available={s.has_lists}")
    add(f"  Total            {format_size(s.total)}")
    add(f"  Available        {format_size(s.available)}   (Task Manager: Available)")
    add(f"  Memory load      {s.load_percent:.0f}%")
    add(f"  In use           {format_size(s.in_use) if s.in_use is not None else '—'}   (Resource Monitor: In Use)")
    add(f"  Modified         {format_size(s.modified) if s.modified is not None else '—'}")
    add(f"  Standby          {format_size(s.standby) if s.standby is not None else '—'}"
        f"  core/normal/reserve = {format_size(s.standby_core or 0)} / {format_size(s.standby_normal or 0)} / "
        f"{format_size(s.standby_reserve or 0)}")
    add(f"  Free+Zero        {format_size(s.free_zero) if s.free_zero is not None else '—'}")
    if s.has_lists:
        diff = s.available - (s.standby + s.free_zero)  # type: ignore[operator]
        add(f"  CHECK available − (standby + free) = {format_size(abs(diff))} "
            f"({'OK' if abs(diff) <= 0.05 * s.available else 'MISMATCH'})")
    add(f"  Committed        {format_size(s.commit_total)} / {format_size(s.commit_limit)} "
        f"({100 * s.commit_ratio:.0f}%), peak {format_size(s.commit_peak or 0)}   (Task Manager: Committed)")
    add(f"  CHECK commit ≤ limit: {'OK' if s.commit_total <= s.commit_limit else 'MISMATCH'}")
    add(f"  System cache     {format_size(s.system_cache or 0)}")
    add(f"  Kernel paged/np  {format_size(s.kernel_paged or 0)} / {format_size(s.kernel_nonpaged or 0)}")
    hf = s.hard_faults_per_sec
    add(f"  Hard faults      {'—' if hf is None else f'{hf:.0f} pages/s (≈{hf * 4096 / 1048576:.1f} MB/s)'}")
    add(f"  Pagefile usage   {'—' if s.pagefile_usage_percent is None else f'{s.pagefile_usage_percent:.1f}%'}")
    add(f"  Collection cost  median {statistics.median(sys_costs):.2f} ms (first {sys_costs[0]:.1f} ms)")
    add("")

    pf = read_pagefile_config()
    a = analysis.assess_system(s, hist.points(), pagefile=pf)
    add(f"[Assessment] {a.severity.name}")
    for f in a.findings:
        add(f"  - {f.severity.name}: {f.code} {f.params}")
    add("")

    add(f"[Pagefile] config known={pf.known}, disabled={pf.disabled}, system_managed={pf.system_managed}, "
        f"lines={pf.configured}")
    for f in pf.files:
        add(f"  {f.name}: size {format_size(f.total)}, in use {format_size(f.in_use)}, peak {format_size(f.peak)}")
    add("")

    pc = ProcessCollector()
    t0 = time.perf_counter()
    procs = pc.scan()
    first = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    procs = pc.scan()
    second = (time.perf_counter() - t0) * 1000
    with_private = sum(1 for p in procs if p.private_bytes is not None)
    add(f"[Processes] {len(procs)} listed, private bytes known for {with_private}, "
        f"owned={sum(p.owned for p in procs)}, protected={sum(p.protected for p in procs)}; "
        f"scan {first:.0f} ms (cold), {second:.0f} ms (cached)")
    add(f"  {'name':<32}{'private':>12}{'working set':>14}  flags")
    for p in sorted(procs, key=lambda p: p.footprint, reverse=True)[:15]:
        flags = ",".join(x for x, on in (("owned", p.owned), ("window", p.has_window), ("protected", p.protected)) if on)
        add(f"  {p.name[:31]:<32}{format_size(p.private_bytes or 0):>12}{format_size(p.working_set):>14}  {flags}")
    add("")

    items = list_system_startup()
    add(f"[Startup] {len(items)} entries")
    for it in items:
        add(f"  {'ON ' if it.enabled else 'OFF'} {it.location:<14} {it.name}  (exe: {it.exe_name or '—'})")
    cleaners = [it.name for it in items if it.enabled and it.exe_name.lower() in analysis.RAM_CLEANERS]
    if cleaners:
        add(f"  NOTE: RAM cleaner in startup: {', '.join(cleaners)} — it forces pages out of RAM / drops the cache")
    add("")
    ws1, pb1 = own_process_memory()
    add(f"Own memory at end: working set {format_size(ws1)}, private {format_size(pb1 or 0)}")
    return lines


def run_report() -> int:
    from app.utils.paths import data_dir

    try:
        lines = build_report()
    except Exception as exc:  # noqa: BLE001
        import traceback

        lines = ["REPORT FAILED", "".join(traceback.format_exception(exc))]
    text = "\n".join(lines)
    path = data_dir() / "memory_report.txt"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        path = None
    if sys.stdout is not None:
        try:
            print(text)
            if path:
                print(f"\nSaved: {path}")
        except UnicodeEncodeError:
            pass
    return 0 if lines and lines[0] != "REPORT FAILED" else 1
