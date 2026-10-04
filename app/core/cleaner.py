"""Удаление файлов. Каждый файл перед удалением проходит SafetyPolicy.check_before_delete.

Удаляются только элементы из результата сканирования (ScanResult / список
найденных файлов) — произвольный путь передать сюда нельзя.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from app.core import recycle_bin
from app.core.categories import KIND_FILES, KIND_RECYCLE, KIND_SYSTEM
from app.core.safety import (
    R_LAST_COPY,
    R_NOT_FOUND,
    R_NOT_SCANNED,
    ResolvedRule,
    SafetyPolicy,
)
from app.core.scanner import Throttle
from app.models.file_item import DuplicateGroup, FileItem
from app.models.scan_result import ScanResult
from app.utils.logger import get_logger
from app.utils.winpaths import (
    FILE_ATTRIBUTE_READONLY,
    file_attributes,
    is_reparse_or_link,
    is_within,
    long_path,
    normalize,
    real_normalized,
)

log = get_logger("cleaner")

# Коды ошибок удаления (переводятся в UI: "reason.<code>")
E_LOCKED = "locked"
E_ACCESS_DENIED = "access_denied"
E_PATH_TOO_LONG = "path_too_long"
E_OS_ERROR = "os_error"
E_TRASH_UNAVAILABLE = "trash_unavailable"
E_RECYCLE_FAILED = "recycle_failed"

MODE_PERMANENT = "permanent"
MODE_RECYCLE = "recycle"

_WINERR_SHARING = {32, 33}  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
_WINERR_TOO_LONG = {206}  # ERROR_FILENAME_EXCED_RANGE


@dataclass
class CleanReport:
    deleted_files: int = 0
    freed_bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (путь, код причины)
    missing: int = 0  # файл уже исчез к моменту удаления
    cancelled: bool = False
    deleted_paths: set[str] = field(default_factory=set)
    recycle_emptied: bool = False
    tools_done: list[str] = field(default_factory=list)
    tool_details: dict[str, str] = field(default_factory=dict)
    # Карантин: файлы перенесены (место освободится при очистке карантина).
    quarantined_files: int = 0
    quarantined_bytes: int = 0
    quarantine_session: str = ""

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def reasons_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, reason in self.failed:
            out[reason] = out.get(reason, 0) + 1
        return out


def _classify_os_error(exc: OSError) -> str:
    winerr = getattr(exc, "winerror", None)
    if winerr in _WINERR_SHARING:
        return E_LOCKED
    if winerr in _WINERR_TOO_LONG or exc.errno == errno.ENAMETOOLONG:
        return E_PATH_TOO_LONG
    if isinstance(exc, PermissionError):
        return E_ACCESS_DENIED
    return E_OS_ERROR


def remove_file_permanently(path: str) -> str | None:
    """Удаляет один файл. None — успех, иначе код причины. Исключения не пробрасываются."""
    lp = long_path(path)
    try:
        os.remove(lp)
        return None
    except FileNotFoundError:
        return R_NOT_FOUND
    except PermissionError as exc:
        reason = _classify_os_error(exc)
        if reason == E_LOCKED:
            return reason
        # Возможно, у файла атрибут «Только чтение» — снимаем его и пробуем ещё раз.
        try:
            st = os.lstat(lp)
            if is_reparse_or_link(st):
                return E_ACCESS_DENIED
            readonly = bool(file_attributes(st) & FILE_ATTRIBUTE_READONLY) or not (
                st.st_mode & stat.S_IWRITE
            )
            if not readonly:
                return reason
            os.chmod(lp, stat.S_IWRITE | stat.S_IREAD)
            os.remove(lp)
            return None
        except FileNotFoundError:
            return R_NOT_FOUND
        except OSError as exc2:
            return _classify_os_error(exc2)
    except OSError as exc:
        return _classify_os_error(exc)


def send_to_trash(path: str) -> str | None:
    """Перемещает файл в Корзину (можно восстановить). None — успех."""
    try:
        from send2trash import send2trash
    except ImportError:
        return E_TRASH_UNAVAILABLE
    try:
        send2trash(path)
        return None
    except FileNotFoundError:
        return R_NOT_FOUND
    except OSError as exc:
        return _classify_os_error(exc)
    except Exception as exc:  # noqa: BLE001 - send2trash может бросать свои исключения
        log.warning("send2trash: %s", exc)
        return E_OS_ERROR


class Cleaner:
    def __init__(
        self,
        policy: SafetyPolicy,
        recycle_api=recycle_bin,
        remover: Callable[[str], str | None] = remove_file_permanently,
        trasher: Callable[[str], str | None] = send_to_trash,
        system_api=None,
    ) -> None:
        self.policy = policy
        self.recycle_api = recycle_api
        self._remove = remover
        self._trash = trasher
        if system_api is None:
            from app.core.system_cleanup import SystemCleanup

            system_api = SystemCleanup()
        self.system_api = system_api

    # ------------------------------------------------------------ мусор
    def clean_junk(
        self,
        scan_result: ScanResult,
        category_ids: Iterable[str],
        progress_cb=None,
        cancel_event: threading.Event | None = None,
        quarantine=None,
    ) -> CleanReport:
        """quarantine — SessionWriter: файлы переносятся в карантин вместо удаления."""
        cancel = cancel_event or threading.Event()
        wanted = set(category_ids)
        targets = [c for c in scan_result.categories if c.category.id in wanted]
        total = sum(len(c.effective_items) if c.category.kind == KIND_FILES else 1 for c in targets)
        report = CleanReport()
        progress = Throttle(progress_cb)
        processed = 0
        log.info(
            "Начало удаления: категорий=%d, файлов=%d", len(targets), sum(c.file_count for c in targets)
        )

        def emit(cat_id: str, force: bool = False) -> None:
            progress(
                {
                    "category": cat_id,
                    "processed": processed,
                    "total": total,
                    "deleted": report.deleted_files,
                    "freed": report.freed_bytes,
                    "failed": report.failed_count,
                },
                force=force,
            )

        for cres in targets:
            if cancel.is_set():
                report.cancelled = True
                break
            cat = cres.category
            emit(cat.id, force=True)

            if cat.kind == KIND_RECYCLE:
                self._empty_recycle_bin(scan_result.drive, report)
                processed += 1
                emit(cat.id, force=True)
                continue

            if cat.kind == KIND_SYSTEM:
                # Встроенные средства Windows. DISM нельзя прерывать, поэтому отмена
                # учитывается только до запуска (проверено в начале цикла).
                def tool_progress(pct: float | None, _cat: str = cat.id, _done: int = processed) -> None:
                    progress(
                        {
                            "category": _cat,
                            "processed": _done,
                            "total": total,
                            "deleted": report.deleted_files,
                            "freed": report.freed_bytes,
                            "failed": report.failed_count,
                            "tool_percent": pct,
                        },
                        force=True,
                    )

                res = self.system_api.run(cat.id, scan_result.drive, tool_progress)
                report.freed_bytes += res.freed
                if res.ok:
                    report.tools_done.append(cat.id)
                    report.deleted_files += cres.file_count
                else:
                    report.failed.append((cat.id, res.code))
                    report.tool_details[cat.id] = res.details
                processed += 1
                emit(cat.id, force=True)
                continue

            touched_dirs: set[str] = set()
            for item in cres.effective_items:  # исключённые вручную файлы пропускаются
                if cancel.is_set():
                    report.cancelled = True
                    break
                processed += 1
                reason = self.policy.check_before_delete(item, cres.rules)
                moved_to_quarantine = False
                if reason is None:
                    if quarantine is not None:
                        before = len(quarantine.entries)
                        reason = quarantine.move(item.path, cat.id)
                        moved_to_quarantine = reason is None and len(quarantine.entries) > before
                    else:
                        reason = self._remove(item.path)
                if reason is None:
                    report.deleted_files += 1
                    if moved_to_quarantine:
                        report.quarantined_files += 1
                        report.quarantined_bytes += item.size
                    else:
                        report.freed_bytes += item.size
                    report.deleted_paths.add(item.path)
                    touched_dirs.add(os.path.dirname(item.path))
                elif reason == R_NOT_FOUND:
                    report.missing += 1
                else:
                    report.failed.append((item.path, reason))
                emit(cat.id)
            self._remove_empty_dirs(touched_dirs, cres.rules)
            if quarantine is not None:
                try:
                    quarantine.save()  # manifest после каждой категории — переживёт сбой
                except OSError as exc:
                    log.error("Не удалось сохранить manifest карантина: %s", exc)
            if report.cancelled:
                break

        if quarantine is not None and quarantine.entries:
            report.quarantine_session = quarantine.session_id
            log.info("В карантин перенесено %d файлов (%d байт), сессия %s",
                     report.quarantined_files, report.quarantined_bytes, quarantine.session_id)
        emit("", force=True)
        log.info(
            "Удаление завершено%s: удалено %d, освобождено %d байт, ошибок %d, уже отсутствовали %d",
            " (отменено)" if report.cancelled else "",
            report.deleted_files,
            report.freed_bytes,
            report.failed_count,
            report.missing,
        )
        for reason, count in report.reasons_summary().items():
            log.info("  не удалено по причине %s: %d", reason, count)
        return report

    def _empty_recycle_bin(self, drive: str, report: CleanReport) -> None:
        if not drive:
            return
        before = self.recycle_api.query(drive)
        if not before.available or before.count == 0:
            return
        ok = self.recycle_api.empty(drive)
        after = self.recycle_api.query(drive)
        if ok:
            freed = max(0, before.size - (after.size if after.available else 0))
            removed = max(0, before.count - (after.count if after.available else 0))
            report.freed_bytes += freed
            report.deleted_files += removed
            report.recycle_emptied = True
            log.info("Корзина %s очищена: %d элементов, %d байт", drive, removed, freed)
        else:
            report.failed.append((f"{drive} $Recycle.Bin", E_RECYCLE_FAILED))

    def _remove_empty_dirs(self, dirs: set[str], rules: Sequence[ResolvedRule]) -> None:
        """Удаляет опустевшие подпапки внутри рекурсивных правил (сам корень правила — никогда)."""
        recursive_roots = [r.root for r in rules if r.recursive]
        if not recursive_roots:
            return
        candidates: set[str] = set()
        for d in dirs:
            cur = d
            while True:
                norm = normalize(cur)
                if not any(is_within(norm, root, strict=True) for root in recursive_roots):
                    break
                candidates.add(cur)
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        # Сначала самые глубокие
        for d in sorted(candidates, key=lambda p: p.count(os.sep), reverse=True):
            try:
                if real_normalized(d) != normalize(d):
                    continue
                st = os.lstat(long_path(d))
                if is_reparse_or_link(st) or not stat.S_ISDIR(st.st_mode):
                    continue
                os.rmdir(long_path(d))  # удаляет только пустую папку
            except OSError:
                continue

    # ---------------------------------------------- большие файлы / дубликаты
    def delete_user_files(
        self,
        selected: Sequence[FileItem],
        found: Sequence[FileItem],
        roots: Sequence[str],
        mode: str = MODE_RECYCLE,
        duplicate_groups: Sequence[DuplicateGroup] | None = None,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> CleanReport:
        """Удаляет выбранные пользователем файлы (большие файлы или дубликаты).

        * каждый файл должен быть в списке found (найден сканером);
        * для дубликатов: в каждой группе обязательно остаётся минимум одна копия,
          и она должна существовать на диске с прежним размером.
        """
        cancel = cancel_event or threading.Event()
        report = CleanReport()
        progress = Throttle(progress_cb)
        found_map = {normalize(i.path): i for i in found}

        rules: list[ResolvedRule] = []
        for root in roots:
            if self.policy.validate_rule_root(root) is None:
                rules.append(
                    ResolvedRule("user_files", real_normalized(root), ("*",), True, 0.0, root)
                )

        blocked: dict[str, str] = {}
        if duplicate_groups is not None:
            blocked = self._duplicate_guard(selected, duplicate_groups)

        log.info("Удаление пользовательских файлов: %d шт., режим=%s", len(selected), mode)
        for n, item in enumerate(selected, start=1):
            if cancel.is_set():
                report.cancelled = True
                break
            key = normalize(item.path)
            known = found_map.get(key)
            if known is None or known.size != item.size or known.mtime != item.mtime:
                reason: str | None = R_NOT_SCANNED
            elif key in blocked:
                reason = blocked[key]
            else:
                reason = self.policy.check_before_delete(item, rules)
            if reason is None:
                reason = self._trash(item.path) if mode == MODE_RECYCLE else self._remove(item.path)
            if reason is None:
                report.deleted_files += 1
                report.freed_bytes += item.size
                report.deleted_paths.add(item.path)
            elif reason == R_NOT_FOUND:
                report.missing += 1
            else:
                report.failed.append((item.path, reason))
            progress(
                {
                    "category": "",
                    "processed": n,
                    "total": len(selected),
                    "deleted": report.deleted_files,
                    "freed": report.freed_bytes,
                    "failed": report.failed_count,
                }
            )
        log.info(
            "Удаление пользовательских файлов завершено: удалено %d, %d байт, ошибок %d",
            report.deleted_files,
            report.freed_bytes,
            report.failed_count,
        )
        return report

    @staticmethod
    def _duplicate_guard(
        selected: Sequence[FileItem], groups: Sequence[DuplicateGroup]
    ) -> dict[str, str]:
        """Возвращает {путь: причина} для файлов, удалять которые нельзя (последняя копия)."""
        selected_keys = {normalize(i.path) for i in selected}
        blocked: dict[str, str] = {}
        for group in groups:
            keys = [normalize(i.path) for i in group.items]
            chosen = [k for k in keys if k in selected_keys]
            if not chosen:
                continue
            keepers = [i for i, k in zip(group.items, keys, strict=True) if k not in selected_keys]
            alive = False
            for keeper in keepers:
                try:
                    st = os.stat(long_path(keeper.path))
                    if st.st_size == group.size:
                        alive = True
                        break
                except OSError:
                    continue
            if not alive:
                for k in chosen:
                    blocked[k] = R_LAST_COPY
        return blocked
