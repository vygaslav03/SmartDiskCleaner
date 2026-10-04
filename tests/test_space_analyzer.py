"""Тесты анализатора занятого места (только чтение)."""

from __future__ import annotations

import os
import threading

from app.core.space_analyzer import SpaceAnalyzer
from tests.helpers import TempDirTestCase


class TestSpaceAnalyzer(TempDirTestCase):
    def test_sizes_are_aggregated(self):
        self.make_file("root/a.bin", 100)
        self.make_file("root/sub/b.bin", 200)
        self.make_file("root/sub/deep/c.bin", 300)
        self.make_file("root/other/d.bin", 50)
        res = SpaceAnalyzer().analyze(str(self.tmp / "root"))
        root = res.root
        self.assertEqual(root.size, 650)
        self.assertEqual(root.files, 4)
        self.assertEqual(root.own_size, 100)
        self.assertEqual(res.scanned_files, 4)
        children = {c.name: c for c in root.children}
        self.assertEqual(children["sub"].size, 500)
        self.assertEqual(children["sub"].files, 2)
        self.assertEqual(children["other"].size, 50)
        self.assertEqual([c.name for c in root.sorted_children()], ["sub", "other"])
        self.assertEqual(root.dirs, 3)

    def test_top_files_limited_and_sorted(self):
        for i in range(10):
            self.make_file(f"root/f{i}.bin", 10 + i)
        res = SpaceAnalyzer(top_files_per_dir=3).analyze(str(self.tmp / "root"))
        top = res.root.top_files
        self.assertEqual([f.size for f in top], [19, 18, 17])
        self.assertEqual(res.root.own_files, 10)

    def test_symlinked_dir_not_followed(self):
        self.make_file("outside/huge.bin", 5000)
        self.make_file("root/a.bin", 10)
        if not self.try_symlink(self.tmp / "outside", self.tmp / "root" / "link", is_dir=True):
            self.skipTest("symlink не поддерживается")
        res = SpaceAnalyzer().analyze(str(self.tmp / "root"))
        self.assertEqual(res.root.size, 10)
        self.assertEqual(res.skipped_links, 1)

    def test_empty_and_missing_root(self):
        self.make_dir("empty")
        res = SpaceAnalyzer().analyze(str(self.tmp / "empty"))
        self.assertEqual(res.root.size, 0)
        res2 = SpaceAnalyzer().analyze(str(self.tmp / "nope"))
        self.assertEqual(res2.root.size, 0)

    def test_cancel(self):
        for i in range(20):
            self.make_file(f"root/d{i}/f.bin", 10)
        ev = threading.Event()
        ev.set()
        res = SpaceAnalyzer().analyze(str(self.tmp / "root"), cancel_event=ev)
        self.assertTrue(res.cancelled)

    def test_unreadable_dir_counted(self):
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.skipTest("chmod-ограничения не действуют для этого окружения")
        d = self.make_dir("root/locked")
        self.make_file("root/ok.bin", 5)
        os.chmod(d, 0)
        try:
            res = SpaceAnalyzer().analyze(str(self.tmp / "root"))
            self.assertEqual(res.root.size, 5)
            self.assertGreaterEqual(res.errors, 1)
        finally:
            os.chmod(d, 0o755)
