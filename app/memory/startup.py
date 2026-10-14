"""Автозагрузка: чтение и обратимое отключение — тем же способом, что Диспетчер задач.

Записи в ключах Run и файлы в папках «Автозагрузка» НЕ удаляются и не меняются.
Отключение — это флаг в ...\\Explorer\\StartupApproved (12 байт: первый байт 0x03
и время отключения). Поэтому:
  * Диспетчер задач показывает программу как «Отключено» и может включить её обратно;
  * DiskCleaner сохраняет прежнее значение флага и умеет восстановить его (отмена).

Без прав администратора можно менять только записи текущего пользователя (HKCU);
записи для всех пользователей (HKLM) помечаются needs_admin.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.utils.logger import get_logger

log = get_logger("memory.startup")

HKCU, HKLM = "HKCU", "HKLM"
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

# location -> (куст записи, путь записи, куст флага, подключ флага)
LOCATIONS = {
    "hkcu_run": (HKCU, RUN, HKCU, "Run"),
    "hklm_run": (HKLM, RUN, HKLM, "Run"),
    "hklm_run32": (HKLM, RUN32, HKLM, "Run32"),
    "user_folder": (None, None, HKCU, "StartupFolder"),
    "common_folder": (None, None, HKLM, "StartupFolder"),
}


class Registry(Protocol):
    def values(self, hive: str, path: str) -> dict[str, object]: ...
    def get(self, hive: str, path: str, name: str) -> bytes | None: ...
    def set_binary(self, hive: str, path: str, name: str, data: bytes) -> None: ...
    def delete(self, hive: str, path: str, name: str) -> None: ...


@dataclass(slots=True)
class StartupItem:
    id: str
    name: str
    command: str
    location: str
    enabled: bool
    exe_name: str = ""

    @property
    def needs_admin(self) -> bool:
        return LOCATIONS[self.location][2] == HKLM

    @property
    def approved_path(self) -> tuple[str, str]:
        _, _, hive, sub = LOCATIONS[self.location]
        return hive, f"{APPROVED}\\{sub}"


def is_enabled_flag(data: bytes | None) -> bool:
    """Нет флага — включено. Младший бит первого байта = 1 — отключено (0x03, 0x07 ...)."""
    if not data:
        return True
    return (data[0] & 1) == 0


def disabled_flag(now: float | None = None) -> bytes:
    """0x03 + 3 нулевых байта + FILETIME момента отключения (как в Диспетчере задач)."""
    ts = time.time() if now is None else now
    filetime = int((ts + 11644473600) * 10_000_000)
    return bytes([3, 0, 0, 0]) + struct.pack("<Q", filetime)


ENABLED_FLAG = bytes([2]) + bytes(11)


def exe_name_from_command(command: str) -> str:
    """Имя exe из командной строки: '"C:\\a b\\app.exe" --min' -> 'app.exe'."""
    cmd = os.path.expandvars(command.strip())
    if not cmd:
        return ""
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        path = cmd[1:end] if end > 0 else cmd[1:]
    else:
        lower = cmd.lower()
        idx = lower.find(".exe")
        path = cmd[: idx + 4] if idx >= 0 else cmd.split()[0]
    name = path.replace("/", "\\").rsplit("\\", 1)[-1]
    return name if name.lower().endswith(".exe") else ""


def list_startup(reg: Registry, folders: dict[str, Path] | None = None) -> list[StartupItem]:
    items: list[StartupItem] = []
    for location, (hive, path, _flag_hive, _sub) in LOCATIONS.items():
        if hive is None:
            continue
        try:
            entries = reg.values(hive, path)
        except OSError:
            continue
        for name, value in entries.items():
            if not name:
                continue
            item = StartupItem(f"{location}:{name}", name, str(value), location, True,
                               exe_name_from_command(str(value)))
            item.enabled = is_enabled_flag(_get_flag(reg, item))
            items.append(item)
    for location, folder in (folders or {}).items():
        try:
            files = sorted(p for p in folder.iterdir() if p.is_file() and p.name.lower() != "desktop.ini")
        except OSError:
            continue
        for f in files:
            exe = f.name if f.suffix.lower() == ".exe" else ""
            item = StartupItem(f"{location}:{f.name}", f.stem, str(f), location, True, exe)
            item.enabled = is_enabled_flag(_get_flag(reg, item))
            items.append(item)
    return items


def _get_flag(reg: Registry, item: StartupItem) -> bytes | None:
    hive, path = item.approved_path
    try:
        return reg.get(hive, path, _flag_name(item))
    except OSError:
        return None


def _flag_name(item: StartupItem) -> str:
    return item.id.split(":", 1)[1]


def set_enabled(reg: Registry, item: StartupItem, enabled: bool) -> dict:
    """Меняет флаг. Возвращает данные для отмены: {'hive','path','name','previous'(hex|None)}."""
    hive, path = item.approved_path
    name = _flag_name(item)
    previous = reg.get(hive, path, name)
    reg.set_binary(hive, path, name, ENABLED_FLAG if enabled else disabled_flag())
    item.enabled = enabled
    log.info("Автозагрузка: %s -> %s", item.id, "включено" if enabled else "отключено")
    return {"hive": hive, "path": path, "name": name, "previous": previous.hex() if previous is not None else None}


def undo(reg: Registry, data: dict) -> None:
    """Возвращает флаг в состояние до изменения (или удаляет, если флага не было)."""
    previous = data.get("previous")
    if previous is None:
        reg.delete(data["hive"], data["path"], data["name"])
    else:
        reg.set_binary(data["hive"], data["path"], data["name"], bytes.fromhex(previous))


# ------------------------------------------------------------------ Windows backend


class WinRegistry:
    def _hive(self, hive: str):
        import winreg

        return winreg.HKEY_CURRENT_USER if hive == HKCU else winreg.HKEY_LOCAL_MACHINE

    def values(self, hive: str, path: str) -> dict[str, object]:
        import winreg

        result: dict[str, object] = {}
        with winreg.OpenKey(self._hive(hive), path) as key:
            i = 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(key, i)
                except OSError:
                    break
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    result[name] = value
                i += 1
        return result

    def get(self, hive: str, path: str, name: str) -> bytes | None:
        import winreg

        try:
            with winreg.OpenKey(self._hive(hive), path) as key:
                value, kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return bytes(value) if kind == winreg.REG_BINARY and value is not None else None

    def set_binary(self, hive: str, path: str, name: str, data: bytes) -> None:
        import winreg

        with winreg.CreateKeyEx(self._hive(hive), path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, data)

    def delete(self, hive: str, path: str, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(self._hive(hive), path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def startup_folders() -> dict[str, Path]:
    folders: dict[str, Path] = {}
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        folders["user_folder"] = Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    if programdata:
        folders["common_folder"] = Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\StartUp"
    return folders


def list_system_startup() -> list[StartupItem]:
    if os.name != "nt":
        return []
    return list_startup(WinRegistry(), startup_folders())
