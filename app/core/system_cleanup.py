"""Очистка встроенными средствами Windows: Windows.old (cleanmgr) и хранилище компонентов (DISM).

Программа САМА эти файлы не удаляет: Windows.old и WinSxS принадлежат
TrustedInstaller, и единственный корректный способ — штатные инструменты Windows.

* Windows.old -> `cleanmgr.exe /sagerun:N` с включённым только обработчиком
  «Previous Installations» (через временное значение StateFlagsNNNN в реестре,
  которое удаляется после запуска).
* Хранилище компонентов -> `Dism.exe /Online /Cleanup-Image /StartComponentCleanup`
  БЕЗ /ResetBase: установленные обновления по-прежнему можно удалить.

Обе операции требуют прав администратора. DISM нельзя прерывать — отмена
проверяется только до запуска.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from app.utils import winpaths
from app.utils.logger import get_logger
from app.utils.winpaths import IS_WINDOWS, is_reparse_or_link, long_path

log = get_logger("system_cleanup")

SAGESET_ID = 4242
VOLUME_CACHES = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches"
PREVIOUS_INSTALLATIONS = "Previous Installations"
DISM_TIMEOUT = 60 * 60  # 1 час
CLEANMGR_TIMEOUT = 60 * 60
_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d)?)\s?%")

# Коды результата (переводятся в UI: "reason.<code>")
T_OK = "ok"
T_NOT_ADMIN = "tool_needs_admin"
T_MISSING = "tool_unavailable"
T_FAILED = "tool_failed"
T_TIMEOUT = "tool_timeout"
T_NOTHING = "tool_nothing"


@dataclass
class Measure:
    available: bool = False
    size: int = 0
    files: int = 0
    size_known: bool = True
    partial: bool = False  # часть папок недоступна — реальный размер больше


@dataclass
class ToolResult:
    ok: bool
    freed: int = 0
    code: str = T_OK
    details: str = ""


class ProcessRunner(Protocol):
    def __call__(self, args: list[str], on_line: Callable[[str], None], timeout: float) -> int: ...


def run_process(args: list[str], on_line: Callable[[str], None], timeout: float) -> int:
    """Запускает консольную утилиту без окна и построчно отдаёт её вывод."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    encoding = "oem" if IS_WINDOWS else "utf-8"
    proc = subprocess.Popen(  # noqa: S603 - аргументы фиксированы, без shell
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        text=True,
        encoding=encoding,
        errors="replace",
    )
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if ch == "":
            break
        if ch in "\r\n":  # DISM рисует прогресс через \r
            if buf.strip():
                on_line(buf)
            buf = ""
        else:
            buf += ch
        if time.monotonic() > deadline:
            proc.kill()
            raise subprocess.TimeoutExpired(args, timeout)
    if buf.strip():
        on_line(buf)
    return proc.wait(timeout=max(1.0, deadline - time.monotonic()))


