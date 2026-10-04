"""Тесты сканера мусора и разрешения правил категорий."""

from __future__ import annotations

import os
import re
import threading
import unittest

from app.core import categories as cats
from app.core.categories import (
    CATEGORIES,
    AGE_TEMP,
    CategoryDef,
    RuleSpec,
    expand_template,
    resolve_category_rules,
)
from app.core.safety import SafetyPolicy
from app.core.scanner import JunkScanner
from app.core.recycle_bin import RecycleBinInfo, parse_info_file
from app.models.file_item import FileItem
from tests.helpers import TempDirTestCase


class FakeRecycle:
    def __init__(self, size=0, count=0):
        self.info = RecycleBinInfo(size=size, count=count, available=True)
        self.emptied = False

    def query(self, drive):
        return self.info

    def list_entries(self, drive):
        return [FileItem(path="C:\\old.txt", size=self.info.size, mtime=0, virtual=True)]

    def empty(self, drive):
        self.emptied = True
        self.info = RecycleBinInfo(size=0, count=0, available=True)
        return True


class ScannerTestBase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.local = self.make_dir("Local")
        self.roaming = self.make_dir("Roaming")
        self.temp = self.make_dir("Local/Temp")
        self.docs = self.make_dir("Documents")
        self.tokens = {
            "LOCALAPPDATA": str(self.local),
            "APPDATA": str(self.roaming),
            "TEMP": str(self.temp),
        }
        self.policy = SafetyPolicy(
            forbidden_roots=[str(self.docs)],
            container_roots=[str(self.tmp), str(self.local), str(self.roaming)],
        )

    def scanner(self, categories, **kw):
        return JunkScanner(self.policy, tokens=self.tokens, categories=categories,
                           recycle_api=kw.pop("recycle_api", FakeRecycle()), **kw)


class TestTemplates(ScannerTestBase):
    def test_expand_plain(self):
        self.make_dir("Local/App/Cache")
        self.assertEqual(
            expand_template("{LOCALAPPDATA}\\App\\Cache", self.tokens),
            [os.path.join(str(self.local), "App", "Cache")],
        )

    def test_expand_missing_dir(self):
        self.assertEqual(expand_template("{LOCALAPPDATA}\\Nope", self.tokens), [])

    def test_expand_unknown_token(self):
        self.assertEqual(expand_template("{STEAM}\\logs", self.tokens), [])

    def test_expand_rejects_traversal(self):
        self.assertEqual(expand_template("{LOCALAPPDATA}\\..\\Documents", self.tokens), [])

    def test_expand_glob_profiles(self):
        self.make_dir("Local/Browser/User Data/Default/Cache")
        self.make_dir("Local/Browser/User Data/Profile 1/Cache")
        self.make_dir("Local/Browser/User Data/Profile 2")  # без Cache
        found = expand_template("{LOCALAPPDATA}\\Browser\\User Data\\*\\Cache", self.tokens)
        self.assertEqual(len(found), 2)

    def test_rule_equal_to_container_rejected(self):
        bad = CategoryDef(id="bad", group="temp", rules=(RuleSpec("{LOCALAPPDATA}"),))
        res = resolve_category_rules(bad, self.tokens, self.policy)
        self.assertEqual(res.rules, [])
        self.assertEqual(len(res.rejected), 1)

    def test_rule_in_forbidden_rejected(self):
        tokens = dict(self.tokens, TEMP=str(self.docs))  # TEMP указывает в Documents!
        cat = CategoryDef(id="t", group="temp", rules=(RuleSpec("{TEMP}"),))
        res = resolve_category_rules(cat, tokens, self.policy)
        self.assertEqual(res.rules, [])

    def test_duplicate_roots_deduped(self):
        cat = CategoryDef(
            id="t", group="temp",
            rules=(RuleSpec("{TEMP}"), RuleSpec("{LOCALAPPDATA}\\Temp")),
        )
        res = resolve_category_rules(cat, self.tokens, self.policy)
        self.assertEqual(len(res.rules), 1)


class TestBuiltinCategories(ScannerTestBase):
    """Встроенные категории должны проходить проверки безопасности и не указывать на контейнеры."""

    def test_definitions_are_valid(self):
        token_names = set()
        for cat in CATEGORIES:
            for spec in cat.rules:
                m = re.match(r"^\{([A-Z0-9]+)\}", spec.template)
                self.assertIsNotNone(m, spec.template)
                token_names.add(m.group(1))
                self.assertNotIn("..", spec.template)
                self.assertTrue(spec.patterns, spec.template)
        self.assertTrue(token_names <= set(cats.default_tokens().keys()))

    def test_every_rule_resolves_safely(self):
        tokens = {}
        containers = []
        for name in ("SYSTEMROOT", "TEMP", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
                     "USERPROFILE", "PROGRAMFILES", "PROGRAMFILESX86", "STEAM"):
            d = self.make_dir(f"tok/{name}")
            tokens[name] = str(d)
            if name != "TEMP":
                containers.append(str(d))
        policy = SafetyPolicy(forbidden_roots=[], container_roots=containers)
        for cat in CATEGORIES:
            for spec in cat.rules:
                rel = spec.template.split("}", 1)[1].replace("*", "Default").strip("\\")
                path = os.path.join(tokens[spec.template[1:spec.template.index("}")]], *rel.split("\\")) if rel else None
                if path:
                    os.makedirs(path, exist_ok=True)
            res = resolve_category_rules(cat, tokens, policy)
            self.assertEqual(res.rejected, [], f"{cat.id}: {res.rejected}")
            if cat.kind == cats.KIND_FILES:
                self.assertTrue(res.rules, cat.id)


