"""Сканер мусора: обходит ТОЛЬКО папки из правил категорий (без полного обхода диска)."""

from __future__ import annotations

import fnmatch
import os
import stat
import threading
import time
from typing import Callable, Iterable, Mapping

from app.core import recycle_bin
from app.core.categories import (
    CATEGORIES,
    KIND_RECYCLE,
    KIND_SYSTEM,
    CategoryDef,
    default_tokens,
    expand_template,
    resolve_category_rules,
)
from app.core.safety import R_OUTSIDE, ResolvedRule, SafetyPolicy
from app.models.file_item import FileItem
from app.models.scan_result import CategoryResult, ScanResult
from app.utils import winpaths
from app.utils.logger import get_logger
from app.utils.winpaths import is_reparse_or_link, long_path

log = get_logger("scanner")

ProgressCallback = Callable[[dict], None]


class Throttle:
    """Ограничивает частоту вызова progress-колбэка (не чаще interval секунд)."""

    def __init__(self, callback: ProgressCallback | None, interval: float = 0.1) -> None:
        self._cb = callback
        self._interval = interval
        self._last = 0.0

    def __call__(self, payload: dict, force: bool = False) -> None:
        if self._cb is None:
            return
        now = time.monotonic()
        if force or now - self._last >= self._interval:
            self._last = now
            try:
                self._cb(payload)
            except Exception:  # noqa: BLE001 - ошибка UI не должна ломать сканирование
                log.exception("Ошибка в progress-колбэке")


