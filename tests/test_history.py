"""Тесты истории очисток и исключения отдельных файлов."""

from __future__ import annotations

import json

from app.core import history as h
from app.core.categories import CategoryDef, RuleSpec
from app.core.cleaner import CleanReport, Cleaner
from app.core.safety import SafetyPolicy
from app.core.scanner import JunkScanner
from tests.helpers import TempDirTestCase
from tests.test_scanner import FakeRecycle


class TestHistory(TempDirTestCase):
    def store(self):
        return h.HistoryStore(self.tmp / "history.json")

    def test_add_and_load_newest_first(self):
        s = self.store()
        s.add(h.HistoryEntry(timestamp=100, kind=h.KIND_JUNK, deleted=5, freed=1000))
        s.add(h.HistoryEntry(timestamp=200, kind=h.KIND_LARGE, deleted=1, freed=50, mode="recycle"))
        entries = s.load()
        self.assertEqual([e.timestamp for e in entries], [200, 100])
        self.assertEqual(h.totals(entries), (1050, 6, 2))

    def test_cap(self):
        s = self.store()
        data = {"entries": [{"timestamp": i, "kind": "junk"} for i in range(h.MAX_ENTRIES)]}
        (self.tmp / "history.json").write_text(json.dumps(data), encoding="utf-8")
        s.add(h.HistoryEntry(timestamp=10**9, kind=h.KIND_JUNK))
        self.assertEqual(len(s.load()), h.MAX_ENTRIES)

    def test_corrupt_file_recovered(self):
        (self.tmp / "history.json").write_text("{not json", encoding="utf-8")
        s = self.store()
        self.assertEqual(s.load(), [])
        self.assertTrue((self.tmp / "history.bad").exists())
        self.assertTrue(s.add(h.HistoryEntry(timestamp=1, kind=h.KIND_JUNK)))
        self.assertEqual(len(s.load()), 1)

    def test_bad_entries_skipped(self):
        data = {"entries": [{"timestamp": "x", "kind": "junk"}, "garbage", {"timestamp": 5, "kind": "junk", "freed": 7}]}
        (self.tmp / "history.json").write_text(json.dumps(data), encoding="utf-8")
        entries = self.store().load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].freed, 7)

    def test_clear(self):
        s = self.store()
        s.add(h.HistoryEntry(timestamp=1, kind=h.KIND_JUNK))
        s.clear()
        self.assertEqual(s.load(), [])

    def test_entry_has_no_paths(self):
        report = CleanReport(deleted_files=2, freed_bytes=10, deleted_paths={"C:\\secret\\a.txt"})
        entry = h.make_entry(h.KIND_JUNK, report, drive="C:\\", categories=["chrome"])
        s = self.store()
        s.add(entry)
        text = (self.tmp / "history.json").read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertIn("chrome", text)


class TestExcludedFiles(TempDirTestCase):
    def test_excluded_files_are_kept(self):
        local = self.make_dir("Local")
        a = self.make_file("Local/App/Cache/a.bin", 10)
        b = self.make_file("Local/App/Cache/b.bin", 20)
        policy = SafetyPolicy([], [str(self.tmp), str(local)])
        cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        result = JunkScanner(policy, tokens={"LOCALAPPDATA": str(local)}, categories=[cat],
                             recycle_api=FakeRecycle()).scan(None)
        cres = result.get("app")
        cres.selected = True
        cres.excluded = {str(b)}
        self.assertEqual(result.selected_files, 1)
        self.assertEqual(result.selected_size, 10)
        report = Cleaner(policy, recycle_api=FakeRecycle()).clean_junk(result, ["app"])
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        self.assertEqual(report.deleted_files, 1)

    def test_all_excluded_category_not_selected(self):
        local = self.make_dir("Local")
        a = self.make_file("Local/App/Cache/a.bin", 10)
        policy = SafetyPolicy([], [str(self.tmp), str(local)])
        cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))
        result = JunkScanner(policy, tokens={"LOCALAPPDATA": str(local)}, categories=[cat],
                             recycle_api=FakeRecycle()).scan(None)
        cres = result.get("app")
        cres.selected = True
        cres.excluded = {str(a)}
        self.assertEqual(result.selected(), [])
