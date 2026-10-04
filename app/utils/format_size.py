"""Форматирование размеров и дат для отображения."""

from __future__ import annotations

import datetime as _dt

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_size(num_bytes: int | float, precision: int = 2) -> str:
    """Возвращает размер в человекочитаемом виде (основание 1024, как в Проводнике).

    >>> format_size(0)
    '0 B'
    >>> format_size(1536)
    '1.50 KB'
    """
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "0 B"
    if size < 0:
        size = 0.0
    unit = 0
    while size >= 1024 and unit < len(_UNITS) - 1:
        size /= 1024.0
        unit += 1
    if unit == 0:
        return f"{int(size)} B"
    return f"{size:.{precision}f} {_UNITS[unit]}"


def format_timestamp(ts: float | None) -> str:
    """Unix-время -> 'YYYY-MM-DD HH:MM'. Некорректные значения -> '—'."""
    if not ts:
        return "—"
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"
