"""Тесты модуля безопасности — самого критичного компонента."""

from __future__ import annotations

import os
import unittest

from app.core import safety
from app.core.safety import ResolvedRule, SafetyPolicy, has_traversal, validate_raw_path
from app.models.file_item import FileItem
from app.utils.winpaths import is_within, normalize, real_normalized
from tests.helpers import TempDirTestCase


def rule_for(root, patterns=("*",), recursive=True, cat="test") -> ResolvedRule:
    return ResolvedRule(cat, real_normalized(str(root)), tuple(patterns), recursive, 0.0, str(root))


def item_for(path) -> FileItem:
    st = os.stat(path)
    return FileItem(path=str(path), size=st.st_size, mtime=st.st_mtime, category_id="test")


class TestPathHelpers(unittest.TestCase):
    def test_is_within_basic(self):
        base = normalize(os.path.join(os.sep, "data", "cache"))
        self.assertTrue(is_within(normalize(os.path.join(base, "a", "b.txt")), base))
        self.assertTrue(is_within(base, base))
        self.assertFalse(is_within(base, base, strict=True))

    def test_is_within_prefix_trick(self):
        # "cache2" начинается с "cache", но не вложена в неё
        base = normalize(os.path.join(os.sep, "data", "cache"))
        sibling = normalize(os.path.join(os.sep, "data", "cache2", "x.txt"))
        self.assertFalse(is_within(sibling, base))

    def test_is_within_empty(self):
        self.assertFalse(is_within("", normalize(os.sep)))
        self.assertFalse(is_within(normalize(os.sep), ""))

    def test_traversal_detection(self):
        self.assertTrue(has_traversal("C:\\Temp\\..\\Windows\\x.dll"))
        self.assertTrue(has_traversal("/tmp/../etc/passwd"))
        self.assertFalse(has_traversal("C:\\Temp\\..foo\\file.txt"))
        self.assertFalse(has_traversal("/tmp/a..b/c"))

    def test_validate_raw_path(self):
        self.assertEqual(validate_raw_path(""), safety.R_INVALID_PATH)
        self.assertEqual(validate_raw_path(None), safety.R_INVALID_PATH)
        self.assertEqual(validate_raw_path("relative/file.txt"), safety.R_INVALID_PATH)
        self.assertEqual(validate_raw_path(os.path.abspath("x") + "\x00.txt"), safety.R_INVALID_PATH)
        bad = os.path.join(os.path.abspath(os.sep), "tmp", "..", "etc", "passwd")
        self.assertEqual(validate_raw_path(bad), safety.R_TRAVERSAL)
        self.assertIsNone(validate_raw_path(os.path.abspath("file.txt")))


class TestResolvedRule(TempDirTestCase):
    def test_recursive_rule(self):
        root = self.make_dir("cache")
        r = rule_for(root)
        self.assertTrue(r.matches(normalize(str(root / "a" / "b.bin"))))
        self.assertFalse(r.matches(normalize(str(root))))  # сам корень — нельзя
        self.assertFalse(r.matches(normalize(str(self.tmp / "cache2" / "b.bin"))))

    def test_non_recursive_rule(self):
        root = self.make_dir("explorer")
        r = rule_for(root, patterns=("thumbcache_*.db",), recursive=False)
        self.assertTrue(r.matches(normalize(str(root / "thumbcache_256.db"))))
        self.assertFalse(r.matches(normalize(str(root / "other.db"))))
        self.assertFalse(r.matches(normalize(str(root / "sub" / "thumbcache_256.db"))))

    def test_patterns(self):
        root = self.make_dir("logs")
        r = rule_for(root, patterns=("*.log", "*.etl"))
        self.assertTrue(r.matches(normalize(str(root / "x" / "a.log"))))
        self.assertFalse(r.matches(normalize(str(root / "a.txt"))))