class JunkScanner:
    def __init__(
        self,
        policy: SafetyPolicy,
        tokens: Mapping[str, str] | None = None,
        categories: Iterable[CategoryDef] = CATEGORIES,
        temp_min_age_hours: int = 24,
        log_min_age_days: int = 7,
        recycle_api=recycle_bin,
        system_api=None,
    ) -> None:
        self.policy = policy
        self.tokens = dict(tokens) if tokens is not None else default_tokens()
        self.categories = list(categories)
        self.temp_hours = temp_min_age_hours
        self.log_days = log_min_age_days
        self.recycle_api = recycle_api
        if system_api is None:
            from app.core.system_cleanup import SystemCleanup

            system_api = SystemCleanup()
        self.system_api = system_api

    def scan(
        self,
        drive: str | None,
        progress_cb: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        category_ids: Iterable[str] | None = None,
    ) -> ScanResult:
        cancel = cancel_event or threading.Event()
        wanted = set(category_ids) if category_ids is not None else None
        cats = [c for c in self.categories if wanted is None or c.id in wanted]
        result = ScanResult(drive=drive or "")
        progress = Throttle(progress_cb)
        log.info("Начало сканирования: диск=%s, категорий=%d", drive, len(cats))

        totals = {"files": 0, "bytes": 0}
        # Общий для всего сканирования: файл попадает только в первую подходящую
        # категорию (встроенные идут раньше импортированных Winapp2).
        self._seen: set[str] = set()
        for index, cat in enumerate(cats):
            if cancel.is_set():
                result.cancelled = True
                break
            progress(
                {"category": cat.id, "index": index, "total": len(cats), **totals}, force=True
            )
            try:
                if cat.kind == KIND_RECYCLE:
                    cres = self._scan_recycle_bin(cat, drive)
                elif cat.kind == KIND_SYSTEM:
                    cres = self._scan_system_tool(cat, drive, cancel)
                else:
                    cres = self._scan_category(cat, drive, cancel, progress, index, len(cats), totals)
            except Exception:  # noqa: BLE001 - одна категория не должна ронять весь скан
                log.exception("Ошибка при сканировании категории %s", cat.id)
                cres = CategoryResult(category=cat, errors=1)
            result.categories.append(cres)
            if cat.kind in (KIND_RECYCLE, KIND_SYSTEM):
                totals["files"] += cres.file_count
                totals["bytes"] += cres.total_size
            if cres.file_count:
                log.info(
                    "Категория %s: %d файлов, %d байт (ошибок: %d, пропущено свежих: %d)",
                    cat.id, cres.file_count, cres.total_size, cres.errors, cres.skipped_recent,
                )
        if cancel.is_set():
            result.cancelled = True
        result.finished_at = time.time()
        progress({"category": "", "index": len(cats), "total": len(cats), **totals}, force=True)
        log.info(
            "Сканирование завершено%s: %d файлов, %d байт",
            " (отменено)" if result.cancelled else "",
            result.total_files,
            result.total_size,
        )
        return result

    # ----------------------------------------------------------------- private
    def _resolve_excludes(self, cat: CategoryDef) -> list[tuple[str, tuple[str, ...], bool]]:
        out: list[tuple[str, tuple[str, ...], bool]] = []
        for ex in cat.excludes:
            for path in expand_template(ex.template, self.tokens):
                out.append((winpaths.real_normalized(path), ex.patterns, ex.whole_dir))
        return out

    @staticmethod
    def _is_excluded(norm: str, excludes: list[tuple[str, tuple[str, ...], bool]]) -> bool:
        name = os.path.basename(norm)
        parent = os.path.dirname(norm)
        for root, patterns, whole_dir in excludes:
            if whole_dir:
                if winpaths.is_within(norm, root, strict=True):
                    return True
            elif parent == root and any(
                fnmatch.fnmatchcase(name, os.path.normcase(p)) for p in patterns
            ):
                return True
        return False

    def _scan_system_tool(self, cat: CategoryDef, drive: str | None, cancel: threading.Event) -> CategoryResult:
        cres = CategoryResult(category=cat)
        m = self.system_api.measure(cat.id, drive, cancel)
        if not m.available:
            return cres
        cres.tool_available = True
        cres.size_known = m.size_known
        cres.size_partial = m.partial
        cres.reported_size = m.size
        cres.reported_count = m.files
        return cres

    def _scan_recycle_bin(self, cat: CategoryDef, drive: str | None) -> CategoryResult:
        cres = CategoryResult(category=cat)
        if not drive:
            return cres
        info = self.recycle_api.query(drive)
        if not info.available:
            return cres
        cres.reported_size = info.size
        cres.reported_count = info.count
        if info.count:
            cres.items = self.recycle_api.list_entries(drive)
        return cres

    def _scan_category(
        self,
        cat: CategoryDef,
        drive: str | None,
        cancel: threading.Event,
        progress: Throttle,
        index: int,
        total: int,
        totals: dict,
    ) -> CategoryResult:
        if cat.detect is not None:
            try:
                installed = cat.detect(self.tokens)
            except Exception:  # noqa: BLE001 - ошибка проверки = считаем, что программы нет
                installed = False
            if not installed:
                return CategoryResult(category=cat)
        resolution = resolve_category_rules(
            cat, self.tokens, self.policy, drive, self.temp_hours, self.log_days
        )
        cres = CategoryResult(category=cat, rules=resolution.rules)
        seen = self._seen
        now = time.time()
        excludes = self._resolve_excludes(cat)
        for rule in resolution.rules:
            if cancel.is_set():
                break
            self._walk_rule(rule, cres, seen, now, cancel, progress, index, total, totals, excludes)
        return cres

    def _walk_rule(
        self,
        rule: ResolvedRule,
        cres: CategoryResult,
        seen: set[str],
        now: float,
        cancel: threading.Event,
        progress: Throttle,
        index: int,
        total: int,
        totals: dict,
        excludes: list[tuple[str, tuple[str, ...], bool]] | None = None,
    ) -> None:
        excludes = excludes or []
        stack = [rule.walk_root]
        while stack:
            if cancel.is_set():
                return
            directory = stack.pop()
            try:
                it = os.scandir(long_path(directory))
            except PermissionError:
                cres.access_denied = True
                cres.errors += 1
                continue
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                cres.errors += 1
                continue
            with it:
                while True:
                    try:
                        entry = next(it)
                    except StopIteration:
                        break
                    except OSError:
                        cres.errors += 1
                        break
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        cres.errors += 1
                        continue
                    full = os.path.join(directory, entry.name)
                    norm = os.path.normcase(full)
                    if stat.S_ISDIR(st.st_mode):
                        if is_reparse_or_link(st):
                            continue  # никогда не заходим в junction/symlink
                        if rule.recursive and not self.policy.is_excluded_dir(norm):
                            stack.append(full)
                        continue
                    if norm in seen:
                        continue
                    if excludes and self._is_excluded(norm, excludes):
                        continue
                    if rule.min_age_seconds and now - st.st_mtime < rule.min_age_seconds:
                        cres.skipped_recent += 1
                        # Файл «занят» этой категорией: другая (например, импортированная)
                        # категория не должна подобрать его в обход защиты свежих файлов.
                        seen.add(norm)
                        continue
                    reason = self.policy.check_candidate(norm, st, (rule,))
                    if reason:
                        if reason != R_OUTSIDE:  # несовпадение маски — это не «небезопасно»
                            cres.skipped_unsafe += 1
                            seen.add(norm)
                        continue
                    seen.add(norm)
                    cres.items.append(
                        FileItem(path=full, size=int(st.st_size), mtime=float(st.st_mtime),
                                 category_id=rule.category_id)
                    )
                    totals["files"] += 1
                    totals["bytes"] += int(st.st_size)
                    progress({"category": rule.category_id, "index": index, "total": total, **totals})
