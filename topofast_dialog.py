import sys

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QComboBox, QDoubleSpinBox,
    QFileDialog, QPushButton, QHBoxLayout, QWidget, QLabel, QCheckBox, QMessageBox
)


def _sync_taskbar_group(widget, reference_widget):
    """En Windows, agrupa la ventana de 'widget' con la de 'reference_widget'
    (la ventana principal de QGIS) en la barra de tareas, copiándole su
    AppUserModelID — así Windows la muestra como una miniatura más al pasar
    el mouse por el ícono de QGIS, en vez de como un ícono aparte (que es lo
    que pasaba con Qt.WindowType.Window solo). Es un detalle cosmético: si
    falla por lo que sea (pywin32 ausente, no-Windows, etc.) no debe romper
    el diálogo, por eso todo queda envuelto en un try/except silencioso.
    """
    if sys.platform != "win32" or reference_widget is None:
        return
    try:
        from win32com.propsys import propsys, pscon

        ref_store = propsys.SHGetPropertyStoreForWindow(
            int(reference_widget.winId()), propsys.IID_IPropertyStore
        )
        app_id = ref_store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        if not app_id:
            return
        own_store = propsys.SHGetPropertyStoreForWindow(
            int(widget.winId()), propsys.IID_IPropertyStore
        )
        own_store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(app_id))
        own_store.Commit()
    except Exception:
        pass


DEM_TYPES = [
    ("Copernicus GLO-30 (recomendado, sin vacíos)", "COP30"),
    ("Copernicus GLO-90", "COP90"),
    ("ALOS World 3D (30 m)", "AW3D30"),
    ("SRTM GL1 (30 m, puede tener vacíos en glaciares)", "SRTMGL1"),
    ("SRTM GL3 (90 m, puede tener vacíos en glaciares)", "SRTMGL3"),
]