class TestRuleValidation(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_dir("Users/me")
        self.local = self.make_dir("Users/me/AppData/Local")
        self.docs = self.make_dir("Users/me/Documents")
        self.policy = SafetyPolicy(
            forbidden_roots=[str(self.docs)],
            container_roots=[str(self.tmp), str(self.profile), str(self.local)],
        )

    def test_valid_cache_root(self):
        root = self.make_dir("Users/me/AppData/Local/App/Cache")
        self.assertIsNone(self.policy.validate_rule_root(str(root)))

    def test_root_equal_to_container_rejected(self):
        self.assertEqual(self.policy.validate_rule_root(str(self.local)), safety.R_BAD_RULE)
        self.assertEqual(self.policy.validate_rule_root(str(self.profile)), safety.R_BAD_RULE)

    def test_root_containing_container_rejected(self):
        # Корень правила — папка Users, содержащая профиль целиком
        self.assertEqual(
            self.policy.validate_rule_root(str(self.tmp / "Users")), safety.R_BAD_RULE
        )

    def test_root_inside_forbidden_rejected(self):
        sub = self.make_dir("Users/me/Documents/cache")
        self.assertEqual(self.policy.validate_rule_root(str(sub)), safety.R_PROTECTED_LOCATION)

    def test_filesystem_root_rejected(self):
        self.assertIsNotNone(self.policy.validate_rule_root(os.path.abspath(os.sep)))

    def test_traversal_root_rejected(self):
        bad = str(self.local) + os.sep + ".." + os.sep + "Documents"
        self.assertEqual(self.policy.validate_rule_root(bad), safety.R_TRAVERSAL)

    def test_non_recursive_specific_mask_on_container_allowed(self):
        self.assertIsNone(
            self.policy.validate_rule_root(str(self.local), recursive=False, patterns=("MEMORY.DMP",))
        )

    def test_non_recursive_wildcard_on_container_rejected(self):
        self.assertEqual(
            self.policy.validate_rule_root(str(self.local), recursive=False, patterns=("*",)),
            safety.R_BAD_RULE,
        )

    def test_broad_roots_for_user_files(self):
        policy = SafetyPolicy([str(self.docs)], [str(self.tmp)], broad_roots_allowed=True)
        self.assertIsNone(policy.validate_rule_root(str(self.tmp)))
        self.assertEqual(policy.validate_rule_root(str(self.docs)), safety.R_PROTECTED_LOCATION)


class TestCheckBeforeDelete(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_dir("cache")
        self.protected = self.make_dir("cache/keep_me")
        self.outside = self.make_dir("important")
        self.policy = SafetyPolicy(forbidden_roots=[str(self.protected)], container_roots=[str(self.tmp)])
        self.rules = [rule_for(self.root)]

    def test_ok(self):
        f = self.make_file("cache/a/b.tmp")
        self.assertIsNone(self.policy.check_before_delete(item_for(f), self.rules))

    def test_outside_root(self):
        f = self.make_file("important/doc.txt")
        self.assertEqual(self.policy.check_before_delete(item_for(f), self.rules), safety.R_OUTSIDE)

    def test_sibling_prefix_dir(self):
        f = self.make_file("cache2/x.tmp")
        self.assertEqual(self.policy.check_before_delete(item_for(f), self.rules), safety.R_OUTSIDE)

    def test_traversal_in_item_path(self):
        f = self.make_file("important/doc.txt")
        st = os.stat(f)
        evil = FileItem(
            path=str(self.root) + os.sep + ".." + os.sep + "important" + os.sep + "doc.txt",
            size=st.st_size,
            mtime=st.st_mtime,
        )
        self.assertEqual(self.policy.check_before_delete(evil, self.rules), safety.R_TRAVERSAL)
        self.assertTrue(f.exists())

    def test_relative_path_rejected(self):
        item = FileItem(path="cache/a.tmp", size=1, mtime=0)
        self.assertEqual(self.policy.check_before_delete(item, self.rules), safety.R_INVALID_PATH)

    def test_protected_subfolder(self):
        f = self.make_file("cache/keep_me/x.bin")
        self.assertEqual(
            self.policy.check_before_delete(item_for(f), self.rules), safety.R_PROTECTED_LOCATION
        )

    def test_directory_rejected(self):
        d = self.make_dir("cache/subdir")
        item = FileItem(path=str(d), size=0, mtime=os.stat(d).st_mtime)
        self.assertEqual(self.policy.check_before_delete(item, self.rules), safety.R_DIRECTORY)

    def test_missing_file(self):
        item = FileItem(path=str(self.root / "ghost.tmp"), size=1, mtime=0)
        self.assertEqual(self.policy.check_before_delete(item, self.rules), safety.R_NOT_FOUND)

    def test_changed_since_scan(self):
        f = self.make_file("cache/a.tmp", size=10)
        item = item_for(f)
        f.write_bytes(b"x" * 50)
        self.assertEqual(self.policy.check_before_delete(item, self.rules), safety.R_CHANGED)

    def test_denied_names(self):
        for name in ("pagefile.sys", "NTUSER.DAT", "desktop.ini", "driver.sys"):
            f = self.make_file(f"cache/{name}")
            self.assertEqual(
                self.policy.check_before_delete(item_for(f), self.rules),
                safety.R_PROTECTED_NAME,
                name,
            )

    def test_virtual_item(self):
        f = self.make_file("cache/a.tmp")
        item = item_for(f)
        item.virtual = True
        self.assertEqual(self.policy.check_before_delete(item, self.rules), safety.R_VIRTUAL)

    def test_no_rules_means_nothing_allowed(self):
        f = self.make_file("cache/a.tmp")
        self.assertEqual(self.policy.check_before_delete(item_for(f), []), safety.R_OUTSIDE)

    def test_symlink_file_pointing_outside(self):
        target = self.make_file("important/secret.txt")
        link = self.root / "link.txt"
        if not self.try_symlink(target, link):
            self.skipTest("symlink не поддерживается")
        st = os.lstat(link)
        item = FileItem(path=str(link), size=st.st_size, mtime=st.st_mtime)
        self.assertIn(
            self.policy.check_before_delete(item, self.rules),
            (safety.R_REDIRECTED, safety.R_REPARSE),
        )

    def test_file_inside_symlinked_dir(self):
        self.make_file("important/data.bin")
        link_dir = self.root / "linked"
        if not self.try_symlink(self.outside, link_dir, is_dir=True):
            self.skipTest("symlink не поддерживается")
        path = link_dir / "data.bin"
        st = os.stat(path)
        item = FileItem(path=str(path), size=st.st_size, mtime=st.st_mtime)
        self.assertEqual(self.policy.check_before_delete(item, self.rules), safety.R_REDIRECTED)


class TestSystemPolicies(unittest.TestCase):
    """Фабрики политик не должны падать и должны защищать пользовательские папки."""

    def test_for_junk_protects_user_folders(self):
        from app.utils import winpaths

        policy = SafetyPolicy.for_junk()
        for name, path in winpaths.user_data_folders().items():
            if path:
                self.assertTrue(policy.location_forbidden(real_normalized(path)), name)

    def test_for_user_files_builds(self):
        policy = SafetyPolicy.for_user_files()
        self.assertTrue(policy.broad_roots_allowed)


if __name__ == "__main__":
    unittest.main()
