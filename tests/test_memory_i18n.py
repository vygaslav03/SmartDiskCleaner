"""Все динамические ключи раздела «Память» переведены на три языка, и форматирование не падает."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from app.memory import startup
from app.memory.models import ActionKind, Severity
from app.utils import i18n_memory

ROOT = Path(__file__).resolve().parents[1]


def _codes(pattern: str, file: str) -> set[str]:
    return set(re.findall(pattern, (ROOT / file).read_text(encoding="utf-8")))


class MemoryI18nTests(unittest.TestCase):
    def setUp(self):
        self.langs = {"ru": i18n_memory.RU, "uk": i18n_memory.UK, "en": i18n_memory.EN}
        finding = _codes(r'Finding\(\s*"([a-z_]+)"', "app/memory/analysis.py") | {"no_action_needed"}
        finding |= _codes(r'code = "([a-z_]+)" if', "app/memory/analysis.py")
        finding |= _codes(r'else "([a-z_]+)"\n', "app/memory/analysis.py")
        recs = _codes(r'Recommendation\(ActionKind\.[A-Z_]+, "([a-z_]+)"', "app/memory/analysis.py") | {"manual"}
        refused = _codes(r'ActionRefused\("([a-z_]+)"\)', "app/memory/actions.py")
        kinds = {v for k, v in vars(ActionKind).items() if k.isupper()}
        self.required = (
            {f"mem.f.{c}" for c in finding}
            | {f"mem.rec.{c}.{p}" for c in recs for p in ("title", "body")}
            | {f"mem.refused.{c}" for c in refused}
            | {f"mem.action.{k}" for k in kinds}
            | {f"mem.rec.effect.{k}" for k in kinds - {ActionKind.STARTUP_ENABLE}}
            | {f"mem.st.loc.{loc}" for loc in startup.LOCATIONS}
            | {f"mem.status.{int(s)}" for s in Severity}
        )
        self.assertGreater(len(finding), 8)
        self.assertGreater(len(recs), 8)

    def test_all_required_keys_present(self):
        for lang, table in self.langs.items():
            missing = sorted(k for k in self.required if k not in table)
            self.assertEqual(missing, [], f"{lang}: нет переводов")

    def test_same_keys_and_placeholders(self):
        ru = self.langs["ru"]
        for lang in ("uk", "en"):
            table = self.langs[lang]
            self.assertEqual(set(ru), set(table), lang)
            for key, text in ru.items():
                self.assertEqual(set(re.findall(r"{(\w+)}", text)), set(re.findall(r"{(\w+)}", table[key])),
                                 f"{lang}:{key}")

    def test_format_with_typical_params(self):
        params = dict(name="app.exe", pct="90", avail_pct="5", rate="1 GB", growth="1 GB", minutes="20", size="1 GB",
                      standby="1 GB", free="1 MB", peak="1 GB", limit="2 GB", when="10:00", count=2, sec=30, m=30,
                      v="1.0", now="1", mbs="1", median="1", ws="1", private="1", sys="1", proc="1", sys_i="2",
                      proc_i="5", proc_bg="15", delta="1", pid=1, r2="0.9", target="x", value="1")
        for table in self.langs.values():
            for key, text in table.items():
                text.format(**params)  # не должно бросать исключение


if __name__ == "__main__":
    unittest.main()
