"""Определения категорий мусора и их правил.

Каждое правило — шаблон пути с токенами {LOCALAPPDATA}, {SYSTEMROOT} и т.д.
В шаблоне допускаются glob-символы ('*') в подпапках, например профили браузера:
    {LOCALAPPDATA}\\Google\\Chrome\\User Data\\*\\Cache
Токены подставляются из Known Folders (а не только из переменных окружения),
поэтому корректно работают перенесённые папки.

ВАЖНО: правила указывают только на папки КЭША. Cookies, пароли, история,
сессии браузеров и мессенджеров лежат в других файлах/папках и сюда не входят.
"""

from __future__ import annotations

import glob
import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Mapping

from app.core.safety import ResolvedRule, SafetyPolicy
from app.utils import winpaths
from app.utils.logger import get_logger

log = get_logger("categories")

AGE_NONE = "none"
AGE_TEMP = "temp"  # settings.temp_min_age_hours
AGE_LOG = "log"  # settings.log_min_age_days

KIND_FILES = "files"
KIND_RECYCLE = "recycle_bin"
# Очистка встроенными средствами Windows (cleanmgr / DISM) — программа сама файлы не удаляет.
KIND_SYSTEM = "system_tool"

GROUP_ORDER = ("windows", "temp", "browsers", "apps", "logs", "winapp2", "recycle")


@dataclass(frozen=True)
class RuleSpec:
    template: str
    patterns: tuple[str, ...] = ("*",)
    recursive: bool = True
    age: str = AGE_NONE


@dataclass(frozen=True)
class CategoryDef:
    id: str
    group: str
    rules: tuple[RuleSpec, ...] = ()
    kind: str = KIND_FILES
    smart: bool = False  # входит в Smart Clean
    requires_admin: bool = False
    note: str = ""  # ключ i18n с подсказкой (например, «закройте браузер»)
    tags: tuple[str, ...] = field(default_factory=tuple)
    # Для импортированных правил (Winapp2.ini): готовые тексты вместо ключей i18n.
    title: str = ""
    description: str = ""
    excludes: tuple["ExcludeSpec", ...] = ()
    # detect(tokens) -> bool: установлена ли программа; None — категория показывается всегда.
    detect: Callable[[Mapping[str, str]], bool] | None = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class ExcludeSpec:
    """Исключение внутри правила: whole_dir=True — вся папка, иначе файлы по маскам в ней."""

    template: str
    patterns: tuple[str, ...] = ("*",)
    whole_dir: bool = False


def _chromium(cat_id: str, user_data: str, note: str = "note.close_browser") -> CategoryDef:
    """Кэш Chromium-браузеров: только Cache / Code Cache / GPUCache профилей."""
    base = user_data + "\\*"
    return CategoryDef(
        id=cat_id,
        group="browsers",
        smart=True,
        note=note,
        rules=(
            RuleSpec(base + "\\Cache"),
            RuleSpec(base + "\\Code Cache"),
            RuleSpec(base + "\\GPUCache"),
            RuleSpec(base + "\\DawnCache"),
            RuleSpec(base + "\\DawnGraphiteCache"),
            RuleSpec(base + "\\DawnWebGPUCache"),
            RuleSpec(user_data + "\\ShaderCache"),
            RuleSpec(user_data + "\\GrShaderCache"),
            RuleSpec(user_data + "\\GraphiteDawnCache"),
        ),
    )


def _electron(cat_id: str, base: str, smart: bool = True, extra: tuple[RuleSpec, ...] = ()) -> CategoryDef:
    return CategoryDef(
        id=cat_id,
        group="apps",
        smart=smart,
        note="note.close_app",
        rules=(
            RuleSpec(base + "\\Cache"),
            RuleSpec(base + "\\Code Cache"),
            RuleSpec(base + "\\GPUCache"),
            RuleSpec(base + "\\DawnCache"),
            *extra,
        ),
    )


