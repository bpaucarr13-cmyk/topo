import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QComboBox, QCheckBox, QDialogButtonBox, QLabel,
    QLineEdit, QPushButton, QHBoxLayout, QWidget, QFileDialog
)

from .topofast_dialog import _sync_taskbar_group

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class TopoFastExportDialog(QDialog):
    def __init__(self, settings, default_dir, report_text, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setWindowTitle("TopoFast - Resultado")
        self.setMinimumWidth(420)
        _sync_taskbar_group(self, parent)
        layout = QFormLayout(self)

        report_label = QLabel(report_text)
        report_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        report_label.setWordWrap(True)
        layout.addRow(report_label)

        layout.addRow(QLabel("¿Exportar también a CAD?"))

        self.export_dxf_check = QCheckBox("Exportar a DXF (CAD)")
        self.export_dxf_check.setChecked(
            str(self.settings.value("topofast/export_dxf", "false")).lower() == "true"
        )
        self.export_dxf_check.toggled.connect(self._on_export_dxf_toggled)
        layout.addRow(self.export_dxf_check)

        self.dxf_mode_combo = QComboBox()
        self.dxf_mode_combo.addItem("3D (con elevación real del DEM, para CAD)", "3D")
        self.dxf_mode_combo.addItem("2D (solo planta, sin altura)", "2D")
        saved_mode = self.settings.value("topofast/dxf_mode", "3D")
        idx = self.dxf_mode_combo.findData(saved_mode)
        if idx >= 0:
            self.dxf_mode_combo.setCurrentIndex(idx)
        self.dxf_mode_combo.setEnabled(self.export_dxf_check.isChecked())
        layout.addRow("Tipo de curvas en el DXF:", self.dxf_mode_combo)

        self.export_landxml_check = QCheckBox(
            "Exportar a LandXML (superficie TIN lista para Civil3D)"
        )
        self.export_landxml_check.setChecked(
            str(self.settings.value("topofast/export_landxml", "false")).lower() == "true"
        )
        layout.addRow(self.export_landxml_check)

        dir_row = QHBoxLayout()
        saved_export_dir = self.settings.value("topofast/export_dir", "") or default_dir
        self.export_dir_edit = QLineEdit(saved_export_dir)
        self.export_dir_edit.setPlaceholderText("Misma carpeta que el DEM/curvas")
        browse_btn = QPushButton("...")
        browse_btn.clicked.connect(self.browse_export_dir)
        dir_row.addWidget(self.export_dir_edit)
        dir_row.addWidget(browse_btn)
        dir_widget = QWidget()
        dir_widget.setLayout(dir_row)
        layout.addRow("Carpeta de destino:", dir_widget)

        self.filename_edit = QLineEdit(
            self.settings.value("topofast/export_filename", "curvas_nivel")
        )
        self.filename_edit.setPlaceholderText("Nombre sin extensión")
        layout.addRow("Nombre de archivo:", self.filename_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Exportar")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Omitir")
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _on_export_dxf_toggled(self, checked):
        self.dxf_mode_combo.setEnabled(checked)

    def browse_export_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Elegir carpeta de destino")
        if directory:
            self.export_dir_edit.setText(directory)

    def on_accept(self):
        self.settings.setValue("topofast/export_dxf", self.export_dxf_check.isChecked())
        self.settings.setValue("topofast/dxf_mode", self.dxf_mode_combo.currentData())
        self.settings.setValue("topofast/export_landxml", self.export_landxml_check.isChecked())
        self.settings.setValue("topofast/export_dir", self.export_dir_edit.text())
        self.settings.setValue("topofast/export_filename", self.filename_edit.text())
        self.accept()

    def get_params(self):
        filename = INVALID_FILENAME_CHARS.sub("_", self.filename_edit.text().strip())
        return {
            "export_dxf": self.export_dxf_check.isChecked(),
            "dxf_mode": self.dxf_mode_combo.currentData(),
            "export_landxml": self.export_landxml_check.isChecked(),
            "export_dir": self.export_dir_edit.text().strip(),
            "filename": filename or "curvas_nivel",
        }
