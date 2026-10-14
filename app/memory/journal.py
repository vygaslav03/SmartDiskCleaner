"""Журнал действий с памятью: что сделано, когда, результат «до/после» и данные для отмены.

Локальный JSON (memory_actions.json рядом с settings.json). Командные строки и пути
не записываются — только имя программы/записи автозагрузки.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from app.utils.logger import get_logger

log = get_logger("memory.journal")

MAX_ENTRIES = 500


@dataclass
class JournalEntry:
    id: str
    timestamp: float
    kind: str
    target: str
    ok: bool
    message: str = ""
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    undo: dict | None = None  # None — отмена технически невозможна
    undone: bool = False

    @property
    def can_undo(self) -> bool:
        return self.ok and self.undo is not None and not self.undone

    @classmethod
    def from_dict(cls, raw: dict) -> "JournalEntry | None":
        if not isinstance(raw, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            entry = cls(**{k: v for k, v in raw.items() if k in known})
            entry.timestamp = float(entry.timestamp)
            return entry
        except (TypeError, ValueError):
            return None


def new_entry(kind: str, target: str, ok: bool, message: str = "", **kw) -> JournalEntry:
    return JournalEntry(uuid.uuid4().hex, time.time(), kind, target, ok, message, **kw)


class ActionJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> list[JournalEntry]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            log.warning("Журнал повреждён (%s), сохранён как .bad", exc)
            try:
                os.replace(self.path, self.path.with_suffix(".bad"))
            except OSError:
                pass
            return []
        items = raw.get("entries", []) if isinstance(raw, dict) else []
        entries = [e for e in (JournalEntry.from_dict(r) for r in items) if e is not None]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries

    def add(self, entry: JournalEntry) -> bool:
        log.info("Действие: %s %s -> %s %s", entry.kind, entry.target, "OK" if entry.ok else "ошибка", entry.message)
        with self._lock:
            entries = self.load()
            entries.insert(0, entry)
            return self._save(entries[:MAX_ENTRIES])

    def mark_undone(self, entry_id: str) -> bool:
        with self._lock:
            entries = self.load()
            for e in entries:
                if e.id == entry_id:
                    e.undone = True
            return self._save(entries)

    def _save(self, entries: list[JournalEntry]) -> bool:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "entries": [asdict(e) for e in entries]}, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("Не удалось сохранить журнал: %s", exc)
            return False


_journal: ActionJournal | None = None


def get_journal() -> ActionJournal:
    global _journal
    if _journal is None:
        from app.utils.paths import data_dir

        _journal = ActionJournal(data_dir() / "memory_actions.json")
    return _journal


def set_journal(journal: ActionJournal) -> None:
    global _journal
    _journal = journal
