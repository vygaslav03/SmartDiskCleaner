"""Модуль безопасности: решает, МОЖНО ли трогать конкретный файл.

Принцип — белый список. Файл может быть удалён, только если ВСЕ условия верны:

1. путь абсолютный, без '..' и NUL-символов (защита от path traversal);
2. после раскрытия symlink / junction / 8.3-имён путь не изменился
   (файл не «уводит» за пределы разрешённой папки);
3. это обычный файл: не папка, не reparse point, не системный (атрибут SYSTEM);
4. имя не входит в список критичных (pagefile.sys, ntuser.dat и т.п.);
5. файл подпадает под одно из правил (ResolvedRule) своей категории;
6. файл не лежит в запрещённой зоне (System32, Program Files, Documents, ...);
7. размер и дата изменения совпадают с зафиксированными при сканировании.

Правила тоже проверяются: корень правила не может быть корнем диска, не может
содержать защищённую папку (например, весь профиль или AppData целиком) и не
может лежать внутри запрещённой зоны.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.models.file_item import FileItem
from app.utils import winpaths
from app.utils.winpaths import (
    is_reparse_or_link,
    is_system_file,
    is_within,
    long_path,
    normalize,
    real_normalized,
)

# Коды причин отказа (переводятся в UI через i18n "reason.<code>")
R_INVALID_PATH = "invalid_path"
R_TRAVERSAL = "traversal"
R_REDIRECTED = "redirected"
R_NOT_FOUND = "not_found"
R_INACCESSIBLE = "inaccessible"
R_DIRECTORY = "is_directory"
R_REPARSE = "reparse_point"
R_SYSTEM = "system_file"
R_PROTECTED_NAME = "protected_name"
R_OUTSIDE = "outside_allowed"
R_PROTECTED_LOCATION = "protected_location"
R_CHANGED = "changed_since_scan"
R_VIRTUAL = "virtual_item"
R_NOT_SCANNED = "not_scanned"
R_LAST_COPY = "last_copy"
R_BAD_RULE = "bad_rule"

# Файлы, которые нельзя удалять ни при каких условиях (сравнение без учёта регистра).
DENIED_NAME_PATTERNS: tuple[str, ...] = (
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    "bootmgr",
    "bootnxt",
    "bootsect.bak",
    "ntuser.dat*",
    "ntuser.ini",
    "ntuser.pol",
    "usrclass.dat*",
    "desktop.ini",
    "*.sys",
)

_MTIME_TOLERANCE = 2.0  # FAT/exFAT хранят время с шагом 2 секунды


def _norm_list(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if not p:
            continue
        n = real_normalized(p)
        if n not in out:
            out.append(n)
    return out


def has_traversal(path: str) -> bool:
    """True, если в пути есть компонент '..'."""
    parts = path.replace("/", "\\").split("\\")
    return any(part == ".." for part in parts) or any(
        part == ".." for part in path.split(os.sep)
    )


def validate_raw_path(path: object) -> str | None:
    """Проверка «сырого» пути до любых обращений к ФС. Возвращает код причины или None."""
    if not isinstance(path, str) or not path.strip():
        return R_INVALID_PATH
    if "\x00" in path:
        return R_INVALID_PATH
    stripped = winpaths.strip_long_prefix(path)
    if not os.path.isabs(stripped):
        return R_INVALID_PATH
    if has_traversal(stripped):
        return R_TRAVERSAL
    return None


@dataclass(frozen=True)
class ResolvedRule:
    """Конкретное правило очистки после подстановки переменных и glob.

    root — нормализованный реальный путь папки.
    recursive=False — разрешены только файлы, лежащие непосредственно в root.
    """

    category_id: str
    root: str
    patterns: tuple[str, ...] = ("*",)
    recursive: bool = True
    min_age_seconds: float = 0.0
    # Путь с исходным регистром — для обхода и отображения (root — для сравнения).
    root_display: str = ""

    @property
    def walk_root(self) -> str:
        return self.root_display or self.root

    def matches(self, norm_path: str) -> bool:
        if self.recursive:
            if not is_within(norm_path, self.root, strict=True):
                return False
        elif os.path.dirname(norm_path) != self.root:
            return False
        name = os.path.normcase(os.path.basename(norm_path))
        return any(fnmatch.fnmatchcase(name, os.path.normcase(p)) for p in self.patterns)


def name_is_denied(norm_path: str) -> bool:
    name = os.path.basename(norm_path).lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) for p in DENIED_NAME_PATTERNS)


class SafetyPolicy:
    """Набор защищённых зон и проверок.

    forbidden_roots — внутри этих папок ничего удалять нельзя.
    container_roots — папки-«контейнеры» (профиль, AppData, Windows, корни дисков):
        правило может указывать на подпапку внутри них, но не на них целиком.
    broad_roots_allowed — для поиска больших файлов/дубликатов корень поиска
        может быть широким (например, весь диск D:\\), файлы при этом всё равно
        фильтруются по forbidden_roots.
    """

    def __init__(
        self,
        forbidden_roots: Iterable[str],
        container_roots: Iterable[str] = (),
        broad_roots_allowed: bool = False,
        deny_system_files: bool = True,
    ) -> None:
        self.forbidden_roots: list[str] = _norm_list(forbidden_roots)
        self.container_roots: list[str] = _norm_list(container_roots)
        self.broad_roots_allowed = broad_roots_allowed
        self.deny_system_files = deny_system_files

    # ------------------------------------------------------------------ фабрики
    @staticmethod
    def _per_drive_system_dirs() -> list[str]:
        out = []
        for drive in winpaths.list_drives():
            for name in (
                "System Volume Information",
                "$Recycle.Bin",
                "Recovery",
                "$WinREAgent",
                "Config.Msi",
                "Boot",
                "EFI",
            ):
                out.append(os.path.join(drive, name))
        return out

    @staticmethod
    def _app_private_dirs() -> list[str]:
        """Собственные папки программы (карантин), которые сканеры не должны видеть."""
        try:
            from app.utils.paths import quarantine_dir

            return [str(quarantine_dir())]
        except Exception:  # noqa: BLE001
            return []

    @classmethod
    def for_junk(
        cls, allow_program_files: bool = False, exclusions: Sequence[str] = ()
    ) -> "SafetyPolicy":
        kf = winpaths.known_folder
        windows = kf("Windows")
        forbidden = [
            *winpaths.user_data_folders().values(),
            *winpaths.onedrive_roots(),
            *cls._per_drive_system_dirs(),
            *cls._app_private_dirs(),
            *exclusions,
        ]
        if windows:
            forbidden += [
                os.path.join(windows, sub)
                for sub in (
                    "System32",
                    "SysWOW64",
                    "SysArm32",
                    "WinSxS",
                    "Installer",
                    "Boot",
                    "Fonts",
                    "servicing",
                    "assembly",
                    "Microsoft.NET",
                    "SystemApps",
                    "SystemResources",
                    "Resources",
                    "INF",
                    "Panther",
                )
            ]
        public = os.path.join(os.path.dirname(kf("Profile")), "Public") if kf("Profile") else ""
        if public:
            forbidden += [
                os.path.join(public, sub)
                for sub in ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos")
            ]
        local_low = (
            os.path.join(os.path.dirname(kf("LocalAppData")), "LocalLow") if kf("LocalAppData") else ""
        )
        containers = [
            *winpaths.list_drives(),
            kf("Profile"),
            public,
            local_low,
            os.path.dirname(kf("LocalAppData")) if kf("LocalAppData") else "",
            os.path.dirname(kf("Profile")) if kf("Profile") else "",
            kf("LocalAppData"),
            kf("RoamingAppData"),
            kf("ProgramData"),
            windows,
        ]
        program_files = [kf("ProgramFiles"), kf("ProgramFilesX86")]
        if allow_program_files:
            containers += program_files
            forbidden += [os.path.join(p, "WindowsApps") for p in program_files if p]
        else:
            forbidden += program_files
        return cls(forbidden, containers, broad_roots_allowed=False)

    @classmethod
    def for_user_files(
        cls, allow_program_files: bool = False, exclusions: Sequence[str] = ()
    ) -> "SafetyPolicy":
        """Политика для больших файлов и дубликатов (пользовательские данные)."""
        kf = winpaths.known_folder
        forbidden = [
            kf("Windows"),
            kf("ProgramData"),
            kf("LocalAppData"),
            kf("RoamingAppData"),
            *cls._per_drive_system_dirs(),
            *cls._app_private_dirs(),
            *exclusions,
        ]
        program_files = [kf("ProgramFiles"), kf("ProgramFilesX86")]
        if allow_program_files:
            forbidden += [os.path.join(p, "WindowsApps") for p in program_files if p]
        else:
            forbidden += program_files
        return cls(forbidden, [*winpaths.list_drives(), kf("Profile")], broad_roots_allowed=True)

    # ---------------------------------------------------------------- проверки
    def location_forbidden(self, norm_path: str) -> bool:
        return any(is_within(norm_path, f) for f in self.forbidden_roots)

    def is_excluded_dir(self, norm_dir: str) -> bool:
        """Для сканеров: папку не нужно обходить вовсе."""
        return self.location_forbidden(norm_dir)

    def validate_rule_root(
        self, root: str, recursive: bool = True, patterns: Sequence[str] = ("*",)
    ) -> str | None:
        """Проверка корня правила. Возвращает код причины или None, если корень допустим.

        Нерекурсивное правило с конкретной маской (например, MEMORY.DMP в C:\\Windows)
        допустимо и для папки-контейнера: оно разрешает только файлы с этим именем
        прямо в этой папке. Маски '*' / '*.*' для контейнеров запрещены.
        """
        reason = validate_raw_path(root)
        if reason:
            return reason
        norm = real_normalized(root)
        if self.location_forbidden(norm):
            return R_PROTECTED_LOCATION
        if self.broad_roots_allowed:
            return None
        if not patterns:
            return R_BAD_RULE
        if not recursive and not any(p.strip() in ("*", "*.*", "") for p in patterns):
            if os.path.dirname(norm) == norm:  # корень диска — никогда
                return R_BAD_RULE
            return None
        # Корень правила не может совпадать с контейнером (профиль, AppData, Windows...)
        # или содержать его. Запрещённые зоны внутри корня допустимы: сканер их
        # пропускает, а check_before_delete отклоняет файлы из них.
        for protected in self.container_roots:
            if is_within(protected, norm):
                return R_BAD_RULE
        # Корень диска
        if os.path.dirname(norm) == norm:
            return R_BAD_RULE
        return None

    def check_candidate(
        self, norm_path: str, st: os.stat_result, rules: Sequence[ResolvedRule]
    ) -> str | None:
        """Быстрая проверка во время сканирования (stat уже получен через scandir)."""
        if stat.S_ISDIR(st.st_mode):
            return R_DIRECTORY
        if is_reparse_or_link(st):
            return R_REPARSE
        if self.deny_system_files and is_system_file(st):
            return R_SYSTEM
        if name_is_denied(norm_path):
            return R_PROTECTED_NAME
        if not any(r.matches(norm_path) for r in rules):
            return R_OUTSIDE
        if self.location_forbidden(norm_path):
            return R_PROTECTED_LOCATION
        return None

    def check_before_delete(self, item: FileItem, rules: Sequence[ResolvedRule]) -> str | None:
        """Полная проверка непосредственно перед удалением. None — удалять можно."""
        if item.virtual:
            return R_VIRTUAL
        reason = validate_raw_path(item.path)
        if reason:
            return reason
        norm = normalize(item.path)
        if real_normalized(item.path) != norm:
            return R_REDIRECTED
        try:
            st = os.lstat(long_path(item.path))
        except FileNotFoundError:
            return R_NOT_FOUND
        except (OSError, ValueError):
            return R_INACCESSIBLE
        reason = self.check_candidate(norm, st, rules)
        if reason:
            return reason
        if st.st_size != item.size or abs(st.st_mtime - item.mtime) > _MTIME_TOLERANCE:
            return R_CHANGED
        return None
