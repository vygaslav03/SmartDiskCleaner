"""Анализ занятого места: дерево папок с размерами (только чтение, ничего не удаляет).

Обход итеративный, без перехода по symlink/junction. Для каждой папки хранится
суммарный размер, число файлов и N самых больших файлов непосредственно в ней —
этого хватает для «проваливания» в дерево без хранения всех файлов диска в памяти.

Файлы OneDrive «только в облаке» не учитываются в размере: на диске они не занимают места.
"""

from __future__ import annotations

import heapq
import os
import stat
import threading
import time
from dataclasses import dataclass, field

from app.core.scanner import Throttle
from app.models.file_item import FileItem
from app.utils import winpaths
from app.utils.logger import get_logger
from app.utils.winpaths import is_cloud_only, is_reparse_or_link, long_path

log = get_logger("space_analyzer")

TOP_FILES_PER_DIR = 30


@dataclass(eq=False)
class DirNode:
    name: str
    path: str
    parent: "DirNode | None" = None
    children: list["DirNode"] = field(default_factory=list)
    own_size: int = 0  # файлы непосредственно в папке
    own_files: int = 0
    size: int = 0  # с учётом подпапок
    files: int = 0
    dirs: int = 0
    mtime: float = 0.0
    errors: int = 0
    access_denied: bool = False
    # куча (size, counter, FileItem) — самые большие файлы в этой папке
    _top: list = field(default_factory=list, repr=False)

    @property
    def top_files(self) -> list[FileItem]:
        return [e[2] for e in sorted(self._top, key=lambda e: e[0], reverse=True)]

    def sorted_children(self) -> list["DirNode"]:
        return sorted(self.children, key=lambda n: n.size, reverse=True)


@dataclass
class SpaceResult:
    root: DirNode
    scanned_files: int = 0
    scanned_dirs: int = 0
    errors: int = 0
    skipped_links: int = 0
    cloud_only_files: int = 0
    duration: float = 0.0
    cancelled: bool = False


class SpaceAnalyzer:
    def __init__(self, top_files_per_dir: int = TOP_FILES_PER_DIR) -> None:
        self.top_n = max(1, top_files_per_dir)

    def analyze(
        self,
        root: str,
        progress_cb=None,
        cancel_event: threading.Event | None = None,
    ) -> SpaceResult:
        cancel = cancel_event or threading.Event()
        t0 = time.monotonic()
        root_path = winpaths.strip_long_prefix(os.path.realpath(root))
        root_node = DirNode(name=root_path, path=root_path)
        result = SpaceResult(root=root_node)
        progress = Throttle(progress_cb, 0.15)
        counter = 0
        total_bytes = 0
        all_nodes: list[DirNode] = [root_node]
        stack: list[DirNode] = [root_node]
        log.info("Анализ места: %s", root_path)

        while stack:
            if cancel.is_set():
                result.cancelled = True
                break
            node = stack.pop()
            result.scanned_dirs += 1
            try:
                it = os.scandir(long_path(node.path))
            except PermissionError:
                node.access_denied = True
                node.errors += 1
                result.errors += 1
                continue
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                node.errors += 1
                result.errors += 1
                continue
            with it:
                while True:
                    try:
                        entry = next(it)
                    except StopIteration:
                        break
                    except OSError:
                        node.errors += 1
                        result.errors += 1
                        break
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        node.errors += 1
                        result.errors += 1
                        continue
                    if is_reparse_or_link(st):
                        result.skipped_links += 1
                        continue
                    full = os.path.join(node.path, entry.name)
                    if stat.S_ISDIR(st.st_mode):
                        child = DirNode(name=entry.name, path=full, parent=node, mtime=float(st.st_mtime))
                        node.children.append(child)
                        all_nodes.append(child)
                        stack.append(child)
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    result.scanned_files += 1
                    if is_cloud_only(st):
                        result.cloud_only_files += 1
                        continue
                    size = int(st.st_size)
                    node.own_size += size
                    node.own_files += 1
                    total_bytes += size
                    counter += 1
                    if len(node._top) < self.top_n:
                        heapq.heappush(node._top, (size, counter, FileItem(full, size, float(st.st_mtime))))
                    elif size > node._top[0][0]:
                        heapq.heapreplace(node._top, (size, counter, FileItem(full, size, float(st.st_mtime))))
            progress(
                {
                    "dirs": result.scanned_dirs,
                    "files": result.scanned_files,
                    "bytes": total_bytes,
                    "current": node.path,
                }
            )

        # Суммы снизу вверх: узлы добавлялись в all_nodes раньше своих детей,
        # поэтому обратный порядок гарантирует, что дети посчитаны до родителя.
        for node in reversed(all_nodes):
            node.size += node.own_size
            node.files += node.own_files
            if node.parent is not None:
                node.parent.size += node.size
                node.parent.files += node.files
                node.parent.dirs += node.dirs + 1

        result.duration = time.monotonic() - t0
        progress(
            {"dirs": result.scanned_dirs, "files": result.scanned_files, "bytes": total_bytes, "current": ""},
            force=True,
        )
        log.info(
            "Анализ места завершён%s: %d папок, %d файлов, %d байт, ошибок %d, %.1f c",
            " (отменён)" if result.cancelled else "",
            result.scanned_dirs, result.scanned_files, root_node.size, result.errors, result.duration,
        )
        return result
