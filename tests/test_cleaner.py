"""Тесты удаления. Только временные папки — никаких реальных системных директорий."""

from __future__ import annotations

import os
import stat
import threading
import unittest

from app.core import safety
from app.core.categories import KIND_RECYCLE, CategoryDef, RuleSpec
from app.core.cleaner import MODE_PERMANENT, Cleaner, remove_file_permanently
from app.core.safety import SafetyPolicy
from app.core.scanner import JunkScanner
from app.models.file_item import DuplicateGroup, FileItem
from tests.helpers import TempDirTestCase
from tests.test_scanner import FakeRecycle


class CleanerTestBase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.local = self.make_dir("Local")
        self.docs = self.make_dir("Documents")
        self.tokens = {"LOCALAPPDATA": str(self.local)}
        self.policy = SafetyPolicy(
            forbidden_roots=[str(self.docs)], container_roots=[str(self.tmp), str(self.local)]
        )
        self.cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        self.recycle = FakeRecycle()

    def scan(self, categories=None):
        scanner = JunkScanner(
            self.policy, tokens=self.tokens, categories=categories or [self.cat], recycle_api=self.recycle
        )
        return scanner.scan("C:\\")

    def cleaner(self, **kw):
        return Cleaner(self.policy, recycle_api=self.recycle, **kw)


