"""Тесты системных операций (cleanmgr/DISM) и проверки запущенных программ.

Реальные cleanmgr/DISM НЕ запускаются: используется подставной runner.
"""

from __future__ import annotations

import threading
import unittest

from app.core import system_cleanup as sc
from app.core.categories import CATEGORIES, KIND_SYSTEM, CategoryDef
from app.core.cleaner import Cleaner
from app.core.processes import running_apps
from app.core.safety import SafetyPolicy
from app.core.scanner import JunkScanner
from app.core.system_cleanup import Measure, SystemCleanup, ToolResult, parse_percent
from tests.helpers import TempDirTestCase
from tests.test_scanner import FakeRecycle


class FakeRunner:
    def __init__(self, code=0, lines=()):
        self.code = code
        self.lines = list(lines)
        self.calls: list[list[str]] = []

    def __call__(self, args, on_line, timeout):
        self.calls.append(list(args))
        for line in self.lines:
            on_line(line)
        return self.code


class Tools(SystemCleanup):
    """Без реестра и поиска exe — только логика."""

    registry_ok = True

    def _tool_path(self, name):  # type: ignore[override]
        return f"C:\\Windows\\System32\\{name}"

    def _set_sageset(self, value_name, enable):  # type: ignore[override]
        return self.registry_ok


class TestParsePercent(unittest.TestCase):
    def test_dism_progress_lines(self):
        self.assertEqual(parse_percent("[==========                 20.0%                          ]"), 20.0)
        self.assertEqual(parse_percent("[===== 34,5% ====]"), 34.5)
        self.assertEqual(parse_percent("[==========================100.0%==========================]"), 100.0)
        self.assertIsNone(parse_percent("Операция успешно завершена."))
        self.assertIsNone(parse_percent("Image Version: 10.0.22631.4317"))


class TestSystemCleanup(TempDirTestCase):
    def tools(self, runner=None, admin=True, free=None, drive=None):
        return Tools(
            runner=runner or FakeRunner(),
            is_admin=lambda: admin,
            system_drive=lambda: drive or str(self.tmp),
            free_space=free or (lambda d: 0),
            enabled=True,
        )

    def test_windows_old_absent(self):
        m = self.tools().measure("windows_old", str(self.tmp), threading.Event())
        self.assertFalse(m.available)

    def test_windows_old_measured(self):
        self.make_file("Windows.old/Windows/a.dll", 1000)
        self.make_file("Windows.old/Users/b.dat", 500)
        m = self.tools().measure("windows_old", str(self.tmp), threading.Event())
        self.assertTrue(m.available)
        self.assertEqual((m.size, m.files), (1500, 2))

    def test_component_store_size_unknown(self):
        m = self.tools().measure("component_store", str(self.tmp), threading.Event())
        self.assertTrue(m.available)
        self.assertFalse(m.size_known)

    def test_disabled_off_windows(self):
        t = SystemCleanup(enabled=False)
        self.assertFalse(t.measure("component_store", "C:\\", threading.Event()).available)
        self.assertEqual(t.run("component_store", "C:\\", lambda p: None).code, sc.T_NOTHING)

    def test_requires_admin(self):
        runner = FakeRunner()
        res = self.tools(runner=runner, admin=False).run("component_store", str(self.tmp), lambda p: None)
        self.assertEqual(res.code, sc.T_NOT_ADMIN)
        self.assertEqual(runner.calls, [])  # ничего не запускалось

    def test_dism_success_and_progress(self):
        frees = iter([1_000, 5_000])
        runner = FakeRunner(0, ["[==== 10.0% ====]", "[==== 55.5% ====]", "Операция успешно завершена."])
        seen = []
        res = self.tools(runner=runner, free=lambda d: next(frees)).run("component_store", str(self.tmp), seen.append)
        self.assertTrue(res.ok)
        self.assertEqual(res.freed, 4_000)
        self.assertEqual(seen, [10.0, 55.5])
        args = runner.calls[0]
        self.assertIn("/StartComponentCleanup", args)
        self.assertNotIn("/ResetBase", args)  # обновления должны оставаться удаляемыми

    def test_dism_reboot_required_is_success(self):
        res = self.tools(runner=FakeRunner(3010)).run("component_store", str(self.tmp), lambda p: None)
        self.assertTrue(res.ok)

    def test_dism_failure(self):
        res = self.tools(runner=FakeRunner(87, ["Error: 87"])).run("component_store", str(self.tmp), lambda p: None)
        self.assertFalse(res.ok)
        self.assertEqual(res.code, sc.T_FAILED)
        self.assertIn("87", res.details)

    def test_other_drive_does_nothing(self):
        runner = FakeRunner()
        t = Tools(runner=runner, is_admin=lambda: True, system_drive=lambda: "C:\\", enabled=True)
        if sc.winpaths.drive_of("D:\\") == sc.winpaths.drive_of("C:\\"):
            self.skipTest("на этой ОС нет букв дисков")
        self.assertEqual(t.run("component_store", "D:\\", lambda p: None).code, sc.T_NOTHING)
        self.assertEqual(runner.calls, [])

    def test_windows_old_via_cleanmgr(self):
        self.make_file("Windows.old/a.bin", 800)
        tools = self.tools()

        def runner(args, on_line, timeout):
            # имитируем cleanmgr: он удаляет Windows.old
            import shutil

            shutil.rmtree(self.tmp / "Windows.old")
            return 0

        tools._run = runner
        res = tools.run("windows_old", str(self.tmp), lambda p: None)
        self.assertTrue(res.ok)
        self.assertEqual(res.freed, 800)

    def test_windows_old_registry_failure(self):
        self.make_file("Windows.old/a.bin", 800)
        tools = self.tools()
        tools.registry_ok = False
        res = tools.run("windows_old", str(self.tmp), lambda p: None)
        self.assertFalse(res.ok)
        self.assertTrue((self.tmp / "Windows.old" / "a.bin").exists())


