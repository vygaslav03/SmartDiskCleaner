"""Информация о дисках и поиск больших файлов."""

from __future__ import annotations

import heapq
import itertools
import os
import shutil
import stat
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

from app.core.safety import ResolvedRule, SafetyPolicy
from app.core.scanner import Throttle
from app.models.file_item import FileItem
from app.utils import winpaths
from app.utils.logger import get_logger
from app.utils.winpaths import is_cloud_only, is_reparse_or_link, long_path

log = get_logger("disk_analyzer")


@dataclass
class DiskInfo:
    drive: str
    total: int = 0
    used: int = 0
    free: int = 0
    label: str = ""
    available: bool = False

    @property
    def percent_used(self) -> float:
        return (self.used / self.total * 100.0) if self.total else 0.0


def get_disk_info(drive: str) -> DiskInfo:
    """Total / Used / Free. Недоступный диск не роняет программу — available=False."""
    info = DiskInfo(drive=drive)
    try:
        try:
            import psutil

            usage = psutil.disk_usage(drive)
            info.total, info.used, info.free = int(usage.total), int(usage.used), int(usage.free)
        except ImportError:
            usage = shutil.disk_usage(drive)
            info.total, info.used, info.free = int(usage.total), int(usage.used), int(usage.free)
        info.available = True
    except (OSError, ValueError) as exc:
        log.warning("Диск %s недоступен: %s", drive, exc)
        return info
    try:
        info.label = winpaths.volume_label(drive)
    except OSError:
        info.label = ""
    return info


@dataclass
class WalkStats:
    files: int = 0
    dirs: int = 0
    errors: int = 0
    current: str = ""


def walk_files(
    root: str,
    policy: SafetyPolicy,
    cancel: threading.Event,
    stats: WalkStats,
    on_dir: Callable[[], None] | None = None,
) -> Iterator[tuple[str, os.stat_result]]:
    """Итеративный обход (без рекурсии Python) с пропуском защищённых зон и junction.

    Не следует по symlink/junction — это защищает от циклов и выхода за пределы корня.
    """
    stack = [winpaths.strip_long_prefix(os.path.realpath(root))]
    while stack:
        if cancel.is_set():
            return
        directory = stack.pop()
        stats.dirs += 1
        stats.current = directory
        if on_dir:
            on_dir()
        try:
            it = os.scandir(long_path(directory))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            stats.errors += 1
            continue
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    stats.errors += 1
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    stats.errors += 1
                    continue
                full = os.path.join(directory, entry.name)
                if stat.S_ISDIR(st.st_mode):
                    if is_reparse_or_link(st):
                        continue
                    if policy.is_excluded_dir(os.path.normcase(full)):
                        continue
                    stack.append(full)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                stats.files += 1
                yield full, st


@dataclass
class LargeFilesResult:
    root: str
    min_size: int
    items: list[FileItem] = field(default_factory=list)
    scanned_files: int = 0
    scanned_dirs: int = 0
    errors: int = 0
    truncated: bool = False  # найдено больше, чем max_results
    cancelled: bool = False


class LargeFileFinder:
    def __init__(self, policy: SafetyPolicy) -> None:
        self.policy = policy

    def find(
        self,
        root: str,
        min_size: int,
        max_results: int = 2000,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> LargeFilesResult:
        cancel = cancel_event or threading.Event()
        result = LargeFilesResult(root=root, min_size=min_size)
        reason = self.policy.validate_rule_root(root)
        if reason:
            log.warning("Поиск больших файлов: корень %s отклонён (%s)", root, reason)
            return result
        rule = ResolvedRule("large_files", winpaths.real_normalized(root), ("*",), True, 0.0, root)
        stats = WalkStats()
        progress = Throttle(progress_cb, 0.15)
        heap: list[tuple[int, int, FileItem]] = []
        counter = itertools.count()
        found_total = 0

        def emit(force: bool = False) -> None:
            progress(
                {"files": stats.files, "dirs": stats.dirs, "found": found_total, "current": stats.current},
                force=force,
            )

        log.info("Поиск больших файлов: %s, порог %d байт", root, min_size)
        for path, st in walk_files(root, self.policy, cancel, stats, on_dir=emit):
            if st.st_size < min_size:
                continue
            norm = os.path.normcase(path)
            if self.policy.check_candidate(norm, st, (rule,)):
                continue
            found_total += 1
            item = FileItem(
                path=path,
                size=int(st.st_size),
                mtime=float(st.st_mtime),
                category_id="large_files",
                cloud_only=is_cloud_only(st),
            )
            entry = (item.size, next(counter), item)
            if len(heap) < max_results:
                heapq.heappush(heap, entry)
            else:
                result.truncated = True
                heapq.heappushpop(heap, entry)
        result.items = [e[2] for e in sorted(heap, key=lambda e: e[0], reverse=True)]
        result.scanned_files = stats.files
        result.scanned_dirs = stats.dirs
        result.errors = stats.errors
        result.cancelled = cancel.is_set()
        emit(force=True)
        log.info(
            "Поиск больших файлов завершён: просмотрено %d файлов, найдено %d (ошибок доступа %d)",
            stats.files, found_total, stats.errors,
        )
        return result
