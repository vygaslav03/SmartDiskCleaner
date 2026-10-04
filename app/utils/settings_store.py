"""Настройки приложения: JSON-файл рядом с программой (или в %LOCALAPPDATA%)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from app.utils.logger import get_logger
from app.utils.paths import data_dir

log = get_logger("settings")

LANGUAGES = ("ru", "uk", "en")
THEMES = ("system", "dark", "light")
DELETE_MODES = ("recycle", "permanent")


@dataclass
class Settings:
    language: str = "ru"
    theme: str = "system"
    # Не трогать временные файлы моложе N часов (их может использовать
    # работающий установщик или программа).
    temp_min_age_hours: int = 24
    # Логи старше N дней считаются старыми.
    log_min_age_days: int = 7
    large_file_threshold_mb: int = 1024
    large_files_max_results: int = 2000
    duplicates_min_size_kb: int = 1024
    duplicate_folders: list[str] = field(default_factory=list)
    # Пользовательские исключения: сюда программа никогда не заходит.
    exclusions: list[str] = field(default_factory=list)
    # Разрешить чистить кэши внутри Program Files (например, Steam).
    allow_program_files: bool = False
    # Большие файлы и дубликаты: в корзину (по умолчанию) или навсегда.
    user_files_delete_mode: str = "recycle"

    def validate(self) -> "Settings":
        if self.language not in LANGUAGES:
            self.language = "ru"
        if self.theme not in THEMES:
            self.theme = "system"
        if self.user_files_delete_mode not in DELETE_MODES:
            self.user_files_delete_mode = "recycle"
        self.temp_min_age_hours = _clamp_int(self.temp_min_age_hours, 0, 24 * 30, 24)
        self.log_min_age_days = _clamp_int(self.log_min_age_days, 0, 365, 7)
        self.large_file_threshold_mb = _clamp_int(self.large_file_threshold_mb, 10, 1024 * 1024, 1024)
        self.large_files_max_results = _clamp_int(self.large_files_max_results, 100, 50000, 2000)
        self.duplicates_min_size_kb = _clamp_int(self.duplicates_min_size_kb, 1, 1024 * 1024, 1024)
        self.duplicate_folders = _str_list(self.duplicate_folders)
        self.exclusions = _str_list(self.exclusions)
        self.allow_program_files = bool(self.allow_program_files)
        return self


def _clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and v.strip() and v not in out:
            out.append(v.strip())
    return out


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("settings root is not an object")
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in raw.items() if k in known}).validate()
    except FileNotFoundError:
        return Settings().validate()
    except (OSError, ValueError, TypeError, UnicodeDecodeError) as exc:
        log.warning("Не удалось прочитать настройки (%s), используются значения по умолчанию", exc)
        return Settings().validate()


def save_settings(settings: Settings, path: Path | None = None) -> bool:
    path = path or settings_path()
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(settings.validate()), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.error("Не удалось сохранить настройки: %s", exc)
        return False
