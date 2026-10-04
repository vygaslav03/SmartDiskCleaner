"""Запуск приложения DiskCleaner."""

from __future__ import annotations

import platform
import sys
import traceback


def _install_excepthook() -> None:
    """Необработанные исключения пишутся в лог и показываются, а не роняют программу молча."""
    from app.utils.logger import get_logger

    log = get_logger("crash")

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("Необработанное исключение:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            from app.utils.i18n import tr

            if QApplication.instance() is not None:
                QMessageBox.critical(None, tr("error.title"), tr("error.unexpected", error=str(exc)))
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def _set_app_user_model_id() -> None:
    """Отдельная иконка в панели задач Windows (а не иконка python.exe)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DiskCleaner.App.1")
    except (OSError, AttributeError):
        pass


def main() -> int:
    if "--selftest" in sys.argv:
        from app.selftest import run_selftest

        return run_selftest()

    from app import __app_name__, __version__
    from app.utils.i18n import set_language
    from app.utils.logger import get_logger, setup_logging
    from app.utils.paths import logs_dir
    from app.utils.permissions import is_admin
    from app.utils.settings_store import load_settings

    setup_logging(logs_dir())
    log = get_logger("main")
    log.info(
        "Запуск %s %s | Python %s | %s %s | admin=%s",
        __app_name__, __version__, platform.python_version(), platform.system(), platform.release(), is_admin(),
    )
    _install_excepthook()
    _set_app_user_model_id()

    settings = load_settings()
    set_language(settings.language)
    try:
        from app.core import winapp2
        from app.core.categories import set_extra_categories

        set_extra_categories(winapp2.load_imported().categories)
    except Exception:  # noqa: BLE001 - повреждённый импорт не должен мешать запуску
        log.exception("Не удалось загрузить winapp2.ini")
    try:
        from app.core.quarantine import get_quarantine

        get_quarantine().purge_expired(settings.quarantine_days)
    except Exception:  # noqa: BLE001 - карантин не должен мешать запуску
        log.exception("Не удалось очистить просроченный карантин")

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from app.ui import theme
    from app.ui.main_window import MainWindow
    from app.utils.paths import resource_path

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)
    icon = resource_path("assets/icon.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    theme.apply_theme(app, settings.theme)

    window = MainWindow(settings)
    window.show()
    code = app.exec()
    log.info("Завершение работы, код %s", code)
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
