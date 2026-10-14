"""Сбор показателей памяти: система (WinAPI + PDH) и процессы (psutil).

Источники выбраны так, чтобы обойтись без прав администратора:
- GlobalMemoryStatusEx      — всего / доступно / Memory Load / Commit Limit;
- GetPerformanceInfo        — Commit Charge / Peak, системный кэш, пулы ядра;
- PDH (английские имена)    — standby (3 приоритета), modified, free+zero, hard faults, % файла подкачки;
- psutil                    — процессы: private bytes, рабочий набор, хендлы, CPU.
  Для чужих процессов без доступа psutil сам берёт данные из NtQuerySystemInformation.

Не на Windows (разработка, тесты) — упрощённый fallback через psutil.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from typing import Callable

from app.memory import winapi
from app.memory.models import PagefileConfig, ProcessMemory, SystemMemory
from app.utils.logger import get_logger

log = get_logger("memory.collector")

PDH_COUNTERS = {
    "standby_core": r"\Memory\Standby Cache Core Bytes",
    "standby_normal": r"\Memory\Standby Cache Normal Priority Bytes",
    "standby_reserve": r"\Memory\Standby Cache Reserve Bytes",
    "modified": r"\Memory\Modified Page List Bytes",
    "free_zero": r"\Memory\Free & Zero Page List Bytes",
    "pages_input": r"\Memory\Pages Input/sec",
    "page_reads": r"\Memory\Page Reads/sec",
    "pagefile_pct": r"\Paging File(_Total)\% Usage",
}

# Процессы, к которым программа никогда не применяет действия (сравнение без регистра).
CRITICAL_NAMES = frozenset({
    "system", "system idle process", "idle", "registry", "memory compression", "secure system",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe",
    "svchost.exe", "fontdrvhost.exe", "dwm.exe", "sihost.exe", "ctfmon.exe", "spoolsv.exe",
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "securityhealthsystray.exe",
    "audiodg.exe", "conhost.exe", "runtimebroker.exe", "taskhostw.exe", "searchindexer.exe",
    "wudfhost.exe", "dllhost.exe", "explorer.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "textinputhost.exe", "searchhost.exe", "lockapp.exe",
    "smartscreen.exe", "wmiprvse.exe", "trustedinstaller.exe", "tiworker.exe", "usocoreworker.exe",
    "mousocoreworker.exe", "msmpengcp.exe", "mpdefendercoreservice.exe",
})


def is_critical_name(name: str) -> bool:
    return name.lower() in CRITICAL_NAMES


def _windows_dir() -> str:
    return os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")).rstrip("\\/") + os.sep


# ------------------------------------------------------------------ system


class SystemCollector:
    """Снимок памяти системы. Держит открытый PDH-запрос — создавайте один на программу."""

    def __init__(self) -> None:
        self._pdh = winapi.PdhQuery(PDH_COUNTERS) if winapi.IS_WINDOWS else None
        if self._pdh is not None and self._pdh.ok:
            self._pdh.collect()  # первая выборка: скорости станут доступны со следующей
        self.lists_available = bool(self._pdh and self._pdh.ok)

    def snapshot(self) -> SystemMemory:
        if winapi.IS_WINDOWS:
            try:
                snap = self._windows_snapshot()
                if snap is not None:
                    return snap
            except Exception:  # noqa: BLE001 - никогда не роняем мониторинг
                log.exception("Сбор через WinAPI не удался, используется psutil")
        return psutil_snapshot()

    def _windows_snapshot(self) -> SystemMemory | None:
        st = winapi.global_memory_status()
        if st is None:
            return None
        pi = winapi.performance_info()
        page = int(pi.PageSize) if pi else 4096
        pdh = self._pdh.collect() if self._pdh else {}

        def pdh_int(key: str) -> int | None:
            v = pdh.get(key)
            return int(v) if v is not None else None

        sc, sn, sr = pdh_int("standby_core"), pdh_int("standby_normal"), pdh_int("standby_reserve")
        standby = sc + sn + sr if None not in (sc, sn, sr) else None  # type: ignore[operator]
        return SystemMemory(
            timestamp=time.time(),
            total=int(st.ullTotalPhys),
            available=int(st.ullAvailPhys),
            load_percent=float(st.dwMemoryLoad),
            commit_total=int(pi.CommitTotal * page) if pi else int(st.ullTotalPageFile - st.ullAvailPageFile),
            commit_limit=int(pi.CommitLimit * page) if pi else int(st.ullTotalPageFile),
            commit_peak=int(pi.CommitPeak * page) if pi else None,
            system_cache=int(pi.SystemCache * page) if pi else None,
            kernel_paged=int(pi.KernelPaged * page) if pi else None,
            kernel_nonpaged=int(pi.KernelNonpaged * page) if pi else None,
            standby=standby,
            standby_core=sc,
            standby_normal=sn,
            standby_reserve=sr,
            modified=pdh_int("modified"),
            free_zero=pdh_int("free_zero"),
            hard_faults_per_sec=pdh.get("pages_input"),
            page_reads_per_sec=pdh.get("page_reads"),
            pagefile_usage_percent=pdh.get("pagefile_pct"),
            process_count=int(pi.ProcessCount) if pi else None,
            source="winapi",
        )

    def close(self) -> None:
        if self._pdh is not None:
            self._pdh.close()


def psutil_snapshot() -> SystemMemory:
    """Fallback: точные total/available, commit — приблизительно (RAM + swap)."""
    import psutil

    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return SystemMemory(
        timestamp=time.time(),
        total=int(vm.total),
        available=int(vm.available),
        load_percent=float(vm.percent),
        commit_total=int(vm.total - vm.available + sw.used),
        commit_limit=int(vm.total + sw.total),
        system_cache=int(getattr(vm, "cached", 0)) or None,
        free_zero=None,
        source="psutil",
    )


# ------------------------------------------------------------------ processes


class ProcessCollector:
    """Список процессов с памятью. Статические поля (владелец, путь, защищённость)
    кэшируются по (pid, время запуска), поэтому повторный опрос дешёвый.
    """

    def __init__(self, services_provider: Callable[[], dict[int, tuple[str, ...]]] | None = None) -> None:
        self._static: dict[tuple[int, float], tuple[bool, str, bool]] = {}
        self._own_pid = os.getpid()
        self._me = _current_username()
        self._services_provider = services_provider or service_names_by_pid
        self._services: dict[int, tuple[str, ...]] = {}
        self._services_at = 0.0

    def scan(self) -> list[ProcessMemory]:
        import psutil

        now = time.monotonic()
        if now - self._services_at > 60:
            try:
                self._services = self._services_provider()
            except Exception:  # noqa: BLE001
                self._services = {}
            self._services_at = now
        windows = winapi.pids_with_visible_windows()
        result: list[ProcessMemory] = []
        alive: set[tuple[int, float]] = set()
        for proc in psutil.process_iter():
            try:
                with proc.oneshot():
                    name = proc.name()
                    create_time = proc.create_time()
                    mi = proc.memory_info()
                    handles = _safe(lambda: proc.num_handles()) if winapi.IS_WINDOWS else None
                    cpu = _safe(lambda: sum(proc.cpu_times()[:2]))
                    key = (proc.pid, round(create_time, 3))
                    alive.add(key)
                    static = self._static.get(key)
                    if static is None:
                        static = self._static_info(proc, name)
                        self._static[key] = static
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                # PID 0 / защищённые процессы: показываем, но без цифр
                continue
            except Exception:  # noqa: BLE001
                log.debug("Процесс %s пропущен", proc.pid, exc_info=True)
                continue
            owned, exe, protected = static
            private = getattr(mi, "private", None)
            result.append(ProcessMemory(
                pid=proc.pid,
                name=name,
                create_time=create_time,
                working_set=int(getattr(mi, "wset", mi.rss)),
                private_bytes=int(private) if private is not None else None,
                handles=handles,
                cpu_time=cpu,
                owned=owned,
                has_window=proc.pid in windows,
                protected=protected,
                exe=exe,
                services=self._services.get(proc.pid, ()),
            ))
        # кэш не растёт бесконечно
        for key in list(self._static):
            if key not in alive:
                del self._static[key]
        return result

    def _static_info(self, proc, name: str) -> tuple[bool, str, bool]:
        exe = _safe(proc.exe) or ""
        user = _safe(proc.username)
        owned = bool(user and self._me and user.lower() == self._me.lower())
        in_windows_dir = bool(exe) and os.path.normcase(exe).startswith(_windows_dir())
        protected = (
            proc.pid in (0, 4)
            or proc.pid == self._own_pid
            or is_critical_name(name)
            or in_windows_dir
            or not owned
        )
        return owned, exe, protected


def private_working_set(pid: int) -> int | None:
    """Private Working Set (USS). Дорого (обход страниц) — только для выбранного процесса."""
    import psutil

    try:
        return int(psutil.Process(pid).memory_full_info().uss)
    except (psutil.Error, OSError, AttributeError):
        return None


def own_process_memory() -> tuple[int, int | None]:
    """(рабочий набор, private bytes) самой программы — для честного отчёта о расходе."""
    import psutil

    mi = psutil.Process().memory_info()
    private = getattr(mi, "private", None)
    return int(getattr(mi, "wset", mi.rss)), int(private) if private is not None else None


def service_names_by_pid() -> dict[int, tuple[str, ...]]:
    """Службы Windows по PID (для svchost.exe). Только чтение."""
    if not winapi.IS_WINDOWS:
        return {}
    import psutil

    result: dict[int, list[str]] = {}
    for svc in psutil.win_service_iter():
        try:
            pid = svc.pid()
            if pid:
                result.setdefault(pid, []).append(svc.name())
        except (psutil.Error, OSError):
            continue
    return {pid: tuple(sorted(names)) for pid, names in result.items()}


def _current_username() -> str | None:
    import psutil

    return _safe(lambda: psutil.Process().username())


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - AccessDenied и пр.: показатель просто недоступен
        return None


# ------------------------------------------------------------------ pagefile


PAGEFILE_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"


def read_pagefile_config(reg_reader: Callable[[], list[str] | None] | None = None,
                         files_reader: Callable[[], list | None] | None = None) -> PagefileConfig:
    """Конфигурация файла подкачки — только чтение (права администратора не нужны)."""
    configured = (reg_reader or _read_paging_files_value)()
    files = (files_reader or winapi.pagefiles)()
    cfg = PagefileConfig(files=list(files or []))
    if configured is None:
        return cfg
    cfg.known = True
    cfg.configured = [c for c in configured if c.strip()]
    cfg.disabled = not cfg.configured
    cfg.system_managed = any(_is_system_managed(line) for line in cfg.configured)
    return cfg


def _is_system_managed(line: str) -> bool:
    # "?:\pagefile.sys" — авто на всех дисках; "C:\pagefile.sys" или "C:\pagefile.sys 0 0" — по выбору системы
    parts = line.split()
    if not parts:
        return False
    if parts[0].startswith("?:"):
        return True
    return len(parts) == 1 or (len(parts) >= 3 and parts[1] == "0" and parts[2] == "0")


def _read_paging_files_value() -> list[str] | None:
    if not winapi.IS_WINDOWS:
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, PAGEFILE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "PagingFiles")
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]
    except OSError:
        return None


def with_timestamp(snap: SystemMemory, ts: float) -> SystemMemory:
    """Тестовый помощник: копия снимка с другим временем."""
    return replace(snap, timestamp=ts)
