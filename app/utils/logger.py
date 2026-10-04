"""Логирование в logs/app.log (ротация 5 x 2 МБ). Только локально."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False
LOGGER_NAME = "diskcleaner"


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> Path | None:
    """Настраивает корневой логгер приложения. Возвращает путь к файлу лога."""
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    if _CONFIGURED:
        return None
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    log_file: Path | None = None
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "app.log"
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            log_file = None

    # В windowed-сборке sys.stderr может быть None.
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.setLevel(logging.WARNING)
        logger.addHandler(sh)

    _CONFIGURED = True
    return log_file


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