class TestJunkScanner(ScannerTestBase):
    def test_finds_files_in_rules_only(self):
        self.make_file("Local/App/Cache/a.bin", 100)
        self.make_file("Local/App/Cache/sub/b.bin", 200)
        self.make_file("Local/App/Data/user.db", 300)  # вне правила
        cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        result = self.scanner([cat]).scan(None)
        cres = result.get("app")
        self.assertEqual(cres.file_count, 2)
        self.assertEqual(cres.total_size, 300)
        # Сравниваем относительные пути: в полном пути на Windows есть "AppData".
        rel = sorted(os.path.relpath(i.path, self.local).replace("\\", "/") for i in cres.items)
        self.assertEqual(rel, ["App/Cache/a.bin", "App/Cache/sub/b.bin"])

    def test_patterns_and_non_recursive(self):
        self.make_file("Local/Explorer/thumbcache_32.db", 10)
        self.make_file("Local/Explorer/other.db", 10)
        self.make_file("Local/Explorer/sub/thumbcache_64.db", 10)
        cat = CategoryDef(
            id="thumbs", group="windows",
            rules=(RuleSpec("{LOCALAPPDATA}\\Explorer", patterns=("thumbcache_*.db",), recursive=False),),
        )
        cres = self.scanner([cat]).scan(None).get("thumbs")
        self.assertEqual([os.path.basename(i.path) for i in cres.items], ["thumbcache_32.db"])

    def test_min_age_skips_recent_files(self):
        self.make_file("Local/Temp/old.tmp", 10, age_seconds=48 * 3600)
        self.make_file("Local/Temp/new.tmp", 10)
        cat = CategoryDef(id="t", group="temp", rules=(RuleSpec("{TEMP}", age=AGE_TEMP),))
        cres = self.scanner([cat], temp_min_age_hours=24).scan(None).get("t")
        self.assertEqual([os.path.basename(i.path) for i in cres.items], ["old.tmp"])
        self.assertEqual(cres.skipped_recent, 1)

    def test_does_not_follow_symlinked_dirs(self):
        self.make_file("Documents/precious.docx", 50)
        self.make_dir("Local/App/Cache")
        if not self.try_symlink(self.docs, self.local / "App" / "Cache" / "evil", is_dir=True):
            self.skipTest("symlink не поддерживается")
        cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        cres = self.scanner([cat]).scan(None).get("app")
        self.assertEqual(cres.file_count, 0)

    def test_forbidden_subfolder_skipped(self):
        self.make_file("Local/App/Cache/ok.bin", 10)
        self.make_file("Local/App/Cache/keep/secret.bin", 10)
        policy = SafetyPolicy(
            forbidden_roots=[str(self.local / "App" / "Cache" / "keep")],
            container_roots=[str(self.tmp), str(self.local)],
        )
        cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        scanner = JunkScanner(policy, tokens=self.tokens, categories=[cat], recycle_api=FakeRecycle())
        cres = scanner.scan(None).get("app")
        self.assertEqual([os.path.basename(i.path) for i in cres.items], ["ok.bin"])

    def test_cancel(self):
        for i in range(50):
            self.make_file(f"Local/App/Cache/{i}.bin", 1)
        cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        ev = threading.Event()
        ev.set()
        result = self.scanner([cat]).scan(None, cancel_event=ev)
        self.assertTrue(result.cancelled)

    def test_recycle_bin_category(self):
        cat = CategoryDef(id="recycle_bin", group="recycle", kind=cats.KIND_RECYCLE)
        result = self.scanner([cat], recycle_api=FakeRecycle(size=5000, count=3)).scan("C:\\")
        cres = result.get("recycle_bin")
        self.assertEqual(cres.total_size, 5000)
        self.assertEqual(cres.file_count, 3)
        self.assertTrue(all(i.virtual for i in cres.items))

    def test_progress_callback_called(self):
        self.make_file("Local/App/Cache/a.bin", 1)
        cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        events = []
        self.scanner([cat]).scan(None, progress_cb=events.append)
        self.assertTrue(events)
        self.assertEqual(events[-1]["files"], 1)

    def test_unreadable_dir_does_not_crash(self):
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.skipTest("chmod-ограничения не действуют для этого окружения")
        d = self.make_dir("Local/App/Cache/locked")
        self.make_file("Local/App/Cache/ok.bin", 1)
        os.chmod(d, 0)
        try:
            cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
            cres = self.scanner([cat]).scan(None).get("app")
            self.assertEqual(cres.file_count, 1)
            self.assertTrue(cres.access_denied)
        finally:
            os.chmod(d, 0o755)


class TestRecycleParsing(unittest.TestCase):
    def test_parse_v2(self):
        import struct

        name = "C:\\Users\\me\\file.txt"
        data = struct.pack("<qqq", 2, 1234, 132000000000000000) + struct.pack("<I", len(name) + 1)
        data += (name + "\x00").encode("utf-16-le")
        parsed = parse_info_file(data)
        self.assertEqual(parsed[0], name)
        self.assertEqual(parsed[1], 1234)
        self.assertGreater(parsed[2], 0)

    def test_parse_v1(self):
        import struct

        name = "D:\\old.bin"
        raw = name.encode("utf-16-le").ljust(520, b"\x00")
        data = struct.pack("<qqq", 1, 99, 0) + raw
        self.assertEqual(parse_info_file(data)[:2], (name, 99))

    def test_parse_garbage(self):
        self.assertIsNone(parse_info_file(b"123"))
        import struct

        self.assertIsNone(parse_info_file(struct.pack("<qqq", 7, 0, 0)))


if __name__ == "__main__":
    unittest.main()