class TopoFastDialog(QDialog):
    # Ventana top-level normal (Qt.WindowType.Window), no un diálogo "hijo":
    # así Windows la agrupa en la barra de tareas junto con QGIS y la
    # muestra como una miniatura aparte al pasar el mouse por el ícono, tal
    # como hace OpenTopography-DEM-Downloader. No modal (.show(), no
    # .exec()) y con su propio botón "Generar" en vez de Aceptar/Cancelar,
    # para poder dejarla abierta y usarla varias veces seguidas.
    paramsReady = pyqtSignal(dict)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)
        self.setWindowTitle("TopoFast")
        self.setMinimumWidth(400)
        _sync_taskbar_group(self, parent)
        layout = QFormLayout(self)

        layout.addRow(QLabel(
            "Necesitás una API Key gratuita de OpenTopography\n"
            "(https://portal.opentopography.org/myopentopo)."
        ))

        self.api_key_edit = QLineEdit(self.settings.value("topofast/api_key", ""))
        layout.addRow("API Key:", self.api_key_edit)

        self.selection_mode_combo = QComboBox()
        self.selection_mode_combo.addItem("Rectángulo (clic + arrastre)", "rect")
        self.selection_mode_combo.addItem(
            "Polígono (clic por vértice, doble clic para cerrar)", "polygon"
        )
        saved_selection_mode = self.settings.value("topofast/selection_mode", "rect")
        idx = self.selection_mode_combo.findData(saved_selection_mode)
        if idx >= 0:
            self.selection_mode_combo.setCurrentIndex(idx)
        layout.addRow("Forma de selección:", self.selection_mode_combo)

        self.demtype_combo = QComboBox()
        for label, value in DEM_TYPES:
            self.demtype_combo.addItem(label, value)
        saved_dem = self.settings.value("topofast/demtype", "COP30")
        idx = self.demtype_combo.findData(saved_dem)
        if idx >= 0:
            self.demtype_combo.setCurrentIndex(idx)
        layout.addRow("Modelo de elevación:", self.demtype_combo)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.5, 1000.0)
        self.interval_spin.setValue(float(self.settings.value("topofast/interval", 10.0)))
        self.interval_spin.setSuffix(" m")
        layout.addRow("Equidistancia de curvas:", self.interval_spin)

        # "Equidistancia de curvas" es el intervalo de las curvas maestras
        # (gruesas, con la cota). Activar "curvas intermedias" agrega líneas
        # más finas EN MEDIO de las maestras, a un intervalo menor — por eso
        # su valor nunca puede superar la equidistancia base.
        intermediate_row = QHBoxLayout()
        self.intermediate_contours_check = QCheckBox("Activar curvas intermedias, cada")
        self.intermediate_contours_check.setChecked(
            str(self.settings.value("topofast/intermediate_contours", "false")).lower() == "true"
        )
        self.intermediate_contours_check.toggled.connect(self._on_intermediate_contours_toggled)
        self.intermediate_interval_spin = QDoubleSpinBox()
        self.intermediate_interval_spin.setRange(0.1, 2000.0)
        self.intermediate_interval_spin.setValue(
            float(self.settings.value("topofast/intermediate_interval", 2.0))
        )
        self.intermediate_interval_spin.setSuffix(" m")
        self.intermediate_interval_spin.setMaximumWidth(100)
        self.intermediate_interval_spin.setEnabled(self.intermediate_contours_check.isChecked())
        intermediate_row.addWidget(self.intermediate_contours_check)
        intermediate_row.addWidget(self.intermediate_interval_spin)
        intermediate_widget = QWidget()
        intermediate_widget.setLayout(intermediate_row)
        layout.addRow(intermediate_widget)

        # El máximo de las intermedias siempre queda por debajo de la
        # equidistancia base: si fueran iguales o mayores, ninguna línea
        # generada caería en un intervalo más fino que las maestras.
        self.interval_spin.valueChanged.connect(self._sync_intermediate_maximum)
        self._sync_intermediate_maximum(self.interval_spin.value())

        self.fill_voids_check = QCheckBox(
            "Rellenar vacíos del DEM (recomendado en zonas de nieve/glaciares)"
        )
        self.fill_voids_check.setChecked(
            str(self.settings.value("topofast/fill_voids", "true")).lower() == "true"
        )
        layout.addRow(self.fill_voids_check)

        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(self.settings.value("topofast/output_dir", ""))
        self.output_edit.setPlaceholderText("Carpeta temporal por defecto")
        browse_btn = QPushButton("...")
        browse_btn.clicked.connect(self.browse_output)
        output_row.addWidget(self.output_edit)
        output_row.addWidget(browse_btn)
        output_widget = QWidget()
        output_widget.setLayout(output_row)
        layout.addRow("Carpeta de salida:", output_widget)

        self.generate_btn = QPushButton("Generar curvas de nivel")
        self.generate_btn.clicked.connect(self.on_generate)
        layout.addRow(self.generate_btn)

    def browse_output(self):
        directory = QFileDialog.getExistingDirectory(self, "Elegir carpeta de salida")
        if directory:
            self.output_edit.setText(directory)

    def _on_intermediate_contours_toggled(self, checked):
        self.intermediate_interval_spin.setEnabled(checked)

    def _sync_intermediate_maximum(self, base_interval):
        max_value = max(base_interval - 0.1, 0.1)
        self.intermediate_interval_spin.setMaximum(max_value)

    def on_generate(self):
        if (self.intermediate_contours_check.isChecked()
                and self.intermediate_interval_spin.value() >= self.interval_spin.value()):
            QMessageBox.warning(
                self, "TopoFast",
                "El intervalo de las curvas intermedias tiene que ser menor que la "
                "equidistancia de curvas (el intervalo de las maestras), si no ninguna línea "
                "queda entre una maestra y la siguiente.\n\n"
                f"Maestras (equidistancia): {self.interval_spin.value():g} m — "
                f"Intermedias: {self.intermediate_interval_spin.value():g} m."
            )
            return

        params = self.get_params()
        if not params["api_key"]:
            QMessageBox.warning(
                self, "TopoFast",
                "Falta la API Key de OpenTopography. Registrate gratis en "
                "https://portal.opentopography.org/myopentopo y volvé a intentarlo."
            )
            return

        self._save_settings()
        self.paramsReady.emit(params)

    def _save_settings(self):
        self.settings.setValue("topofast/api_key", self.api_key_edit.text())
        self.settings.setValue("topofast/selection_mode", self.selection_mode_combo.currentData())
        self.settings.setValue("topofast/demtype", self.demtype_combo.currentData())
        self.settings.setValue("topofast/interval", self.interval_spin.value())
        self.settings.setValue("topofast/fill_voids", self.fill_voids_check.isChecked())
        self.settings.setValue(
            "topofast/intermediate_contours", self.intermediate_contours_check.isChecked()
        )
        self.settings.setValue(
            "topofast/intermediate_interval", self.intermediate_interval_spin.value()
        )
        self.settings.setValue("topofast/output_dir", self.output_edit.text())

    def get_params(self):
        return {
            "api_key": self.api_key_edit.text().strip(),
            "selection_mode": self.selection_mode_combo.currentData(),
            "demtype": self.demtype_combo.currentData(),
            "interval": self.interval_spin.value(),
            "fill_voids": self.fill_voids_check.isChecked(),
            "intermediate_contours": self.intermediate_contours_check.isChecked(),
            "intermediate_interval": self.intermediate_interval_spin.value(),
            "output_dir": self.output_edit.text().strip(),
        }
