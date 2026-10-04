"""Модели найденных файлов."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(slots=True)
class FileItem:
    """Файл, найденный сканером.

    path / size / mtime фиксируются в момент сканирования: перед удалением
    они сверяются с текущим состоянием файла — изменившийся файл не удаляется.
    virtual=True — элемент только для отображения (например, запись корзины),
    удалять его как файл нельзя.
    """

    path: str
    size: int
    mtime: float
    category_id: str = ""
    display_name: str | None = None
    virtual: bool = False
    cloud_only: bool = False

    @property
    def name(self) -> str:
        return self.display_name or os.path.basename(self.path) or self.path

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


@dataclass(slots=True)
class DuplicateGroup:
    """Группа файлов с одинаковым содержимым (совпадает размер и полный хеш)."""

    size: int
    digest: str
    items: list[FileItem] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def wasted_bytes(self) -> int:
        return self.size * max(0, len(self.items) - 1)
