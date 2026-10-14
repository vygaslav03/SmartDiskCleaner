"""История в памяти: кольцевые буферы для графика и рядов процессов (поиск утечек).

Хранится компактно (array('d')), а не списками словарей: 30 минут истории системы
и ~40 процессов — меньше 1 МБ.
"""

from __future__ import annotations

import threading
from array import array
from collections import deque
from dataclasses import dataclass

from app.memory.models import ProcessMemory, SystemMemory


class Ring:
    """Кольцевой буфер чисел фиксированной ёмкости."""

    __slots__ = ("_data", "_cap", "_start", "_len")

    def __init__(self, capacity: int) -> None:
        self._cap = max(1, int(capacity))
        self._data = array("d", [0.0]) * self._cap
        self._start = 0
        self._len = 0

    def append(self, value: float) -> None:
        idx = (self._start + self._len) % self._cap
        self._data[idx] = float(value)
        if self._len < self._cap:
            self._len += 1
        else:
            self._start = (self._start + 1) % self._cap

    def values(self) -> list[float]:
        return [self._data[(self._start + i) % self._cap] for i in range(self._len)]

    def last(self) -> float | None:
        return self._data[(self._start + self._len - 1) % self._cap] if self._len else None

    def __len__(self) -> int:
        return self._len


@dataclass(slots=True)
class SystemPoint:
    t: float
    in_use_pct: float
    commit_pct: float
    available: float
    commit: float
    hard_faults: float  # страниц/с, NaN если нет


class SystemHistory:
    FIELDS = ("t", "in_use_pct", "commit_pct", "available", "commit", "hard_faults")

    def __init__(self, capacity: int = 1800) -> None:
        self._lock = threading.Lock()
        self._rings = {f: Ring(capacity) for f in self.FIELDS}

    def add(self, s: SystemMemory) -> None:
        used = s.in_use if s.in_use is not None else s.used
        with self._lock:
            self._rings["t"].append(s.timestamp)
            self._rings["in_use_pct"].append(100.0 * used / s.total if s.total else 0.0)
            self._rings["commit_pct"].append(100.0 * s.commit_ratio)
            self._rings["available"].append(s.available)
            self._rings["commit"].append(s.commit_total)
            self._rings["hard_faults"].append(
                s.hard_faults_per_sec if s.hard_faults_per_sec is not None else float("nan")
            )

    def series(self, name: str) -> list[float]:
        with self._lock:
            return self._rings[name].values()

    def points(self) -> list[SystemPoint]:
        with self._lock:
            cols = [self._rings[f].values() for f in self.FIELDS]
        return [SystemPoint(*row) for row in zip(*cols)]

    def __len__(self) -> int:
        return len(self._rings["t"])


class ProcessHistory:
    """Ряды private bytes по процессам. Отслеживаются top-N по памяти + уже отслеживаемые,
    чтобы процесс, медленно ползущий вверх, не терял историю.
    """

    def __init__(self, capacity: int = 360, max_tracked: int = 40) -> None:
        self._cap = capacity
        self._max = max_tracked
        self._lock = threading.Lock()
        self._series: dict[tuple[int, float], tuple[str, Ring, Ring]] = {}

    def add(self, timestamp: float, processes: list[ProcessMemory]) -> None:
        by_key = {p.key: p for p in processes}
        top = sorted(processes, key=lambda p: p.footprint, reverse=True)[: self._max]
        with self._lock:
            # процессы, которые завершились, — удаляем
            for key in [k for k in self._series if k not in by_key]:
                del self._series[key]
            wanted = [p.key for p in top] + [k for k in self._series if k in by_key]
            for key in dict.fromkeys(wanted):
                p = by_key[key]
                entry = self._series.get(key)
                if entry is None:
                    if len(self._series) >= self._max * 2:
                        continue
                    entry = (p.name, Ring(self._cap), Ring(self._cap))
                    self._series[key] = entry
                entry[1].append(timestamp)
                entry[2].append(p.footprint)

    def items(self) -> list[tuple[tuple[int, float], str, list[float], list[float]]]:
        with self._lock:
            return [(k, name, t.values(), v.values()) for k, (name, t, v) in self._series.items()]

    def series_for(self, key: tuple[int, float]) -> tuple[list[float], list[float]] | None:
        with self._lock:
            entry = self._series.get(key)
            return (entry[1].values(), entry[2].values()) if entry else None


class EventLog:
    """Последние события (скачки) для отображения."""

    def __init__(self, maxlen: int = 200) -> None:
        self._items: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, item) -> None:
        with self._lock:
            self._items.append(item)

    def items(self) -> list:
        with self._lock:
            return list(self._items)