CATEGORIES: tuple[CategoryDef, ...] = (
    # ------------------------------------------------------------- Windows
    CategoryDef(
        id="win_temp",
        group="windows",
        smart=True,
        requires_admin=True,
        rules=(RuleSpec("{SYSTEMROOT}\\Temp", age=AGE_TEMP),),
    ),
    CategoryDef(
        id="win_cache",
        group="windows",
        smart=True,
        rules=(
            RuleSpec("{LOCALAPPDATA}\\Microsoft\\Windows\\INetCache"),
            RuleSpec("{LOCALAPPDATA}\\D3DSCache"),
            RuleSpec("{LOCALAPPDATA}\\NVIDIA\\DXCache"),
            RuleSpec("{LOCALAPPDATA}\\NVIDIA\\GLCache"),
            RuleSpec("{LOCALAPPDATA}\\AMD\\DxCache"),
            RuleSpec("{LOCALAPPDATA}\\AMD\\DxcCache"),
            RuleSpec("{LOCALAPPDATA}\\AMD\\VkCache"),
            RuleSpec("{LOCALAPPDATA}\\Intel\\ShaderCache"),
        ),
    ),
    CategoryDef(
        id="thumbnails",
        group="windows",
        smart=True,
        note="note.thumbnails",
        rules=(
            RuleSpec(
                "{LOCALAPPDATA}\\Microsoft\\Windows\\Explorer",
                patterns=("thumbcache_*.db", "iconcache_*.db"),
                recursive=False,
            ),
        ),
    ),
    CategoryDef(
        id="crash_dumps",
        group="windows",
        smart=True,
        rules=(
            RuleSpec("{LOCALAPPDATA}\\CrashDumps", patterns=("*.dmp",)),
            RuleSpec("{SYSTEMROOT}\\Minidump", patterns=("*.dmp",)),
            RuleSpec("{SYSTEMROOT}", patterns=("MEMORY.DMP",), recursive=False),
            RuleSpec("{SYSTEMROOT}\\LiveKernelReports", patterns=("*.dmp",)),
        ),
    ),
    CategoryDef(
        id="win_update",
        group="windows",
        requires_admin=True,
        note="note.win_update",
        rules=(RuleSpec("{SYSTEMROOT}\\SoftwareDistribution\\Download", age=AGE_TEMP),),
    ),
    CategoryDef(
        id="delivery_opt",
        group="windows",
        requires_admin=True,
        rules=(
            RuleSpec(
                "{SYSTEMROOT}\\ServiceProfiles\\NetworkService\\AppData\\Local\\Microsoft"
                "\\Windows\\DeliveryOptimization\\Cache"
            ),
        ),
    ),
    # --------------------------------------------------------------- Temp
    CategoryDef(
        id="user_temp",
        group="temp",
        smart=True,
        rules=(
            RuleSpec("{TEMP}", age=AGE_TEMP),
            RuleSpec("{LOCALAPPDATA}\\Temp", age=AGE_TEMP),
        ),
    ),
    # ----------------------------------------------------------- Browsers
    _chromium("chrome", "{LOCALAPPDATA}\\Google\\Chrome\\User Data"),
    _chromium("edge", "{LOCALAPPDATA}\\Microsoft\\Edge\\User Data"),
    _chromium("brave", "{LOCALAPPDATA}\\BraveSoftware\\Brave-Browser\\User Data"),
    CategoryDef(
        id="opera",
        group="browsers",
        smart=True,
        note="note.close_browser",
        rules=(
            RuleSpec("{LOCALAPPDATA}\\Opera Software\\Opera Stable\\Cache"),
            RuleSpec("{LOCALAPPDATA}\\Opera Software\\Opera GX Stable\\Cache"),
            RuleSpec("{APPDATA}\\Opera Software\\Opera Stable\\Code Cache"),
            RuleSpec("{APPDATA}\\Opera Software\\Opera Stable\\GPUCache"),
            RuleSpec("{APPDATA}\\Opera Software\\Opera GX Stable\\Code Cache"),
            RuleSpec("{APPDATA}\\Opera Software\\Opera GX Stable\\GPUCache"),
        ),
    ),
    CategoryDef(
        id="firefox",
        group="browsers",
        smart=True,
        note="note.close_browser",
        rules=(
            RuleSpec("{LOCALAPPDATA}\\Mozilla\\Firefox\\Profiles\\*\\cache2"),
            RuleSpec("{LOCALAPPDATA}\\Mozilla\\Firefox\\Profiles\\*\\startupCache"),
            RuleSpec("{LOCALAPPDATA}\\Mozilla\\Firefox\\Profiles\\*\\thumbnails"),
        ),
    ),
    # --------------------------------------------------------------- Apps
    _electron("discord", "{APPDATA}\\discord"),
    CategoryDef(
        id="telegram",
        group="apps",
        smart=False,  # медиа-кэш: пользователь может ожидать офлайн-доступ
        note="note.telegram",
        rules=(
            RuleSpec("{APPDATA}\\Telegram Desktop\\tdata\\user_data\\cache"),
            RuleSpec("{APPDATA}\\Telegram Desktop\\tdata\\user_data\\media_cache"),
        ),
    ),
    CategoryDef(
        id="steam",
        group="apps",
        smart=True,
        note="note.steam",
        rules=(
            RuleSpec("{STEAM}\\appcache\\httpcache"),
            RuleSpec("{STEAM}\\dumps"),
            RuleSpec("{STEAM}\\logs", age=AGE_LOG),
            RuleSpec("{LOCALAPPDATA}\\Steam\\htmlcache\\Cache"),
            RuleSpec("{LOCALAPPDATA}\\Steam\\htmlcache\\Code Cache"),
            RuleSpec("{LOCALAPPDATA}\\Steam\\htmlcache\\GPUCache"),
        ),
    ),
    _electron(
        "vscode",
        "{APPDATA}\\Code",
        extra=(
            RuleSpec("{APPDATA}\\Code\\CachedData"),
            RuleSpec("{APPDATA}\\Code\\CachedExtensionVSIXs"),
        ),
    ),
    CategoryDef(
        id="spotify",
        group="apps",
        smart=True,
        note="note.close_app",
        rules=(RuleSpec("{LOCALAPPDATA}\\Spotify\\Data"),),
    ),
    CategoryDef(
        id="dev_cache",
        group="apps",
        smart=False,
        rules=(
            RuleSpec("{LOCALAPPDATA}\\pip\\cache"),
            RuleSpec("{LOCALAPPDATA}\\npm-cache\\_cacache"),
            RuleSpec("{LOCALAPPDATA}\\NuGet\\v3-cache"),
            RuleSpec("{LOCALAPPDATA}\\Yarn\\Cache"),
        ),
    ),
    # --------------------------------------------------------------- Logs
    CategoryDef(
        id="wer_reports",
        group="logs",
        smart=True,
        rules=(
            RuleSpec("{LOCALAPPDATA}\\Microsoft\\Windows\\WER\\ReportArchive"),
            RuleSpec("{LOCALAPPDATA}\\Microsoft\\Windows\\WER\\ReportQueue"),
            RuleSpec("{LOCALAPPDATA}\\Microsoft\\Windows\\WER\\Temp"),
            RuleSpec("{PROGRAMDATA}\\Microsoft\\Windows\\WER\\ReportArchive"),
            RuleSpec("{PROGRAMDATA}\\Microsoft\\Windows\\WER\\ReportQueue"),
            RuleSpec("{PROGRAMDATA}\\Microsoft\\Windows\\WER\\Temp"),
        ),
    ),
    CategoryDef(
        id="win_logs",
        group="logs",
        requires_admin=True,
        rules=(
            RuleSpec("{SYSTEMROOT}\\Logs", patterns=("*.log", "*.etl", "*.cab", "*.txt"), age=AGE_LOG),
            RuleSpec("{SYSTEMROOT}\\Debug", patterns=("*.log",), age=AGE_LOG),
        ),
    ),
    CategoryDef(
        id="app_logs",
        group="logs",
        rules=(
            RuleSpec("{APPDATA}\\Code\\logs", age=AGE_LOG),
            RuleSpec("{LOCALAPPDATA}\\npm-cache\\_logs", age=AGE_LOG),
            RuleSpec("{APPDATA}\\discord\\logs", age=AGE_LOG),
        ),
    ),
    # ------------------------------------------------------------ Recycle
    # ----------------------------------------------------- System tools
    CategoryDef(
        id="windows_old",
        group="windows",
        kind=KIND_SYSTEM,
        requires_admin=True,
        note="note.windows_old",
    ),
    CategoryDef(
        id="component_store",
        group="windows",
        kind=KIND_SYSTEM,
        requires_admin=True,
        note="note.component_store",
    ),
    CategoryDef(id="recycle_bin", group="recycle", kind=KIND_RECYCLE, smart=True),
)


