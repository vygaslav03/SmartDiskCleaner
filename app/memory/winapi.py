"""Тонкие обёртки ctypes над Windows API для модуля памяти.

Все функции чтения работают БЕЗ прав администратора. Функции, меняющие систему
(empty_working_set, purge_standby_list, post_close_to_windows), здесь только
объявлены — вызываются они исключительно из app.memory.actions после
подтверждения пользователя.

На не-Windows модуль импортируется, но функции возвращают None / пустые значения.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Iterator

from app.memory.models import PagefileInfo
from app.utils.logger import get_logger

log = get_logger("memory.winapi")

IS_WINDOWS = sys.platform == "win32"

# ------------------------------------------------------------------ structures


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),  # = Commit Limit
        ("ullAvailPageFile", ctypes.c_ulonglong),  # = Commit Limit − Commit Charge
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    # Значения SIZE_T — в СТРАНИЦАХ (умножать на PageSize).
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class SYSTEM_PAGEFILE_INFORMATION(ctypes.Structure):
    # Размеры — в страницах.
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("TotalSize", wintypes.ULONG),
        ("TotalInUse", wintypes.ULONG),
        ("PeakUsage", wintypes.ULONG),
        ("PageFileName", UNICODE_STRING),
    ]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    # union начинается со смещения 8 (выравнивание double)
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
ERROR_SUCCESS = 0
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
SystemPageFileInformation = 18
SystemMemoryListInformation = 80
MemoryPurgeStandbyList = 4
MemoryPurgeLowPriorityStandbyList = 5

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
WM_CLOSE = 0x0010

# ------------------------------------------------------------------ helpers


def _dll(name: str):
    return ctypes.WinDLL(name, use_last_error=True)


def global_memory_status() -> MEMORYSTATUSEX | None:
    if not IS_WINDOWS:
        return None
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    k32 = _dll("kernel32")
    k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not k32.GlobalMemoryStatusEx(ctypes.byref(st)):
        log.warning("GlobalMemoryStatusEx: ошибка %s", ctypes.get_last_error())
        return None
    return st


def performance_info() -> PERFORMANCE_INFORMATION | None:
    if not IS_WINDOWS:
        return None
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(PERFORMANCE_INFORMATION)
    psapi = _dll("psapi")
    psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), wintypes.DWORD]
    psapi.GetPerformanceInfo.restype = wintypes.BOOL
    if not psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        log.warning("GetPerformanceInfo: ошибка %s", ctypes.get_last_error())
        return None
    return pi


def pagefiles() -> list[PagefileInfo] | None:
    """Фактические файлы подкачки (NtQuerySystemInformation, без прав администратора).

    [] — файлов подкачки нет; None — не удалось получить.
    """
    if not IS_WINDOWS:
        return None
    ntdll = _dll("ntdll")
    ntdll.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                               ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    size = 4096
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        ret_len = wintypes.ULONG(0)
        status = ntdll.NtQuerySystemInformation(SystemPageFileInformation, buf, size, ctypes.byref(ret_len))
        status &= 0xFFFFFFFF
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, ret_len.value + 256)
            continue
        if status != 0:
            log.warning("NtQuerySystemInformation(PageFile): 0x%08X", status)
            return None
        if ret_len.value == 0:
            return []
        page = _page_size()
        result: list[PagefileInfo] = []
        base = ctypes.addressof(buf)
        offset = 0
        while offset + ctypes.sizeof(SYSTEM_PAGEFILE_INFORMATION) <= size:
            entry = SYSTEM_PAGEFILE_INFORMATION.from_address(base + offset)
            name = ""
            if entry.PageFileName.Buffer and entry.PageFileName.Length:
                name = ctypes.wstring_at(entry.PageFileName.Buffer, entry.PageFileName.Length // 2)
            result.append(PagefileInfo(
                name=_clean_nt_path(name),
                total=entry.TotalSize * page,
                in_use=entry.TotalInUse * page,
                peak=entry.PeakUsage * page,
            ))
            if not entry.NextEntryOffset:
                break
            offset += entry.NextEntryOffset
        return result
    return None


def _clean_nt_path(name: str) -> str:
    # \??\C:\pagefile.sys -> C:\pagefile.sys
    for prefix in ("\\??\\", "\\\\?\\"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


_PAGE_SIZE: int | None = None


def _page_size() -> int:
    global _PAGE_SIZE
    if _PAGE_SIZE is None:
        pi = performance_info()
        _PAGE_SIZE = int(pi.PageSize) if pi and pi.PageSize else 4096
    return _PAGE_SIZE


# ------------------------------------------------------------------ PDH


class PdhQuery:
    """Долгоживущий запрос PDH. Имена счётчиков — английские (PdhAddEnglishCounterW),
    поэтому работает на русской/украинской Windows. Счётчики-скорости дают значение
    со второй выборки.
    """

    def __init__(self, paths: dict[str, str]) -> None:
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}
        self.ok = False
        if not IS_WINDOWS:
            return
        try:
            self._pdh = _dll("pdh")
            self._pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self._pdh.PdhOpenQueryW.restype = ctypes.c_long
            self._pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
                                                        ctypes.POINTER(ctypes.c_void_p)]
            self._pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
            self._pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self._pdh.PdhCollectQueryData.restype = ctypes.c_long
            self._pdh.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                              ctypes.POINTER(wintypes.DWORD),
                                                              ctypes.POINTER(PDH_FMT_COUNTERVALUE)]
            self._pdh.PdhGetFormattedCounterValue.restype = ctypes.c_long
            self._pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            self._pdh.PdhCloseQuery.restype = ctypes.c_long
            if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != ERROR_SUCCESS:
                log.warning("PdhOpenQueryW не удался — списки страниц будут недоступны")
                return
            for key, path in paths.items():
                handle = ctypes.c_void_p()
                status = self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle))
                if status == ERROR_SUCCESS:
                    self._counters[key] = handle
                else:
                    log.info("Счётчик PDH недоступен: %s (0x%08X)", path, status & 0xFFFFFFFF)
            self.ok = bool(self._counters)
        except (OSError, AttributeError) as exc:
            log.warning("PDH недоступен: %s", exc)
            self.ok = False

    def collect(self) -> dict[str, float]:
        """Одна выборка. Ключи без валидного значения отсутствуют в результате."""
        if not self.ok:
            return {}
        if self._pdh.PdhCollectQueryData(self._query) != ERROR_SUCCESS:
            return {}
        values: dict[str, float] = {}
        for key, handle in self._counters.items():
            val = PDH_FMT_COUNTERVALUE()
            status = self._pdh.PdhGetFormattedCounterValue(
                handle, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(val)
            )
            # CStatus 0 (VALID_DATA) или 1 (NEW_DATA) — значение корректно
            if status == ERROR_SUCCESS and val.CStatus in (0, 1):
                values[key] = float(val.doubleValue)
        return values

    def close(self) -> None:
        if IS_WINDOWS and self._query:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except OSError:
                pass
        self._query = ctypes.c_void_p()
        self.ok = False


# ------------------------------------------------------------------ windows / processes


_USER32 = None


def _user32():
    """user32 с объявленными сигнатурами (HWND — указатель, без усечения на x64)."""
    global _USER32
    if _USER32 is None:
        u = _dll("user32")
        u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        _USER32 = u
    return _USER32


_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if IS_WINDOWS else None



def pids_with_visible_windows() -> set[int]:
    """PID процессов, у которых есть видимое окно верхнего уровня с заголовком."""
    if not IS_WINDOWS:
        return set()
    user32 = _user32()
    result: set[int] = set()

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 \
                and not user32.GetWindow(hwnd, 4):  # GW_OWNER: пропускаем диалоги-владельцы
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.add(pid.value)
        return True

    cb = _ENUM_PROC(callback)  # ссылка держится до конца вызова
    user32.EnumWindows(cb, 0)
    return result


def iter_top_windows(pid: int) -> Iterator[int]:
    """Видимые окна верхнего уровня процесса (для мягкого закрытия)."""
    if not IS_WINDOWS:
        return iter(())
    user32 = _user32()
    found: list[int] = []

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            found.append(hwnd)
        return True

    cb = _ENUM_PROC(callback)
    user32.EnumWindows(cb, 0)
    return iter(found)


def post_close_to_windows(pid: int) -> int:
    """Посылает WM_CLOSE видимым окнам процесса — как нажатие крестика.

    Программа сама решает, закрыться ли (и может спросить про несохранённые данные).
    Возвращает число окон, которым отправлено сообщение.
    """
    if not IS_WINDOWS:
        return 0
    user32 = _user32()
    sent = 0
    for hwnd in iter_top_windows(pid):
        if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            sent += 1
    return sent


def empty_working_set(pid: int) -> bool:
    """EmptyWorkingSet: страницы процесса уходят в standby/modified и вернутся при обращении."""
    if not IS_WINDOWS:
        return False
    k32 = _dll("kernel32")
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = _dll("psapi")
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.restype = wintypes.BOOL
    handle = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PermissionError(f"OpenProcess({pid}): ошибка {ctypes.get_last_error()}")
    try:
        if not psapi.EmptyWorkingSet(handle):
            raise OSError(f"EmptyWorkingSet({pid}): ошибка {ctypes.get_last_error()}")
        return True
    finally:
        k32.CloseHandle(handle)


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


def _enable_privilege(name: str) -> bool:
    advapi = _dll("advapi32")
    k32 = _dll("kernel32")
    TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY, SE_PRIVILEGE_ENABLED = 0x20, 0x8, 0x2
    token = wintypes.HANDLE()
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                   ctypes.byref(token)):
        return False
    try:
        luid = LUID()
        advapi.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
        if not advapi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED)))
        advapi.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
                                                 wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0  # ERROR_NOT_ALL_ASSIGNED = 1300 — привилегии нет
    finally:
        k32.CloseHandle(token)


def purge_standby_list(low_priority_only: bool) -> None:
    """Сброс standby-списка (нужны права администратора). Кэш придётся читать с диска заново."""
    if not IS_WINDOWS:
        raise OSError("Только Windows")
    if not _enable_privilege("SeProfileSingleProcessPrivilege"):
        raise PermissionError("Нет привилегии SeProfileSingleProcessPrivilege (нужны права администратора)")
    ntdll = _dll("ntdll")
    ntdll.NtSetSystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    ntdll.NtSetSystemInformation.restype = ctypes.c_long
    command = ctypes.c_int(MemoryPurgeLowPriorityStandbyList if low_priority_only else MemoryPurgeStandbyList)
    status = ntdll.NtSetSystemInformation(SystemMemoryListInformation, ctypes.byref(command),
                                          ctypes.sizeof(command)) & 0xFFFFFFFF
    if status != 0:
        raise OSError(f"NtSetSystemInformation(MemoryList): 0x{status:08X}")
