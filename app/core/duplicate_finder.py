"""Поиск дубликатов: размер -> частичный хеш (начало+конец) -> полный хеш BLAKE2b.

Нагрузка ограничена: читаются только файлы, у которых есть «пара» по размеру,
и полностью — только те, у кого совпал частичный хеш. Файлы OneDrive «только в
облаке» не читаются (иначе Windows начнёт их скачивать). Жёсткие ссылки на один
и тот же файл не считаются дубликатами (их удаление не освобождает место).
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from typing import Iterable

from app.core.disk_analyzer import WalkStats, walk_files
from app.core.safety import ResolvedRule, SafetyPolicy
from app.core.scanner import Throttle
from app.models.file_item import DuplicateGroup, FileItem
from app.utils import winpaths
from app.utils.logger import get_logger
from app.utils.winpaths import is_cloud_only, long_path

log = get_logger("duplicates")

PARTIAL_CHUNK = 64 * 1024
FULL_CHUNK = 1024 * 1024


@dataclass
class DuplicatesResult:
    roots: list[str]
    groups: list[DuplicateGroup] = field(default_factory=list)
    scanned_files: int = 0
    errors: int = 0
    cancelled: bool = False

    @property
    def all_items(self) -> list[FileItem]:
        return [i for g in self.groups for i in g.items]

    @property
    def wasted_bytes(self) -> int:
        return sum(g.wasted_bytes for g in self.groups)


def _hash_partial(path: str, size: int) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(long_path(path), "rb") as fh:
        if size <= PARTIAL_CHUNK * 2:
            h.update(fh.read())
        else:
            h.update(fh.read(PARTIAL_CHUNK))
            fh.seek(-PARTIAL_CHUNK, os.SEEK_END)
            h.update(fh.read(PARTIAL_CHUNK))
    return h.hexdigest()


def _hash_full(path: str, cancel: threading.Event, on_bytes) -> str | None:
    h = hashlib.blake2b(digest_size=32)
    with open(long_path(path), "rb") as fh:
        while True:
            if cancel.is_set():
                return None
            chunk = fh.read(FULL_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            on_bytes(len(chunk))
    return h.hexdigest()


class DuplicateFinder:
    def __init__(self, policy: SafetyPolicy) -> None:
        self.policy = policy

    def find(
        self,
        roots: Iterable[str],
        min_size: int = 1024 * 1024,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> DuplicatesResult:
        cancel = cancel_event or threading.Event()
        min_size = max(1, int(min_size))
        valid_roots: list[str] = []
        for r in roots:
            if r and os.path.isdir(r) and self.policy.validate_rule_root(r) is None:
                valid_roots.append(r)
        # Убираем вложенные корни (Documents внутри OneDrive и т.п.), чтобы не обходить дважды.
        norm_roots = sorted({winpaths.real_normalized(r): r for r in valid_roots}.items())
        top: list[tuple[str, str]] = []
        for norm, raw in norm_roots:
            if not any(winpaths.is_within(norm, t[0]) for t in top):
                top.append((norm, raw))
        result = DuplicatesResult(roots=[raw for _, raw in top])
        progress = Throttle(progress_cb, 0.15)
        stats = WalkStats()
        log.info("Поиск дубликатов: %d папок, мин. размер %d байт", len(top), min_size)

        # ---- 1. группировка по размеру
        by_size: dict[int, list[tuple[str, float]]] = {}
        seen: set[str] = set()
        for norm_root, raw_root in top:
            rule = ResolvedRule("duplicates", norm_root, ("*",), True, 0.0, raw_root)

            def on_dir() -> None:
                progress({"phase": "collect", "files": stats.files, "current": stats.current})

            for path, st in walk_files(raw_root, self.policy, cancel, stats, on_dir=on_dir):
                if st.st_size < min_size or is_cloud_only(st):
                    continue
                norm = os.path.normcase(path)
                if norm in seen or self.policy.check_candidate(norm, st, (rule,)):
                    continue
                seen.add(norm)
                by_size.setdefault(int(st.st_size), []).append((path, float(st.st_mtime)))
            if cancel.is_set():
                break
        result.scanned_files = stats.files
        result.errors = stats.errors
        if cancel.is_set():
            result.cancelled = True
            return result

        candidates = {s: files for s, files in by_size.items() if len(files) > 1}

        # ---- 2. отсев жёстких ссылок + частичный хеш
        partial_groups: dict[tuple[int, str], list[tuple[str, float]]] = {}
        total_candidates = sum(len(v) for v in candidates.values())
        done = 0
        for size, files in candidates.items():
            inodes: set[tuple[int, int]] = set()
            for path, mtime in files:
                if cancel.is_set():
                    result.cancelled = True
                    return result
                done += 1
                progress({"phase": "partial", "done": done, "total": total_candidates})
                try:
                    st = os.stat(long_path(path))
                    if st.st_ino:
                        ident = (st.st_dev, st.st_ino)
                        if ident in inodes:
                            continue
                        inodes.add(ident)
                    digest = _hash_partial(path, size)
                except OSError:
                    result.errors += 1
                    continue
                partial_groups.setdefault((size, digest), []).append((path, mtime))

        # ---- 3. полный хеш
        to_hash = {k: v for k, v in partial_groups.items() if len(v) > 1}
        total_bytes = sum(k[0] * len(v) for k, v in to_hash.items() if k[0] > PARTIAL_CHUNK * 2)
        hashed = [0]

        def on_bytes(n: int) -> None:
            hashed[0] += n
            progress({"phase": "full", "bytes": hashed[0], "total_bytes": total_bytes})

        groups: list[DuplicateGroup] = []
        for (size, partial), files in to_hash.items():
            full_groups: dict[str, list[FileItem]] = {}
            for path, mtime in files:
                if cancel.is_set():
                    result.cancelled = True
                    return result
                if size <= PARTIAL_CHUNK * 2:
                    digest: str | None = partial  # частичный хеш уже покрыл весь файл
                else:
                    try:
                        digest = _hash_full(path, cancel, on_bytes)
                    except OSError:
                        result.errors += 1
                        continue
                if digest is None:
                    result.cancelled = True
                    return result
                full_groups.setdefault(digest, []).append(
                    FileItem(path=path, size=size, mtime=mtime, category_id="duplicates")
                )
            for digest, items in full_groups.items():
                if len(items) > 1:
                    items.sort(key=lambda i: i.mtime)
                    groups.append(DuplicateGroup(size=size, digest=digest, items=items))

        groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
        result.groups = groups
        progress({"phase": "done", "groups": len(groups)}, force=True)
        log.info(
            "Поиск дубликатов завершён: групп %d, лишних байт %d, просмотрено файлов %d",
            len(groups), result.wasted_bytes, stats.files,
        )
        return result