# Процессы, которые держат файлы кэша открытыми (имена в нижнем регистре).
PROCESSES: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "brave": ("brave.exe",),
    "opera": ("opera.exe", "opera_gx.exe"),
    "firefox": ("firefox.exe",),
    "discord": ("discord.exe",),
    "telegram": ("telegram.exe",),
    "steam": ("steam.exe", "steamwebhelper.exe"),
    "vscode": ("code.exe",),
    "spotify": ("spotify.exe",),
}


_EXTRA_CATEGORIES: list[CategoryDef] = []


def set_extra_categories(categories: list[CategoryDef]) -> None:
    """Подключает импортированные категории (Winapp2.ini). Встроенные всегда идут первыми."""
    _EXTRA_CATEGORIES[:] = list(categories)


def all_categories() -> list[CategoryDef]:
    builtin_ids = {c.id for c in CATEGORIES}
    return [*CATEGORIES, *(c for c in _EXTRA_CATEGORIES if c.id not in builtin_ids)]


def category_by_id(cat_id: str) -> CategoryDef | None:
    for c in all_categories():
        if c.id == cat_id:
            return c
    return None


# --------------------------------------------------------------------- tokens
def _steam_path() -> str:
    """Путь установки Steam из реестра (HKCU\\Software\\Valve\\Steam\\SteamPath)."""
    if not winpaths.IS_WINDOWS:
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg.QueryValueEx(key, "SteamPath")
            if isinstance(value, str) and value:
                return os.path.normpath(value)
    except OSError:
        pass
    pf86 = winpaths.known_folder("ProgramFilesX86")
    candidate = os.path.join(pf86, "Steam") if pf86 else ""
    return candidate if candidate and os.path.isdir(candidate) else ""


