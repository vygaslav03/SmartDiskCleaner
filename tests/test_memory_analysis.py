"""Анализ памяти: статус системы, утечки, скачки, рекомендации — на синтетических данных."""

from __future__ import annotations

import math
import random
import unittest

from app.memory import analysis
from app.memory.analysis import Thresholds, assess_system, build_recommendations, detect_leaks, leak_score
from app.memory.history import SystemPoint
from app.memory.models import (
    GB,
    MB,
    ActionKind,
    Assessment,
    LeakSuspect,
    PagefileConfig,
    ProcessMemory,
    Severity,
    SystemMemory,
)
from app.memory.startup import StartupItem


def snap(total=16 * GB, available=8 * GB, commit=8 * GB, limit=20 * GB, standby=None, free=None, modified=None,
         peak=None, pf=None) -> SystemMemory:
    return SystemMemory(timestamp=0.0, total=total, available=available, load_percent=0, commit_total=commit,
                        commit_limit=limit, commit_peak=peak, standby=standby, free_zero=free, modified=modified,
                        pagefile_usage_percent=pf, source="test")


def points(n=20, hard_faults=0.0, commit=8 * GB, step=2.0) -> list[SystemPoint]:
    return [SystemPoint(i * step, 50, 40, 8 * GB, commit, hard_faults) for i in range(n)]


def proc(pid, name, private, ws=None, owned=True, window=False, protected=False, ctime=1000.0) -> ProcessMemory:
    return ProcessMemory(pid=pid, name=name, create_time=ctime, working_set=ws if ws is not None else private,
                         private_bytes=private, owned=owned, has_window=window, protected=protected)


class AssessTests(unittest.TestCase):
    def codes(self, a: Assessment) -> list[str]:
        return [f.code for f in a.findings]

    def test_healthy_system_says_no_action_needed(self):
        a = assess_system(snap(), points(hard_faults=10))
        self.assertEqual(a.severity, Severity.OK)
        self.assertIn("no_action_needed", self.codes(a))
        self.assertFalse(a.needs_action)

    def test_high_usage_with_big_cache_is_not_a_problem(self):
        # «занято» 90%, но 6 ГБ из 16 — standby-кэш и подкачки нет
        s = snap(available=7 * GB, standby=6 * GB, free=1 * GB, modified=200 * MB)
        a = assess_system(s, points(hard_faults=5))
        self.assertEqual(a.severity, Severity.OK)
        self.assertIn("cache_large", self.codes(a))

    def test_low_available_without_paging_is_only_watch(self):
        a = assess_system(snap(available=1 * GB), points(hard_faults=20))
        self.assertEqual(a.severity, Severity.WATCH)
        self.assertIn("low_available_ok", self.codes(a))

    def test_low_available_with_sustained_paging_is_warning(self):
        a = assess_system(snap(available=1200 * MB), points(hard_faults=8000))  # ~31 МБ/с
        self.assertEqual(a.severity, Severity.WARNING)
        self.assertIn("paging_pressure", self.codes(a))

    def test_very_low_available_with_paging_is_critical(self):
        a = assess_system(snap(available=500 * MB), points(hard_faults=8000))
        self.assertEqual(a.severity, Severity.CRITICAL)

    def test_single_fault_burst_is_ignored(self):
        pts = points(hard_faults=10)
        pts[-1] = SystemPoint(pts[-1].t, 50, 40, 1, 1, 50000)  # запуск программы
        a = assess_system(snap(available=1 * GB), pts)
        self.assertNotIn("paging_pressure", self.codes(a))

    def test_paging_with_plenty_of_memory_is_info(self):
        a = assess_system(snap(), points(hard_faults=9000))
        self.assertEqual(a.severity, Severity.OK)
        self.assertIn("paging_not_memory", self.codes(a))

    def test_commit_thresholds(self):
        self.assertEqual(assess_system(snap(commit=18 * GB), points()).severity, Severity.WARNING)
        self.assertEqual(assess_system(snap(commit=19.5 * GB), points()).severity, Severity.CRITICAL)
        self.assertEqual(assess_system(snap(commit=16 * GB), points()).severity, Severity.OK)

    def test_unknown_faults_reported(self):
        a = assess_system(snap(), [])
        self.assertIn("no_fault_data", self.codes(a))
        self.assertIn("low_available", self.codes(assess_system(snap(available=1 * GB), [])))

    def test_nan_faults_are_ignored(self):
        self.assertIsNone(analysis.sustained_hard_faults(points(hard_faults=math.nan), 30))


