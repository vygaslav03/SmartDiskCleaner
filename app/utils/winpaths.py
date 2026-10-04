"""Работа с путями Windows: Known Folders, длинные пути, нормализация, атрибуты.

Модуль импортируется и на других ОС (для тестов) — Windows-специфичные вызовы
защищены проверкой IS_WINDOWS.
"""

from __future__ import annotations

import os
import stat
import string
import sys
import uuid
from functools import lru_cache

IS_WINDOWS = sys.platform == "win32"

# Атрибуты файлов Windows (winnt.h)
FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

CLOUD_ONLY_MASK = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

_LONG_PREFIX = "\\\\?\\"
_LONG_UNC_PREFIX = "\\\\?\\UNC\\"

# KNOWNFOLDERID (KnownFolders.h)
KNOWN_FOLDERS = {
    "Profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "LocalAppData": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "RoamingAppData": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    "ProgramData": "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
    "ProgramFiles": "{905E63B6-C1BF-494E-B29C-65B732D3D21A}",
    "ProgramFilesX86": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}",
    "Windows": "{F38BF404-1D43-42F2-9305-67DE0B28FC23}",
}

_FALLBACKS = {
    "Profile": lambda: os.path.expanduser("~"),
    "Desktop": lambda: os.path.join(os.path.expanduser("~"), "Desktop"),
    "Documents": lambda: os.path.join(os.path.expanduser("~"), "Documents"),
    "Downloads": lambda: os.path.join(os.path.expanduser("~"), "Downloads"),
    "Pictures": lambda: os.path.join(os.path.expanduser("~"), "Pictures"),
    "Videos": lambda: os.path.join(os.path.expanduser("~"), "Videos"),
    "Music": lambda: os.path.join(os.path.expanduser("~"), "Music"),
    "LocalAppData": lambda: os.environ.get("LOCALAPPDATA", ""),
    "RoamingAppData": lambda: os.environ.get("APPDATA", ""),
    "ProgramData": lambda: os.environ.get("ProgramData", ""),
    "ProgramFiles": lambda: os.environ.get("ProgramFiles", ""),
    "ProgramFilesX86": lambda: os.environ.get("ProgramFiles(x86)", ""),
    "Windows": lambda: os.environ.get("SystemRoot", os.environ.get("windir", "")),
}


def _sh_get_known_folder(folder_id: str) -> str | None:
    """SHGetKnownFolderPath — учитывает перенос папок (OneDrive, другой диск)."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    out = ctypes.c_wchar_p()
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    try:
        if hr != 0 or not out.value:
            return None
        return out.value
    finally:
        if out:
            ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))


@lru_cache(maxsize=None)
def known_folder(name: str) -> str:
    """Реальный путь Known Folder ('' если не удалось определить)."""
    path = None
    fid = KNOWN_FOLDERS.get(name)
    if fid:
        try:
            path = _sh_get_known_folder(fid)
        except (OSError, AttributeError, ValueError):
            path = None
    if not path:
        fb = _FALLBACKS.get(name)
        path = fb() if fb else ""
    return path or ""


def user_data_folders() -> dict[str, str]:
    """Пользовательские папки, которые программа никогда не чистит как «мусор»."""
    return {
        name: known_folder(name)
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")
    }


def onedrive_roots() -> list[str]:
    roots = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        val = os.environ.get(var)
        if val and val not in roots:
            roots.append(val)
    return roots


def strip_long_prefix(path: str) -> str:
    if path.startswith(_LONG_UNC_PREFIX):
        return "\\\\" + path[len(_LONG_UNC_PREFIX):]
    if path.startswith(_LONG_PREFIX):
        return path[len(_LONG_PREFIX):]
    return path


def long_path(path: str) -> str:
    """Добавляет префикс \\\\?\\ для обхода ограничения MAX_PATH (260 символов).

    На не-Windows возвращает путь без изменений.
    """
    if not IS_WINDOWS:
        return path
    if path.startswith(_LONG_PREFIX):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return _LONG_UNC_PREFIX + absolute[2:]
    return _LONG_PREFIX + absolute


def normalize(path: str) -> str:
    """Каноническая форма для сравнения: абсолютный, без '..', регистр по правилам ОС."""
    return os.path.normcase(os.path.abspath(strip_long_prefix(path)))


def real_normalized(path: str) -> str:
    """Нормализованный путь после раскрытия symlink/junction и 8.3-имён."""
    try:
        resolved = os.path.realpath(strip_long_prefix(path))
    except (OSError, ValueError):
        resolved = path
    return normalize(resolved)


def is_within(child: str, parent: str, strict: bool = False) -> bool:
    """True, если child лежит внутри parent. Оба пути должны быть нормализованы.

    strict=True — child не может совпадать с parent.
    Корректно обрабатывает пары вида C:\\Temp и C:\\Temp2 (не вложены).
    """
    if not child or not parent:
        return False
    if child == parent:
        return not strict
    # Быстрая проверка по префиксу (вызывается для каждого файла при сканировании).
    # Оба пути нормализованы normalize(): единый разделитель, без '..' и хвостового '\'
    # (кроме корня диска 'c:\'), поэтому 'c:\temp2' не считается вложенным в 'c:\temp'.
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def file_attributes(st: os.stat_result) -> int:
    return int(getattr(st, "st_file_attributes", 0) or 0)


def is_reparse_or_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(file_attributes(st) & FILE_ATTRIBUTE_REPARSE_POINT)


def is_system_file(st: os.stat_result) -> bool:
    return bool(file_attributes(st) & FILE_ATTRIBUTE_SYSTEM)


def is_cloud_only(st: os.stat_result) -> bool:
    return bool(file_attributes(st) & CLOUD_ONLY_MASK)


def system_drive() -> str:
    drive = os.environ.get("SystemDrive", "C:")
    return drive.rstrip("\\/") + "\\"


def list_drives() -> list[str]:
    """Список локальных дисков вида 'C:\\'. Сетевые и CD-ROM без носителя пропускаются."""
    drives: list[str] = []
    try:
        import psutil

        for part in psutil.disk_partitions(all=False):
            opts = (part.opts or "").lower()
            if "cdrom" in opts or not part.fstype:
                continue
            mp = part.mountpoint
            if mp and mp not in drives:
                drives.append(mp)
    except Exception:  # noqa: BLE001 - psutil может отсутствовать/падать на экзотике
        drives = []
    if not drives and IS_WINDOWS:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                if os.path.isdir(root):
                    drives.append(root)
    if not drives:
        drives = [os.path.abspath(os.sep)]
    return drives


def volume_label(drive: str) -> str:
    if not IS_WINDOWS:
        return ""
    import ctypes

    buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(drive), buf, 261, None, None, None, fs_buf, 261
    )
    return buf.value if ok else ""


def drive_of(path: str) -> str:
    """'C:\\Windows\\Temp' -> 'c:\\' (нормализовано)."""
    drv, _ = os.path.splitdrive(strip_long_prefix(path))
    return os.path.normcase(drv + "\\") if drv else os.path.normcase(os.sep)
