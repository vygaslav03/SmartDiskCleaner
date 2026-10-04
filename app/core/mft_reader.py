"""Быстрый анализ места: чтение главной таблицы файлов NTFS (MFT) напрямую с тома.

Обычный обход (os.scandir) открывает каждую папку — на системном диске это
сотни тысяч системных вызовов. MFT содержит запись о каждом файле и папке тома
подряд, поэтому её последовательное чтение в разы быстрее.

Только чтение: том открывается на чтение (\\\\.\\C:), в него ничего не пишется.
Нужны права администратора; без них, на не-NTFS томах и при любой ошибке
SpaceAnalyzer возвращается к обычному обходу.

Семантика совпадает с обычным обходом:
* размер файла — логический размер основного потока данных ($DATA без имени);
* точки повторной обработки (symlink, junction, файлы OneDrive и т. п.) пропускаются
  вместе с содержимым — обход по ним тоже не ходит;
* служебные файлы NTFS ($MFT, $LogFile, $Extend\\…) не показываются.
Отличие: файл с несколькими жёсткими ссылками считается один раз (по первому
имени), поэтому, например, WinSxS не «удваивает» размер Windows.
"""

from __future__ import annotations

import heapq
import ntpath
import os
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import compress

from app.core.space_analyzer import DirNode, SpaceResult
from app.models.file_item import FileItem
from app.utils.logger import get_logger

log = get_logger("mft")

ROOT_RECORD = 5
FIRST_USER_RECORD = 16  # 0..15 — служебные файлы NTFS (кроме корня, запись 5)

ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

FLAG_IN_USE = 0x01
FLAG_DIRECTORY = 0x02

NS_DOS = 2

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
# 0x40000 на диске NTFS — это «у файла есть расширенные атрибуты» (EA): его ставит, например,
# Windows на exe/dll ($KERNEL.PURGE) и WSL. Пользователю этот бит не виден (он же RECALL_ON_OPEN),
# поэтому для MFT он не означает «файл только в облаке».
FILE_ATTRIBUTE_HAS_EA = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_MASK = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

FIXUP_STRIDE = 512
READ_CHUNK = 4 * 1024 * 1024
REF_MASK = (1 << 48) - 1
EPOCH_DIFF = 116444736000000000  # 1601-01-01 -> 1970-01-01 в интервалах по 100 нс

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_USA = struct.Struct("<HH")
_ATTR_HDR = struct.Struct("<IIBBH")  # type, length, non_resident, name_len, name_off


class MftError(Exception):
    """Том не NTFS, повреждённые структуры или нет доступа."""


@dataclass(frozen=True)
class BootInfo:
    bytes_per_sector: int
    cluster_size: int
    mft_lcn: int
    record_size: int


# ---------------------------------------------------------------------------- том
class Volume:
    """Чтение тома или файла-образа выровненными блоками (для \\\\.\\C: это обязательно)."""

    def __init__(self, path: str) -> None:
        self.path = path
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            self._fd = os.open(path, flags)
        except OSError as e:
            raise MftError(f"не удалось открыть том {path}: {e}") from e
        self.align = 512

    def read(self, offset: int, size: int) -> bytes:
        start = offset - offset % self.align
        end = offset + size
        end += (-end) % self.align
        os.lseek(self._fd, start, os.SEEK_SET)
        parts: list[bytes] = []
        need = end - start
        while need > 0:
            chunk = os.read(self._fd, need)
            if not chunk:
                break
            parts.append(chunk)
            need -= len(chunk)
        data = b"".join(parts)
        lo = offset - start
        out = data[lo:lo + size]
        if len(out) != size:
            raise MftError(f"короткое чтение тома: {len(out)} из {size} байт по смещению {offset}")
        return out

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


def parse_boot(sector: bytes) -> BootInfo:
    if len(sector) < 512 or sector[3:11] != b"NTFS    ":
        raise MftError("не NTFS")
    bps = _U16.unpack_from(sector, 0x0B)[0]
    spc = sector[0x0D]
    if spc > 0x80:  # большие кластеры: 2^(256 - x)
        spc = 1 << (256 - spc)
    if bps not in (512, 1024, 2048, 4096) or spc == 0:
        raise MftError("неверная геометрия NTFS")
    cluster = bps * spc
    mft_lcn = _U64.unpack_from(sector, 0x30)[0]
    cpr = struct.unpack_from("<b", sector, 0x40)[0]
    record_size = (1 << -cpr) if cpr < 0 else cpr * cluster
    if record_size < 512 or record_size > 65536:
        raise MftError("неверный размер записи MFT")
    return BootInfo(bps, cluster, mft_lcn, record_size)