class LeakTests(unittest.TestCase):
    cfg = Thresholds()

    def series(self, minutes=20, start=500 * MB, per_min=5 * MB, noise=0.0, step=5.0, seed=1):
        rnd = random.Random(seed)
        n = int(minutes * 60 / step)
        ts = [i * step for i in range(n)]
        vs = [start + per_min * (t / 60) + rnd.uniform(-noise, noise) for t in ts]
        return ts, vs

    def test_steady_growth_is_leak(self):
        ts, vs = self.series(per_min=5 * MB, noise=2 * MB)  # 300 МБ/ч
        res = leak_score(ts, vs, self.cfg)
        self.assertIsNotNone(res)
        per_hour, r2, growth, window = res
        self.assertAlmostEqual(per_hour / MB, 300, delta=15)
        self.assertGreater(r2, 0.95)

    def test_flat_usage_is_not_leak(self):
        self.assertIsNone(leak_score(*self.series(per_min=0, noise=20 * MB), self.cfg))

    def test_short_window_is_not_enough(self):
        self.assertIsNone(leak_score(*self.series(minutes=5, per_min=20 * MB), self.cfg))

    def test_sawtooth_cache_is_not_leak(self):
        # браузер: растёт и периодически сбрасывает — R² и монотонность низкие
        ts = [i * 5.0 for i in range(240)]
        vs = [800 * MB + (i % 24) * 30 * MB for i in range(240)]
        self.assertIsNone(leak_score(ts, vs, self.cfg))

    def test_slow_growth_relative_to_size_is_ignored(self):
        # 4 ГБ процесс +60 МБ/ч — меньше 5%/ч, не утечка
        self.assertIsNone(leak_score(*self.series(start=4 * GB, per_min=1 * MB, minutes=30), self.cfg))

    def test_detect_leaks_sorted(self):
        a = self.series(per_min=4 * MB)  # 80 МБ за 20 мин — выше порога роста 64 МБ
        b = self.series(per_min=8 * MB)
        leaks = detect_leaks([((1, 1.0), "a.exe", *a), ((2, 2.0), "b.exe", *b)], self.cfg)
        self.assertEqual([x.name for x in leaks], ["b.exe", "a.exe"])

    def test_linear_fit_degenerate(self):
        self.assertEqual(analysis.linear_fit([1, 1, 1], [1, 2, 3]), (0.0, 0.0))
        self.assertEqual(analysis.linear_fit([1], [1]), (0.0, 0.0))


class SpikeTests(unittest.TestCase):
    def test_system_spike(self):
        pts = points(n=16, commit=8 * GB)
        pts.append(SystemPoint(40, 50, 40, 1, 10 * GB, 0))
        ev = analysis.detect_system_spike(pts, 16 * GB)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.delta, 2 * GB)

    def test_small_change_is_not_spike(self):
        pts = points(n=16, commit=8 * GB)
        pts.append(SystemPoint(40, 50, 40, 1, 8 * GB + 100 * MB, 0))
        self.assertIsNone(analysis.detect_system_spike(pts, 16 * GB))

    def test_process_spike(self):
        prev = {(1, 1000.0): 200 * MB, (2, 1000.0): 2 * GB}
        cur = [proc(1, "game.exe", 900 * MB), proc(2, "big.exe", 2 * GB + 200 * MB), proc(3, "new.exe", 5 * GB)]
        events = analysis.detect_process_spikes(prev, cur, 0.0)
        self.assertEqual([e.name for e in events], ["game.exe"])


