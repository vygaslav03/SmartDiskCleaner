"""Тесты импорта Winapp2.ini: разбор, перевод путей, детект и безопасность."""

from __future__ import annotations

import os
import unittest

from app.core import winapp2
from app.core.categories import resolve_category_rules
from app.core.safety import SafetyPolicy
from app.core.scanner import JunkScanner
from tests.helpers import TempDirTestCase
from tests.test_scanner import FakeRecycle

SAMPLE = """\ufeff; Version: 999999
; comment
[Version]
Version=1

[Some App Cache *]
LangSecRef=3021
DetectFile=%LocalAppData%\\SomeApp
Default=False
Warning=Close SomeApp first.
FileKey1=%LocalAppData%\\SomeApp\\Cache|*.*|RECURSE
FileKey2=%LocalAppData%\\SomeApp\\logs|*.log;*.txt
ExcludeKey1=FILE|%LocalAppData%\\SomeApp\\Cache\\|keep.dat
ExcludeKey2=PATH|%LocalAppData%\\SomeApp\\Cache\\Important\\

[Missing App *]
DetectFile=%LocalAppData%\\NotInstalledApp
FileKey1=%LocalAppData%\\NotInstalledApp\\Cache|*.*|RECURSE

[Registry Only *]
Detect=HKCU\\Software\\Something
RegKey1=HKCU\\Software\\Something\\MRU

[Evil Profile Wipe *]
FileKey1=%UserProfile%|*.*|RECURSE

[Evil Documents *]
FileKey1=%Documents%\\Stuff|*.*|RECURSE

[Evil Absolute *]
FileKey1=C:\\Windows\\System32|*.dll

[Evil Traversal *]
FileKey1=%LocalAppData%\\..\\..\\Desktop|*.*|RECURSE

[Evil Pattern *]
FileKey1=%LocalAppData%\\SomeApp|..\\*.*

[Program Files App *]
FileKey1=%ProgramFiles%\\Vendor\\Logs|*.log
"""


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.res = winapp2.parse_text(SAMPLE)
        self.by_title = {c.title: c for c in self.res.categories}

    def test_counts(self):
        self.assertEqual(self.res.total_entries, 9)
        self.assertEqual(self.res.registry_only, 1)
        self.assertNotIn("Registry Only", self.by_title)

    def test_rules_translated(self):
        cat = self.by_title["Some App Cache"]
        self.assertEqual(cat.group, winapp2.GROUP)
        self.assertFalse(cat.smart)
        self.assertEqual(cat.description, "Close SomeApp first.")
        self.assertEqual(cat.rules[0].template, "{LOCALAPPDATA}\\SomeApp\\Cache")
        self.assertTrue(cat.rules[0].recursive)
        self.assertEqual(cat.rules[0].patterns, ("*",))
        self.assertFalse(cat.rules[1].recursive)
        self.assertEqual(cat.rules[1].patterns, ("*.log", "*.txt"))
        self.assertEqual(len(cat.excludes), 2)

    def test_unsupported_paths_skipped(self):
        for evil in ("Evil Documents", "Evil Absolute", "Evil Traversal", "Evil Pattern"):
            self.assertNotIn(evil, self.by_title, evil)
        self.assertGreaterEqual(self.res.unsupported_keys, 4)

    def test_program_files_expands_both(self):
        cat = self.by_title["Program Files App"]
        self.assertEqual(
            sorted(r.template for r in cat.rules),
            ["{PROGRAMFILESX86}\\Vendor\\Logs", "{PROGRAMFILES}\\Vendor\\Logs"],
        )

    def test_ids_unique_and_prefixed(self):
        ids = [c.id for c in self.res.categories]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith(winapp2.ID_PREFIX) for i in ids))

    def test_translate_path(self):
        self.assertEqual(winapp2.translate_path("%WinDir%\\Temp"), ["{SYSTEMROOT}\\Temp"])
        self.assertEqual(winapp2.translate_path("%Pictures%\\x"), [])
        self.assertEqual(winapp2.translate_path("D:\\x"), [])
        self.assertEqual(winapp2.translate_path("%AppData%\\a\\..\\b"), [])