def decode_runs(buf: bytes | memoryview, pos: int, end: int) -> list[tuple[int | None, int]]:
    """Data runs -> [(lcn | None для разреженного участка, длина в кластерах)]."""
    runs: list[tuple[int | None, int]] = []
    lcn = 0
    while pos < end:
        header = buf[pos]
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = header >> 4
        pos += 1
        if len_size == 0 or pos + len_size + off_size > end:
            raise MftError("повреждённые data runs")
        length = int.from_bytes(buf[pos:pos + len_size], "little")
        pos += len_size
        if off_size:
            delta = int.from_bytes(buf[pos:pos + off_size], "little", signed=True)
            pos += off_size
            lcn += delta
            runs.append((lcn, length))
        else:
            runs.append((None, length))
    return runs


def apply_fixups(rec, record_size: int) -> bool:
    """Восстанавливает последние 2 байта каждого 512-байтного блока. False — запись повреждена."""
    usa_off, usa_cnt = _USA.unpack_from(rec, 4)
    if usa_cnt < 2 or usa_off + 2 * usa_cnt > record_size or (usa_cnt - 1) * FIXUP_STRIDE > record_size:
        return False
    usn = rec[usa_off:usa_off + 2]
    for i in range(1, usa_cnt):
        end = i * FIXUP_STRIDE
        if rec[end - 2:end] != usn:
            return False
        rec[end - 2:end] = rec[usa_off + 2 * i:usa_off + 2 * i + 2]
    return True


def filetime_to_unix(ft: int) -> float:
    if ft <= EPOCH_DIFF:
        return 0.0
    return (ft - EPOCH_DIFF) / 10_000_000


# ------------------------------------------------------------------ разбор записи
_HDR = struct.Struct("<HHIIQ")  # @0x14: attr_off, flags, used, allocated, base_ref
_ATTR_RES = struct.Struct("<IIBBHHHIH")  # type, len, non_res, name_len, name_off, flags, id, value_len, value_off
_SI = struct.Struct("<8xQ16xI")  # mtime, file attributes
_FN_HEAD = struct.Struct("<Q")


class _Tables:
    """Сведения по номерам базовых записей (списки — быстрее и компактнее словарей).

    Имена хранятся как сырые UTF-16 байты и декодируются только для папок и
    файлов, попавших в топ; время — как FILETIME.
    """

    def __init__(self, count: int) -> None:
        n = count
        self.count = n
        self.in_use = bytearray(n)
        self.is_dir = bytearray(n)
        self.parent = [-1] * n
        self.raw_name: list[bytes | None] = [None] * n
        self.name_dos = bytearray(n)  # текущее имя — DOS 8.3 (будет заменено длинным)
        self.size = [0] * n
        self.ft = [0] * n
        self.attrs = [0] * n

    def name(self, i: int) -> str:
        raw = self.raw_name[i]
        return raw.decode("utf-16-le", "replace") if raw is not None else "?"

    def mtime(self, i: int) -> float:
        return filetime_to_unix(self.ft[i])


