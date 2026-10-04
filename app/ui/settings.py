"""Страница «Настройки»."""

from __future__ import annotations

import copy
import os

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.utils.i18n import tr
from app.utils.paths import logs_dir
from app.utils.settings_store import Settings, save_settings
from app.ui.widgets import Card, page_header


class SettingsPage(QWidget):
    settings_changed = Signal(object)  # Settings

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        header, _ = page_header(tr("set.title"), tr("set.subtitle"))
        root.addWidget(header)

        # ---- внешний вид
        look = Card()
        look.layout_.addWidget(self._section(tr("set.appearance")))
        form = QFormLayout()
        self.language = QComboBox()
        for code, label in (("ru", "Русский"), ("uk", "Українська"), ("en", "English")):
            self.language.addItem(label, code)
        self.theme = QComboBox()
        for code in ("system", "dark", "light"):
            self.theme.addItem(tr(f"set.theme.{code}"), code)
        form.addRow(tr("set.language"), self.language)
        form.addRow(tr("set.theme"), self.theme)
        look.layout_.addLayout(form)
        root.addWidget(look)

        # ---- сканирование
        scan = Card()
        scan.layout_.addWidget(self._section(tr("set.scanning")))
        sform = QFormLayout()
        self.temp_age = QSpinBox()
        self.temp_age.setRange(0, 24 * 30)
        self.temp_age.setSuffix(" " + tr("unit.hours"))
        self.log_age = QSpinBox()
        self.log_age.setRange(0, 365)
        self.log_age.setSuffix(" " + tr("unit.days"))
        self.large_threshold = QSpinBox()
        self.large_threshold.setRange(10, 1024 * 1024)
        self.large_threshold.setSingleStep(100)
        self.large_threshold.setSuffix(" MB")
        self.large_max = QSpinBox()
        self.large_max.setRange(100, 50000)
        self.large_max.setSingleStep(500)
        self.dup_min = QSpinBox()
        self.dup_min.setRange(1, 1024 * 1024)
        self.dup_min.setSuffix(" KB")
        for spin in (self.temp_age, self.log_age, self.large_threshold, self.large_max, self.dup_min):
            # Кнопки +/- почти не видны в Fusion+QSS; значение вводится с клавиатуры или колесом.
            spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            spin.setMaximumWidth(220)
        sform.addRow(tr("set.temp_age"), self.temp_age)
        sform.addRow(tr("set.log_age"), self.log_age)
        sform.addRow(tr("set.large_threshold"), self.large_threshold)
        sform.addRow(tr("set.large_max"), self.large_max)
        sform.addRow(tr("set.dup_min"), self.dup_min)
        scan.layout_.addLayout(sform)
        hint = QLabel(tr("set.temp_age_hint"))
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        scan.layout_.addWidget(hint)
        root.addWidget(scan)

        # ---- безопасность
        safe = Card()
        safe.layout_.addWidget(self._section(tr("set.safety")))
        self.delete_mode = QComboBox()
        self.delete_mode.addItem(tr("set.mode.recycle"), "recycle")
        self.delete_mode.addItem(tr("set.mode.permanent"), "permanent")
        mrow = QFormLayout()
        mrow.addRow(tr("set.delete_mode"), self.delete_mode)
        safe.layout_.addLayout(mrow)
        self.q_enabled = QCheckBox(tr("set.quarantine"))
        safe.layout_.addWidget(self.q_enabled)
        qrow = QFormLayout()
        self.q_days = QSpinBox()
        self.q_days.setRange(1, 90)
        self.q_days.setSuffix(" " + tr("unit.days"))
        self.q_days.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.q_days.setMaximumWidth(220)
        qrow.addRow(tr("set.quarantine_days"), self.q_days)
        safe.layout_.addLayout(qrow)
        q_hint = QLabel(tr("set.quarantine_hint"))
        q_hint.setObjectName("Muted")
        q_hint.setWordWrap(True)
        safe.layout_.addWidget(q_hint)
        self.q_enabled.toggled.connect(self.q_days.setEnabled)
        self.allow_pf = QCheckBox(tr("set.allow_pf"))
        safe.layout_.addWidget(self.allow_pf)
        pf_hint = QLabel(tr("set.allow_pf_hint"))
        pf_hint.setObjectName("Muted")
        pf_hint.setWordWrap(True)
        safe.layout_.addWidget(pf_hint)
        self.fast_analysis = QCheckBox(tr("set.fast_analysis"))
        safe.layout_.addWidget(self.fast_analysis)
        fa_hint = QLabel(tr("set.fast_analysis_hint"))
        fa_hint.setObjectName("Muted")
        fa_hint.setWordWrap(True)
        safe.layout_.addWidget(fa_hint)

        safe.layout_.addWidget(self._section(tr("set.exclusions")))
        ex_hint = QLabel(tr("set.exclusions_hint"))
        ex_hint.setObjectName("Muted")
        ex_hint.setWordWrap(True)
        safe.layout_.addWidget(ex_hint)
        erow = QHBoxLayout()
        self.exclusions = QListWidget()
        self.exclusions.setMaximumHeight(120)
        ebtns = QVBoxLayout()
        add = QPushButton(tr("btn.add_folder"))
        add.clicked.connect(self._add_exclusion)
        rm = QPushButton(tr("btn.remove"))
        rm.clicked.connect(self._remove_exclusion)
        ebtns.addWidget(add)
        ebtns.addWidget(rm)
        ebtns.addStretch(1)
        erow.addWidget(self.exclusions, 1)
        erow.addLayout(ebtns)
        safe.layout_.addLayout(erow)
        root.addWidget(safe)

        # ---- о программе
        # ---- Winapp2.ini
        wa = Card()
        wa.layout_.addWidget(self._section(tr("set.winapp2")))
        wa_hint = QLabel(tr("set.winapp2_hint"))
        wa_hint.setObjectName("Muted")
        wa_hint.setWordWrap(True)
        wa_hint.setOpenExternalLinks(False)
        wa.layout_.addWidget(wa_hint)
        self.wa_status = QLabel()
        self.wa_status.setWordWrap(True)
        wa.layout_.addWidget(self.wa_status)
        wa_row = QHBoxLayout()
        wa_import = QPushButton(tr("set.winapp2_import"))
        wa_import.clicked.connect(self._import_winapp2)
        self.wa_remove = QPushButton(tr("set.winapp2_remove"))
        self.wa_remove.clicked.connect(self._remove_winapp2)
        wa_row.addWidget(wa_import)
        wa_row.addWidget(self.wa_remove)
        wa_row.addStretch(1)
        wa.layout_.addLayout(wa_row)
        root.addWidget(wa)
        self._refresh_winapp2()

        about = Card()
        about.layout_.addWidget(self._section(tr("set.about")))
        info = QLabel(tr("set.about_text", version=__version__))
        info.setWordWrap(True)
        about.layout_.addWidget(info)
        logs_btn = QPushButton(tr("set.open_logs"))
        logs_btn.clicked.connect(self._open_logs)
        about.layout_.addWidget(logs_btn)
        root.addWidget(about)

        brow = QHBoxLayout()
        brow.addStretch(1)
        reset = QPushButton(tr("btn.revert"))
        reset.clicked.connect(lambda: self.load(self.settings))
        save = QPushButton(tr("btn.save"))
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        brow.addWidget(reset)
        brow.addWidget(save)
        root.addLayout(brow)
        root.addStretch(1)

        self.load(settings)

    @staticmethod
    def _section(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("CardValue")
        lbl.setStyleSheet("font-size: 12pt;")
        return lbl

    def load(self, s: Settings) -> None:
        self.language.setCurrentIndex(max(0, self.language.findData(s.language)))
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        self.temp_age.setValue(s.temp_min_age_hours)
        self.log_age.setValue(s.log_min_age_days)
        self.large_threshold.setValue(s.large_file_threshold_mb)
        self.large_max.setValue(s.large_files_max_results)
        self.dup_min.setValue(s.duplicates_min_size_kb)
        self.delete_mode.setCurrentIndex(max(0, self.delete_mode.findData(s.user_files_delete_mode)))
        self.allow_pf.setChecked(s.allow_program_files)
        self.q_enabled.setChecked(s.quarantine_enabled)
        self.q_days.setValue(s.quarantine_days)
        self.fast_analysis.setChecked(s.fast_analysis)
        self.q_days.setEnabled(s.quarantine_enabled)
        self.exclusions.clear()
        for e in s.exclusions:
            self.exclusions.addItem(e)

    def _add_exclusion(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("set.exclusions"))
        if folder:
            folder = os.path.normpath(folder)
            existing = [self.exclusions.item(i).text() for i in range(self.exclusions.count())]
            if folder not in existing:
                self.exclusions.addItem(folder)

    def _remove_exclusion(self) -> None:
        for it in self.exclusions.selectedItems():
            self.exclusions.takeItem(self.exclusions.row(it))

    def _refresh_winapp2(self) -> None:
        from app.core import winapp2

        res = winapp2.load_imported()
        if not res.categories:
            self.wa_status.setText(tr("set.winapp2_status_none"))
            self.wa_remove.setEnabled(False)
            return
        self.wa_status.setText(
            tr("set.winapp2_status", entries=len(res.categories), registry=res.registry_only,
               unsupported=res.unsupported_keys)
        )
        self.wa_remove.setEnabled(True)

    def _import_winapp2(self) -> None:
        from pathlib import Path

        from app.core import winapp2
        from app.core.categories import set_extra_categories

        path, _ = QFileDialog.getOpenFileName(self, tr("set.winapp2_import"), "", "Winapp2 (*.ini);;*.*")
        if not path:
            return
        try:
            res = winapp2.import_file(Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("set.winapp2"), tr("set.winapp2_failed", error=str(exc)))
            return
        set_extra_categories(res.categories)
        self._refresh_winapp2()
        QMessageBox.information(self, tr("set.winapp2"), tr("set.winapp2_imported", entries=len(res.categories)))

    def _remove_winapp2(self) -> None:
        from app.core import winapp2
        from app.core.categories import set_extra_categories

        answer = QMessageBox.question(
            self, tr("set.winapp2"), tr("set.winapp2_remove_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        winapp2.remove_imported()
        set_extra_categories([])
        self._refresh_winapp2()

    def _open_logs(self) -> None:
        path = logs_dir()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _save(self) -> None:
        if self.allow_pf.isChecked() and not self.settings.allow_program_files:
            answer = QMessageBox.warning(
                self,
                tr("set.allow_pf"),
                tr("set.allow_pf_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.allow_pf.setChecked(False)
        if self.delete_mode.currentData() == "permanent" and self.settings.user_files_delete_mode != "permanent":
            answer = QMessageBox.warning(
                self,
                tr("set.delete_mode"),
                tr("set.permanent_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.delete_mode.setCurrentIndex(self.delete_mode.findData("recycle"))

        new = copy.deepcopy(self.settings)
        old_language = new.language
        new.language = self.language.currentData()
        new.theme = self.theme.currentData()
        new.temp_min_age_hours = self.temp_age.value()
        new.log_min_age_days = self.log_age.value()
        new.large_file_threshold_mb = self.large_threshold.value()
        new.large_files_max_results = self.large_max.value()
        new.duplicates_min_size_kb = self.dup_min.value()
        new.user_files_delete_mode = self.delete_mode.currentData()
        new.allow_program_files = self.allow_pf.isChecked()
        new.quarantine_enabled = self.q_enabled.isChecked()
        new.quarantine_days = self.q_days.value()
        new.fast_analysis = self.fast_analysis.isChecked()
        new.exclusions = [self.exclusions.item(i).text() for i in range(self.exclusions.count())]
        new.validate()
        if not save_settings(new):
            QMessageBox.critical(self, tr("error.title"), tr("set.save_failed"))
            return
        # Общий объект настроек используется всеми страницами — обновляем его на месте.
        vars(self.settings).update(vars(new))
        self.settings_changed.emit(self.settings)
        msg = tr("set.saved")
        if new.language != old_language:
            msg += "\n" + tr("set.restart_language")
        QMessageBox.information(self, tr("set.title"), msg)
