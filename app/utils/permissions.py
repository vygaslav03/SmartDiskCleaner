"""Права администратора: проверка и перезапуск с повышением (UAC)."""

from __future__ import annotations

import os
import subprocess
import sys

from app.utils.logger import get_logger
from app.utils.paths import is_frozen, project_root
from app.utils.winpaths import IS_WINDOWS

log = get_logger("permissions")


def is_admin() -> bool:
    """True, если процесс запущен с правами администратора."""
    if IS_WINDOWS:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (OSError, AttributeError):
            return False
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def relaunch_as_admin() -> bool:
    """Запускает копию программы через UAC ('runas').

    Возвращает True, если запрос UAC принят и новый процесс запущен —
    тогда текущий экземпляр должен завершиться. False — пользователь отказал
    или ОС не Windows.
    """
    if not IS_WINDOWS:
        return False
    import ctypes

    if is_frozen():
        executable = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        # pythonw.exe, чтобы не появлялось консольное окно
        exe_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(exe_dir, "pythonw.exe")
        executable = pythonw if os.path.exists(pythonw) else sys.executable
        params = subprocess.list2cmdline([str(project_root() / "run.py"), *sys.argv[1:]])

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(None, "runas", executable, params, str(project_root()), 1)
    ok = (result or 0) > 32
    log.info("Запрос повышения прав: %s", "принят" if ok else "отклонён")
    return ok
