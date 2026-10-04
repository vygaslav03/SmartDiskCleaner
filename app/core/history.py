"""История очисток: локальный JSON (history.json рядом с settings.json).

Хранятся только итоги операции: дата, тип, диск, категории, количество, размер.
Пути и имена файлов в историю НЕ записываются.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from app.utils.logger import get_logger

log = get_logger("history")

MAX_ENTRIES = 500
KIND_JUNK = "junk"
KIND_LARGE = "large"
KIND_DUPLICATES = "duplicates"


@dataclass
class HistoryEntry:
    timestamp: float
    kind: str
    deleted: int = 0
    freed: int = 0
    failed: int = 0
    missing: int = 0
    drive: str = ""
    categories: list[str] = field(default_factory=list)
    mode: str = ""  # для пользовательских файлов: recycle / permanent
    cancelled: bool = False
    quarantined: int = 0  # байт перенесено в карантин (ещё не освобождено)

    @classmethod
    def from_dict(cls, raw: dict) -> "HistoryEntry | None":
        if not isinstance(raw, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            entry = cls(**{k: v for k, v in raw.items() if k in known})
            entry.timestamp = float(entry.timestamp)
            entry.deleted = int(entry.deleted)
            entry.freed = int(entry.freed)
            entry.failed = int(entry.failed)
            entry.missing = int(entry.missing)
            entry.quarantined = int(entry.quarantined)
            entry.categories = [str(c) for c in entry.categories] if isinstance(entry.categories, list) else []
            return entry
        except (TypeError, ValueError):
            return None


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> list[HistoryEntry]:
        """Записи от новых к старым. Повреждённый файл не роняет программу."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            log.warning("История повреждена (%s), файл сохранён как .bad и начат заново", exc)
            try:
                os.replace(self.path, self.path.with_suffix(".bad"))
            except OSError:
                pass
            return []
        items = raw.get("entries", []) if isinstance(raw, dict) else []
        entries = [e for e in (HistoryEntry.from_dict(r) for r in items) if e is not None]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries

    def add(self, entry: HistoryEntry) -> bool:
        with self._lock:
            entries = self.load()
            entries.insert(0, entry)
            return self._save(entries[:MAX_ENTRIES])

    def clear(self) -> bool:
        with self._lock:
            return self._save([])

    def _save(self, entries: list[HistoryEntry]) -> bool:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "entries": [asdict(e) for e in entries]}, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("Не удалось сохранить историю: %s", exc)
            return False


def totals(entries: list[HistoryEntry]) -> tuple[int, int, int]:
    """(освобождено всего, удалено файлов всего, число операций)."""
    return sum(e.freed for e in entries), sum(e.deleted for e in entries), len(entries)


def make_entry(kind: str, report, drive: str = "", categories=(), mode: str = "") -> HistoryEntry:
    """Запись истории из CleanReport (без путей файлов)."""
    return HistoryEntry(
        timestamp=time.time(),
        kind=kind,
        deleted=int(report.deleted_files),
        freed=int(report.freed_bytes),
        failed=int(report.failed_count),
        missing=int(report.missing),
        drive=drive,
        categories=list(categories),
        mode=mode,
        cancelled=bool(report.cancelled),
        quarantined=int(getattr(report, "quarantined_bytes", 0)),
    )


_store: HistoryStore | None = None


def get_history() -> HistoryStore:
    global _store
    if _store is None:
        from app.utils.paths import data_dir

        _store = HistoryStore(data_dir() / "history.json")
    return _store


def set_history(store: HistoryStore) -> None:
    """Подмена хранилища (тесты, самопроверка)."""
    global _store
    _store = store
