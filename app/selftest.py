"""Самопроверка интерфейса: `python run.py --selftest`.

Открывает все страницы в тёмной и светлой теме, выполняет реальные операции
ТОЛЬКО ЧТЕНИЯ (сканирование мусора, поиск больших файлов, поиск дубликатов во
временной папке) и сохраняет скриншоты + отчёт в папку selftest/.

НИЧЕГО НЕ УДАЛЯЕТ: методы удаления заблокированы на время самопроверки.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from pathlib import Path

REPORT: list[str] = []
ERRORS: list[str] = []
# Предупреждения Qt (например, ограничения offscreen-режима в CI) — не ошибки,
# но записываются в отчёт отдельно.
QT_WARNINGS: list[str] = []


def _log(line: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    REPORT.append(f"[{stamp}] {line}")


def _fail(step: str, exc: BaseException | str) -> None:
    text = exc if isinstance(exc, str) else "".join(traceback.format_exception(exc))
    ERRORS.append(f"--- {step} ---\n{text}")
    _log(f"FAIL {step}")


def run_selftest() -> int:
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app import __version__
    from app.core import cleaner as core_cleaner
    from app.ui import theme
    from app.utils.i18n import set_language
    from app.utils.logger import setup_logging
    from app.utils.paths import logs_dir, project_root
    from app.utils.permissions import is_admin
    from app.utils.settings_store import Settings

    out = project_root() / "selftest"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    setup_logging(logs_dir())

    # --- предохранители: никакого удаления и никаких модальных окон
    def _blocked(*_a, **_k):
        raise RuntimeError("Удаление заблокировано в режиме самопроверки")

    core_cleaner.Cleaner.clean_junk = _blocked  # type: ignore[method-assign]
    core_cleaner.Cleaner.delete_user_files = _blocked  # type: ignore[method-assign]
    core_cleaner.remove_file_permanently = _blocked  # type: ignore[assignment]
    core_cleaner.send_to_trash = _blocked  # type: ignore[assignment]
    # Самопроверка не должна менять settings.json пользователя.
    import app.ui.duplicates as ui_dup
    import app.ui.settings as ui_set

    ui_dup.save_settings = lambda *_a, **_k: True  # type: ignore[assignment]
    # История самопроверки пишется в песочницу, а не в history.json пользователя.
    from app.core import history as core_history

    core_history.set_history(core_history.HistoryStore(out / "_sandbox_history.json"))
    # Карантин самопроверки — тоже в песочнице, настоящий карантин пользователя не трогаем.
    from app.core import quarantine as core_q

    shutil.rmtree(out / "_sandbox_quarantine", ignore_errors=True)
    core_q.set_quarantine(core_q.QuarantineStore(out / "_sandbox_quarantine"))
    ui_set.save_settings = lambda *_a, **_k: True  # type: ignore[assignment]

    def _box(kind):
        def inner(*args, **_k):
            _log(f"QMessageBox.{kind}: {args[2] if len(args) > 2 else ''}"[:300])
            return QMessageBox.StandardButton.No
        return inner

    for kind in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, kind, staticmethod(_box(kind)))
    QMessageBox.exec = lambda self: _log(f"QMessageBox.exec: {self.text()[:200]}") or 0  # type: ignore

    def qt_handler(mode, _ctx, message):
        if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            ERRORS.append(f"--- Qt {mode.name} ---\n{message}")
        elif mode == QtMsgType.QtWarningMsg and len(QT_WARNINGS) < 200:
            QT_WARNINGS.append(message)

    qInstallMessageHandler(qt_handler)

    def hook(et, ev, tb):
        _fail("uncaught exception", "".join(traceback.format_exception(et, ev, tb)))

    sys.excepthook = hook

    app = QApplication.instance() or QApplication(sys.argv)
    _log(f"DiskCleaner {__version__}, Python {sys.version.split()[0]}, admin={is_admin()}")

    # Песочница внутри папки программы: %TEMP% лежит в AppData, а AppData
    # защищена политикой для больших файлов и дубликатов.
    sandbox = out / "_sandbox"
    shutil.rmtree(sandbox, ignore_errors=True)
    sandbox.mkdir(parents=True, exist_ok=True)
    try:
        _run_steps(app, out, sandbox, Settings().validate(), theme, set_language)
    except Exception as exc:  # noqa: BLE001
        _fail("selftest crashed", exc)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    report = out / "report.txt"
    status = "OK" if not ERRORS else f"ERRORS: {len(ERRORS)}"
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(f"SELFTEST {status}\n\n")
        fh.write("\n".join(REPORT))
        if ERRORS:
            fh.write("\n\n===== ERRORS =====\n")
            fh.write("\n\n".join(ERRORS))
        if QT_WARNINGS:
            unique = sorted(set(QT_WARNINGS))
            fh.write(f"\n\n===== QT WARNINGS (not errors): {len(QT_WARNINGS)}, unique {len(unique)} =====\n")
            fh.write("\n".join(unique))
    print(f"Selftest finished: {status}. Report: {report}")
    # В CI отчёт сразу виден в логе; у оконного .exe консоли нет — тогда пропускаем.
    if sys.stdout is not None:
        try:
            print(report.read_text(encoding="utf-8"), flush=True)
        except (OSError, UnicodeEncodeError):
            pass
    return 0 if not ERRORS else 1


def _wait(app, predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.03)
    return False


def _shot(app, widget, out: Path, name: str) -> None:
    for _ in range(5):
        app.processEvents()
    try:
        widget.grab().save(str(out / f"{name}.png"))
        _log(f"screenshot {name}.png")
    except Exception as exc:  # noqa: BLE001
        _fail(f"screenshot {name}", exc)


def _step(name: str, fn) -> None:
    t0 = time.monotonic()
    try:
        fn()
        _log(f"OK   {name} ({time.monotonic() - t0:.1f}s)")
    except Exception as exc:  # noqa: BLE001
        _fail(name, exc)


def _run_steps(app, out: Path, sandbox: Path, settings, theme, set_language) -> None:
    from app.models.file_item import FileItem
    from app.ui.main_window import MainWindow
    from app.ui.widgets import ConfirmDeleteDialog, FileListDialog
    from app.utils.format_size import format_size

    # Тестовые данные для дубликатов и больших файлов — только во временной папке.
    data = os.urandom(300 * 1024)
    for rel in ("a/photo.jpg", "b/photo (1).jpg", "c/photo-copy.jpg"):
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (sandbox / "a" / "unique.bin").write_bytes(os.urandom(300 * 1024))
    big = sandbox / "big" / "video.mkv"
    big.parent.mkdir(parents=True, exist_ok=True)
    with open(big, "wb") as fh:
        fh.truncate(120 * 1024 * 1024)  # разреженный файл 120 МБ, место почти не занимает
    # вложенные папки — для проверки второго уровня treemap и перехода внутрь
    for rel, kb in (("projects/app/src/main.bin", 900), ("projects/app/assets/logo.bin", 400),
                    ("projects/site/index.bin", 250), ("music/album/track.bin", 700)):
        p = sandbox / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(kb * 1024))

    set_language("ru")
    theme.apply_theme(app, "dark")
    win = MainWindow(settings)
    win.resize(1280, 820)
    win.show()
    _wait(app, lambda: False, 0.5)

    names = ["dashboard", "cleaner", "analyzer", "large", "duplicates", "history", "quarantine", "settings"]
    for i, n in enumerate(names):
        _step(f"open page {n}", lambda i=i: win.go_to(i))
        _shot(app, win, out, f"dark_{i}_{n}_empty")

    # --- реальное сканирование мусора (только чтение)
    def scan():
        t0 = time.monotonic()
        win._scan_from_dashboard(win.dashboard.current_drive())
        if not _wait(app, lambda: not win.cleaner.busy, 300):
            win.cleaner._cancel()
            _wait(app, lambda: not win.cleaner.busy, 30)
            raise TimeoutError("junk scan > 300s")
        r = win.cleaner.result
        if r is None:
            raise RuntimeError("scan produced no result")
        _log(f"     junk scan: {r.total_files} files, {format_size(r.total_size)}, {time.monotonic()-t0:.1f}s")
        for c in r.categories:
            if c.file_count or c.errors:
                _log(f"       {c.category.id}: {c.file_count} files, {format_size(c.total_size)}, "
                     f"errors={c.errors}, recent={c.skipped_recent}, denied={c.access_denied}")

    _step("junk scan", scan)
    _shot(app, win, out, "dark_1_cleaner_scanned")

    def expand_first():
        tree = win.cleaner.tree
        for i in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(i)
            if g.childCount():
                g.child(0).setExpanded(True)
                tree.setCurrentItem(g.child(0))
                return

    def group_checkboxes():
        from PySide6.QtCore import Qt

        tree = win.cleaner.tree
        for i in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(i)
            if win.cleaner._checkable_children(g) and g.data(0, Qt.ItemDataRole.CheckStateRole) is None:
                raise AssertionError(f"у группы '{g.text(0)}' нет чекбокса")
            # клик по группе должен отметить все её активные категории
            if win.cleaner._checkable_children(g):
                g.setCheckState(0, Qt.CheckState.Checked)
                if any(c.checkState(0) != Qt.CheckState.Checked for c in win.cleaner._checkable_children(g)):
                    raise AssertionError(f"группа '{g.text(0)}' не отметила категории")
        tools = [c.category.id for c in win.cleaner.result.categories
                 if c.selected and c.category.kind == "system_tool"]
        if tools:
            raise AssertionError(f"чекбокс группы отметил системные операции: {tools}")

    _step("group checkboxes", group_checkboxes)
    _shot(app, win, out, "dark_1_cleaner_all_checked")
    _step("expand category", expand_first)
    _shot(app, win, out, "dark_1_cleaner_expanded")
    _step("select all", lambda: win.cleaner._set_all(True))
    def select_all_skips_tools():
        win.cleaner._set_all(True)
        r = win.cleaner.result
        picked_tools = [c.category.id for c in r.categories if c.selected and c.category.kind == "system_tool"]
        if picked_tools:
            raise AssertionError(f"«Выбрать всё» отметило системные операции: {picked_tools}")
        tools = [(c.category.id, c.tool_available, c.reported_size) for c in r.categories
                 if c.category.kind == "system_tool"]
        _log(f"     system tools: {tools}")
        from app.core.processes import running_apps

        _log(f"     running apps: {running_apps(c.category.id for c in r.categories)}")

    _step("select all skips system tools", select_all_skips_tools)
    _step("deselect all", lambda: win.cleaner._set_all(False))
    _step("smart selection", win.cleaner._apply_smart_selection)
    _shot(app, win, out, "dark_1_cleaner_smart")

    # Диалоги — показываем немодально, делаем снимок и закрываем.
    items = [FileItem(str(sandbox / "a" / "photo.jpg"), 300 * 1024, time.time()),
             FileItem(str(big), 120 * 1024 * 1024, time.time())]

    def dialogs():
        d1 = ConfirmDeleteDialog("Confirm", "<b>Будет удалено:</b><br>2 файла<br>120 MB<br><br>Продолжить?",
                                 rows=[("Google Chrome", "10", "50 MB")], warning="note", parent=win)
        sfx = "" if theme.is_dark() else "_light"
        d1.show(); _shot(app, d1, out, "dialog_confirm_junk" + sfx); d1.close()
        d2 = ConfirmDeleteDialog("Confirm", "summary", items=items, warning="w", require_ack=True, parent=win)
        d2.show(); _shot(app, d2, out, "dialog_confirm_files" + sfx)
        if d2.ok.isEnabled():
            raise AssertionError("кнопка удаления активна без галочки подтверждения")
        d2.close()
        d3 = FileListDialog("Details", items, "note", win)
        d3.show(); _shot(app, d3, out, "dialog_details" + sfx); d3.close()

    _step("dialogs", dialogs)

    def details_exclusion():
        cres = next((c for c in win.cleaner.result.categories
                     if c.category.kind == "files" and len(c.items) >= 2), None)
        if cres is None:
            _log("     details exclusion: no category with 2+ files, skipped")
            return
        d = FileListDialog("Details", cres.items, "", win, checkable=True, excluded=set())
        d.show()
        first = d.model.item_at(0)
        d.model.set_checked_paths(i.path for i in cres.items if i.path != first.path)
        _shot(app, d, out, "dialog_details_editable")
        excluded = d.excluded_paths()
        d.close()
        if excluded != {first.path}:
            raise AssertionError(f"excluded_paths неверный: {len(excluded)}")
        cres.selected = True
        cres.excluded = excluded
        win.cleaner._populate()
        _log(f"     details exclusion: {cres.category.id} {cres.effective_count}/{cres.file_count}")
        if cres.effective_count != cres.file_count - 1:
            raise AssertionError("исключение файла не учтено")
        cres.excluded = set()
        cres.selected = False
        win.cleaner._populate()

    _step("details exclusion", details_exclusion)

    def winapp2_rules():
        # Пользовательский winapp2.ini не трогаем: правила подключаются только в памяти.
        from app.core import categories as core_cats
        from app.core import winapp2

        sample = (
            "[DiskCleaner Selftest Temp *]\n"
            "DetectFile=%LocalAppData%\n"
            "FileKey1=%LocalAppData%\\Temp|*.tmp|RECURSE\n"
            "[DiskCleaner Evil Profile *]\n"
            "FileKey1=%UserProfile%|*.*|RECURSE\n"
            "[DiskCleaner Evil Documents *]\n"
            "FileKey1=%Documents%|*.*|RECURSE\n"
            "[DiskCleaner Registry *]\n"
            "RegKey1=HKCU\\Software\\X\n"
        )
        parsed = winapp2.parse_text(sample)
        _log(f"     winapp2 sample: categories={len(parsed.categories)}, registry_only={parsed.registry_only}, "
             f"unsupported={parsed.unsupported_keys}")
        previous = list(core_cats._EXTRA_CATEGORIES)
        core_cats.set_extra_categories(parsed.categories)
        try:
            win._scan_from_dashboard(win.dashboard.current_drive())
            if not _wait(app, lambda: not win.cleaner.busy, 300):
                raise TimeoutError("scan with winapp2 > 300s")
            r = win.cleaner.result
            evil = [c for c in r.categories if c.category.title == "DiskCleaner Evil Profile"]
            if not evil or not evil[0].is_empty:
                raise AssertionError("опасное правило %UserProfile% не было отклонено")
            temp = [c for c in r.categories if c.category.title == "DiskCleaner Selftest Temp"]
            _log(f"     winapp2 temp rule: {temp[0].file_count if temp else 'n/a'} files "
                 "(дубликаты встроенной категории %TEMP% не повторяются)")
            groups = [win.cleaner.tree.topLevelItem(i).text(0) for i in range(win.cleaner.tree.topLevelItemCount())]
            _log(f"     groups: {groups}")
        finally:
            core_cats.set_extra_categories(previous)

    _step("winapp2 rules", winapp2_rules)
    _shot(app, win, out, "dark_1_cleaner_winapp2")

    def history_page():
        from app.core import history as core_history

        store = core_history.get_history()
        store.clear()  # песочница самопроверки: начинаем с пустой истории
        store.add(core_history.HistoryEntry(timestamp=time.time() - 3600, kind="junk", deleted=1287,
                                            freed=441 * 1024 * 1024, drive="C:\\", categories=["chrome", "discord"]))
        store.add(core_history.HistoryEntry(timestamp=time.time(), kind="large", deleted=1,
                                            freed=120 * 1024 * 1024, drive=str(sandbox), mode="recycle"))
        win.go_to(5)
        win.history.refresh()
        if win.history.tree.topLevelItemCount() != 2:
            raise AssertionError("история не отображается")

    _step("history page", history_page)

    def quarantine_roundtrip():
        from app.core.quarantine import get_quarantine

        victim = sandbox / "qtest" / "cache.bin"
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_bytes(b"x" * 4096)
        writer = get_quarantine().new_session(["chrome"])
        if writer.move(str(victim), "chrome") is not None or victim.exists():
            raise AssertionError("файл не перенесён в карантин")
        writer.save()
        win.go_to(6)
        win.quarantine.refresh()
        if win.quarantine.tree.topLevelItemCount() != 1:
            raise AssertionError("сессия карантина не отображается")
        _shot(app, win, out, "dark_6_quarantine")
        session = get_quarantine().sessions()[0]
        rep = get_quarantine().restore(session)
        if rep.restored != 1 or not victim.exists():
            raise AssertionError("восстановление из карантина не сработало")
        _log("     quarantine: move + restore OK")

    _step("quarantine roundtrip", quarantine_roundtrip)
    _shot(app, win, out, "dark_5_history")

    # --- анализ места: сначала песочница (с проверкой сумм), затем весь системный диск
    def analyzer():
        win.go_to(2)
        page = win.analyzer
        page.root_combo.insertItem(0, str(sandbox), str(sandbox))
        page.root_combo.setCurrentIndex(0)
        page.start()
        if not _wait(app, lambda: not page.busy, 60):
            raise TimeoutError("analyzer sandbox > 60s")
        expected = sum(p.stat().st_size for p in sandbox.rglob("*") if p.is_file())
        got = page.result.root.size if page.result else -1
        _log(f"     analyzer sandbox: {format_size(got)} (expected {format_size(expected)})")
        if got != expected:
            raise AssertionError(f"размер песочницы {got} != {expected}")
        top = page.tree.topLevelItem(0)
        if top is None or top.childCount() == 0:
            raise AssertionError("дерево анализатора пустое")

    _step("analyzer sandbox", analyzer)
    _shot(app, win, out, "dark_2_analyzer_sandbox")

    def treemap_check():
        from PySide6.QtCore import Qt

        from app.core.space_analyzer import DirNode

        page = win.analyzer
        tm = page.treemap
        app.processEvents()
        root = page.result.root if page.result else None
        if root is None or tm.view is not root:
            raise AssertionError("treemap не показывает корень анализа")
        keys = tm.visible_keys()
        if not keys:
            raise AssertionError("treemap пуст")
        area = sum(r.area for r in tm._rects)
        full = tm.width() * tm.height()
        if full <= 0 or abs(area - full) > full * 0.001:
            raise AssertionError(f"площадь treemap {area:.0f} != {full}")
        for r in tm._rects:
            if r.x < -0.5 or r.y < -0.5 or r.x + r.w > tm.width() + 0.5 or r.y + r.h > tm.height() + 0.5:
                raise AssertionError(f"прямоугольник за границей: {r}")
        dirs = [k for k in keys if isinstance(k, DirNode)]
        if not dirs:
            raise AssertionError("в treemap нет папок")
        biggest = max(dirs, key=lambda d: d.size)
        if tm.rect_for(biggest) is not max((tm.rect_for(d) for d in dirs), key=lambda r: r.area):
            raise AssertionError("самая большая папка не самый большой прямоугольник")
        # клик -> выделение в дереве
        page._on_map_selected(biggest)
        cur = page.tree.currentItem()
        if cur is None or cur.data(0, Qt.ItemDataRole.UserRole + 1) != biggest.path:
            raise AssertionError("клик по treemap не выделил папку в дереве")
        target = next((d for d in sorted(dirs, key=lambda d: -d.size) if d.children), None)
        if target is not None:
            page._on_map_activated(target)
            app.processEvents()
            if tm.view is not target or not page.up_btn.isEnabled():
                raise AssertionError("двойной клик не зашёл в папку")
            _shot(app, win, out, "dark_2_treemap_inside")
            page._treemap_up()
            if tm.view is not root or page.up_btn.isEnabled():
                raise AssertionError("кнопка «Вверх» не вернула к корню")
        _log(f"     treemap: {len(keys)} blocks, drill/up/tree sync OK")

    _step("treemap", treemap_check)

    def analyzer_drive():
        page = win.analyzer
        page.root_combo.setCurrentIndex(page.root_combo.findData(win.dashboard.current_drive()))
        t0 = time.monotonic()
        page.start()
        if not _wait(app, lambda: not page.busy, 170):
            page._cancel()
            _wait(app, lambda: not page.busy, 30)
            _log("     analyzer drive: cancelled after 170s")
        r = page.result
        if r is None:
            raise RuntimeError("no analyzer result")
        _log(f"     analyzer drive: {format_size(r.root.size)}, files={r.scanned_files}, dirs={r.scanned_dirs}, "
             f"errors={r.errors}, links={r.skipped_links}, cloud={r.cloud_only_files}, {time.monotonic()-t0:.1f}s")
        top = page.tree.topLevelItem(0)
        if top is not None and top.childCount():
            top.child(0).setExpanded(True)

    _step("analyzer system drive", analyzer_drive)
    _shot(app, win, out, "dark_2_analyzer_drive")

    # --- большие файлы во временной папке
    def large():
        win.go_to(3)
        win.large.root_combo.insertItem(0, str(sandbox), str(sandbox))
        win.large.root_combo.setCurrentIndex(0)
        win.large.threshold_combo.setCurrentIndex(0)  # > 100 MB
        win.large.start_scan()
        if not _wait(app, lambda: not win.large.busy, 60):
            raise TimeoutError("large files > 60s")
        n = len(win.large.model.items())
        _log(f"     large files found: {n}")
        if n != 1:
            raise AssertionError(f"ожидался 1 большой файл, найдено {n}")
        win.large.model.set_all_checked(True)

    _step("large files", large)
    _shot(app, win, out, "dark_3_large_found")

    # --- дубликаты во временной папке
    def dups():
        win.go_to(4)
        win.duplicates.folders.clear()
        win.duplicates.folders.addItem(str(sandbox))
        win.duplicates.min_size.setCurrentIndex(0)  # > 100 KB
        win.duplicates.start_scan()
        if not _wait(app, lambda: not win.duplicates.busy, 60):
            raise TimeoutError("duplicates > 60s")
        res = win.duplicates.result
        groups = len(res.groups) if res else -1
        _log(f"     duplicate groups: {groups}")
        if groups != 1 or res.groups[0].count != 3:
            raise AssertionError(f"ожидалась 1 группа из 3 файлов, получено {groups}")
        win.duplicates._auto_select(keep_oldest=True)
        if len(win.duplicates._checked) != 2:
            raise AssertionError("авто-выбор должен отметить 2 копии из 3")

    _step("duplicates", dups)
    _shot(app, win, out, "dark_4_duplicates_found")
    # Восстановить список папок пользователя (start_scan сохранил временную папку).
    settings.duplicate_folders = []

    # --- светлая тема
    theme.apply_theme(app, "light")
    for i, n in enumerate(names):
        win.go_to(i)
        _shot(app, win, out, f"light_{i}_{n}")

    _step("dialogs light", dialogs)
    _step("close window", lambda: (setattr(win, "_force_close", True), win.close()))
