"""Структуры данных модуля памяти. Без обращений к ОС — их удобно создавать в тестах."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

MB = 1024 * 1024
GB = 1024 * MB


@dataclass(frozen=True, slots=True)
class SystemMemory:
    """Снимок памяти системы. Поля None — показатель недоступен в этой среде.

    Термины как в Диспетчере задач / Мониторе ресурсов:
      available = standby + free (то, что Windows отдаст программам без подкачки);
      commit_*  = выделенная память (обещания программам), лимит = RAM + файлы подкачки.
    """

    timestamp: float
    total: int
    available: int
    load_percent: float
    commit_total: int
    commit_limit: int
    commit_peak: int | None = None
    system_cache: int | None = None
    kernel_paged: int | None = None
    kernel_nonpaged: int | None = None
    # списки страниц (PDH); None, если счётчики недоступны
    standby: int | None = None
    standby_core: int | None = None
    standby_normal: int | None = None
    standby_reserve: int | None = None
    modified: int | None = None
    free_zero: int | None = None
    # скорости (PDH, нужна вторая выборка); None на первом снимке
    hard_faults_per_sec: float | None = None  # \Memory\Pages Input/sec — страниц, прочитанных с диска
    page_reads_per_sec: float | None = None  # \Memory\Page Reads/sec — число операций чтения
    pagefile_usage_percent: float | None = None
    process_count: int | None = None
    source: str = "psutil"  # "winapi" | "psutil" | "test"

    @property
    def used(self) -> int:
        """Занято = всего − доступно (то, что показывает большинство утилит)."""
        return max(0, self.total - self.available)

    @property
    def has_lists(self) -> bool:
        return self.standby is not None and self.free_zero is not None and self.modified is not None

    @property
    def in_use(self) -> int | None:
        """«Используется» как в Мониторе ресурсов: всего − (standby + free) − modified."""
        if not self.has_lists:
            return None
        return max(0, self.total - self.standby - self.free_zero - self.modified)  # type: ignore[operator]

    @property
    def commit_ratio(self) -> float:
        return self.commit_total / self.commit_limit if self.commit_limit else 0.0

    @property
    def hard_fault_bytes_per_sec(self) -> float | None:
        if self.hard_faults_per_sec is None:
            return None
        return self.hard_faults_per_sec * 4096  # страница x64 — 4 КБ


@dataclass(slots=True)
class ProcessMemory:
    """Память одного процесса. private_bytes — выделено процессом (не делится с другими)."""

    pid: int
    name: str
    create_time: float
    working_set: int
    private_bytes: int | None
    private_ws: int | None = None  # Private Working Set (USS) — только по запросу, дорого
    handles: int | None = None
    cpu_time: float | None = None
    owned: bool = False  # процесс текущего пользователя
    has_window: bool = False
    protected: bool = False  # системный/критический — действия запрещены
    exe: str = ""
    services: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[int, float]:
        """Уникальный ключ: PID переиспользуются, поэтому вместе со временем запуска."""
        return (self.pid, round(self.create_time, 3))

    @property
    def footprint(self) -> int:
        """Для сортировки: private bytes, а если недоступны — рабочий набор."""
        return self.private_bytes if self.private_bytes is not None else self.working_set


@dataclass(slots=True)
class PagefileInfo:
    name: str
    total: int
    in_use: int
    peak: int


@dataclass(slots=True)
class PagefileConfig:
    """Настройка файла подкачки (только чтение реестра)."""

    files: list[PagefileInfo] = field(default_factory=list)
    configured: list[str] = field(default_factory=list)  # строки PagingFiles из реестра
    system_managed: bool | None = None
    disabled: bool | None = None
    known: bool = False  # удалось ли прочитать конфигурацию

    @property
    def total(self) -> int:
        return sum(f.total for f in self.files)


class Severity(IntEnum):
    OK = 0
    INFO = 1
    WATCH = 2
    WARNING = 3
    CRITICAL = 4


@dataclass(slots=True)
class Finding:
    """Вывод анализа. Тексты — ключи i18n + параметры, чтобы анализ не зависел от языка."""

    code: str
    severity: Severity
    params: dict = field(default_factory=dict)


@dataclass(slots=True)
class Assessment:
    severity: Severity
    findings: list[Finding]

    @property
    def needs_action(self) -> bool:
        return self.severity >= Severity.WARNING


@dataclass(slots=True)
class LeakSuspect:
    pid: int
    name: str
    create_time: float
    slope_bytes_per_hour: float
    r2: float
    growth: int
    window_sec: float
    current: int


@dataclass(slots=True)
class SpikeEvent:
    timestamp: float
    scope: str  # "system" | "process"
    name: str
    delta: int
    pid: int | None = None


class ActionKind:
    CLOSE_APP = "close_app"
    TERMINATE = "terminate"
    TRIM = "trim_ws"
    STARTUP_DISABLE = "startup_disable"
    STARTUP_ENABLE = "startup_enable"
    PURGE_STANDBY = "purge_standby"
    PAGEFILE_INFO = "pagefile_info"


@dataclass(slots=True)
class Recommendation:
    """Предложение действия. Ничего не выполняет — только описывает."""

    kind: str
    code: str  # ключ причины (i18n: mem.rec.<code>)
    params: dict = field(default_factory=dict)
    target_pid: int | None = None
    target_create_time: float | None = None
    target_name: str = ""
    startup_id: str = ""
    reversible: bool = False
    needs_admin: bool = False
    executable: bool = True  # False — только совет (например, файл подкачки)
    priority: int = 0