def default_tokens() -> dict[str, str]:
    kf = winpaths.known_folder
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    return {
        "SYSTEMROOT": kf("Windows"),
        "TEMP": temp,
        "LOCALAPPDATA": kf("LocalAppData"),
        "APPDATA": kf("RoamingAppData"),
        "PROGRAMDATA": kf("ProgramData"),
        "USERPROFILE": kf("Profile"),
        "PROGRAMFILES": kf("ProgramFiles"),
        "PROGRAMFILESX86": kf("ProgramFilesX86"),
        "STEAM": _steam_path(),
        "LOCALLOWAPPDATA": os.path.join(os.path.dirname(kf("LocalAppData")), "LocalLow")
        if kf("LocalAppData")
        else "",
        "SYSTEMDRIVE": winpaths.system_drive(),
    }


def expand_template(template: str, tokens: Mapping[str, str]) -> list[str]:
    """Шаблон -> список существующих папок. Пустой список, если токен не определён."""
    if not template.startswith("{"):
        return []
    end = template.find("}")
    if end < 0:
        return []
    token = template[1:end]
    base = tokens.get(token, "")
    if not base:
        return []
    rest = [p for p in template[end + 1 :].replace("/", "\\").split("\\") if p]
    if any(p == ".." for p in rest):
        return []
    if any(ch in part for part in rest for ch in "*?["):
        pattern = os.path.join(glob.escape(base), *rest)
        try:
            matches = glob.glob(pattern)
        except OSError:
            return []
        return sorted(m for m in matches if os.path.isdir(m) and not os.path.islink(m))
    path = os.path.join(base, *rest)
    return [path] if os.path.isdir(path) else []


def _display_real(path: str) -> str:
    try:
        return winpaths.strip_long_prefix(os.path.realpath(path))
    except (OSError, ValueError):
        return path


def age_seconds(age: str, temp_hours: int, log_days: int) -> float:
    if age == AGE_TEMP:
        return max(0, temp_hours) * 3600.0
    if age == AGE_LOG:
        return max(0, log_days) * 86400.0
    return 0.0


@dataclass
class RuleResolution:
    rules: list[ResolvedRule]
    rejected: list[tuple[str, str]]  # (путь, код причины)


def resolve_category_rules(
    category: CategoryDef,
    tokens: Mapping[str, str],
    policy: SafetyPolicy,
    drive: str | None = None,
    temp_hours: int = 24,
    log_days: int = 7,
) -> RuleResolution:
    """Превращает шаблоны категории в конкретные проверенные правила.

    drive — если задан (например 'C:\\'), берутся только папки на этом диске.
    """
    resolved: list[ResolvedRule] = []
    rejected: list[tuple[str, str]] = []
    seen: set[tuple[str, bool, tuple[str, ...]]] = set()
    drive_norm = winpaths.drive_of(drive) if drive else None
    for spec in category.rules:
        for raw_root in expand_template(spec.template, tokens):
            root = winpaths.real_normalized(raw_root)
            if drive_norm and winpaths.drive_of(root) != drive_norm:
                continue
            key = (root, spec.recursive, spec.patterns)
            if key in seen:
                continue
            seen.add(key)
            reason = policy.validate_rule_root(root, spec.recursive, spec.patterns)
            if reason:
                rejected.append((raw_root, reason))
                log.info("Правило %s отклонено (%s): %s", category.id, reason, raw_root)
                continue
            resolved.append(
                ResolvedRule(
                    category_id=category.id,
                    root=root,
                    root_display=_display_real(raw_root),
                    patterns=spec.patterns,
                    recursive=spec.recursive,
                    min_age_seconds=age_seconds(spec.age, temp_hours, log_days),
                )
            )
    # Если одно рекурсивное правило вложено в другое (например TEMP == LOCALAPPDATA\Temp\X),
    # оставляем только внешнее, чтобы не находить файлы дважды.
    recursive_roots = [r for r in resolved if r.recursive]
    deduped = [
        r
        for r in resolved
        if not any(
            o is not r
            and o.recursive
            and winpaths.is_within(r.root, o.root, strict=True)
            and o.patterns == ("*",)
            for o in recursive_roots
        )
    ]
    return RuleResolution(deduped, rejected)
