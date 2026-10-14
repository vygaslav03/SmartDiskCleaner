"""Сбор, история, автозагрузка, журнал, действия и монитор памяти."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from app.memory import actions, collector, startup
from app.memory.history import ProcessHistory, Ring, SystemHistory
from app.memory.journal import ActionJournal, new_entry
from app.memory.models import GB, MB, ActionKind, ProcessMemory, SystemMemory
from app.memory.monitor import MemoryMonitor
from tests.helpers import TempDirTestCase


class FakeRegistry:
    def __init__(self, data=None):
        self.data: dict[tuple[str, str], dict[str, object]] = data or {}

    def values(self, hive, path):
        if (hive, path) not in self.data:
            raise FileNotFoundError(path)
        return dict(self.data[(hive, path)])

    def get(self, hive, path, name):
        return self.data.get((hive, path), {}).get(name)

    def set_binary(self, hive, path, name, data):
        self.data.setdefault((hive, path), {})[name] = bytes(data)

    def delete(self, hive, path, name):
        self.data.get((hive, path), {}).pop(name, None)


APPROVED_RUN = (startup.HKCU, startup.APPROVED + "\\Run")


class RingTests(unittest.TestCase):
    def test_wraps(self):
        r = Ring(3)
        for v in range(5):
            r.append(v)
        self.assertEqual(r.values(), [2, 3, 4])
        self.assertEqual(r.last(), 4)
        self.assertEqual(len(r), 3)

    def test_system_history(self):
        h = SystemHistory(capacity=10)
        for i in range(15):
            h.add(SystemMemory(float(i), 16 * GB, 8 * GB, 50, 4 * GB, 16 * GB))
        pts = h.points()
        self.assertEqual(len(pts), 10)
        self.assertEqual(pts[0].t, 5.0)
        self.assertAlmostEqual(pts[-1].in_use_pct, 50.0)
        self.assertAlmostEqual(pts[-1].commit_pct, 25.0)

    def test_process_history_drops_exited_and_limits(self):
        h = ProcessHistory(capacity=5, max_tracked=2)
        a = ProcessMemory(1, "a", 1.0, 100, 100)
        b = ProcessMemory(2, "b", 1.0, 300, 300)
        c = ProcessMemory(3, "c", 1.0, 200, 200)
        h.add(1.0, [a, b, c])
        self.assertEqual({k for k, *_ in h.items()}, {(2, 1.0), (3, 1.0)})
        h.add(2.0, [a, c])  # b завершился
        self.assertEqual({k for k, *_ in h.items()}, {(1, 1.0), (3, 1.0)})
        self.assertEqual(h.series_for((3, 1.0))[1], [200.0, 200.0])


class CollectorTests(unittest.TestCase):
    def test_snapshot_invariants(self):
        s = collector.SystemCollector().snapshot()
        self.assertGreater(s.total, 0)
        self.assertGreater(s.available, 0)
        self.assertLessEqual(s.available, s.total)
        self.assertGreater(s.commit_limit, 0)
        self.assertLessEqual(s.commit_total, s.commit_limit * 1.01)
        self.assertTrue(0 <= s.load_percent <= 100)

    @unittest.skipUnless(sys.platform == "win32", "только Windows")
    def test_windows_sources(self):
        c = collector.SystemCollector()
        time.sleep(1.1)
        s = c.snapshot()
        c.close()
        self.assertEqual(s.source, "winapi")
        self.assertIsNotNone(s.commit_peak)
        self.assertGreaterEqual(s.commit_peak, s.commit_total)
        if s.has_lists:
            # Available = Standby + Free&Zero (с допуском на разницу во времени выборки)
            self.assertAlmostEqual((s.standby + s.free_zero) / s.available, 1.0, delta=0.05)
            self.assertIsNotNone(s.hard_faults_per_sec)

    def test_process_scan_contains_self(self):
        procs = collector.ProcessCollector(services_provider=dict).scan()
        me = [p for p in procs if p.pid == os.getpid()]
        self.assertEqual(len(me), 1)
        self.assertGreater(me[0].working_set, 0)
        self.assertTrue(me[0].protected, "своя программа защищена от действий")
        if sys.platform == "win32":
            self.assertIsNotNone(me[0].private_bytes)

    def test_critical_names(self):
        self.assertTrue(collector.is_critical_name("LSASS.EXE"))
        self.assertTrue(collector.is_critical_name("explorer.exe"))
        self.assertFalse(collector.is_critical_name("chrome.exe"))

    def test_pagefile_config(self):
        def cfg(lines):
            return collector.read_pagefile_config(lambda: lines, lambda: [])
        self.assertTrue(cfg(["?:\\pagefile.sys"]).system_managed)
        self.assertTrue(cfg(["C:\\pagefile.sys 0 0"]).system_managed)
        c = cfg(["C:\\pagefile.sys 4096 4096"])
        self.assertFalse(c.system_managed)
        self.assertFalse(c.disabled)
        self.assertTrue(cfg([""]).disabled)
        self.assertFalse(collector.read_pagefile_config(lambda: None, lambda: None).known)

    def test_own_memory(self):
        ws, _private = collector.own_process_memory()
        self.assertGreater(ws, 1 * MB)


class StartupTests(TempDirTestCase):
    def reg(self):
        return FakeRegistry({
            (startup.HKCU, startup.RUN): {"Discord": '"C:\\Users\\u\\AppData\\Local\\Discord\\Update.exe" --start',
                                         "Steam": "C:\\Program Files\\Steam\\steam.exe -silent"},
            (startup.HKLM, startup.RUN): {"SecurityHealth": "%windir%\\system32\\SecurityHealthSystray.exe"},
            APPROVED_RUN: {"Steam": bytes([3, 0, 0, 0]) + bytes(8)},
        })

    def test_list_and_flags(self):
        folder = self.make_dir("Startup")
        self.make_file("Startup/Tool.lnk", 10)
        self.make_file("Startup/desktop.ini", 10)
        items = {i.id: i for i in startup.list_startup(self.reg(), {"user_folder": folder})}
        self.assertTrue(items["hkcu_run:Discord"].enabled)
        self.assertFalse(items["hkcu_run:Steam"].enabled)
        self.assertEqual(items["hkcu_run:Discord"].exe_name, "Update.exe")
        self.assertEqual(items["hkcu_run:Steam"].exe_name, "steam.exe")
        self.assertTrue(items["hklm_run:SecurityHealth"].needs_admin)
        self.assertIn("user_folder:Tool.lnk", items)
        self.assertNotIn("user_folder:desktop.ini", items)

    def test_disable_and_undo_restores_absent_flag(self):
        reg = self.reg()
        item = next(i for i in startup.list_startup(reg) if i.name == "Discord")
        undo = startup.set_enabled(reg, item, False)
        flag = reg.get(*APPROVED_RUN, "Discord")
        self.assertEqual(len(flag), 12)
        self.assertFalse(startup.is_enabled_flag(flag))
        self.assertIsNone(undo["previous"])
        startup.undo(reg, undo)
        self.assertIsNone(reg.get(*APPROVED_RUN, "Discord"))
        # сама запись Run не тронута
        self.assertIn("Discord", reg.values(startup.HKCU, startup.RUN))

    def test_enable_and_undo_restores_previous(self):
        reg = self.reg()
        old = reg.get(*APPROVED_RUN, "Steam")
        item = next(i for i in startup.list_startup(reg) if i.name == "Steam")
        undo = startup.set_enabled(reg, item, True)
        self.assertTrue(startup.is_enabled_flag(reg.get(*APPROVED_RUN, "Steam")))
        startup.undo(reg, undo)
        self.assertEqual(reg.get(*APPROVED_RUN, "Steam"), old)

    def test_exe_name_parsing(self):
        f = startup.exe_name_from_command
        self.assertEqual(f('"C:\\a b\\App.exe" /min'), "App.exe")
        self.assertEqual(f("C:\\Program Files\\x\\y.exe -a"), "y.exe")
        self.assertEqual(f("rundll32.exe shell32.dll,Control"), "rundll32.exe")
        self.assertEqual(f(""), "")
        self.assertEqual(f("C:\\script.bat"), "")


class JournalTests(TempDirTestCase):
    def test_roundtrip_and_undo_flag(self):
        j = ActionJournal(self.tmp / "j.json")
        e = new_entry(ActionKind.STARTUP_DISABLE, "Discord", True, undo={"type": "startup"})
        j.add(e)
        j.add(new_entry(ActionKind.TRIM, "app.exe", True))
        items = j.load()
        self.assertEqual([x.target for x in items], ["app.exe", "Discord"])
        self.assertTrue(items[1].can_undo)
        self.assertFalse(items[0].can_undo)
        j.mark_undone(e.id)
        self.assertFalse(j.load()[1].can_undo)

    def test_corrupt_file(self):
        p = self.tmp / "j.json"
        p.write_text("{broken", encoding="utf-8")
        self.assertEqual(ActionJournal(p).load(), [])
        self.assertTrue(p.with_suffix(".bad").exists())


class ActionTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.journal = ActionJournal(self.tmp / "j.json")
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        import psutil

        self.child_ctime = psutil.Process(self.child.pid).create_time()

    def tearDown(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(5)
        super().tearDown()

    def test_refuses_self_and_reused_pid(self):
        import psutil

        with self.assertRaises(actions.ActionRefused) as cm:
            actions.get_verified_process(os.getpid(), psutil.Process().create_time())
        self.assertEqual(cm.exception.args[0], "protected")
        with self.assertRaises(actions.ActionRefused) as cm:
            actions.get_verified_process(self.child.pid, self.child_ctime - 100)
        self.assertEqual(cm.exception.args[0], "pid_reused")

    def test_close_without_window_does_nothing(self):
        e = actions.close_app(self.child.pid, self.child_ctime, journal=self.journal, post_close=lambda pid: 0)
        self.assertFalse(e.ok)
        self.assertEqual(e.message, "no_window")
        self.assertIsNone(self.child.poll(), "процесс не должен быть завершён")

    def test_trim_records_before_after(self):
        called = []
        e = actions.trim_working_set(self.child.pid, self.child_ctime, journal=self.journal,
                                     empty=lambda pid: called.append(pid) or True)
        self.assertEqual(called, [self.child.pid])
        self.assertTrue(e.ok)
        self.assertIn("working_set", e.before)
        self.assertIsNone(e.undo)

    def test_terminate(self):
        e = actions.terminate(self.child.pid, self.child_ctime, journal=self.journal)
        self.assertTrue(e.ok)
        self.assertEqual(self.journal.load()[0].kind, ActionKind.TERMINATE)

    def test_purge_needs_admin(self):
        with self.assertRaises(actions.ActionRefused):
            actions.purge_standby(True, lambda: None, is_admin=False, journal=self.journal,
                                  purge=lambda _l: self.fail("не должно вызываться"))

    def test_startup_action_and_undo(self):
        reg = FakeRegistry({(startup.HKCU, startup.RUN): {"App": "app.exe"},
                            (startup.HKLM, startup.RUN): {"Sys": "sys.exe"}})
        items = {i.name: i for i in startup.list_startup(reg)}
        with self.assertRaises(actions.ActionRefused):
            actions.set_startup(items["Sys"], False, is_admin=False, reg=reg, journal=self.journal)
        e = actions.set_startup(items["App"], False, is_admin=False, reg=reg, journal=self.journal)
        self.assertTrue(e.can_undo)
        self.assertFalse(startup.is_enabled_flag(reg.get(*APPROVED_RUN, "App")))
        actions.undo_entry(e, reg=reg, journal=self.journal)
        self.assertIsNone(reg.get(*APPROVED_RUN, "App"))
        self.assertFalse(self.journal.load()[0].can_undo)
        with self.assertRaises(actions.ActionRefused):
            actions.undo_entry(new_entry(ActionKind.TRIM, "x", True), reg=reg, journal=self.journal)


class FakeSys:
    def __init__(self):
        self.commit = 4 * GB

    def snapshot(self):
        return SystemMemory(time.time(), 16 * GB, 8 * GB, 50, self.commit, 20 * GB, source="test")

    def close(self):
        pass


class FakeProcs:
    def __init__(self):
        self.calls = 0

    def scan(self):
        self.calls += 1
        return [ProcessMemory(100, "app.exe", 1.0, 200 * MB, 200 * MB * self.calls, cpu_time=1.0)]


class MonitorTests(unittest.TestCase):
    def test_tick_schedules_process_scans(self):
        procs = FakeProcs()
        m = MemoryMonitor(FakeSys(), procs, process_interval_fast=5, process_interval_slow=15)
        m.tick(now=100.0)
        self.assertEqual(procs.calls, 1)
        m.tick(now=102.0)
        self.assertEqual(procs.calls, 1)
        m.set_fast(True)
        m.tick(now=105.5)
        self.assertEqual(procs.calls, 2)
        m.request_processes()
        state = m.tick(now=106.0)
        self.assertEqual(procs.calls, 3)
        self.assertIsNotNone(state.system)
        self.assertIsNotNone(state.assessment)
        self.assertEqual(len(state.processes), 1)
        # 200 -> 400 -> 600 МБ: скачок +200 МБ меньше порога 300 МБ и 50%? 400->600 = +50% — порог
        self.assertTrue(all(e.scope == "process" for e in m.recent_spikes()))

    def test_thread_start_stop(self):
        m = MemoryMonitor(FakeSys(), FakeProcs(), system_interval=0.05)
        got = []
        m.add_listener(got.append)
        m.start()
        deadline = time.time() + 3
        while len(got) < 3 and time.time() < deadline:
            time.sleep(0.02)
        m.stop()
        self.assertGreaterEqual(len(got), 3)
        self.assertFalse(m.running)


if __name__ == "__main__":
    unittest.main()