def _parse_record(rec, index: int, t: _Tables, record_size: int) -> None:
    """Разбирает одну запись (fixup уже применён). rec — bytes/bytearray/memoryview."""
    pos, flags, used, _alloc, base = _HDR.unpack_from(rec, 0x14)
    if not flags & FLAG_IN_USE:
        return
    base &= REF_MASK
    owner = base if base else index
    if owner >= t.count:
        return
    if not base:
        t.in_use[owner] = 1
        if flags & FLAG_DIRECTORY:
            t.is_dir[owner] = 1
    used = min(used, record_size)
    unpack_attr = _ATTR_RES.unpack_from
    while pos + 24 <= used:
        atype, alen, nonres, name_len, _no, _fl, _id, vlen, voff = unpack_attr(rec, pos)
        if atype == ATTR_END or alen < 16 or pos + alen > used:
            break
        if atype == ATTR_FILE_NAME:
            if not nonres:
                v = pos + voff
                if v + 0x42 <= pos + alen:
                    ns = rec[v + 0x41]
                    if t.raw_name[owner] is None or (t.name_dos[owner] and ns != NS_DOS):
                        nlen = rec[v + 0x40]
                        t.raw_name[owner] = bytes(rec[v + 0x42:v + 0x42 + 2 * nlen])
                        t.parent[owner] = _FN_HEAD.unpack_from(rec, v)[0] & REF_MASK
                        t.name_dos[owner] = 1 if ns == NS_DOS else 0
        elif atype == ATTR_STANDARD_INFORMATION:
            if not nonres:
                v = pos + voff
                if v + 0x24 <= pos + alen:
                    t.ft[owner], t.attrs[owner] = _SI.unpack_from(rec, v)
        elif atype == ATTR_DATA and name_len == 0:
            if nonres:
                if _U64.unpack_from(rec, pos + 0x10)[0] == 0:  # lowest VCN 0 — первый сегмент
                    t.size[owner] = _U64.unpack_from(rec, pos + 0x30)[0]
            else:
                t.size[owner] = vlen
        pos += alen


def _record_attrs(rec: bytes | memoryview, record_size: int):
    """(type, offset, length, non_resident, name_len) всех атрибутов записи."""
    pos = _U16.unpack_from(rec, 0x14)[0]
    used = min(_U32.unpack_from(rec, 0x18)[0], record_size)
    while pos + 16 <= used:
        atype, alen, nonres, name_len, _ = _ATTR_HDR.unpack_from(rec, pos)
        if atype == ATTR_END or alen < 16 or pos + alen > used:
            break
        yield atype, pos, alen, nonres, name_len
        pos += alen


def _data_segment(rec: bytes, record_size: int) -> tuple[int, list[tuple[int | None, int]], int] | None:
    """Безымянный нерезидентный $DATA записи: (lowest_vcn, runs, real_size)."""
    for atype, pos, alen, nonres, name_len in _record_attrs(rec, record_size):
        if atype == ATTR_DATA and nonres and name_len == 0:
            low = _U64.unpack_from(rec, pos + 0x10)[0]
            runs_off = _U16.unpack_from(rec, pos + 0x20)[0]
            real = _U64.unpack_from(rec, pos + 0x30)[0]
            return low, decode_runs(rec, pos + runs_off, pos + alen), real
    return None


