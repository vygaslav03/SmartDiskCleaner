import ntpath
import os
import tempfile
import threading
import unittest

from app.core.mft_reader import (
    MftAnalyzer,
    MftError,
    apply_fixups,
    decode_runs,
    parse_boot,
)
from tests.ntfs_image import Image, Record, data_nonres, data_resident, encode_runs, fn, si, standard_volume

MB = 1_000_000


def sample_image(**kw) -> Image:
    img = standard_volume(Image(**kw))
    img.add(Record(24, [si(), fn(11, "$UsnJrnl"), data_nonres(1_000 * MB)]))  # в $Extend — скрыт
    img.add(Record(25, [si(), fn(5, "Users")], directory=True))
    img.add(Record(26, [si(), fn(25, "ALICE~1", ns=2), fn(25, "Alice", ns=1)], directory=True))
    img.add(Record(27, [si(mtime=1_650_000_000), fn(26, "video.mkv"), data_nonres(5 * MB)]))
    img.add(Record(28, [si(), fn(26, "note.txt"), data_resident(100), data_resident(26, "Zone.Identifier")]))
    img.add(Record(29, [si(), fn(30, "a.dll"), fn(31, "a.dll"), data_resident(50)]))  # жёсткая ссылка
    img.add(Record(30, [si(), fn(5, "Windows")], directory=True))
    img.add(Record(31, [si(), fn(30, "WinSxS")], directory=True))
    img.add(Record(32, [si(attrs=0x400), fn(5, "Link")], directory=True))  # junction
    img.add(Record(33, [si(), fn(32, "inside.bin"), data_nonres(999)]))
    img.add(Record(34, [si(attrs=0x400000), fn(26, "cloud.docx"), data_nonres(7777)]))
    img.add(Record(35, [si(), fn(26, "deleted.bin"), data_nonres(123456)], in_use=False))
    img.add(Record(36, [si(), fn(50, "orphan.bin"), data_nonres(4444)]))
    img.add(Record(37, [si(), fn(25, "big.iso")]))  # $DATA — в записи-продолжении 38
    img.add(Record(38, [data_nonres(8 * MB)], base=37))
    img.add(Record(39, [si(), fn(25, "Empty")], directory=True))
    img.add(Record(40, [si(attrs=0x40000 | 0x20), fn(30, "kernel.dll"), data_nonres(2 * MB)]))  # EA, не облако
    return img


class MftTestBase(unittest.TestCase):
    def analyze(self, img: Image, root: str = "X:\\", **kw):
        fd, path = tempfile.mkstemp(suffix=".img")
        os.write(fd, img.build())
        os.close(fd)
        self.addCleanup(os.remove, path)
        return MftAnalyzer(**kw).analyze(root, volume_path=path)