class TestWinapp2Safety(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.local = self.make_dir("Users/me/AppData/Local")
        self.profile = self.tmp / "Users" / "me"
        self.tokens = {"LOCALAPPDATA": str(self.local), "USERPROFILE": str(self.profile)}
        self.policy = SafetyPolicy(
            forbidden_roots=[str(self.profile / "Documents")],
            container_roots=[str(self.tmp), str(self.profile), str(self.local)],
        )
        self.res = winapp2.parse_text(SAMPLE)
        self.by_title = {c.title: c for c in self.res.categories}

    def test_profile_wipe_rejected_by_policy(self):
        cat = self.by_title["Evil Profile Wipe"]
        resolution = resolve_category_rules(cat, self.tokens, self.policy)
        self.assertEqual(resolution.rules, [])
        self.assertEqual(len(resolution.rejected), 1)

    def test_scan_with_detect_and_excludes(self):
        old = 3 * 86400  # импортированные правила не трогают свежие файлы
        self.make_file("Users/me/AppData/Local/SomeApp/Cache/a.bin", 10, age_seconds=old)
        self.make_file("Users/me/AppData/Local/SomeApp/Cache/keep.dat", 10, age_seconds=old)
        self.make_file("Users/me/AppData/Local/SomeApp/Cache/Important/x.bin", 10, age_seconds=old)
        self.make_file("Users/me/AppData/Local/SomeApp/logs/old.log", 10, age_seconds=old)
        self.make_file("Users/me/AppData/Local/SomeApp/logs/data.db", 10, age_seconds=old)
        self.make_file("Users/me/doc.txt", 10, age_seconds=old)
        scanner = JunkScanner(self.policy, tokens=self.tokens, categories=self.res.categories,
                              recycle_api=FakeRecycle())
        result = scanner.scan(None)
        some = result.get(self.by_title["Some App Cache"].id)
        names = sorted(os.path.basename(i.path) for i in some.items)
        self.assertEqual(names, ["a.bin", "old.log"])
        missing = result.get(self.by_title["Missing App"].id)
        self.assertTrue(missing.is_empty)
        wipe = result.get(self.by_title["Evil Profile Wipe"].id)
        self.assertTrue(wipe.is_empty)

    def test_builtin_wins_duplicates(self):
        from app.core.categories import CategoryDef, RuleSpec

        builtin = CategoryDef(id="someapp", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\SomeApp\\Cache"),))
        self.make_file("Users/me/AppData/Local/SomeApp/Cache/a.bin", 10, age_seconds=3 * 86400)
        scanner = JunkScanner(self.policy, tokens=self.tokens,
                              categories=[builtin, *self.res.categories], recycle_api=FakeRecycle())
        result = scanner.scan(None)
        self.assertEqual(result.get("someapp").file_count, 1)
        self.assertEqual(result.get(self.by_title["Some App Cache"].id).file_count, 0)

    def test_import_and_remove(self):
        src = self.tmp / "winapp2.ini"
        src.write_text(SAMPLE, encoding="utf-8")
        from unittest import mock

        dest = self.tmp / "data" / "winapp2.ini"
        with mock.patch.object(winapp2, "imported_path", return_value=dest):
            res = winapp2.import_file(src)
            self.assertTrue(dest.exists())
            self.assertEqual(len(winapp2.load_imported().categories), len(res.categories))
            winapp2.remove_imported()
            self.assertFalse(dest.exists())
            self.assertEqual(winapp2.load_imported().categories, [])

    def test_import_rejects_garbage(self):
        src = self.tmp / "bad.ini"
        src.write_text("[Only]\nRegKey1=HKCU\\x\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            winapp2.import_file(src)


if __name__ == "__main__":
    unittest.main()


class TestImportedRulesRespectProtections(TempDirTestCase):
    def test_recent_temp_files_not_taken_by_imported_rule(self):
        from app.core.categories import AGE_TEMP, CategoryDef, RuleSpec

        local = self.make_dir("Local")
        temp = self.make_dir("Local/Temp")
        self.make_file("Local/Temp/fresh.tmp", 10)  # свежий
        self.make_file("Local/Temp/old.tmp", 10, age_seconds=3 * 86400)
        tokens = {"LOCALAPPDATA": str(local), "TEMP": str(temp)}
        policy = SafetyPolicy([], [str(self.tmp), str(local)])
        builtin = CategoryDef(id="user_temp", group="temp", rules=(RuleSpec("{TEMP}", age=AGE_TEMP),))
        imported = winapp2.parse_text("[Temp Tmp *]\nFileKey1=%LocalAppData%\\Temp|*.tmp|RECURSE\n").categories
        result = JunkScanner(policy, tokens=tokens, categories=[builtin, *imported],
                             recycle_api=FakeRecycle(), temp_min_age_hours=24).scan(None)
        b = result.get("user_temp")
        i = result.get(imported[0].id)
        self.assertEqual([os.path.basename(x.path) for x in b.items], ["old.tmp"])
        self.assertEqual(i.file_count, 0)  # свежий файл не подобран импортированным правилом
        self.assertEqual(b.skipped_recent, 1)

    def test_imported_rules_have_min_age(self):
        cat = winapp2.parse_text("[X *]\nFileKey1=%AppData%\\X\\Cache|*.*|RECURSE\n").categories[0]
        from app.core.categories import AGE_TEMP

        self.assertTrue(all(r.age == AGE_TEMP for r in cat.rules))
