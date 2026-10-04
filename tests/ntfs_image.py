"""Построитель маленьких синтетических образов NTFS для тестов MFT-анализатора.

Образ содержит загрузочный сектор и MFT (можно в двух фрагментах) — этого
достаточно для MftAnalyzer. Записи собираются по формату NTFS 3.1 с fixup-ами.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

CLUSTER = 4096
SECTOR = 512
RECORD = 1024
USN = b"\x07\x00"
EPOCH_DIFF = 116444736000000000


def _ft(unix: float) -> int:
    return int(unix * 10_000_000) + EPOCH_DIFF


def _pad8(b: bytes) -> bytes:
    return b + b"\x00" * ((-len(b)) % 8)


def _resident(atype: int, value: bytes, name: str = "") -> bytes:
    name_b = name.encode("utf-16-le")
    name_off = 0x18
    value_off = (name_off + len(name_b) + 7) // 8 * 8
    hdr = struct.pack("<IIBBHHHIHBB", atype, 0, 0, len(name), name_off, 0, 0, len(value), value_off, 0, 0)
    body = hdr + name_b
    body += b"\x00" * (value_off - len(body)) + value
    body = _pad8(body)
    return body[:4] + struct.pack("<I", len(body)) + body[8:]


def encode_runs(runs: list[tuple[int, int]]) -> bytes:
    """[(lcn, длина)] -> data runs (смещения относительно предыдущего LCN)."""
    out = b""
    prev = 0
    for lcn, length in runs:
        delta = lcn - prev
        prev = lcn
        lb = length.to_bytes(max(1, (length.bit_length() + 7) // 8), "little")
        ob_len = 1
        while not (-(1 << (8 * ob_len - 1)) <= delta < (1 << (8 * ob_len - 1))):
            ob_len += 1
        ob = delta.to_bytes(ob_len, "little", signed=True)
        out += bytes([(len(ob) << 4) | len(lb)]) + lb + ob
    return out + b"\x00"


def _nonresident(atype: int, real_size: int, runs: bytes, lowest_vcn: int = 0, highest_vcn: int = 0) -> bytes:
    runs_off = 0x40
    hdr = struct.pack("<IIBBHHH", atype, 0, 1, 0, runs_off, 0, 0)
    hdr += struct.pack("<QQHH4xQQQ", lowest_vcn, highest_vcn, runs_off, 0,
                       (real_size + CLUSTER - 1) // CLUSTER * CLUSTER, real_size, real_size)
    body = _pad8(hdr + runs)
    return body[:4] + struct.pack("<I", len(body)) + body[8:]


def si(mtime: float = 1_700_000_000.0, attrs: int = 0) -> bytes:
    ft = _ft(mtime)
    value = struct.pack("<QQQQI", ft, ft, ft, ft, attrs) + b"\x00" * 0x24
    return _resident(0x10, value)


def fn(parent: int, name: str, ns: int = 1, seq: int = 1) -> bytes:
    ref = (seq << 48) | parent
    value = struct.pack("<Q", ref) + b"\x00" * 32 + struct.pack("<QQII", 0, 0, 0, 0)
    value += bytes([len(name), ns]) + name.encode("utf-16-le")
    return _resident(0x30, value)


def data_resident(size: int, stream: str = "") -> bytes:
    return _resident(0x80, b"x" * size, stream)


def data_nonres(real_size: int, runs: list[tuple[int, int]] | None = None, lowest_vcn: int = 0,
                highest_vcn: int = 0) -> bytes:
    return _nonresident(0x80, real_size, encode_runs(runs or [(1000, 1)]), lowest_vcn, highest_vcn)


def attr_list(entries: list[tuple[int, int, int]]) -> bytes:
    """[(тип атрибута, lowest_vcn, номер записи)]"""
    value = b""
    for atype, vcn, ref in entries:
        value += struct.pack("<IHBBQQH", atype, 0x20, 0, 0x1A, vcn, (1 << 48) | ref, 0) + b"\x00" * 6
    return _resident(0x20, value)


@dataclass
class Record:
    index: int
    attrs: list[bytes] = field(default_factory=list)
    directory: bool = False
    in_use: bool = True
    base: int = 0

    def build(self) -> bytes:
        attr_off = 0x38
        body = b"".join(self.attrs) + struct.pack("<I", 0xFFFFFFFF) + b"\x00" * 4
        used = attr_off + len(body)
        assert used <= RECORD, "запись не помещается"
        flags = (1 if self.in_use else 0) | (2 if self.directory else 0)
        hdr = struct.pack("<4sHHQHHHHIIQHHI", b"FILE", 0x30, 3, 0, 1, 1, attr_off, flags, used, RECORD,
                          (1 << 48) | self.base if self.base else 0, 0, 0, self.index)
        rec = bytearray(hdr + b"\x00" * (0x30 - len(hdr)))
        rec += USN + b"\x00\x00\x00\x00"  # массив fixup: USN + 2 исходных значения
        rec += b"\x00" * (attr_off - len(rec))
        rec += body
        rec += b"\x00" * (RECORD - len(rec))
        for i in (1, 2):
            end = i * SECTOR
            rec[0x30 + 2 * i:0x30 + 2 * i + 2] = rec[end - 2:end]
            rec[end - 2:end] = USN
        return bytes(rec)


@dataclass
class Image:
    records: dict[int, Record] = field(default_factory=dict)
    count: int = 64
    extents: list[tuple[int, int]] = field(default_factory=lambda: [(10, 6), (40, 10)])  # (LCN, кластеров)
    split_mft_data: bool = False  # второй фрагмент $DATA у $MFT — в записи-продолжении 15

    def add(self, rec: Record) -> Record:
        self.records[rec.index] = rec
        return rec

    def build(self) -> bytes:
        per_cluster = CLUSTER // RECORD
        assert sum(n for _, n in self.extents) * per_cluster >= self.count
        mft_bytes = self.count * RECORD
        if self.split_mft_data:
            first, rest = self.extents[0], self.extents[1:]
            vcn0_end = first[1] - 1
            rec0 = Record(0, [si(), attr_list([(0x80, 0, 0), (0x80, first[1], 15)]), fn(5, "$MFT"),
                              data_nonres(mft_bytes, [first], 0, vcn0_end)])
            total = sum(n for _, n in self.extents)
            self.records[15] = _Ext(15, [data_nonres(mft_bytes, rest, first[1], total - 1)])
        else:
            rec0 = Record(0, [si(), fn(5, "$MFT"), data_nonres(mft_bytes, self.extents)])
        self.records[0] = rec0
        mft = bytearray(mft_bytes)
        for idx, rec in self.records.items():
            mft[idx * RECORD:(idx + 1) * RECORD] = rec.build()
        size = (max(lcn + n for lcn, n in self.extents) + 1) * CLUSTER
        img = bytearray(size)
        boot = bytearray(SECTOR)
        boot[3:11] = b"NTFS    "
        struct.pack_into("<HB", boot, 0x0B, SECTOR, CLUSTER // SECTOR)
        struct.pack_into("<Q", boot, 0x30, self.extents[0][0])
        struct.pack_into("<b", boot, 0x40, -10)  # 2^10 = 1024 байт на запись
        boot[510:512] = b"\x55\xaa"
        img[0:SECTOR] = boot
        pos = 0
        for lcn, n in self.extents:
            chunk = mft[pos:pos + n * CLUSTER]
            img[lcn * CLUSTER:lcn * CLUSTER + len(chunk)] = chunk
            pos += n * CLUSTER
        return bytes(img)


class _Ext(Record):
    """Запись-продолжение $MFT: в NTFS её базовая ссылка указывает на запись 0."""

    def build(self) -> bytes:
        rec = bytearray(super().build())
        # base reference = запись 0 с последовательностью 1 (ненулевая ссылка -> это продолжение)
        struct.pack_into("<Q", rec, 0x20, 1 << 48)
        return bytes(rec)


def standard_volume(img: Image | None = None) -> Image:
    """Служебные записи NTFS: $MFT…, корень (5), $Extend (11)."""
    img = img or Image()
    names = {1: "$MFTMirr", 2: "$LogFile", 3: "$Volume", 4: "$AttrDef", 6: "$Bitmap", 7: "$Boot",
             8: "$BadClus", 9: "$Secure", 10: "$UpCase"}
    for idx, name in names.items():
        img.add(Record(idx, [si(), fn(5, name), data_nonres(64 * 1024 * 1024)]))
    img.add(Record(5, [si(), fn(5, ".")], directory=True))
    img.add(Record(11, [si(), fn(5, "$Extend")], directory=True))
    return img
