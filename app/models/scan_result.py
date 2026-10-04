"""Результаты сканирования мусора."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.models.file_item import FileItem

if TYPE_CHECKING:  # pragma: no cover
    from app.core.categories import CategoryDef
    from app.core.safety import ResolvedRule


@dataclass
class CategoryResult:
    category: "CategoryDef"
    items: list[FileItem] = field(default_factory=list)
    rules: list["ResolvedRule"] = field(default_factory=list)
    # Для корзины размер/количество берутся из WinAPI, а не из items.
    reported_size: int | None = None
    reported_count: int | None = None
    errors: int = 0
    skipped_recent: int = 0
    skipped_unsafe: int = 0
    access_denied: bool = False
    selected: bool = False
    # Для системных инструментов (Windows.old, DISM): категория доступна,
    # даже если файлов «нет» в списке, и размер может быть неизвестен заранее.
    tool_available: bool = False
    size_known: bool = True
    size_partial: bool = False
    # Файлы, с которых пользователь снял галочку в «Подробнее» — они не удаляются.
    excluded: set[str] = field(default_factory=set)

    @property
    def total_size(self) -> int:
        if self.reported_size is not None:
            return self.reported_size
        return sum(i.size for i in self.items)

    @property
    def file_count(self) -> int:
        if self.reported_count is not None:
            return self.reported_count
        return len(self.items)

    @property
    def is_empty(self) -> bool:
        if self.tool_available:
            return False
        return self.file_count == 0 and self.total_size == 0

    @property
    def effective_items(self) -> list[FileItem]:
        """Элементы, которые реально пойдут на удаление (без исключённых вручную)."""
        if not self.excluded:
            return self.items
        return [i for i in self.items if i.path not in self.excluded]

    @property
    def effective_size(self) -> int:
        if not self.excluded:
            return self.total_size
        return sum(i.size for i in self.effective_items)

    @property
    def effective_count(self) -> int:
        if not self.excluded:
            return self.file_count
        return len(self.effective_items)

    def remove_paths(self, paths: set[str]) -> None:
        if not paths:
            return
        self.items = [i for i in self.items if i.path not in paths]
        self.excluded -= paths


@dataclass
class ScanResult:
    drive: str
    categories: list[CategoryResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancelled: bool = False

    def get(self, category_id: str) -> CategoryResult | None:
        for c in self.categories:
            if c.category.id == category_id:
                return c
        return None

    @property
    def total_size(self) -> int:
        return sum(c.total_size for c in self.categories)

    @property
    def total_files(self) -> int:
        return sum(c.file_count for c in self.categories)

    def selected(self) -> list[CategoryResult]:
        # Категория, в которой пользователь снял галочки со всех файлов, не выбирается.
        return [
            c
            for c in self.categories
            if c.selected and not c.is_empty and not (c.excluded and c.effective_count == 0)
        ]

    @property
    def selected_size(self) -> int:
        return sum(c.effective_size for c in self.selected())

    @property
    def selected_files(self) -> int:
        return sum(c.effective_count for c in self.selected())
