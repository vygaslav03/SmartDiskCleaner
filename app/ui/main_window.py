"""Главное окно: боковая панель навигации + стек страниц."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from app import __app_name__, __version__
from app.utils.i18n import tr
from app.utils.logger import get_logger
from app.utils.paths import resource_path
from app.utils.permissions import is_admin, relaunch_as_admin
from app.utils.settings_store import Settings
from app.ui import theme
from app.ui.analyzer import AnalyzerPage
from app.ui.cleaner import CleanerPage
from app.ui.dashboard import DashboardPage
from app.ui.duplicates import DuplicatesPage
from app.ui.history import HistoryPage
from app.ui.large_files import LargeFilesPage
from app.ui.quarantine import QuarantinePage
from app.ui.settings import SettingsPage

log = get_logger("ui.main")

PAGE_DASHBOARD, PAGE_CLEANER, PAGE_ANALYZER, PAGE_LARGE, PAGE_DUPLICATES, PAGE_HISTORY, PAGE_QUARANTINE, PAGE_SETTINGS = range(8)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle(f"{__app_name__} {__version__}" + (f" — {tr('admin.window_suffix')}" if is_admin() else ""))
        icon_path = resource_path("assets/icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1180, 780)
        self.setMinimumSize(960, 640)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- sidebar
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(230)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 18, 12, 14)
        side.setSpacing(4)
        title = QLabel(__app_name__)
        title.setObjectName("AppTitle")
        side.addWidget(title)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for idx, key in enumerate(("nav.dashboard", "nav.cleaner", "nav.analyzer", "nav.large", "nav.duplicates", "nav.history", "nav.quarantine", "nav.settings")):
            btn = QPushButton(tr(key))
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav_group.addButton(btn, idx)
            side.addWidget(btn)
        self.nav_group.idClicked.connect(self.go_to)
        side.addStretch(1)

        if not is_admin():
            self.admin_btn = QPushButton(tr("admin.restart_short"))
            self.admin_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_VistaShield))
            self.admin_btn.setToolTip(tr("admin.why"))
            self.admin_btn.clicked.connect(self.request_elevation)
            side.addWidget(self.admin_btn)
        else:
            lbl = QLabel(tr("admin.running"))
            lbl.setObjectName("Muted")
            side.addWidget(lbl)
        privacy = QLabel(tr("app.privacy_short"))
        privacy.setObjectName("Muted")
        privacy.setWordWrap(True)
        side.addWidget(privacy)
        layout.addWidget(sidebar)

        # ---- pages
        self.stack = QStackedWidget()
        self.dashboard = DashboardPage()
        self.cleaner = CleanerPage(settings)
        self.analyzer = AnalyzerPage()
        self.large = LargeFilesPage(settings)
        self.duplicates = DuplicatesPage(settings)
        self.history = HistoryPage()
        self.quarantine = QuarantinePage(settings)
        self.settings_page = SettingsPage(settings)
        for page in (self.dashboard, self.cleaner, self.analyzer, self.large, self.duplicates, self.history, self.quarantine, self.settings_page):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)

        drive = self.dashboard.current_drive()
        self.cleaner.set_drive(drive)
        self.large.set_drive(drive)
        self.analyzer.set_drive(drive)

        # ---- связи между страницами
        self.dashboard.scan_requested.connect(self._scan_from_dashboard)
        self.dashboard.smart_clean_requested.connect(self._smart_from_dashboard)
        self.dashboard.drive_changed.connect(self._on_drive_changed)
        self.cleaner.scan_started.connect(lambda: self.dashboard.set_busy(True))
        self.cleaner.scan_finished.connect(self._on_scan_finished)
        self.cleaner.clean_finished.connect(
            lambda r: self.dashboard.on_cleaned(r.freed_bytes, r.quarantined_bytes)
        )
        self.cleaner.clean_finished.connect(lambda _r: self.history.refresh())
        self.cleaner.clean_finished.connect(lambda _r: self.quarantine.refresh())
        self.quarantine.space_freed.connect(lambda freed: self.dashboard.refresh_disk_info())
        self.large.files_deleted.connect(lambda _r: self.history.refresh())
        self.duplicates.files_deleted.connect(lambda _r: self.history.refresh())
        self.cleaner.restart_requested.connect(self.request_elevation)
        self.large.files_deleted.connect(lambda r: self.dashboard.on_cleaned(r.freed_bytes))
        self.duplicates.files_deleted.connect(lambda r: self.dashboard.on_cleaned(r.freed_bytes))
        self.settings_page.settings_changed.connect(self._on_settings_changed)
        self.analyzer.find_large_requested.connect(self._find_large_in)

        self.statusBar().showMessage(tr("app.ready"))
        self.go_to(PAGE_DASHBOARD)

    # ------------------------------------------------------------------ nav
    def go_to(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        btn = self.nav_group.button(index)
        if btn:
            btn.setChecked(True)

    def _scan_from_dashboard(self, drive: str) -> None:
        self.go_to(PAGE_CLEANER)
        self.cleaner.start_scan(drive)

    def _smart_from_dashboard(self, drive: str) -> None:
        self.go_to(PAGE_CLEANER)
        self.cleaner.start_scan(drive, smart=True)

    def _on_drive_changed(self, drive: str) -> None:
        self.cleaner.set_drive(drive)
        self.large.set_drive(drive)
        self.analyzer.set_drive(drive)

    def _find_large_in(self, path: str) -> None:
        self.go_to(PAGE_LARGE)
        self.large.set_root(path)

    def _on_scan_finished(self, result) -> None:
        self.dashboard.set_busy(False)
        self.dashboard.on_scan_finished(result)

    def _on_settings_changed(self, settings: Settings) -> None:
        self.settings = settings
        for page in (self.cleaner, self.large, self.duplicates, self.quarantine):
            page.update_settings(settings)
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, settings.theme)
        log.info("Настройки изменены")

    # ------------------------------------------------------------ elevation
    def _any_busy(self) -> bool:
        return (self.cleaner.busy or self.analyzer.busy or self.large.busy or self.duplicates.busy
                or self.quarantine.busy)

    def request_elevation(self) -> None:
        if self._any_busy():
            QMessageBox.information(self, tr("admin.title"), tr("admin.busy"))
            return
        answer = QMessageBox.question(
            self,
            tr("admin.title"),
            tr("admin.why"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if relaunch_as_admin():
            self._force_close = True
            self.close()
        else:
            QMessageBox.warning(self, tr("admin.title"), tr("admin.failed"))

    # ---------------------------------------------------------------- close
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._any_busy() and not getattr(self, "_force_close", False):
            answer = QMessageBox.question(
                self,
                __app_name__,
                tr("app.confirm_exit_busy"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        for page in (self.cleaner, self.analyzer, self.large, self.duplicates, self.quarantine):
            page.shutdown()
        log.info("Приложение закрыто")
        event.accept()