def parse_percent(line: str) -> float | None:
    """'[==== 34.5% ====]' -> 34.5 (не зависит от языка Windows)."""
    matches = list(_PERCENT_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if 0 <= value <= 100 else None


class SystemCleanup:
    def __init__(
        self,
        runner: ProcessRunner = run_process,
        is_admin: Callable[[], bool] | None = None,
        system_drive: Callable[[], str] = winpaths.system_drive,
        free_space: Callable[[str], int] | None = None,
        enabled: bool = IS_WINDOWS,
    ) -> None:
        from app.utils.permissions import is_admin as _is_admin

        self._run = runner
        self._is_admin = is_admin or _is_admin
        self._system_drive = system_drive
        self._free = free_space or (lambda d: shutil.disk_usage(d).free)
        self.enabled = enabled

    # ------------------------------------------------------------- general
    def _on_system_drive(self, drive: str | None) -> bool:
        if not drive:
            return False
        return winpaths.drive_of(drive) == winpaths.drive_of(self._system_drive())

    def windows_old_path(self) -> str:
        return os.path.join(self._system_drive(), "Windows.old")

    def measure(self, cat_id: str, drive: str | None, cancel: threading.Event) -> Measure:
        if not self.enabled or not self._on_system_drive(drive):
            return Measure()
        if cat_id == "windows_old":
            return self.measure_windows_old(cancel)
        if cat_id == "component_store":
            # Размер освобождаемого места заранее неизвестен: точная оценка (AnalyzeComponentStore)
            # занимает минуты, поэтому считаем фактически освобождённое после очистки.
            return Measure(available=True, size=0, files=0, size_known=False)
        return Measure()

    def measure_windows_old(self, cancel: threading.Event) -> Measure:
        root = self.windows_old_path()
        try:
            st = os.lstat(long_path(root))
        except OSError:
            return Measure()
        if not stat.S_ISDIR(st.st_mode) or is_reparse_or_link(st):
            return Measure()
        m = Measure(available=True)
        stack = [root]
        while stack:
            if cancel.is_set():
                m.partial = True
                break
            d = stack.pop()
            try:
                it = os.scandir(long_path(d))
            except OSError:
                m.partial = True
                continue
            with it:
                for entry in it:
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        m.partial = True
                        continue
                    if is_reparse_or_link(est):
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        stack.append(os.path.join(d, entry.name))
                    elif stat.S_ISREG(est.st_mode):
                        m.size += int(est.st_size)
                        m.files += 1
        return m

    # ------------------------------------------------------------- cleaning
    def run(self, cat_id: str, drive: str | None, progress: Callable[[float | None], None]) -> ToolResult:
        if not self.enabled or not self._on_system_drive(drive):
            return ToolResult(False, code=T_NOTHING)
        if not self._is_admin():
            return ToolResult(False, code=T_NOT_ADMIN)
        if cat_id == "windows_old":
            return self.clean_windows_old(progress)
        if cat_id == "component_store":
            return self.clean_component_store(progress)
        return ToolResult(False, code=T_NOTHING)

    def clean_component_store(self, progress: Callable[[float | None], None]) -> ToolResult:
        dism = self._tool_path("Dism.exe")
        if dism is None:
            return ToolResult(False, code=T_MISSING)
        drive = self._system_drive()
        before = self._free(drive)
        lines: list[str] = []

        def on_line(line: str) -> None:
            lines.append(line)
            pct = parse_percent(line)
            if pct is not None:
                progress(pct)

        log.info("Запуск DISM StartComponentCleanup")
        try:
            code = self._run([dism, "/Online", "/Cleanup-Image", "/StartComponentCleanup"], on_line, DISM_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ToolResult(False, code=T_TIMEOUT)
        except OSError as exc:
            log.error("DISM не запустился: %s", exc)
            return ToolResult(False, code=T_MISSING, details=str(exc))
        freed = max(0, self._free(drive) - before)
        tail = "\n".join(lines[-15:])
        log.info("DISM завершён с кодом %s, освобождено ~%d байт", code, freed)
        if code in (0, 3010):  # 3010 = нужна перезагрузка, операция успешна
            return ToolResult(True, freed=freed, details=tail)
        return ToolResult(False, freed=freed, code=T_FAILED, details=f"exit code {code}\n{tail}")

    def clean_windows_old(self, progress: Callable[[float | None], None]) -> ToolResult:
        before = self.measure_windows_old(threading.Event())
        if not before.available:
            return ToolResult(False, code=T_NOTHING)
        cleanmgr = self._tool_path("cleanmgr.exe")
        if cleanmgr is None:
            return ToolResult(False, code=T_MISSING)
        value_name = f"StateFlags{SAGESET_ID:04d}"
        if not self._set_sageset(value_name, True):
            return ToolResult(False, code=T_FAILED, details="registry")
        progress(None)
        log.info("Запуск cleanmgr /sagerun:%d (Previous Installations)", SAGESET_ID)
        try:
            code = self._run([cleanmgr, f"/sagerun:{SAGESET_ID}"], lambda _l: None, CLEANMGR_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ToolResult(False, code=T_TIMEOUT)
        except OSError as exc:
            return ToolResult(False, code=T_MISSING, details=str(exc))
        finally:
            self._set_sageset(value_name, False)
        after = self.measure_windows_old(threading.Event())
        freed = max(0, before.size - (after.size if after.available else 0))
        log.info("cleanmgr завершён с кодом %s, Windows.old освобождено %d байт", code, freed)
        if not after.available or after.size < before.size:
            return ToolResult(True, freed=freed)
        return ToolResult(False, freed=freed, code=T_FAILED, details=f"exit code {code}")

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _tool_path(name: str) -> str | None:
        windir = winpaths.known_folder("Windows") or os.environ.get("SystemRoot", "")
        # Sysnative: из 32-битного процесса на 64-битной Windows иначе откроется SysWOW64
        for sub in ("Sysnative", "System32"):
            p = os.path.join(windir, sub, name)
            if os.path.isfile(p):
                return p
        return shutil.which(name)

    @staticmethod
    def _set_sageset(value_name: str, enable: bool) -> bool:
        """Включает в cleanmgr ТОЛЬКО «Previous Installations» для нашего sagerun-номера."""
        if not IS_WINDOWS:
            return False
        import winreg

        path = f"{VOLUME_CACHES}\\{PREVIOUS_INSTALLATIONS}"
        try:
            access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, access) as key:
                if enable:
                    winreg.SetValueEx(key, value_name, 0, winreg.REG_DWORD, 2)
                else:
                    try:
                        winreg.DeleteValue(key, value_name)
                    except FileNotFoundError:
                        pass
            return True
        except OSError as exc:
            log.error("Не удалось %s %s: %s", "записать" if enable else "удалить", value_name, exc)
            return False