class FakeSystem:
    def __init__(self, measure: Measure, result: ToolResult):
        self._m = measure
        self._r = result
        self.ran: list[str] = []

    def measure(self, cat_id, drive, cancel):
        return self._m

    def run(self, cat_id, drive, progress):
        self.ran.append(cat_id)
        progress(50.0)
        return self._r


class TestIntegration(TempDirTestCase):
    def test_scan_and_clean_system_tool(self):
        cat = CategoryDef(id="component_store", group="windows", kind=KIND_SYSTEM, requires_admin=True)
        fake = FakeSystem(Measure(available=True, size_known=False), ToolResult(True, freed=1234))
        policy = SafetyPolicy([], [str(self.tmp)])
        result = JunkScanner(policy, tokens={}, categories=[cat], recycle_api=FakeRecycle(), system_api=fake).scan("C:\\")
        cres = result.get("component_store")
        self.assertFalse(cres.is_empty)  # показывается, хотя размер неизвестен
        self.assertFalse(cres.size_known)
        events = []
        report = Cleaner(policy, recycle_api=FakeRecycle(), system_api=fake).clean_junk(
            result, ["component_store"], progress_cb=events.append
        )
        self.assertEqual(fake.ran, ["component_store"])
        self.assertEqual(report.freed_bytes, 1234)
        self.assertEqual(report.tools_done, ["component_store"])
        self.assertTrue(any(e.get("tool_percent") == 50.0 for e in events))

    def test_tool_failure_reported(self):
        cat = CategoryDef(id="windows_old", group="windows", kind=KIND_SYSTEM)
        fake = FakeSystem(Measure(available=True, size=10, files=1), ToolResult(False, code=sc.T_NOT_ADMIN))
        policy = SafetyPolicy([], [str(self.tmp)])
        result = JunkScanner(policy, tokens={}, categories=[cat], recycle_api=FakeRecycle(), system_api=fake).scan("C:\\")
        report = Cleaner(policy, recycle_api=FakeRecycle(), system_api=fake).clean_junk(result, ["windows_old"])
        self.assertEqual(report.failed, [("windows_old", sc.T_NOT_ADMIN)])
        self.assertEqual(report.deleted_files, 0)

    def test_system_categories_not_in_smart_clean(self):
        for c in CATEGORIES:
            if c.kind == KIND_SYSTEM:
                self.assertFalse(c.smart, c.id)
                self.assertTrue(c.requires_admin, c.id)


class TestRunningApps(unittest.TestCase):
    def test_detects_running(self):
        names = {"chrome.exe", "explorer.exe", "discord.exe"}
        res = running_apps(["chrome", "discord", "firefox", "user_temp"], lambda: names)
        self.assertEqual(res, {"chrome": ["chrome.exe"], "discord": ["discord.exe"]})

    def test_provider_failure_is_harmless(self):
        def boom():
            raise RuntimeError("no access")

        self.assertEqual(running_apps(["chrome"], boom), {})

    def test_no_relevant_categories(self):
        self.assertEqual(running_apps(["user_temp"], lambda: {"chrome.exe"}), {})


if __name__ == "__main__":
    unittest.main()
