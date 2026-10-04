"""Тесты поиска дубликатов и больших файлов."""

from __future__ import annotations

import os

from app.core.disk_analyzer import LargeFileFinder, get_disk_info
from app.core.duplicate_finder import PARTIAL_CHUNK, DuplicateFinder
from app.core.safety import SafetyPolicy
from tests.helpers import TempDirTestCase


class TestDuplicateFinder(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.policy = SafetyPolicy([str(self.tmp / "skip")], [], broad_roots_allowed=True)

    def test_finds_identical_files(self):
        data = os.urandom(5000)
        self.make_file("a/one.bin", content=data)
        self.make_file("b/two.bin", content=data)
        self.make_file("b/three.bin", content=data)
        self.make_file("a/other.bin", 5000)  # тот же размер, другое содержимое
        res = DuplicateFinder(self.policy).find([str(self.tmp)], min_size=1)
        self.assertEqual(len(res.groups), 1)
        self.assertEqual(res.groups[0].count, 3)
        self.assertEqual(res.wasted_bytes, 10000)

    def test_large_files_full_hash(self):
        size = PARTIAL_CHUNK * 4
        base = bytearray(os.urandom(size))
        self.make_file("x/a.bin", content=bytes(base))
        self.make_file("x/b.bin", content=bytes(base))
        changed = bytearray(base)
        changed[size // 2] ^= 0xFF  # отличие в середине — частичный хеш совпадёт, полный нет
        self.make_file("x/c.bin", content=bytes(changed))
        res = DuplicateFinder(self.policy).find([str(self.tmp)], min_size=1)
        self.assertEqual(len(res.groups), 1)
        self.assertEqual(sorted(os.path.basename(i.path) for i in res.groups[0].items), ["a.bin", "b.bin"])

    def test_min_size_and_forbidden(self):
        data = os.urandom(100)
        self.make_file("a/1.bin", content=data)
        self.make_file("a/2.bin", content=data)
        big = os.urandom(3000)
        self.make_file("a/3.bin", content=big)
        self.make_file("skip/4.bin", content=big)
        res = DuplicateFinder(self.policy).find([str(self.tmp)], min_size=1000)
        self.assertEqual(res.groups, [])

    def test_hardlinks_are_not_duplicates(self):
        a = self.make_file("a/1.bin", 2000)
        try:
            os.link(a, self.tmp / "a" / "2.bin")
        except OSError:
            self.skipTest("hardlink не поддерживается")
        res = DuplicateFinder(self.policy).find([str(self.tmp)], min_size=1)
        self.assertEqual(res.groups, [])

    def test_overlapping_roots(self):
        data = os.urandom(2000)
        self.make_file("a/1.bin", content=data)
        self.make_file("a/sub/2.bin", content=data)
        res = DuplicateFinder(self.policy).find([str(self.tmp / "a"), str(self.tmp / "a" / "sub")], min_size=1)
        self.assertEqual(len(res.groups), 1)
        self.assertEqual(res.groups[0].count, 2)


class TestLargeFiles(TempDirTestCase):
    def test_threshold_and_order(self):
        self.make_file("d/small.bin", 100)
        self.make_file("d/mid.bin", 2000)
        self.make_file("d/big.bin", 5000)
        self.make_file("skip/huge.bin", 9000)
        policy = SafetyPolicy([str(self.tmp / "skip")], [], broad_roots_allowed=True)
        res = LargeFileFinder(policy).find(str(self.tmp), min_size=1000)
        self.assertEqual([os.path.basename(i.path) for i in res.items], ["big.bin", "mid.bin"])

    def test_max_results(self):
        for i in range(10):
            self.make_file(f"d/{i}.bin", 1000 + i)
        policy = SafetyPolicy([], [], broad_roots_allowed=True)
        res = LargeFileFinder(policy).find(str(self.tmp), min_size=1, max_results=3)
        self.assertEqual(len(res.items), 3)
        self.assertTrue(res.truncated)
        self.assertEqual(res.items[0].size, 1009)

    def test_disk_info(self):
        info = get_disk_info(str(self.tmp))
        self.assertTrue(info.available)
        self.assertGreater(info.total, 0)
        bad = get_disk_info(str(self.tmp / "does_not_exist" / "x"))
        self.assertFalse(bad.available)
