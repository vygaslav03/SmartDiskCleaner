"""Пути приложения: ресурсы (в т.ч. внутри PyInstaller-сборки), данные, логи."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app import __app_name__


def is_frozen() -> bool:
    """True, если запущено из собранного PyInstaller'ом .exe."""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """Корень проекта (в dev-режиме) или папка с .exe (в собранном виде)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_path(relative: str) -> Path:
    """Путь к ресурсу (assets/...). В onefile-сборке ресурсы лежат в sys._MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return project_root() / relative


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        probe.unlink()
        return True
    except OSError:
        return False


_DATA_DIR: Path | None = None


def data_dir() -> Path:
    """Папка для settings.json и logs/.

    Портативный режим: рядом с программой (удобно передавать друзьям).
    Если туда писать нельзя (например, Program Files) — %LOCALAPPDATA%\\DiskCleaner.
    """
    global _DATA_DIR
    if _DATA_DIR is not None:
        return _DATA_DIR
    candidates = [project_root()]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / __app_name__)
    candidates.append(Path.home() / f".{__app_name__.lower()}")
    for cand in candidates:
        if _is_writable_dir(cand):
            _DATA_DIR = cand
            return cand
    _DATA_DIR = candidates[-1]
    return _DATA_DIR


def quarantine_dir() -> Path:
    """Папка карантина: %LOCALAPPDATA%\\DiskCleaner\\Quarantine (тот же диск, что и мусор)."""
    local = os.environ.get("LOCALAPPDATA")
    try:
        from app.utils.winpaths import known_folder

        local = known_folder("LocalAppData") or local
    except Exception:  # noqa: BLE001
        pass
    if local:
        return Path(local) / __app_name__ / "Quarantine"
    return data_dir() / "quarantine"


def logs_dir() -> Path:
    return data_dir() / "logs"
