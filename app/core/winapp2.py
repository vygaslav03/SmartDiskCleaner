"""Импорт правил очистки из Winapp2.ini (формат CCleaner/BleachBit).

Файл НЕ скачивается программой (она работает без сети): пользователь сам выбирает
скачанный winapp2.ini в настройках, и копия сохраняется рядом с settings.json.

Безопасность:
* поддерживаются только FileKey; RegKey (очистка реестра) игнорируется полностью;
* путь обязан начинаться с известной переменной (%LocalAppData%, %AppData%, %WinDir%...);
  переменные пользовательских папок (%Documents%, %Pictures%...) и абсолютные пути
  не поддерживаются — такие ключи пропускаются;
* каждое правило затем проходит те же проверки SafetyPolicy, что и встроенные:
  корень не может быть профилем, AppData целиком, Program Files, System32 и т.п.;
* импортированные категории не входят в Smart Clean и не отмечены по умолчанию.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from app.core.categories import AGE_TEMP, CategoryDef, ExcludeSpec, RuleSpec
from app.utils.logger import get_logger
from app.utils.winpaths import IS_WINDOWS

log = get_logger("winapp2")

GROUP = "winapp2"
ID_PREFIX = "wa2:"

# Переменная Winapp2 -> токены шаблонов (несколько — правило размножается).
VARIABLES: dict[str, tuple[str, ...]] = {
    "localappdata": ("LOCALAPPDATA",),
    "appdata": ("APPDATA",),
    "locallowappdata": ("LOCALLOWAPPDATA",),
    "programfiles": ("PROGRAMFILES", "PROGRAMFILESX86"),
    "commonappdata": ("PROGRAMDATA",),
    "programdata": ("PROGRAMDATA",),
    "allusersprofile": ("PROGRAMDATA",),
    "windir": ("SYSTEMROOT",),
    "systemroot": ("SYSTEMROOT",),
    "userprofile": ("USERPROFILE",),
    "systemdrive": ("SYSTEMDRIVE",),
}

SPECIAL_DETECT: dict[str, str] = {
    "det_chrome": "{LOCALAPPDATA}\\Google\\Chrome",
    "det_mozilla": "{APPDATA}\\Mozilla\\Firefox",
    "det_thunderbird": "{APPDATA}\\Thunderbird",
    "det_opera": "{APPDATA}\\Opera Software",
}

_VAR_RE = re.compile(r"^%([A-Za-z0-9_]+)%(.*)$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class ParseResult:
    categories: list[CategoryDef] = field(default_factory=list)
    total_entries: int = 0
    registry_only: int = 0  # записи только с RegKey — пропущены
    unsupported_keys: int = 0  # FileKey с неподдерживаемыми путями — пропущены


def translate_path(path: str) -> list[str]:
    """'%LocalAppData%\\App\\Cache' -> ['{LOCALAPPDATA}\\App\\Cache']. [] — путь не поддерживается."""
    path = path.strip().strip('"').rstrip("\\/")
    m = _VAR_RE.match(path)
    if not m:
        return []
    tokens = VARIABLES.get(m.group(1).lower())
    if not tokens:
        return []
    rest = m.group(2).replace("/", "\\")
    parts = [p for p in rest.split("\\") if p]
    if any(p in ("..", ".") for p in parts):
        return []
    suffix = ("\\" + "\\".join(parts)) if parts else ""
    return [f"{{{t}}}{suffix}" for t in tokens]


def _parse_patterns(raw: str) -> tuple[str, ...]:
    pats = [p.strip() for p in raw.split(";") if p.strip()]
    if not pats or any(("\\" in p or "/" in p or ".." in p) for p in pats):
        return ()
    return tuple("*" if p == "*.*" else p for p in pats)


def parse_filekey(value: str) -> list[RuleSpec]:
    """FileKey: путь|маски|флаги. RECURSE/REMOVESELF -> рекурсивно (сам корень не удаляется)."""
    parts = value.split("|")
    if len(parts) < 2:
        return []
    templates = translate_path(parts[0])
    patterns = _parse_patterns(parts[1])
    if not templates or not patterns:
        return []
    flags = {f.strip().upper() for f in parts[2:]}
    recursive = bool(flags & {"RECURSE", "REMOVESELF"})
    # Импортированные правила консервативны: свежие файлы (моложе порога из настроек)
    # не трогаются — их может использовать работающая программа или установщик.
    return [RuleSpec(t, patterns=patterns, recursive=recursive, age=AGE_TEMP) for t in templates]


def parse_excludekey(value: str) -> list[ExcludeSpec]:
    """ExcludeKey: FILE|папка|маски  или  PATH|папка|маски (вся папка)."""
    parts = value.split("|")
    if len(parts) < 2:
        return []
    kind = parts[0].strip().upper()
    templates = translate_path(parts[1])
    if not templates:
        return []
    if kind == "PATH":
        return [ExcludeSpec(t, ("*",), whole_dir=True) for t in templates]
    if kind == "FILE" and len(parts) >= 3:
        pats = _parse_patterns(parts[2])
        return [ExcludeSpec(t, pats, whole_dir=False) for t in templates] if pats else []
    return []


def _registry_key_exists(spec: str) -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    hives = {
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKU": winreg.HKEY_USERS,
    }
    hive_name, _, sub = spec.strip().partition("\\")
    hive = hives.get(hive_name.upper())
    if hive is None:
        return False
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view):
                return True
        except OSError:
            continue
    return False


def _path_exists(template: str, tokens: Mapping[str, str]) -> bool:
    end = template.find("}")
    base = tokens.get(template[1:end], "") if template.startswith("{") and end > 0 else ""
    if not base:
        return False
    rest = [p for p in template[end + 1 :].split("\\") if p]
    path = os.path.join(base, *rest)
    if any(ch in path for ch in "*?["):
        try:
            return bool(glob.glob(os.path.join(glob.escape(base), *rest)))
        except OSError:
            return False
    return os.path.exists(path)


def make_detector(reg_keys: list[str], file_templates: list[str]):
    """Программа считается установленной, если найден ЛЮБОЙ ключ реестра или путь (только чтение)."""
    if not reg_keys and not file_templates:
        return None

    def detect(tokens: Mapping[str, str]) -> bool:
        if any(_path_exists(t, tokens) for t in file_templates):
            return True
        return any(_registry_key_exists(k) for k in reg_keys)

    return detect


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:60] or "entry"


def parse_text(text: str) -> ParseResult:
    result = ParseResult()
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    current: list[tuple[str, str]] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = []
            sections.append((line[1:-1].strip(), current))
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current.append((key.strip().lower(), value.strip()))

    used_ids: set[str] = set()
    for name, pairs in sections:
        if not name or name.lower() in ("version", "winapp2"):
            continue
        result.total_entries += 1
        rules: list[RuleSpec] = []
        excludes: list[ExcludeSpec] = []
        reg_detect: list[str] = []
        file_detect: list[str] = []
        warning = ""
        has_filekey = False
        for key, value in pairs:
            if key.startswith("filekey"):
                has_filekey = True
                parsed = parse_filekey(value)
                if parsed:
                    rules.extend(parsed)
                else:
                    result.unsupported_keys += 1
            elif key.startswith("excludekey"):
                excludes.extend(parse_excludekey(value))
            elif key.startswith("detectos"):
                pass  # версия Windows — не влияет на безопасность, игнорируем
            elif key.startswith("detectfile"):
                file_detect.extend(translate_path(value))
            elif key.startswith("detect"):
                reg_detect.append(value)
            elif key == "specialdetect":
                tpl = SPECIAL_DETECT.get(value.strip().lower())
                if tpl:
                    file_detect.append(tpl)
            elif key == "warning":
                warning = value
            # RegKey* и прочие ключи намеренно игнорируются
        if not has_filekey:
            result.registry_only += 1
            continue
        if not rules:
            continue
        cat_id = ID_PREFIX + _slug(name)
        n = 2
        base_id = cat_id
        while cat_id in used_ids:
            cat_id = f"{base_id}-{n}"
            n += 1
        used_ids.add(cat_id)
        title = name[:-2].strip() if name.endswith(" *") else name
        result.categories.append(
            CategoryDef(
                id=cat_id,
                group=GROUP,
                rules=tuple(rules),
                smart=False,
                title=title,
                description=warning,
                excludes=tuple(excludes),
                detect=make_detector(reg_detect, file_detect),
                tags=("winapp2",),
            )
        )
    return result


def read_file(path: Path) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_file(path: Path) -> ParseResult:
    return parse_text(read_file(path))


# ------------------------------------------------------------------ импорт
_cache: dict[str, tuple[float, ParseResult]] = {}


def imported_path() -> Path:
    from app.utils.paths import data_dir

    return data_dir() / "winapp2.ini"


def load_imported() -> ParseResult:
    """Правила из импортированной копии (кэш по времени изменения файла)."""
    path = imported_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ParseResult()
    key = str(path)
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        result = parse_file(path)
    except OSError as exc:
        log.warning("Не удалось прочитать winapp2.ini: %s", exc)
        return ParseResult()
    _cache[key] = (mtime, result)
    log.info(
        "Winapp2: записей %d, с файлами %d, только реестр %d, неподдерживаемых ключей %d",
        result.total_entries, len(result.categories), result.registry_only, result.unsupported_keys,
    )
    return result


def import_file(source: Path) -> ParseResult:
    """Проверяет выбранный файл и сохраняет копию рядом с настройками."""
    text = read_file(source)
    result = parse_text(text)
    if not result.categories:
        raise ValueError("no usable entries")
    dest = imported_path()
    tmp = dest.with_suffix(".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    _cache.clear()
    return result


def remove_imported() -> None:
    """Отключает импорт: удаляет только собственную копию программы."""
    try:
        imported_path().unlink()
    except FileNotFoundError:
        pass
    _cache.clear()
