"""Общие помощники для тестов. Все файлы создаются ТОЛЬКО во временных папках."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path


class TempDirTestCase(unittest.TestCase):
    """Создаёт изолированную временную папку для каждого теста и удаляет её после."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="diskcleaner_test_")
        # realpath: на macOS /var -> /private/var, на Windows — раскрытие 8.3-имён
        self.tmp = Path(os.path.realpath(self._tmp))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -------------------------------------------------------------- helpers
    def make_file(self, rel: str, size: int = 100, age_seconds: float = 0, content: bytes | None = None) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content if content is not None else os.urandom(size)
        path.write_bytes(data)
        if age_seconds:
            t = time.time() - age_seconds
            os.utime(path, (t, t))
        return path

    def make_dir(self, rel: str) -> Path:
        path = self.tmp / rel
        path.mkdir(parents=True, exist_ok=True)
        return path

    def try_symlink(self, target: Path, link: Path, is_dir: bool = False) -> bool:
        """Создаёт symlink; на Windows без прав разработчика это невозможно — тест пропускается."""
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link, target_is_directory=is_dir)
            return True
        except (OSError, NotImplementedError):
            return False