# ------------------------------------------------------------------ анализатор
class MftAnalyzer:
    def __init__(self, top_files_per_dir: int = 30, volume_factory: Callable[[str], Volume] = Volume) -> None:
        self.top_n = max(1, top_files_per_dir)
        self._volume_factory = volume_factory

    # -- MFT
    def _read_record(self, vol: Volume, boot: BootInfo, runs: list[tuple[int | None, int]], index: int) -> bytes:
        offset = index * boot.record_size
        vcn_bytes = 0
        for lcn, length in runs:
            run_bytes = length * boot.cluster_size
            if offset < vcn_bytes + run_bytes:
                if lcn is None:
                    raise MftError("запись MFT в разреженном участке")
                rec = bytearray(vol.read(lcn * boot.cluster_size + (offset - vcn_bytes), boot.record_size))
                if rec[0:4] != b"FILE" or not apply_fixups(rec, boot.record_size):
                    raise MftError(f"повреждена запись MFT {index}")
                return bytes(rec)
            vcn_bytes += run_bytes
        raise MftError(f"запись MFT {index} вне таблицы")

    def _mft_layout(self, vol: Volume, boot: BootInfo) -> tuple[list[tuple[int | None, int]], int]:
        rec0 = bytearray(vol.read(boot.mft_lcn * boot.cluster_size, boot.record_size))
        if rec0[0:4] != b"FILE" or not apply_fixups(rec0, boot.record_size):
            raise MftError("повреждена запись $MFT")
        seg = _data_segment(bytes(rec0), boot.record_size)
        if seg is None:
            raise MftError("у $MFT нет $DATA")
        _low, runs, real = seg
        # Очень фрагментированная MFT: остальные сегменты $DATA лежат в записях-продолжениях,
        # перечисленных в $ATTRIBUTE_LIST записи 0.
        ext_records: list[int] = []
        for atype, pos, alen, nonres, _nl in _record_attrs(bytes(rec0), boot.record_size):
            if atype == ATTR_ATTRIBUTE_LIST and not nonres:
                vlen = _U32.unpack_from(rec0, pos + 0x10)[0]
                v = pos + _U16.unpack_from(rec0, pos + 0x14)[0]
                p = v
                while p + 0x1A <= v + vlen:
                    etype = _U32.unpack_from(rec0, p)[0]
                    elen = _U16.unpack_from(rec0, p + 4)[0]
                    if elen == 0:
                        break
                    ref = _U64.unpack_from(rec0, p + 0x10)[0] & REF_MASK
                    if etype == ATTR_DATA and ref != 0 and ref not in ext_records:
                        ext_records.append(ref)
                    p += elen
        if ext_records:
            segs = []
            for ref in ext_records:
                s = _data_segment(self._read_record(vol, boot, runs, ref), boot.record_size)
                if s is not None and s[0] > 0:
                    segs.append(s)
            for _low, more, _r in sorted(segs, key=lambda s: s[0]):
                runs = runs + more
        return runs, real

    def _iter_mft(self, vol: Volume, boot: BootInfo, runs, total_bytes: int, cancel: threading.Event):
        """Последовательно отдаёт куски MFT, кратные размеру записи (запись может лежать на стыке участков)."""
        rs = boot.record_size
        carry = b""
        consumed = 0
        for lcn, length in runs:
            run_bytes = length * boot.cluster_size
            done = 0
            while done < run_bytes and consumed < total_bytes:
                if cancel.is_set():
                    return
                n = min(READ_CHUNK, run_bytes - done)
                data = bytes(n) if lcn is None else vol.read(lcn * boot.cluster_size + done, n)
                done += n
                data = data[: total_bytes - consumed]
                consumed += len(data)
                buf = carry + data if carry else data
                whole = len(buf) - len(buf) % rs
                if whole:
                    yield buf[:whole]
                carry = buf[whole:]
            if consumed >= total_bytes:
                break

    # -- публичный API
    def analyze(
        self,
        root: str,
        progress_cb: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        volume_path: str | None = None,
    ) -> SpaceResult:
        cancel = cancel_event or threading.Event()
        t0 = time.monotonic()
        drive, rel = ntpath.splitdrive(root)
        vol_path = volume_path or ("\\\\.\\" + drive.rstrip("\\/"))
        vol = self._volume_factory(vol_path)
        try:
            boot = parse_boot(vol.read(0, 512))
            vol.align = boot.bytes_per_sector
            runs, mft_bytes = self._mft_layout(vol, boot)
            count = mft_bytes // boot.record_size
            t = _Tables(count)
            rs = boot.record_size
            index = 0
            last_pct = -1
            for chunk in self._iter_mft(vol, boot, runs, count * rs, cancel):
                for off in range(0, len(chunk), rs):
                    if chunk[off + 0x16] & FLAG_IN_USE and chunk[off:off + 4] == b"FILE":
                        rec = bytearray(chunk[off:off + rs])
                        if apply_fixups(rec, rs):
                            _parse_record(rec, index, t, rs)
                    index += 1
                if progress_cb is not None:
                    pct = int(index * 100 / count) if count else 100
                    if pct != last_pct:
                        last_pct = pct
                        progress_cb({"mode": "mft", "percent": pct, "dirs": 0, "files": index,
                                     "bytes": 0, "current": ""})
        finally:
            vol.close()

        if cancel.is_set():
            result = SpaceResult(root=DirNode(name=root, path=root), cancelled=True)
            result.duration = time.monotonic() - t0
            result.method = "mft"
            return result

        result = self._build(root, drive, rel, t)
        result.duration = time.monotonic() - t0
        result.method = "mft"
        log.info(
            "MFT: %s — %d записей, %d папок, %d файлов, %d байт, %.1f c",
            root, count, result.scanned_dirs, result.scanned_files, result.root.size, result.duration,
        )
        return result

    # -- дерево
    def _build(self, root: str, drive: str, rel: str, t: _Tables) -> SpaceResult:
        n = t.count
        if n <= ROOT_RECORD or not t.in_use[ROOT_RECORD] or not t.is_dir[ROOT_RECORD]:
            raise MftError("нет корневой папки в MFT")
        children: dict[int, list[int]] = {}
        files_of: dict[int, list[int]] = {}
        cloud_of: dict[int, int] = {}
        skipped_links = 0
        parent, attrs, is_dir, raw_name = t.parent, t.attrs, t.is_dir, t.raw_name
        for i in compress(range(FIRST_USER_RECORD, n), t.in_use[FIRST_USER_RECORD:]):
            if raw_name[i] is None:
                continue
            p = parent[i]
            if p == i:
                continue
            a = attrs[i]
            if a & FILE_ATTRIBUTE_REPARSE_POINT:
                skipped_links += 1
            elif is_dir[i]:
                if p in children:
                    children[p].append(i)
                else:
                    children[p] = [i]
            elif a & CLOUD_MASK:
                cloud_of[p] = cloud_of.get(p, 0) + 1
            elif p in files_of:
                files_of[p].append(i)
            else:
                files_of[p] = [i]

        # корень анализа: весь том или папка внутри него
        start = ROOT_RECORD
        root_path = drive.rstrip("\\/") + "\\"
        for part in [x for x in rel.replace("/", "\\").split("\\") if x]:
            low = part.lower()
            nxt = next((c for c in children.get(start, ()) if t.name(c).lower() == low), None)
            if nxt is None:
                raise MftError(f"папка не найдена в MFT: {root}")
            start = nxt
            root_path = root_path + t.name(nxt) + "\\"
        if start != ROOT_RECORD:
            root_path = root_path.rstrip("\\")

        root_node = DirNode(name=root_path, path=root_path, mtime=t.mtime(start))
        result = SpaceResult(root=root_node)
        result.skipped_links = skipped_links
        order: list[DirNode] = [root_node]
        queue: list[tuple[int, DirNode]] = [(start, root_node)]
        size = t.size
        size_of = size.__getitem__
        top_n = self.top_n
        scanned = cloud = 0
        qi = 0
        while qi < len(queue):
            rec, node = queue[qi]
            qi += 1
            base = node.path if node.path.endswith("\\") else node.path + "\\"
            for c in children.get(rec, ()):
                name = t.name(c)
                child = DirNode(name=name, path=base + name, parent=node, mtime=t.mtime(c))
                node.children.append(child)
                order.append(child)
                queue.append((c, child))
            n_cloud = cloud_of.get(rec, 0)
            cloud += n_cloud
            scanned += n_cloud
            files = files_of.get(rec)
            if not files:
                continue
            scanned += len(files)
            node.own_files = len(files)
            node.own_size = sum(map(size_of, files))
            best = files if len(files) <= top_n else heapq.nlargest(top_n, files, key=size_of)
            # FileItem создаём только для попавших в топ — экономия памяти на сотнях тысяч файлов
            node._top = [(size[f], f, FileItem(base + t.name(f), size[f], t.mtime(f))) for f in best]
        result.scanned_dirs = len(order)
        result.scanned_files = scanned
        result.cloud_only_files = cloud
        for node in reversed(order):
            node.size += node.own_size
            node.files += node.own_files
            par = node.parent
            if par is not None:
                par.size += node.size
                par.files += node.files
                par.dirs += node.dirs + 1
        return result


