"""Карантин: очищенный мусор не удаляется сразу, а переносится в папку карантина.

* Перенос — переименование в пределах одного диска (мгновенно, без копирования).
  Файлы с другого диска удаляются как раньше (копировать гигабайты кэша нет смысла).
* Каждая очистка — отдельная «сессия»: папка с файлами и manifest.json
  (исходный путь, размер, дата изменения).
* Восстановление — только по исходному пути и только если там сейчас ничего нет:
  программа никогда не перезаписывает файлы при восстановлении.
* Сессии старше N дней удаляются автоматически при запуске программы.

Место на диске освобождается только при удалении сессии (по сроку или вручную).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core.safety import R_NOT_FOUND, validate_raw_path
from app.utils import winpaths
from app.utils.logger import get_logger
from app.utils.winpaths import is_reparse_or_link, is_within, long_path, normalize

log = get_logger("quarantine")

MANIFEST = "manifest.json"
FILES_DIR = "files"

# Коды результата для отчётов
Q_OTHER_DRIVE = "other_drive"
Q_CONFLICT = "restore_conflict"
Q_MISSING = "quarantine_missing"
Q_BAD_ENTRY = "bad_entry"


@dataclass
class QEntry:
    original: str
    stored: str  # имя файла внутри files/
    size: int
    mtime: float
    category: str = ""


@dataclass
class QSession:
    id: str
    created: float
    path: Path
    categories: list[str] = field(default_factory=list)
    entries: list[QEntry] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(e.size for e in self.entries)

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass
class RestoreReport:
    restored: int = 0
    restored_bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return len(self.failed)


def default_root() -> Path:
    from app.utils.paths import quarantine_dir

    return quarantine_dir()


def _classify(exc: OSError) -> str:
    from app.core.cleaner import _classify_os_error

    return _classify_os_error(exc)


class SessionWriter:
    """Пишет одну сессию карантина во время очистки. Используется как remover в Cleaner."""

    def __init__(self, root: Path, categories: list[str] | None = None) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.session_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
        self.root = Path(root)
        self.path = self.root / self.session_id
        self.files = self.path / FILES_DIR
        self.created = time.time()
        self.categories = list(categories or [])
        self.entries: list[QEntry] = []
        self.other_drive = 0
        self._counter = 0
        self._lock = threading.Lock()
        self._ready = False
        self._root_drive = winpaths.drive_of(str(self.root))

    def _ensure(self) -> None:
        if not self._ready:
            self.files.mkdir(parents=True, exist_ok=True)
            self._ready = True

    def same_volume(self, path: str) -> bool:
        return winpaths.drive_of(path) == self._root_drive

    def move(self, path: str, category: str = "") -> str | None:
        """Переносит файл в карантин. None — успех, иначе код причины (как у remover)."""
        from app.core.cleaner import remove_file_permanently

        if not self.same_volume(path):
            # С другого диска переименованием не перенести — удаляем как без карантина.
            reason = remove_file_permanently(path)
            if reason is None:
                self.other_drive += 1
            return reason
        try:
            st = os.lstat(long_path(path))
        except FileNotFoundError:
            return R_NOT_FOUND
        except OSError as exc:
            return _classify(exc)
        with self._lock:
            self._ensure()
            self._counter += 1
            stored = f"{self._counter:07d}"
        dest = self.files / stored
        try:
            os.replace(long_path(path), long_path(str(dest)))
        except FileNotFoundError:
            return R_NOT_FOUND
        except OSError as exc:
            if getattr(exc, "winerror", None) == 17 or exc.errno == 18:  # другой том
                from app.core.cleaner import remove_file_permanently as _rm

                reason = _rm(path)
                if reason is None:
                    self.other_drive += 1
                return reason
            return _classify(exc)
        with self._lock:
            self.entries.append(QEntry(path, stored, int(st.st_size), float(st.st_mtime), category))
        return None

    def save(self) -> None:
        """Сохраняет manifest (вызывается после каждой категории и в конце)."""
        if not self.entries:
            return
        self._ensure()
        data = {
            "version": 1,
            "id": self.session_id,
            "created": self.created,
            "categories": self.categories,
            "entries": [asdict(e) for e in self.entries],
        }
        tmp = self.path / (MANIFEST + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.path / MANIFEST)


class QuarantineStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_root()

    def new_session(self, categories: list[str] | None = None) -> SessionWriter:
        return SessionWriter(self.root, categories)

    # ------------------------------------------------------------ reading
    def sessions(self) -> list[QSession]:
        out: list[QSession] = []
        try:
            dirs = [p for p in self.root.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            return []
        for d in dirs:
            s = self._load(d)
            if s is not None:
                out.append(s)
        out.sort(key=lambda s: s.created, reverse=True)
        return out

    def _load(self, d: Path) -> QSession | None:
        try:
            with open(d / MANIFEST, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        entries: list[QEntry] = []
        for e in raw.get("entries", []):
            try:
                entries.append(
                    QEntry(str(e["original"]), str(e["stored"]), int(e["size"]), float(e["mtime"]),
                           str(e.get("category", "")))
                )
            except (KeyError, TypeError, ValueError):
                continue
        try:
            created = float(raw.get("created", 0))
        except (TypeError, ValueError):
            created = 0.0
        cats = raw.get("categories", [])
        return QSession(
            id=d.name,
            created=created,
            path=d,
            categories=[str(c) for c in cats] if isinstance(cats, list) else [],
            entries=entries,
        )

    def total_size(self) -> int:
        return sum(s.size for s in self.sessions())

    # ------------------------------------------------------------ restore
    def _stored_path(self, session: QSession, entry: QEntry) -> Path | None:
        if "/" in entry.stored or "\\" in entry.stored or entry.stored in ("", ".", ".."):
            return None
        p = session.path / FILES_DIR / entry.stored
        if not is_within(normalize(str(p)), normalize(str(session.path)), strict=True):
            return None
        return p

    def restore(self, session: QSession, progress_cb=None, cancel_event: threading.Event | None = None) -> RestoreReport:
        report = RestoreReport()
        remaining: list[QEntry] = []
        for n, entry in enumerate(session.entries, start=1):
            if cancel_event is not None and cancel_event.is_set():
                remaining.extend(session.entries[n - 1 :])
                break
            reason = self._restore_one(session, entry)
            if reason is None:
                report.restored += 1
                report.restored_bytes += entry.size
            else:
                report.failed.append((entry.original, reason))
                if reason != Q_MISSING:
                    remaining.append(entry)
            if progress_cb:
                progress_cb({"processed": n, "total": session.count, "restored": report.restored})
        # Сессию оставляем только с тем, что не удалось вернуть.
        session.entries = remaining
        if remaining:
            self._rewrite(session)
        else:
            self._delete_dir(session.path)
        log.info("Восстановлено из карантина %s: %d файлов, ошибок %d", session.id, report.restored,
                 report.failed_count)
        return report

    def _restore_one(self, session: QSession, entry: QEntry) -> str | None:
        if validate_raw_path(entry.original):
            return Q_BAD_ENTRY
        src = self._stored_path(session, entry)
        if src is None:
            return Q_BAD_ENTRY
        if not src.exists():
            return Q_MISSING
        if os.path.lexists(long_path(entry.original)):
            return Q_CONFLICT  # никогда не перезаписываем
        try:
            os.makedirs(long_path(os.path.dirname(entry.original)), exist_ok=True)
            os.replace(long_path(str(src)), long_path(entry.original))
            return None
        except OSError as exc:
            return _classify(exc)

    def _rewrite(self, session: QSession) -> None:
        data = {
            "version": 1,
            "id": session.id,
            "created": session.created,
            "categories": session.categories,
            "entries": [asdict(e) for e in session.entries],
        }
        try:
            with open(session.path / MANIFEST, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except OSError as exc:
            log.error("Не удалось обновить manifest %s: %s", session.id, exc)

    # ------------------------------------------------------------ delete
    def _delete_dir(self, d: Path) -> bool:
        """Удаляет папку сессии. Только внутри корня карантина и без перехода по ссылкам."""
        try:
            nd, nr = normalize(str(d)), normalize(str(self.root))
            if not is_within(nd, nr, strict=True):
                log.error("Отказ удалять папку вне карантина: %s", d)
                return False
            st = os.lstat(long_path(str(d)))
            if is_reparse_or_link(st) or not stat.S_ISDIR(st.st_mode):
                return False

            def onerror(func, path, _exc):
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except OSError:
                    pass

            shutil.rmtree(long_path(str(d)), onerror=onerror)
            return not d.exists()
        except OSError as exc:
            log.error("Не удалось удалить сессию карантина %s: %s", d, exc)
            return False

    def delete(self, session: QSession) -> int:
        """Окончательно удаляет сессию. Возвращает освобождённые байты."""
        size = session.size
        return size if self._delete_dir(session.path) else 0

    def purge_expired(self, days: int, now: float | None = None) -> int:
        now = now or time.time()
        freed = 0
        for s in self.sessions():
            if s.created and now - s.created > days * 86400:
                freed += self.delete(s)
        # Папки без manifest (прерванная очистка без файлов) — тоже убираем, если они старые.
        try:
            for d in self.root.iterdir():
                if d.is_dir() and not (d / MANIFEST).exists():
                    try:
                        if now - d.stat().st_mtime > 86400 and not any((d / FILES_DIR).iterdir()):
                            self._delete_dir(d)
                    except OSError:
                        continue
        except OSError:
            pass
        if freed:
            log.info("Карантин: удалены просроченные сессии, освобождено %d байт", freed)
        return freed

    def expires_at(self, session: QSession, days: int) -> float:
        return session.created + days * 86400


_store: QuarantineStore | None = None


def get_quarantine() -> QuarantineStore:
    global _store
    if _store is None:
        _store = QuarantineStore()
    return _store


def set_quarantine(store: QuarantineStore) -> None:
    global _store
    _store = store