class MftAnalyzerTests(MftTestBase):
    def test_tree_sizes_and_counts(self):
        r = self.analyze(sample_image())
        root = r.root
        self.assertEqual(r.method, "mft")
        self.assertEqual(root.path, "X:\\")
        names = {c.name: c for c in root.children}
        self.assertEqual(set(names), {"Users", "Windows"})  # без $-файлов, $Extend и junction
        users = names["Users"]
        alice = {c.name: c for c in users.children}["Alice"]
        self.assertEqual(alice.own_size, 5 * MB + 100)
        self.assertEqual(alice.own_files, 2)
        self.assertEqual(users.own_size, 8 * MB)  # $DATA из записи-продолжения
        self.assertEqual(users.size, 13 * MB + 100)
        self.assertEqual(names["Windows"].size, 2 * MB + 50)  # жёсткая ссылка — один раз; файл с EA учтён
        self.assertEqual(root.size, 15 * MB + 150)
        self.assertEqual(root.own_size, 0)
        self.assertEqual(root.dirs, 5)
        self.assertEqual(r.scanned_dirs, 6)
        self.assertEqual(r.scanned_files, 6)
        self.assertEqual(r.cloud_only_files, 1)
        self.assertEqual(r.skipped_links, 1)

    def test_paths_names_and_top_files(self):
        r = self.analyze(sample_image())
        alice = r.root.sorted_children()[0].sorted_children()[0]
        self.assertEqual(alice.name, "Alice")  # длинное имя, не DOS 8.3
        self.assertEqual(alice.path, "X:\\Users\\Alice")
        top = alice.top_files
        self.assertEqual([ntpath.basename(f.path) for f in top], ["video.mkv", "note.txt"])
        self.assertEqual(top[0].path, "X:\\Users\\Alice\\video.mkv")
        self.assertAlmostEqual(top[0].mtime, 1_650_000_000, places=3)
        self.assertIs(alice.parent.parent, r.root)

    def test_subfolder_root_case_insensitive(self):
        r = self.analyze(sample_image(), root="X:\\users\\ALICE")
        self.assertEqual(r.root.path, "X:\\Users\\Alice")
        self.assertIsNone(r.root.parent)
        self.assertEqual(r.root.size, 5 * MB + 100)
        self.assertEqual(r.scanned_dirs, 1)

    def test_missing_folder_raises(self):
        with self.assertRaises(MftError):
            self.analyze(sample_image(), root="X:\\Nope")

    def test_fragmented_mft_with_attribute_list(self):
        r = self.analyze(sample_image(split_mft_data=True))
        self.assertEqual(r.root.size, 15 * MB + 150)

    def test_top_n_limit(self):
        img = standard_volume(Image())
        img.add(Record(24, [si(), fn(5, "d")], directory=True))
        for i in range(10):
            img.add(Record(25 + i, [si(), fn(24, f"f{i}"), data_resident(10 + i)]))
        r = self.analyze(img, top_files_per_dir=3)
        d = r.root.children[0]
        self.assertEqual([f.size for f in d.top_files], [19, 18, 17])
        self.assertEqual(d.own_files, 10)

    def test_cancel(self):
        fd, path = tempfile.mkstemp(suffix=".img")
        os.write(fd, sample_image().build())
        os.close(fd)
        self.addCleanup(os.remove, path)
        ev = threading.Event()
        ev.set()
        r = MftAnalyzer().analyze("X:\\", cancel_event=ev, volume_path=path)
        self.assertTrue(r.cancelled)

    def test_progress_reported(self):
        fd, path = tempfile.mkstemp(suffix=".img")
        os.write(fd, sample_image().build())
        os.close(fd)
        self.addCleanup(os.remove, path)
        seen = []
        MftAnalyzer().analyze("X:\\", progress_cb=seen.append, volume_path=path)
        self.assertTrue(seen)
        self.assertEqual(seen[-1]["percent"], 100)
        self.assertEqual(seen[-1]["mode"], "mft")

    def test_not_ntfs(self):
        fd, path = tempfile.mkstemp(suffix=".img")
        os.write(fd, b"\x00" * 8192)
        os.close(fd)
        self.addCleanup(os.remove, path)
        with self.assertRaises(MftError):
            MftAnalyzer().analyze("X:\\", volume_path=path)

    def test_volume_is_only_read(self):
        data = sample_image().build()
        fd, path = tempfile.mkstemp(suffix=".img")
        os.write(fd, data)
        os.close(fd)
        self.addCleanup(os.remove, path)
        MftAnalyzer().analyze("X:\\", volume_path=path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), data)


class LowLevelTests(unittest.TestCase):
    def test_decode_runs_with_negative_offset_and_sparse(self):
        raw = encode_runs([(1000, 4), (200, 2)])
        self.assertEqual(decode_runs(raw, 0, len(raw)), [(1000, 4), (200, 2)])
        sparse = bytes([0x01, 0x05, 0x00])  # 5 кластеров без LCN
        self.assertEqual(decode_runs(sparse, 0, len(sparse)), [(None, 5)])

    def test_fixup_detects_torn_write(self):
        rec = bytearray(Record(30, [si(), fn(5, "x")]).build())
        good = bytearray(rec)
        self.assertTrue(apply_fixups(good, 1024))
        rec[510] ^= 0xFF  # «порванная» запись: конец сектора не совпадает с USN
        self.assertFalse(apply_fixups(rec, 1024))

    def test_parse_boot(self):
        img = sample_image().build()
        b = parse_boot(img[:512])
        self.assertEqual((b.cluster_size, b.record_size, b.mft_lcn), (4096, 1024, 10))
        with self.assertRaises(MftError):
            parse_boot(b"\x00" * 512)


if __name__ == "__main__":
    unittest.main()


class FallbackTests(unittest.TestCase):
    def test_falls_back_to_walk_on_mft_error(self):
        from unittest import mock

        from app.core import mft_reader
        from app.core.space_analyzer import SpaceAnalyzer

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "f.bin"), "wb") as fh:
                fh.write(b"x" * 1234)
            with mock.patch.object(mft_reader, "mft_unavailable_reason", return_value=None), \
                    mock.patch.object(mft_reader.MftAnalyzer, "analyze", side_effect=MftError("boom")):
                r = SpaceAnalyzer(prefer_mft=True).analyze(d)
            self.assertEqual(r.method, "walk")
            self.assertEqual(r.fallback_reason, "error")
            self.assertEqual(r.root.size, 1234)

    def test_walk_when_mft_unavailable(self):
        from unittest import mock

        from app.core import mft_reader
        from app.core.space_analyzer import SpaceAnalyzer

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(mft_reader, "mft_unavailable_reason", return_value="not_admin"):
                r = SpaceAnalyzer(prefer_mft=True).analyze(d)
            self.assertEqual((r.method, r.fallback_reason), ("walk", "not_admin"))
            r2 = SpaceAnalyzer().analyze(d)
            self.assertEqual((r2.method, r2.fallback_reason), ("walk", ""))
