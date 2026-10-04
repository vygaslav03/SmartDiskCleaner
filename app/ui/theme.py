"""Тёмная и светлая темы (Fusion + QSS) в стиле современных утилит Windows 11."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from app.utils.winpaths import IS_WINDOWS

DARK = {
    "bg": "#1c1d21",
    "sidebar": "#16171a",
    "surface": "#25272c",
    "surface2": "#2d3036",
    "border": "#383b42",
    "text": "#e8e9ec",
    "muted": "#9aa0aa",
    "accent": "#4f8cff",
    "accent_hover": "#6b9fff",
    "accent_text": "#ffffff",
    "danger": "#e5484d",
    "danger_hover": "#f2555a",
    "success": "#3dd68c",
    "warning": "#f5b041",
    "selection": "#2f4a7a",
    "track": "#33363d",
    "check_border": "#6b707b",
}

LIGHT = {
    "bg": "#f3f4f7",
    "sidebar": "#e9ebf0",
    "surface": "#ffffff",
    "surface2": "#f6f7f9",
    "border": "#dcdfe5",
    "text": "#1d2129",
    "muted": "#5f6672",
    "accent": "#2563eb",
    "accent_hover": "#3b76f0",
    "accent_text": "#ffffff",
    "danger": "#d93036",
    "danger_hover": "#e5484d",
    "success": "#1a8a52",
    "warning": "#b7791f",
    "selection": "#cfe0ff",
    "track": "#e3e6eb",
    "check_border": "#8a909b",
}

_current = {"name": "dark", "colors": DARK}


def system_prefers_dark() -> bool:
    hints = QGuiApplication.styleHints()
    scheme_getter = getattr(hints, "colorScheme", None)
    if scheme_getter is not None:
        try:
            scheme = scheme_getter()
            if scheme == Qt.ColorScheme.Dark:
                return True
            if scheme == Qt.ColorScheme.Light:
                return False
        except (AttributeError, RuntimeError):
            pass
    if IS_WINDOWS:
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(value) == 0
        except OSError:
            pass
    return False


def colors() -> dict[str, str]:
    return _current["colors"]


def is_dark() -> bool:
    return _current["name"] == "dark"


def _asset_url(name: str) -> str:
    from app.utils.paths import resource_path

    return resource_path(f"assets/{name}").as_posix()


def _stylesheet(c: dict[str, str]) -> str:
    check = _asset_url("check.png")
    partial = _asset_url("partial.png")
    return f"""
    QWidget {{
        background-color: {c['bg']};
        color: {c['text']};
        font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif;
        font-size: 10pt;
    }}
    QToolTip {{
        background-color: {c['surface2']}; color: {c['text']};
        border: 1px solid {c['border']}; padding: 6px; border-radius: 6px;
    }}
    #Sidebar {{ background-color: {c['sidebar']}; border-right: 1px solid {c['border']}; }}
    #Sidebar QLabel {{ background: transparent; }}
    #AppTitle {{ font-size: 15pt; font-weight: 600; padding: 4px 8px 16px 8px; }}
    QPushButton#NavButton {{
        text-align: left; padding: 10px 14px; border: none; border-radius: 8px;
        background: transparent; color: {c['muted']}; font-size: 10.5pt;
    }}
    QPushButton#NavButton:hover {{ background-color: {c['surface']}; color: {c['text']}; }}
    QPushButton#NavButton:checked {{
        background-color: {c['surface2']}; color: {c['text']};
        border-left: 3px solid {c['accent']}; font-weight: 600;
    }}
    #PageTitle {{ font-size: 20pt; font-weight: 600; background: transparent; }}
    #PageSubtitle, #Muted {{ color: {c['muted']}; background: transparent; }}
    #Card {{
        background-color: {c['surface']}; border: 1px solid {c['border']}; border-radius: 12px;
    }}
    #Card QLabel, #Card QCheckBox {{ background: transparent; }}
    #CardTitle {{ color: {c['muted']}; font-size: 9.5pt; }}
    #CardValue {{ font-size: 18pt; font-weight: 600; }}
    #BigValue {{ font-size: 26pt; font-weight: 700; color: {c['accent']}; }}
    #Warning {{ color: {c['warning']}; background: transparent; }}
    #Success {{ color: {c['success']}; background: transparent; }}
    #Danger {{ color: {c['danger']}; background: transparent; }}

    QPushButton {{
        background-color: {c['surface2']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 8px; padding: 8px 16px;
    }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:pressed {{ background-color: {c['border']}; }}
    QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; background-color: {c['surface']}; }}
    QPushButton#Primary {{
        background-color: {c['accent']}; color: {c['accent_text']}; border: none; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background-color: {c['accent_hover']}; }}
    QPushButton#Primary:disabled {{ background-color: {c['track']}; color: {c['muted']}; }}
    QPushButton#DangerButton {{
        background-color: {c['danger']}; color: #ffffff; border: none; font-weight: 600;
    }}
    QPushButton#DangerButton:hover {{ background-color: {c['danger_hover']}; }}
    QPushButton#DangerButton:disabled {{ background-color: {c['track']}; color: {c['muted']}; }}

    QProgressBar {{
        background-color: {c['track']}; border: none; border-radius: 5px;
        height: 10px; text-align: center; color: transparent;
    }}
    QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 5px; }}
    QProgressBar#UsageBar {{ height: 14px; border-radius: 7px; }}
    QProgressBar#UsageBar::chunk {{ border-radius: 7px; }}

    QTreeWidget, QTreeView, QTableView, QListWidget {{
        background-color: {c['surface']}; alternate-background-color: {c['surface2']};
        border: 1px solid {c['border']}; border-radius: 10px;
        selection-background-color: {c['selection']}; selection-color: {c['text']};
        outline: 0;
    }}
    QTreeView::item, QTableView::item, QListWidget::item {{ padding: 4px 2px; }}
    QHeaderView::section {{
        background-color: {c['surface2']}; color: {c['muted']};
        border: none; border-bottom: 1px solid {c['border']}; padding: 6px 8px; font-weight: 600;
    }}
    QTableCornerButton::section {{ background-color: {c['surface2']}; border: none; }}

    QLineEdit, QComboBox, QSpinBox {{
        background-color: {c['surface']}; border: 1px solid {c['border']};
        border-radius: 8px; padding: 6px 10px; min-height: 20px;
    }}
    QComboBox:hover, QSpinBox:hover, QLineEdit:focus {{ border-color: {c['accent']}; }}
    QComboBox QAbstractItemView {{
        background-color: {c['surface']}; border: 1px solid {c['border']};
        selection-background-color: {c['selection']};
    }}
    QCheckBox {{ spacing: 8px; background: transparent; }}
    QCheckBox::indicator, QTreeView::indicator, QTableView::indicator, QListView::indicator {{
        width: 16px; height: 16px; border: 1px solid {c['check_border']};
        border-radius: 4px; background-color: {c['surface']};
    }}
    QCheckBox::indicator:hover, QTreeView::indicator:hover, QTableView::indicator:hover {{
        border-color: {c['accent']};
    }}
    QCheckBox::indicator:checked, QTreeView::indicator:checked, QTableView::indicator:checked,
    QListView::indicator:checked {{
        background-color: {c['accent']}; border-color: {c['accent']}; image: url("{check}");
    }}
    QCheckBox::indicator:indeterminate, QTreeView::indicator:indeterminate,
    QTableView::indicator:indeterminate {{
        background-color: {c['accent']}; border-color: {c['accent']}; image: url("{partial}");
    }}
    QCheckBox::indicator:disabled, QTreeView::indicator:disabled {{
        background-color: {c['track']}; border-color: {c['border']};
    }}
    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 4px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QDialog, QMessageBox {{ background-color: {c['bg']}; }}
    QStatusBar {{ background-color: {c['sidebar']}; color: {c['muted']}; }}
    """


def _palette(c: dict[str, str]) -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(c["bg"]))
    p.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.Base, QColor(c["surface"]))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(c["surface2"]))
    p.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.Button, QColor(c["surface2"]))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.Highlight, QColor(c["accent"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(c["accent_text"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["surface2"]))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    p.setColor(QPalette.ColorRole.Link, QColor(c["accent"]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["muted"]))
    return p


def apply_theme(app: QApplication, theme: str) -> str:
    """theme: 'system' | 'dark' | 'light'. Возвращает фактически применённую тему."""
    name = theme
    if theme == "system":
        name = "dark" if system_prefers_dark() else "light"
    c = DARK if name == "dark" else LIGHT
    _current["name"] = name
    _current["colors"] = c
    app.setStyle("Fusion")
    app.setPalette(_palette(c))
    app.setStyleSheet(_stylesheet(c))
    return name
