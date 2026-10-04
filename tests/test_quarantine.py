"""Тесты карантина: перенос, восстановление без перезаписи, срок хранения, защита."""

from __future__ import annotations

import json
import os
import time
from unittest import mock

from app.core import quarantine as q
from app.core.categories import CategoryDef, RuleSpec
from app.core.cleaner import Cleaner
from app.core.safety import SafetyPolicy
from app.core.scanner import JunkScanner
from tests.helpers import TempDirTestCase
from tests.test_scanner import FakeRecycle


class QuarantineTestBase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.store = q.QuarantineStore(self.tmp / "Q")
        self.local = self.make_dir("Local")
        self.policy = SafetyPolicy([str(self.tmp / "Q")], [str(self.tmp), str(self.local)])
        self.cat = CategoryDef(id="app", group="apps", rules=(RuleSpec("{LOCALAPPDATA}\\App\\Cache"),))

    def scan(self):
        return JunkScanner(self.policy, tokens={"LOCALAPPDATA": str(self.local)}, categories=[self.cat],
                           recycle_api=FakeRecycle()).scan(None)

    def clean_into_quarantine(self):
        result = self.scan()
        writer = self.store.new_session(["app"])
        report = Cleaner(self.policy, recycle_api=FakeRecycle()).clean_junk(result, ["app"], quarantine=writer)
        return report, writer


class TestMoveAndRestore(QuarantineTestBase):
    def test_clean_moves_files_instead_of_deleting(self):
        a = self.make_file("Local/App/Cache/a.bin", 100)
        b = self.make_file("Local/App/Cache/sub/b.bin", 50)
        report, writer = self.clean_into_quarantine()
        self.assertFalse(a.exists() or b.exists())
        self.assertEqual(report.deleted_files, 2)
        self.assertEqual(report.quarantined_files, 2)
        self.assertEqual(report.quarantined_bytes, 150)
        self.assertEqual(report.freed_bytes, 0)  # место ещё не освобождено
        sessions = self.store.sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].size, 150)
        self.assertEqual(sessions[0].categories, ["app"])

    def test_restore_returns_files(self):
        a = self.make_file("Local/App/Cache/a.bin", content=b"hello")
        b = self.make_file("Local/App/Cache/sub/b.bin", content=b"world")
        self.clean_into_quarantine()
        session = self.store.sessions()[0]
        rep = self.store.restore(session)
        self.assertEqual(rep.restored, 2)
        self.assertEqual(a.read_bytes(), b"hello")
        self.assertEqual(b.read_bytes(), b"world")  # подпапка восстановлена
        self.assertEqual(self.store.sessions(), [])  # пустая сессия удалена

    def test_restore_never_overwrites(self):
        a = self.make_file("Local/App/Cache/a.bin", content=b"old")
        self.clean_into_quarantine()
        a.write_bytes(b"NEW FILE")  # на том же месте появился новый файл
        session = self.store.sessions()[0]
        rep = self.store.restore(session)
        self.assertEqual(rep.restored, 0)
        self.assertEqual(rep.failed, [(str(a), q.Q_CONFLICT)])
        self.assertEqual(a.read_bytes(), b"NEW FILE")
        self.assertEqual(len(self.store.sessions()), 1)  # конфликтный файл остался в карантине

    def test_tampered_manifest_rejected(self):
        self.make_file("Local/App/Cache/a.bin", 10)
        self.clean_into_quarantine()
        session = self.store.sessions()[0]
        manifest = session.path / q.MANIFEST
        data = json.loads(manifest.read_text(encoding="utf-8"))
        outside = self.make_file("Other/secret.txt", content=b"secret")
        data["entries"].append({"original": str(self.tmp / "x.txt"), "stored": "../../Other/secret.txt",
                                "size": 6, "mtime": 0})
        data["entries"].append({"original": str(self.tmp) + os.sep + ".." + os.sep + "evil.txt",
                                "stored": "0000001", "size": 1, "mtime": 0})
        manifest.write_text(json.dumps(data), encoding="utf-8")
        rep = self.store.restore(self.store.sessions()[0])
        self.assertTrue(outside.exists())
        self.assertFalse((self.tmp / "x.txt").exists())
        reasons = sorted(r for _, r in rep.failed)
        self.assertEqual(reasons, [q.Q_BAD_ENTRY, q.Q_BAD_ENTRY])
        self.assertEqual(rep.restored, 1)

    def test_locked_file_not_moved_reports_failure(self):
        self.make_file("Local/App/Cache/a.bin", 10)
        result = self.scan()
        writer = self.store.new_session(["app"])
        with mock.patch.object(q.os, "replace", side_effect=PermissionError(13, "denied")):
            report = Cleaner(self.policy, recycle_api=FakeRecycle()).clean_junk(result, ["app"], quarantine=writer)
        self.assertEqual(report.quarantined_files, 0)
        self.assertEqual(report.failed_count, 1)
        self.assertTrue((self.local / "App" / "Cache" / "a.bin").exists())

    def test_other_drive_falls_back_to_delete(self):
        a = self.make_file("Local/App/Cache/a.bin", 10)
        result = self.scan()
        writer = self.store.new_session(["app"])
        with mock.patch.object(writer, "same_volume", return_value=False):
            report = Cleaner(self.policy, recycle_api=FakeRecycle()).clean_junk(result, ["app"], quarantine=writer)
        self.assertFalse(a.exists())
        self.assertEqual(report.quarantined_files, 0)
        self.assertEqual(report.freed_bytes, 10)
        self.assertEqual(writer.other_drive, 1)


class TestExpiryAndDelete(QuarantineTestBase):
    def test_purge_expired(self):
        self.make_file("Local/App/Cache/a.bin", 100)
        self.clean_into_quarantine()
        self.assertEqual(self.store.purge_expired(7), 0)  # ещё свежая
        freed = self.store.purge_expired(7, now=time.time() + 8 * 86400)
        self.assertEqual(freed, 100)
        self.assertEqual(self.store.sessions(), [])

    def test_delete_session(self):
        self.make_file("Local/App/Cache/a.bin", 100)
        self.clean_into_quarantine()
        s = self.store.sessions()[0]
        self.assertEqual(self.store.delete(s), 100)
        self.assertFalse(s.path.exists())

    def test_delete_refuses_outside_root(self):
        victim = self.make_dir("Important")
        self.make_file("Important/data.txt", 5)
        fake = q.QSession(id="x", created=0, path=victim)
        self.assertEqual(self.store.delete(fake), 0)
        self.assertTrue((victim / "data.txt").exists())

    def test_quarantine_dir_invisible_to_scanner(self):
        self.make_file("Q/session/files/0000001", 10)
        policy = SafetyPolicy([str(self.tmp / "Q")], [], broad_roots_allowed=True)
        from app.core.disk_analyzer import LargeFileFinder

        res = LargeFileFinder(policy).find(str(self.tmp), min_size=1)
        self.assertFalse(any("Q" + os.sep in i.path for i in res.items))

    def test_real_policies_protect_quarantine(self):
        from app.utils.paths import quarantine_dir
        from app.utils.winpaths import real_normalized

        qd = real_normalized(str(quarantine_dir() / "s" / "files" / "1"))
        self.assertTrue(SafetyPolicy.for_junk().location_forbidden(qd))
        self.assertTrue(SafetyPolicy.for_user_files().location_forbidden(qd))
