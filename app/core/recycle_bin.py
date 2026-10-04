"""Корзина Windows через Shell API.

* размер и количество — SHQueryRecycleBinW;
* очистка — SHEmptyRecycleBinW (без диалогов, без звука);
* список содержимого для «Подробнее» — разбор служебных файлов $I* в
  <диск>:\\$Recycle.Bin\\<SID текущего пользователя> (только чтение).

Файлы корзины никогда не удаляются напрямую через os.remove — только через API.
"""

from __future__ import annotations

import ctypes
import os
import struct
from dataclasses import dataclass

from app.models.file_item import FileItem
from app.utils.logger import get_logger
from app.utils.winpaths import IS_WINDOWS

log = get_logger("recycle_bin")

SHERB_NOCONFIRMATION = 0x1
SHERB_NOPROGRESSUI = 0x2
SHERB_NOSOUND = 0x4

_FILETIME_EPOCH_DIFF = 11644473600  # секунд между 1601-01-01 и 1970-01-01


@dataclass
class RecycleBinInfo:
    size: int = 0
    count: int = 0
    available: bool = False


if IS_WINDOWS:
    from ctypes import wintypes

    class _SHQUERYRBINFO(ctypes.Structure):
        # В shellapi.h структура упакована по 1 байту только на 32-битных системах.
        if struct.calcsize("P") == 4:
            _pack_ = 1
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("i64Size", ctypes.c_longlong),
            ("i64NumItems", ctypes.c_longlong),
        ]


def _drive_root(drive: str) -> str:
    drive = drive.strip()
    if len(drive) >= 2 and drive[1] == ":":
        return drive[:2] + "\\"
    return drive


def query(drive: str) -> RecycleBinInfo:
    """Размер и число элементов корзины на диске (для текущего пользователя)."""
    if not IS_WINDOWS:
        return RecycleBinInfo()
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        fn = ctypes.windll.shell32.SHQueryRecycleBinW
        fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SHQUERYRBINFO)]
        fn.restype = ctypes.c_long
        hr = fn(_drive_root(drive), ctypes.byref(info))
        if hr != 0:
            log.warning("SHQueryRecycleBinW вернул 0x%08X", hr & 0xFFFFFFFF)
            return RecycleBinInfo()
        return RecycleBinInfo(size=int(info.i64Size), count=int(info.i64NumItems), available=True)
    except (OSError, AttributeError) as exc:
        log.warning("Не удалось получить информацию о корзине: %s", exc)
        return RecycleBinInfo()


def empty(drive: str) -> bool:
    """Очищает корзину на указанном диске. True — успешно (или уже пуста)."""
    if not IS_WINDOWS:
        return False
    try:
        fn = ctypes.windll.shell32.SHEmptyRecycleBinW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
        fn.restype = ctypes.c_long
        hr = fn(None, _drive_root(drive), SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
    except (OSError, AttributeError) as exc:
        log.error("SHEmptyRecycleBinW недоступна: %s", exc)
        return False
    if hr == 0:
        return True
    # E_UNEXPECTED возвращается, если корзина уже пуста.
    after = query(drive)
    ok = after.available and after.count == 0
    if not ok:
        log.warning("SHEmptyRecycleBinW вернул 0x%08X", hr & 0xFFFFFFFF)
    return ok


def current_user_sid() -> str | None:
    """Строковый SID текущего пользователя (S-1-5-21-...)."""
    if not IS_WINDOWS:
        return None
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            return None
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, TokenUser, buf, needed, ctypes.byref(needed)):
            return None
        # TOKEN_USER { SID_AND_ATTRIBUTES User { PSID Sid; DWORD Attributes; } }
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_str = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(sid_str)):
            return None
        try:
            return sid_str.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_str, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def parse_info_file(data: bytes) -> tuple[str, int, float] | None:
    """Разбор $I-файла: (исходный путь, размер, время удаления unix).

    Версия 1 (Vista–8.1): путь фиксированной длины 260 символов.
    Версия 2 (Windows 10/11): длина пути в символах + путь.
    """
    if len(data) < 24:
        return None
    version, size, filetime = struct.unpack_from("<qqq", data, 0)
    try:
        if version == 1:
            raw = data[24 : 24 + 520]
            name = raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
        elif version == 2:
            if len(data) < 28:
                return None
            (length,) = struct.unpack_from("<I", data, 24)
            raw = data[28 : 28 + length * 2]
            name = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        else:
            return None
    except (UnicodeDecodeError, struct.error):
        return None
    deleted = filetime / 10_000_000 - _FILETIME_EPOCH_DIFF if filetime > 0 else 0.0
    return name, max(0, int(size)), max(0.0, deleted)


def list_entries(drive: str, limit: int = 5000) -> list[FileItem]:
    """Содержимое корзины текущего пользователя (только для показа, virtual=True)."""
    if not IS_WINDOWS:
        return []
    sid = current_user_sid()
    if not sid:
        return []
    folder = os.path.join(_drive_root(drive), "$Recycle.Bin", sid)
    items: list[FileItem] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if len(items) >= limit:
                    break
                if not entry.name.upper().startswith("$I"):
                    continue
                try:
                    with open(entry.path, "rb") as fh:
                        parsed = parse_info_file(fh.read(4096))
                except OSError:
                    continue
                if not parsed:
                    continue
                original, size, deleted = parsed
                items.append(
                    FileItem(
                        path=original or entry.path,
                        size=size,
                        mtime=deleted,
                        category_id="recycle_bin",
                        virtual=True,
                    )
                )
    except OSError as exc:
        log.info("Нет доступа к содержимому корзины %s: %s", folder, exc)
    items.sort(key=lambda i: i.size, reverse=True)
    return items