# --------------------------------------------------------------- доступность
def mft_unavailable_reason(root: str) -> str | None:
    """None — можно читать MFT; иначе короткая причина (для лога и подсказки)."""
    from app.utils.permissions import is_admin
    from app.utils.winpaths import IS_WINDOWS

    if not IS_WINDOWS:
        return "not_windows"
    if root.startswith("\\\\") and not root.startswith("\\\\?\\"):
        return "network"
    drive, rel = ntpath.splitdrive(root)
    if not drive or len(drive) != 2:
        return "no_drive"
    if rel.strip("\\/"):
        # Чтение MFT занимает несколько секунд независимо от размера папки — выигрыш есть
        # только для всего диска; обычную папку быстрее обойти.
        return "subfolder"
    if not is_admin():
        return "not_admin"
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if k32.GetDriveTypeW(ctypes.c_wchar_p(drive + "\\")) != 3:  # DRIVE_FIXED
            return "not_fixed"
        fs = ctypes.create_unicode_buffer(32)
        ok = k32.GetVolumeInformationW(ctypes.c_wchar_p(drive + "\\"), None, 0, None, None, None, fs, 32)
        if not ok:
            return "no_volume_info"
        if fs.value.upper() != "NTFS":
            return "not_ntfs"
    except (OSError, AttributeError):
        return "no_volume_info"
    return None