class TestCleanJunk(CleanerTestBase):
    def test_deletes_only_scanned_files(self):
        a = self.make_file("Local/App/Cache/a.bin", 100)
        b = self.make_file("Local/App/Cache/deep/er/b.bin", 50)
        keep = self.make_file("Local/App/Data/settings.json", 10)
        doc = self.make_file("Documents/report.docx", 10)
        result = self.scan()
        report = self.cleaner().clean_junk(result, ["app"])
        self.assertEqual(report.deleted_files, 2)
        self.assertEqual(report.freed_bytes, 150)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())
        self.assertTrue(keep.exists())
        self.assertTrue(doc.exists())
        # пустые подпапки удалены, сам корень правила — нет
        self.assertFalse((self.local / "App" / "Cache" / "deep").exists())
        self.assertTrue((self.local / "App" / "Cache").is_dir())

    def test_unselected_category_untouched(self):
        a = self.make_file("Local/App/Cache/a.bin", 100)
        result = self.scan()
        report = self.cleaner().clean_junk(result, [])
        self.assertEqual(report.deleted_files, 0)
        self.assertTrue(a.exists())

    def test_file_added_after_scan_not_deleted(self):
        self.make_file("Local/App/Cache/a.bin", 100)
        result = self.scan()
        late = self.make_file("Local/App/Cache/late.bin", 100)
        self.cleaner().clean_junk(result, ["app"])
        self.assertTrue(late.exists())

    def test_file_changed_after_scan_not_deleted(self):
        a = self.make_file("Local/App/Cache/a.bin", 100)
        result = self.scan()
        a.write_bytes(b"new content of different size")
        report = self.cleaner().clean_junk(result, ["app"])
        self.assertTrue(a.exists())
        self.assertEqual(report.failed, [(str(a), safety.R_CHANGED)])

    def test_injected_foreign_item_rejected(self):
        """Даже если в результат подложили чужой путь, он не будет удалён."""
        self.make_file("Local/App/Cache/a.bin", 10)
        doc = self.make_file("Documents/thesis.docx", 10)
        outside = self.make_file("Other/file.txt", 10)
        result = self.scan()
        cres = result.get("app")
        for p in (doc, outside):
            st = os.stat(p)
            cres.items.append(FileItem(path=str(p), size=st.st_size, mtime=st.st_mtime))
        traversal = str(self.local / "App" / "Cache") + os.sep + ".." + os.sep + ".." + os.sep + ".." + os.sep + "Other" + os.sep + "file.txt"
        cres.items.append(FileItem(path=traversal, size=10, mtime=os.stat(outside).st_mtime))
        report = self.cleaner().clean_junk(result, ["app"])
        self.assertTrue(doc.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(report.deleted_files, 1)
        reasons = sorted(r for _, r in report.failed)
        self.assertEqual(reasons, sorted([safety.R_OUTSIDE, safety.R_OUTSIDE, safety.R_TRAVERSAL]))

    def test_file_swapped_for_symlink_after_scan(self):
        a = self.make_file("Local/App/Cache/a.bin", 10)
        target = self.make_file("Documents/precious.txt", 10)
        result = self.scan()
        a.unlink()
        if not self.try_symlink(target, a):
            self.skipTest("symlink не поддерживается")
        self.cleaner().clean_junk(result, ["app"])
        self.assertTrue(target.exists())

    def test_locked_files_skipped_and_counted(self):
        for i in range(3):
            self.make_file(f"Local/App/Cache/{i}.bin", 10)
        result = self.scan()
        calls = []

        def remover(path):
            calls.append(path)
            if path.endswith("1.bin"):
                return "locked"
            return remove_file_permanently(path)

        report = self.cleaner(remover=remover).clean_junk(result, ["app"])
        self.assertEqual(len(calls), 3)  # ошибка одного файла не остановила остальные
        self.assertEqual(report.deleted_files, 2)
        self.assertEqual(report.failed_count, 1)
        self.assertEqual(report.reasons_summary(), {"locked": 1})

    def test_missing_file_is_not_failure(self):
        a = self.make_file("Local/App/Cache/a.bin", 10)
        result = self.scan()
        a.unlink()
        report = self.cleaner().clean_junk(result, ["app"])
        self.assertEqual(report.failed_count, 0)
        self.assertEqual(report.missing, 1)

    def test_readonly_file(self):
        a = self.make_file("Local/App/Cache/ro.bin", 10)
        os.chmod(a, stat.S_IREAD)
        result = self.scan()
        report = self.cleaner().clean_junk(result, ["app"])
        self.assertEqual(report.deleted_files, 1)
        self.assertFalse(a.exists())

    def test_cancel(self):
        for i in range(5):
            self.make_file(f"Local/App/Cache/{i}.bin", 10)
        result = self.scan()
        ev = threading.Event()
        ev.set()
        report = self.cleaner().clean_junk(result, ["app"], cancel_event=ev)
        self.assertTrue(report.cancelled)
        self.assertEqual(report.deleted_files, 0)

    def test_recycle_bin_uses_api(self):
        self.recycle = FakeRecycle(size=4096, count=2)
        rb = CategoryDef(id="recycle_bin", group="recycle", kind=KIND_RECYCLE)
        result = self.scan([rb])
        report = self.cleaner().clean_junk(result, ["recycle_bin"])
        self.assertTrue(self.recycle.emptied)
        self.assertEqual(report.freed_bytes, 4096)
        self.assertEqual(report.deleted_files, 2)


class TestDeleteUserFiles(CleanerTestBase):
    def setUp(self):
        super().setUp()
        self.user_policy = SafetyPolicy(
            forbidden_roots=[str(self.local)], container_roots=[str(self.tmp)], broad_roots_allowed=True
        )

    def item(self, p):
        st = os.stat(p)
        return FileItem(path=str(p), size=st.st_size, mtime=st.st_mtime)

    def test_not_scanned_rejected(self):
        a = self.make_file("Files/a.bin", 10)
        b = self.make_file("Files/b.bin", 10)
        report = Cleaner(self.user_policy).delete_user_files(
            [self.item(b)], found=[self.item(a)], roots=[str(self.tmp / "Files")], mode=MODE_PERMANENT
        )
        self.assertTrue(b.exists())
        self.assertEqual(report.failed, [(str(b), safety.R_NOT_SCANNED)])

    def test_delete_large_file(self):
        a = self.make_file("Files/big.iso", 1000)
        it = self.item(a)
        report = Cleaner(self.user_policy).delete_user_files(
            [it], found=[it], roots=[str(self.tmp)], mode=MODE_PERMANENT
        )
        self.assertFalse(a.exists())
        self.assertEqual(report.freed_bytes, 1000)

    def test_forbidden_zone_protected(self):
        a = self.make_file("Local/app.db", 1000)
        it = self.item(a)
        report = Cleaner(self.user_policy).delete_user_files(
            [it], found=[it], roots=[str(self.tmp)], mode=MODE_PERMANENT
        )
        self.assertTrue(a.exists())
        self.assertEqual(report.failed[0][1], safety.R_PROTECTED_LOCATION)

    def test_recycle_mode_uses_trasher(self):
        a = self.make_file("Files/a.bin", 10)
        it = self.item(a)
        trashed = []
        cleaner = Cleaner(self.user_policy, trasher=lambda p: trashed.append(p) or None)
        cleaner.delete_user_files([it], found=[it], roots=[str(self.tmp)])
        self.assertEqual(trashed, [str(a)])
        self.assertTrue(a.exists())  # фейковый trasher ничего не удалял

    def test_duplicate_last_copy_protected(self):
        content = os.urandom(500)
        a = self.make_file("Files/a.bin", content=content)
        b = self.make_file("Files/b.bin", content=content)
        group = DuplicateGroup(size=500, digest="x", items=[self.item(a), self.item(b)])
        report = Cleaner(self.user_policy).delete_user_files(
            group.items, found=group.items, roots=[str(self.tmp)], mode=MODE_PERMANENT,
            duplicate_groups=[group],
        )
        self.assertTrue(a.exists() and b.exists())
        self.assertEqual({r for _, r in report.failed}, {safety.R_LAST_COPY})

    def test_duplicate_one_copy_kept(self):
        content = os.urandom(500)
        a = self.make_file("Files/a.bin", content=content)
        b = self.make_file("Files/b.bin", content=content)
        group = DuplicateGroup(size=500, digest="x", items=[self.item(a), self.item(b)])
        report = Cleaner(self.user_policy).delete_user_files(
            [group.items[1]], found=group.items, roots=[str(self.tmp)], mode=MODE_PERMANENT,
            duplicate_groups=[group],
        )
        self.assertTrue(a.exists())
        self.assertFalse(b.exists())
        self.assertEqual(report.deleted_files, 1)

    def test_duplicate_keeper_vanished(self):
        content = os.urandom(500)
        a = self.make_file("Files/a.bin", content=content)
        b = self.make_file("Files/b.bin", content=content)
        group = DuplicateGroup(size=500, digest="x", items=[self.item(a), self.item(b)])
        a.unlink()  # «оставляемая» копия исчезла
        Cleaner(self.user_policy).delete_user_files(
            [group.items[1]], found=group.items, roots=[str(self.tmp)], mode=MODE_PERMANENT,
            duplicate_groups=[group],
        )
        self.assertTrue(b.exists())


if __name__ == "__main__":
    unittest.main()