class RecommendationTests(unittest.TestCase):
    ok = Assessment(Severity.OK, [])
    pressure = Assessment(Severity.WARNING, [])

    def test_nothing_to_do_on_healthy_system(self):
        procs = [proc(10, "chrome.exe", 2 * GB, window=True), proc(11, "helper.exe", 500 * MB)]
        self.assertEqual(build_recommendations(snap(), self.ok, procs), [])

    def test_pressure_suggests_heavy_user_apps_only(self):
        procs = [
            proc(10, "chrome.exe", 2 * GB, window=True),
            proc(11, "updater.exe", 500 * MB),
            proc(12, "svchost.exe", 3 * GB, owned=False, protected=True),
            proc(13, "small.exe", 50 * MB),
        ]
        recs = build_recommendations(snap(), self.pressure, procs, idle_keys={(11, 1000.0)})
        targets = {(r.kind, r.target_name) for r in recs}
        self.assertIn((ActionKind.CLOSE_APP, "chrome.exe"), targets)
        self.assertIn((ActionKind.CLOSE_APP, "updater.exe"), targets)
        self.assertIn((ActionKind.TRIM, "updater.exe"), targets)
        self.assertNotIn("svchost.exe", {r.target_name for r in recs})
        self.assertNotIn("small.exe", {r.target_name for r in recs})

    def test_leak_of_protected_process_is_info_only(self):
        leak = LeakSuspect(12, "svchost.exe", 1000.0, 400 * MB, 0.99, 200 * MB, 1200, 1 * GB)
        procs = [proc(12, "svchost.exe", 1 * GB, owned=False, protected=True)]
        recs = build_recommendations(snap(), self.ok, procs, leaks=[leak])
        self.assertEqual(len(recs), 1)
        self.assertFalse(recs[0].executable)
        self.assertEqual(recs[0].code, "leak_system")

    def test_leak_of_user_app_is_actionable(self):
        leak = LeakSuspect(20, "app.exe", 1000.0, 400 * MB, 0.99, 200 * MB, 1200, 1 * GB)
        recs = build_recommendations(snap(), self.ok, [proc(20, "app.exe", 1 * GB)], leaks=[leak])
        self.assertEqual(recs[0].code, "leak")
        self.assertTrue(recs[0].executable)

    def test_pagefile_disabled_is_advice_only(self):
        cfg = PagefileConfig(known=True, disabled=True, system_managed=False)
        recs = build_recommendations(snap(), self.ok, [], pagefile=cfg)
        self.assertEqual([(r.code, r.executable) for r in recs], [("pagefile_disabled", False)])

    def test_pagefile_small_fixed(self):
        cfg = PagefileConfig(known=True, disabled=False, system_managed=False, configured=["C:\\pagefile.sys 2048 2048"])
        recs = build_recommendations(snap(peak=19 * GB), self.ok, [], pagefile=cfg)
        self.assertEqual(recs[0].code, "pagefile_small")
        cfg.system_managed = True
        self.assertEqual(build_recommendations(snap(peak=19 * GB), self.ok, [], pagefile=cfg), [])

    def test_heavy_startup(self):
        items = [StartupItem("hkcu_run:Discord", "Discord", "x", "hkcu_run", True, "Discord.exe"),
                 StartupItem("hkcu_run:Off", "Off", "x", "hkcu_run", False, "off.exe"),
                 StartupItem("hklm_run:Tiny", "Tiny", "x", "hklm_run", True, "tiny.exe")]
        procs = [proc(1, "discord.exe", 120 * MB), proc(2, "Discord.exe", 100 * MB, ctime=2.0),
                 proc(3, "off.exe", 900 * MB), proc(4, "tiny.exe", 10 * MB)]
        recs = build_recommendations(snap(), self.ok, procs, startup_items=items)
        self.assertEqual([(r.kind, r.target_name, r.reversible) for r in recs],
                         [(ActionKind.STARTUP_DISABLE, "Discord", True)])

    def test_standby_purge_requires_objective_reason(self):
        normal = snap(standby=6 * GB, free=2 * GB, modified=100 * MB)
        self.assertEqual(build_recommendations(normal, self.ok, []), [])
        starved = snap(standby=8 * GB, free=20 * MB, modified=100 * MB)
        recs = build_recommendations(starved, self.ok, [])
        self.assertEqual([r.kind for r in recs], [ActionKind.PURGE_STANDBY])
        self.assertTrue(recs[0].needs_admin)


class MiscTests(unittest.TestCase):
    def test_compare(self):
        d = {x.key: x for x in analysis.compare(snap(available=4 * GB), snap(available=5 * GB), 100, 40)}
        self.assertEqual(d["available"].change, 1 * GB)
        self.assertEqual(d["hard_faults"].change, -60)
        self.assertIsNone(d["in_use"].change)

    def test_idle(self):
        p_idle = proc(1, "a", 1, ctime=1.0)
        p_idle.cpu_time = 10.001
        p_busy = proc(2, "b", 1, ctime=1.0)
        p_busy.cpu_time = 15.0
        idle = analysis.idle_processes({(1, 1.0): 10.0, (2, 1.0): 10.0}, [p_idle, p_busy], 5.0)
        self.assertEqual(idle, {(1, 1.0)})

    def test_in_use_breakdown(self):
        s = snap(total=16 * GB, available=10 * GB, standby=8 * GB, free=2 * GB, modified=1 * GB)
        self.assertEqual(s.in_use, 5 * GB)
        self.assertEqual(s.used, 6 * GB)
        self.assertIsNone(snap().in_use)


if __name__ == "__main__":
    unittest.main()
