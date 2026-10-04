"""Проверка запущенных программ, которые держат файлы кэша открытыми.

Программа НЕ закрывает процессы сама (это может стоить пользователю несохранённых
данных) — только сообщает, что запущено, и предлагает закрыть вручную.
"""

from __future__ import annotations

from typing import Callable, Iterable

from app.core.categories import PROCESSES
from app.utils.logger import get_logger

log = get_logger("processes")


def running_process_names() -> set[str]:
    try:
        import psutil
    except ImportError:
        return set()
    names: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
        except Exception:  # noqa: BLE001 - процесс мог завершиться или быть недоступен
            continue
        if name:
            names.add(name.lower())
    return names


def running_apps(
    category_ids: Iterable[str],
    names_provider: Callable[[], set[str]] = running_process_names,
) -> dict[str, list[str]]:
    """{id категории: [запущенные процессы]} только для категорий, где что-то запущено."""
    wanted = [c for c in category_ids if c in PROCESSES]
    if not wanted:
        return {}
    try:
        names = names_provider()
    except Exception:  # noqa: BLE001 - проверка вспомогательная, ошибка не должна мешать
        log.exception("Не удалось получить список процессов")
        return {}
    result: dict[str, list[str]] = {}
    for cat_id in wanted:
        found = [p for p in PROCESSES[cat_id] if p in names]
        if found:
            result[cat_id] = found
    return result
