"""Ручные действия с памятью. Вызываются ТОЛЬКО из интерфейса после подтверждения.

Общие правила для всех действий с процессами:
  * процесс определяется парой (PID, время запуска) — если PID уже занят другой
    программой, действие отменяется (защита от переиспользования PID);
  * системные/критические процессы, чужие процессы и сама программа — запрещены;
  * каждое действие записывается в журнал вместе с замером «до/после».
"""

from __future__ import annotations

import time
from typing import Callable

from app.memory import startup as startup_mod
from app.memory import winapi
from app.memory.collector import is_critical_name
from app.memory.journal import ActionJournal, JournalEntry, get_journal, new_entry
from app.memory.models import ActionKind, SystemMemory
from app.utils.logger import get_logger

log = get_logger("memory.actions")


class ActionRefused(Exception):
    """Действие запрещено правилами безопасности (код причины — в args[0])."""


def _psutil():
    import psutil

    return psutil


def get_verified_process(pid: int, create_time: float):
    """Процесс, если это всё ещё ТОТ ЖЕ процесс и действия с ним разрешены."""
    import os

    psutil = _psutil()
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - create_time) > 1.0:
            raise ActionRefused("pid_reused")
        name = proc.name()
    except psutil.NoSuchProcess as exc:
        raise ActionRefused("gone") from exc
    except psutil.AccessDenied as exc:
        raise ActionRefused("not_owned") from exc
    if pid in (0, 4) or pid == os.getpid() or is_critical_name(name):
        raise ActionRefused("protected")
    try:
        me = psutil.Process().username()
        owner = proc.username()
    except psutil.Error as exc:
        raise ActionRefused("not_owned") from exc
    if not me or not owner or me.lower() != owner.lower():
        raise ActionRefused("not_owned")
    return proc


def _footprint(proc) -> dict:
    try:
        mi = proc.memory_info()
        return {"working_set": int(getattr(mi, "wset", mi.rss)),
                "private_bytes": int(getattr(mi, "private", 0)) or None}
    except Exception:  # noqa: BLE001
        return {}


def _record(journal: ActionJournal | None, entry: JournalEntry) -> JournalEntry:
    (journal or get_journal()).add(entry)
    return entry


# ------------------------------------------------------------------ processes


def close_app(pid: int, create_time: float, timeout: float = 10.0, journal: ActionJournal | None = None,
              post_close: Callable[[int], int] = winapi.post_close_to_windows) -> JournalEntry:
    """Мягкое закрытие: WM_CLOSE окнам — программа может спросить о сохранении."""
    proc = get_verified_process(pid, create_time)
    name = proc.name()
    before = _footprint(proc)
    sent = post_close(pid)
    if sent == 0:
        return _record(journal, new_entry(ActionKind.CLOSE_APP, name, False, "no_window", before=before))
    psutil = _psutil()
    try:
        proc.wait(timeout=timeout)
        closed = True
    except psutil.TimeoutExpired:
        closed = False
    except psutil.NoSuchProcess:
        closed = True
    msg = "closed" if closed else "still_running"
    return _record(journal, new_entry(ActionKind.CLOSE_APP, name, closed, msg, before=before))


def terminate(pid: int, create_time: float, timeout: float = 5.0,
              journal: ActionJournal | None = None) -> JournalEntry:
    """Принудительное завершение. Несохранённые данные программы будут потеряны."""
    proc = get_verified_process(pid, create_time)
    name = proc.name()
    before = _footprint(proc)
    psutil = _psutil()
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
        ok, msg = True, "terminated"
    except psutil.NoSuchProcess:
        ok, msg = True, "terminated"
    except psutil.TimeoutExpired:
        ok, msg = False, "still_running"
    except psutil.AccessDenied:
        ok, msg = False, "access_denied"
    return _record(journal, new_entry(ActionKind.TERMINATE, name, ok, msg, before=before))


def trim_working_set(pid: int, create_time: float, journal: ActionJournal | None = None,
                     empty: Callable[[int], bool] = winapi.empty_working_set) -> JournalEntry:
    """EmptyWorkingSet для одного процесса. Отмены нет, но и вреда данным нет:
    страницы вернутся при обращении (ценой обращений к диску/standby)."""
    proc = get_verified_process(pid, create_time)
    name = proc.name()
    before = _footprint(proc)
    try:
        empty(pid)
        ok, msg = True, "trimmed"
    except (OSError, PermissionError) as exc:
        ok, msg = False, str(exc)
    time.sleep(0.2)
    after = _footprint(proc)
    return _record(journal, new_entry(ActionKind.TRIM, name, ok, msg, before=before, after=after))


# ------------------------------------------------------------------ system


def purge_standby(low_priority_only: bool, snapshot: Callable[[], SystemMemory], is_admin: bool,
                  journal: ActionJournal | None = None,
                  purge: Callable[[bool], None] = winapi.purge_standby_list) -> JournalEntry:
    if not is_admin:
        raise ActionRefused("needs_admin")
    b = snapshot()
    try:
        purge(low_priority_only)
        ok, msg = True, "low_priority" if low_priority_only else "all"
    except (OSError, PermissionError) as exc:
        ok, msg = False, str(exc)
    time.sleep(0.3)
    a = snapshot()

    def lists(s: SystemMemory) -> dict:
        return {"standby": s.standby, "free_zero": s.free_zero, "available": s.available}

    return _record(journal, new_entry(ActionKind.PURGE_STANDBY, "standby", ok, msg, before=lists(b), after=lists(a)))


# ------------------------------------------------------------------ startup


def set_startup(item: startup_mod.StartupItem, enabled: bool, is_admin: bool,
                reg: startup_mod.Registry | None = None, journal: ActionJournal | None = None) -> JournalEntry:
    if item.needs_admin and not is_admin:
        raise ActionRefused("needs_admin")
    registry = reg or startup_mod.WinRegistry()
    kind = ActionKind.STARTUP_ENABLE if enabled else ActionKind.STARTUP_DISABLE
    try:
        undo = startup_mod.set_enabled(registry, item, enabled)
        entry = new_entry(kind, item.name, True, "", undo={"type": "startup", **undo})
    except OSError as exc:
        entry = new_entry(kind, item.name, False, str(exc))
    return _record(journal, entry)


def undo_entry(entry: JournalEntry, reg: startup_mod.Registry | None = None,
               journal: ActionJournal | None = None) -> bool:
    if not entry.can_undo:
        raise ActionRefused("cannot_undo")
    data = entry.undo or {}
    if data.get("type") != "startup":
        raise ActionRefused("cannot_undo")
    startup_mod.undo(reg or startup_mod.WinRegistry(), data)
    j = journal or get_journal()
    j.mark_undone(entry.id)
    log.info("Отменено действие %s (%s)", entry.kind, entry.target)
    return True
