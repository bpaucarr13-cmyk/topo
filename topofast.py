import os
import shutil
import subprocess
import tempfile
import uuid
import urllib.request
import urllib.parse
import urllib.error

import processing
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, QSettings, QDateTime
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QProgressDialog, QApplication
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProject,
    QgsRectangle,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsColorRampShader,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsRuleBasedRenderer,
    QgsLineSymbol,
    QgsSingleSymbolRenderer,
    QgsPalLayerSettings,
    QgsVectorLayerSimpleLabeling,
    QgsTextFormat,
    QgsProperty,
    QgsGeometry,
    QgsFeature,
    QgsDistanceArea,
)

from .topofast_dialog import TopoFastDialog
from .topofast_export_dialog import TopoFastExportDialog
from .map_tool_extent import ExtentMapTool
from .map_tool_polygon import PolygonMapTool

NODATA_VALUE = -9999.0
FILL_SEARCH_DISTANCE = 100


class TopoFast:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.menu = "&TopoFast"
        self.map_tool = None
        self.params = None
        self.settings = QSettings()
        self.run_seq = 0
        self._params_dialog = None
        self._export_dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QgsApplication.getThemeIcon("/mActionAddRasterLayer.svg")
        self.action = QAction(icon, "Generar curvas de nivel (TopoFast)", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.menu, self.action)
        self.actions.append(self.action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        canvas = self.iface.mapCanvas()
        if self.map_tool is not None and canvas.mapTool() == self.map_tool:
            canvas.unsetMapTool(self.map_tool)

    def run(self):
        if self._bring_to_front(self._params_dialog):
            return
        # Ventana top-level normal, no modal, con botón "Generar" propio (ver
        # topofast_dialog.py) y agrupada con QGIS en la barra de tareas. Se
        # oculta (no se cierra) apenas arranca una selección, para que no
        # quede superpuesta con el diálogo de exportación que aparece
        # después: una ventana a la vez, en secuencia.
        dlg = TopoFastDialog(self.settings, self.iface.mainWindow())
        dlg.paramsReady.connect(self.on_params_ready)
        dlg.show()
        self._params_dialog = dlg

    @staticmethod
    def _bring_to_front(dlg):
        if dlg is None:
            return False
        try:
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            return True
        except RuntimeError:
            pass
        return False

    def on_params_ready(self, params):
        self.params = params
        if self._params_dialog is not None:
            self._params_dialog.hide()
        self.start_selection()

    def start_selection(self):
        canvas = self.iface.mapCanvas()
        if self.params["selection_mode"] == "polygon":
            self.map_tool = PolygonMapTool(canvas)
            self.map_tool.polygonSelected.connect(self.on_polygon_selected)
            message = (
                "Hacé clic para cada vértice del área; doble clic (o clic derecho) "
                "para cerrar el polígono."
            )
        else:
            self.map_tool = ExtentMapTool(canvas)
            self.map_tool.extentSelected.connect(self.on_extent_selected)
            message = "Dibujá (clic + arrastre) el área para la que querés las curvas de nivel."
        canvas.setMapTool(self.map_tool)
        self.iface.messageBar().pushMessage(
            "TopoFast", message, level=Qgis.MessageLevel.Info, duration=6
        )

    def _project_to_wgs84_transform(self):
        canvas = self.iface.mapCanvas()
        project_crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        return QgsCoordinateTransform(project_crs, wgs84, QgsProject.instance())

    def on_extent_selected(self, rect: QgsRectangle):
        canvas = self.iface.mapCanvas()
        canvas.unsetMapTool(self.map_tool)

        rect_4326 = self._project_to_wgs84_transform().transformBoundingBox(rect)

        try:
            self.process_area(rect_4326)
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "TopoFast", str(exc))

    def on_polygon_selected(self, geometry: QgsGeometry):
        canvas = self.iface.mapCanvas()
        canvas.unsetMapTool(self.map_tool)

        geometry_4326 = QgsGeometry(geometry)
        geometry_4326.transform(self._project_to_wgs84_transform())
        rect_4326 = geometry_4326.boundingBox()

        try:
            self.process_area(rect_4326, mask_geometry_4326=geometry_4326)
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "TopoFast", str(exc))

    def process_area(self, rect_4326: QgsRectangle, mask_geometry_4326: QgsGeometry = None):
        params = self.params
        output_dir = params["output_dir"] or tempfile.gettempdir()
        os.makedirs(output_dir, exist_ok=True)

        # Cada corrida usa un sufijo único de archivo y de nombre de capa: al
        # reutilizar siempre el mismo nombre, Windows bloqueaba el archivo de la
        # corrida anterior (seguía abierto como capa) y la escritura fallaba,
        # mostrando el resultado de la primera selección en vez del nuevo.
        self.run_seq += 1
        run_id = uuid.uuid4().hex[:8]
        dem_layer_name = f"TopoFast DEM {self.run_seq}"
        contour_layer_name = f"Curvas de nivel {self.run_seq}"

        # Rutas de archivos de paso intermedio (se generan pero no quedan
        # como capa final en el proyecto): se borran al final para no
        # acumular GeoTIFFs de más en la carpeta de salida en cada corrida.
        stale_paths = []

        dem_raw_path = os.path.join(output_dir, f"topofast_dem_raw_{run_id}.tif")
        self._download_dem(rect_4326, params, dem_raw_path)
        stale_paths.append(dem_raw_path)

        dem_path = os.path.join(output_dir, f"topofast_dem_{run_id}.tif")
        processing.run("gdal:translate", {
            "INPUT": dem_raw_path,
            "TARGET_CRS": None,
            "NODATA": NODATA_VALUE,
            "COPY_SUBDATASETS": False,
            "OPTIONS": "",
            "EXTRA": "",
            "DATA_TYPE": 0,
            "OUTPUT": dem_path,
        })

        dem_layer = QgsRasterLayer(dem_path, dem_layer_name)
        if not dem_layer.isValid():
            raise RuntimeError("El DEM descargado no es una capa ráster válida.")

        self._warn_if_many_voids(dem_layer)

        contour_input_layer = dem_layer
        filled_layer = None
        if params["fill_voids"]:
            filled_path = os.path.join(output_dir, f"topofast_dem_relleno_{run_id}.tif")
            processing.run("gdal:fillnodata", {
                "INPUT": dem_layer,
                "BAND": 1,
                "DISTANCE": FILL_SEARCH_DISTANCE,
                "ITERATIONS": 0,
                "MASK_LAYER": None,
                "NO_MASK": False,
                "OPTIONS": "",
                "EXTRA": "",
                "OUTPUT": filled_path,
            })
            filled_layer = QgsRasterLayer(filled_path, dem_layer_name)
            if filled_layer.isValid():
                stale_paths.append(dem_path)
                dem_layer = filled_layer
                contour_input_layer = filled_layer

        # Referencia al DEM con vecinos completos (sin recortar todavía), para
        # calcular la pendiente más adelante evitando el artefacto de borde
        # que deja el recorte por polígono (ver _build_area_report).
        dem_layer_for_slope = contour_input_layer

        if mask_geometry_4326 is not None:
            # Recorta al polígono exacto (no al rectángulo completo). Esto se
            # hace después de rellenar vacíos, no antes: si se hiciera antes,
            # gdal:fillnodata interpolaría también el área fuera del polígono
            # (que queda en NoData por el recorte) en vez de dejarla vacía.
            clipped_path = os.path.join(output_dir, f"topofast_dem_recortado_{run_id}.tif")
            clipped_layer = self._clip_raster_to_mask(
                contour_input_layer, mask_geometry_4326, clipped_path, dem_layer_name
            )
            if clipped_layer.isValid():
                stale_paths.append(contour_input_layer.source())
                dem_layer = clipped_layer
                contour_input_layer = clipped_layer

        self._style_dem(dem_layer)
        QgsProject.instance().addMapLayer(dem_layer)

        # Una sola generación de curvas, siempre en 3D (CREATE_3D=True): la
        # elevación real queda en la geometría (no solo en el atributo ELEV),
        # y de ahí salen tanto la capa que se ve en QGIS como el DXF para CAD.
        # Antes había dos corridas de gdal:contour (una en 2D para el lienzo,
        # otra en 3D para el DXF) — quedaba "Producir vectorial 3D" apagado en
        # la corrida del lienzo, lo cual generaba dudas sobre si el 3D
        # realmente se estaba activando. Con una sola fuente 3D no hay
        # ambigüedad: siempre está activado, y el modo 2D/3D del DXF se aplica
        # recién al exportar (aplanando o no esa misma geometría).
        # "Equidistancia de curvas" es el intervalo de las curvas maestras.
        # Si "curvas intermedias" está activo, la extracción en sí corre al
        # intervalo más fino de las intermedias (así existen líneas de
        # verdad entre una maestra y la siguiente); si no, corre directo a
        # la equidistancia y todas las líneas quedan iguales.
        generation_interval = (
            params["intermediate_interval"] if params["intermediate_contours"]
            else params["interval"]
        )
        contour_path = os.path.join(output_dir, f"topofast_curvas_nivel_{run_id}.gpkg")
        result = processing.run("gdal:contour", {
            "INPUT": contour_input_layer,
            "BAND": 1,
            "INTERVAL": generation_interval,
            "FIELD_NAME": "ELEV",
            "CREATE_3D": True,
            "IGNORE_NODATA": False,
            "NODATA": NODATA_VALUE,
            "OFFSET": 0,
            "EXTRA": "",
            "OUTPUT": contour_path,
        })

        contour_layer = QgsVectorLayer(result["OUTPUT"], contour_layer_name, "ogr")
        if not contour_layer.isValid():
            raise RuntimeError("No se pudieron generar las curvas de nivel a partir del DEM.")
        self._style_contours(contour_layer, params)
        QgsProject.instance().addMapLayer(contour_layer)

        self.iface.messageBar().pushMessage(
            "TopoFast", "Listo: DEM y curvas de nivel cargados en el proyecto.",
            level=Qgis.MessageLevel.Success, duration=6
        )

        report_text = "No se pudo generar el informe del área."
        try:
            report_path, report_text = self._build_area_report(
                dem_layer, dem_layer_for_slope, rect_4326, mask_geometry_4326,
                params, output_dir, run_id
            )
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                "TopoFast", f"No se pudo generar el informe del área: {exc}",
                level=Qgis.MessageLevel.Warning, duration=10
            )

        # Soltar toda referencia a los DEM intermedios (sin recortar / sin
        # rellenar) antes de borrar sus archivos: en Windows, GDAL mantiene
        # el archivo abierto mientras exista el objeto QgsRasterLayer, y
        # os.remove() fallaría en silencio si no se libera antes.
        dem_layer_for_slope = None
        filled_layer = None
        self._cleanup_files(stale_paths)

        # El informe y las opciones de exportación (DXF/LandXML) van en un
        # solo diálogo, para no encadenar dos pop-ups después de procesar.
        self._offer_export(contour_path, dem_layer, rect_4326, output_dir, run_id, report_text)

    def _offer_export(self, contour_path, dem_layer, rect_4326, output_dir, run_id, report_text):
        # No modal (.show(), no .exec()): así se puede seguir usando QGIS
        # (paneo, zoom, panel de capas) con este diálogo abierto, sin tener
        # que cerrarlo primero. La instancia se guarda en self para que no
        # la borre el recolector de basura mientras sigue abierta.
        export_dlg = TopoFastExportDialog(
            self.settings, output_dir, report_text, self.iface.mainWindow()
        )
        export_dlg.accepted.connect(lambda: self._run_export(
            export_dlg, contour_path, dem_layer, rect_4326, output_dir, run_id
        ))
        export_dlg.show()
        self._export_dialog = export_dlg

    def _run_export(self, export_dlg, contour_path, dem_layer, rect_4326, output_dir, run_id):
        export_params = export_dlg.get_params()
        export_dir = export_params["export_dir"] or output_dir
        os.makedirs(export_dir, exist_ok=True)

        message = "Exportación de TopoFast finalizada."
        if export_params["export_dxf"]:
            try:
                dxf_path, zone_label = self._export_dxf(
                    contour_path, export_params, export_dir, run_id, rect_4326
                )
                message += f" DXF exportado a {dxf_path} ({zone_label})."
            except Exception as exc:
                self.iface.messageBar().pushMessage(
                    "TopoFast", f"No se pudo exportar el DXF: {exc}",
                    level=Qgis.MessageLevel.Warning, duration=10
                )

        if export_params["export_landxml"]:
            try:
                # Como el diálogo de exportación no es modal, se puede haber
                # borrado la capa DEM del panel de Capas mientras seguía
                # abierto y no bien se apretó "Exportar": sip deja un objeto
                # Python "zombie" que revienta con RuntimeError al tocarlo.
                if sip.isdeleted(dem_layer):
                    raise RuntimeError(
                        "La capa del DEM ya no está en el proyecto (se borró antes de "
                        "exportar). Volvé a generar las curvas para exportar a LandXML."
                    )
                landxml_path = self._export_landxml(
                    dem_layer, rect_4326, export_dir, run_id, export_params["filename"]
                )
                message += f" LandXML exportado a {landxml_path}."
            except Exception as exc:
                self.iface.messageBar().pushMessage(
                    "TopoFast", f"No se pudo exportar el LandXML: {exc}",
                    level=Qgis.MessageLevel.Warning, duration=10
                )

        if export_params["export_dxf"] or export_params["export_landxml"]:
            self.iface.messageBar().pushMessage(
                "TopoFast", message, level=Qgis.MessageLevel.Success, duration=8
            )

    def _build_area_report(self, dem_layer, dem_layer_for_slope, rect_4326,
                            mask_geometry_4326, params, output_dir, run_id):
        stats = dem_layer.dataProvider().bandStatistics(1)

        # La pendiente se calcula sobre el DEM SIN recortar (con vecinos
        # reales en todas direcciones) y recién después, si hay polígono, se
        # recorta el resultado — no al revés (evita que el borde del recorte,
        # datos válidos contra NoData, se lea como un acantilado artificial).
        #
        # SCALE=111120: el DEM está en EPSG:4326 (X/Y en grados) pero la
        # elevación en metros. Sin este factor (metros por grado), gdaldem
        # compara un delta de altura en metros contra un delta horizontal en
        # grados como si fueran la misma unidad, y la pendiente sale ~90° en
        # absolutamente todos los píxeles (no es un problema de bordes).
        slope_path = os.path.join(output_dir, f"topofast_pendiente_{run_id}.tif")
        processing.run("gdal:slope", {
            "INPUT": dem_layer_for_slope,
            "BAND": 1,
            "SCALE": 111120,
            "AS_PERCENT": False,
            "COMPUTE_EDGES": False,
            "ZEVENBERGEN": False,
            "OPTIONS": "",
            "EXTRA": "",
            "OUTPUT": slope_path,
        })
        slope_layer = QgsRasterLayer(slope_path, "topofast_pendiente")
        slope_stale_paths = [slope_path]
        clipped_slope_layer = None

        if mask_geometry_4326 is not None and slope_layer.isValid():
            slope_clip_path = os.path.join(output_dir, f"topofast_pendiente_recortada_{run_id}.tif")
            clipped_slope_layer = self._clip_raster_to_mask(
                slope_layer, mask_geometry_4326, slope_clip_path, "topofast_pendiente"
            )
            if clipped_slope_layer.isValid():
                slope_stale_paths.append(slope_clip_path)

        active_slope_layer = clipped_slope_layer if clipped_slope_layer is not None and clipped_slope_layer.isValid() else slope_layer
        slope_mean = None
        if active_slope_layer.isValid():
            slope_mean = active_slope_layer.dataProvider().bandStatistics(1).mean

        # La capa de pendiente es solo un paso interno para sacar el
        # promedio del informe: nunca se agrega al proyecto. Hay que soltar
        # la referencia a los objetos QgsRasterLayer antes de borrar sus
        # archivos, porque en Windows GDAL mantiene el archivo abierto
        # mientras la capa exista y os.remove() fallaría en silencio.
        active_slope_layer = None
        slope_layer = None
        clipped_slope_layer = None
        self._cleanup_files(slope_stale_paths)

        geometry = mask_geometry_4326 if mask_geometry_4326 is not None else QgsGeometry.fromRect(rect_4326)
        distance_area = QgsDistanceArea()
        distance_area.setEllipsoid("WGS84")
        area_m2 = distance_area.measureArea(geometry)

        centroid = rect_4326.center()
        target_epsg = self._utm_epsg_for_point(centroid.x(), centroid.y())
        zone_number = target_epsg % 100
        hemisphere = "N" if target_epsg // 100 == 326 else "S"

        lines = [
            "TopoFast - Informe del área",
            "=" * 32,
            f"Fecha: {QDateTime.currentDateTime().toString('yyyy-MM-dd HH:mm')}",
            f"Modelo de elevación: {params['demtype']}",
            f"Equidistancia de curvas: {params['interval']:g} m",
            "",
            f"Elevación mínima: {stats.minimumValue:.0f} m",
            f"Elevación máxima: {stats.maximumValue:.0f} m",
            f"Elevación media: {stats.mean:.0f} m",
        ]
        if slope_mean is not None:
            lines.append(f"Pendiente media: {slope_mean:.1f}°")
        lines += [
            "",
            f"Área: {area_m2 / 10000.0:.2f} ha ({area_m2 / 1_000_000.0:.4f} km²)",
            f"Zona UTM: {zone_number}{hemisphere} / EPSG:{target_epsg}",
        ]
        report_text = "\n".join(lines)

        report_path = os.path.join(output_dir, f"topofast_informe_{run_id}.txt")
        with open(report_path, "w", encoding="utf-8") as report_file:
            report_file.write(report_text)

        return report_path, report_text

    def _export_dxf(self, contour_gpkg_path, params, output_dir, run_id, rect_4326):
        # El GeoPackage de curvas ya viene en 3D (ver process_area) pero en
        # EPSG:4326 (grados). Un DXF en grados es inútil para medir en CAD
        # (distancias/áreas no tienen sentido en esa unidad), así que antes de
        # exportar se reproyecta a la zona UTM que corresponda al área
        # seleccionada (X/Y quedan en metros; Z no se toca, ya está en metros).
        #
        # Convertir directo a DXF con el conversor de formato de Processing no
        # alcanza: gdal_contour agrega un campo "ID" que el driver DXF no
        # admite, y sin "-skipfailures" la conversión aborta sin escribir
        # ninguna entidad; y sin forzar el tipo de geometría con "-dim"/"-nlt",
        # OGR descarta la elevación al pasar a DXF. El algoritmo
        # "gdal:convertformat" de Processing no expone esas opciones (ni
        # "-t_srs"), así que se llama a ogr2ogr directamente (el mismo binario
        # que usa QGIS internamente).
        is_3d = params["dxf_mode"] == "3D"
        centroid = rect_4326.center()
        target_epsg = self._utm_epsg_for_point(centroid.x(), centroid.y())
        zone_number = target_epsg % 100
        hemisphere = "N" if target_epsg // 100 == 326 else "S"
        zone_label = f"UTM {zone_number}{hemisphere} / EPSG:{target_epsg}"

        dxf_path = os.path.join(output_dir, f"{params['filename']}.dxf")
        if os.path.exists(dxf_path):
            os.remove(dxf_path)

        ogr2ogr = shutil.which("ogr2ogr") or os.path.join(
            QgsApplication.prefixPath(), "bin",
            "ogr2ogr.exe" if os.name == "nt" else "ogr2ogr"
        )
        if not os.path.exists(ogr2ogr) and shutil.which("ogr2ogr") is None:
            raise RuntimeError("No se encontró ogr2ogr en el entorno de QGIS.")

        cmd = [ogr2ogr, "-f", "DXF", "-skipfailures", "-t_srs", f"EPSG:{target_epsg}"]
        if is_3d:
            cmd += ["-dim", "3", "-nlt", "LINESTRING25D"]
        else:
            cmd += ["-dim", "2"]
        cmd += [dxf_path, contour_gpkg_path, "contour"]

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=creationflags)

        dxf_check_layer = QgsVectorLayer(dxf_path, "check", "ogr")
        if not dxf_check_layer.isValid() or dxf_check_layer.featureCount() == 0:
            detail = (result.stderr or "").strip() or "sin detalles"
            raise RuntimeError(f"El DXF se generó vacío (sin curvas). Detalle: {detail}")

        return dxf_path, zone_label

    def _export_landxml(self, dem_layer, rect_4326, output_dir, run_id, filename):
        # En vez de exportar solo las curvas (que en Civil3D hay que
        # triangular a mano como líneas de quiebre, con riesgo de armar una
        # malla cruzada si no se hace bien), acá se arma directamente una
        # superficie TIN ya triangulada a partir de la grilla del DEM:
        # cada celda 2x2 de píxeles válidos se parte en dos triángulos. El
        # resultado se importa en Civil3D como superficie terminada con un
        # solo paso (Insertar > LandXML).
        centroid = rect_4326.center()
        target_epsg = self._utm_epsg_for_point(centroid.x(), centroid.y())

        reprojected_path = os.path.join(output_dir, f"topofast_dem_utm_{run_id}.tif")
        processing.run("gdal:warpreproject", {
            "INPUT": dem_layer,
            "SOURCE_CRS": None,
            "TARGET_CRS": f"EPSG:{target_epsg}",
            "RESAMPLING": 0,
            "NODATA": NODATA_VALUE,
            "TARGET_RESOLUTION": None,
            "OPTIONS": "",
            "DATA_TYPE": 0,
            "TARGET_EXTENT": None,
            "TARGET_EXTENT_CRS": None,
            "MULTITHREADING": False,
            "EXTRA": "",
            "OUTPUT": reprojected_path,
        })
        utm_layer = QgsRasterLayer(reprojected_path, "topofast_utm")
        if not utm_layer.isValid():
            raise RuntimeError("No se pudo reproyectar el DEM para el LandXML.")

        provider = utm_layer.dataProvider()
        extent = utm_layer.extent()
        width = utm_layer.width()
        height = utm_layer.height()
        block = provider.block(1, extent, width, height)
        if block is None:
            raise RuntimeError("No se pudo leer la grilla del DEM para el LandXML.")

        pixel_w = extent.width() / width
        pixel_h = extent.height() / height

        # Se escribe el XML directo al archivo en vez de armar listas de
        # strings con cada punto/cara en memoria: para un DEM grande (varios
        # millones de píxeles) esas listas llegaban a cientos de MB de RAM
        # antes de siquiera empezar a escribir. Solo se mantiene en memoria
        # la grilla de ids (enteros, liviana) necesaria para armar las caras.
        point_ids = [[None] * width for _ in range(height)]
        next_id = 1
        point_count = 0
        face_count = 0
        timestamp = QDateTime.currentDateTime()
        landxml_path = os.path.join(output_dir, f"{filename}.xml")

        with open(landxml_path, "w", encoding="utf-8") as landxml_file:
            landxml_file.write(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2" '
                f'date="{timestamp.toString("yyyy-MM-dd")}" time="{timestamp.toString("HH:mm:ss")}">\n'
                '  <Units>\n'
                '    <Metric areaUnit="squareMeter" linearUnit="meter" volumeUnit="cubicMeter" '
                'temperatureUnit="celsius" pressureUnit="mmHG"/>\n'
                '  </Units>\n'
                '  <Surfaces>\n'
                '    <Surface name="TopoFast">\n'
                '      <Definition surfType="TIN">\n'
                '        <Pnts>\n'
            )
            for row in range(height):
                y = extent.yMaximum() - (row + 0.5) * pixel_h
                for col in range(width):
                    if block.isNoData(row, col):
                        continue
                    z = block.value(row, col)
                    x = extent.xMinimum() + (col + 0.5) * pixel_w
                    point_ids[row][col] = next_id
                    # LandXML: convención Norte(Y) Este(X) Elevación(Z).
                    landxml_file.write(f'          <P id="{next_id}">{y:.3f} {x:.3f} {z:.3f}</P>\n')
                    next_id += 1
                    point_count += 1

            landxml_file.write('        </Pnts>\n        <Faces>\n')
            for row in range(height - 1):
                for col in range(width - 1):
                    top_left = point_ids[row][col]
                    top_right = point_ids[row][col + 1]
                    bottom_left = point_ids[row + 1][col]
                    bottom_right = point_ids[row + 1][col + 1]
                    if None in (top_left, top_right, bottom_left, bottom_right):
                        continue
                    landxml_file.write(f"          <F>{top_left} {bottom_left} {bottom_right}</F>\n")
                    landxml_file.write(f"          <F>{top_left} {bottom_right} {top_right}</F>\n")
                    face_count += 1

            landxml_file.write(
                '        </Faces>\n'
                '      </Definition>\n'
                '    </Surface>\n'
                '  </Surfaces>\n'
                '</LandXML>\n'
            )

        # Soltar la capa UTM temporal antes de borrar su archivo: en
        # Windows, GDAL lo mantiene abierto mientras el objeto exista.
        block = None
        provider = None
        utm_layer = None
        self._cleanup_files([reprojected_path])

        if point_count == 0 or face_count == 0:
            self._cleanup_files([landxml_path])
            raise RuntimeError("No hay suficientes datos válidos para armar la superficie TIN.")

        return landxml_path

    @staticmethod
    def _cleanup_files(paths):
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _utm_epsg_for_point(lon, lat):
        zone = int((lon + 180) // 6) + 1
        zone = min(max(zone, 1), 60)
        return (32600 if lat >= 0 else 32700) + zone

    @staticmethod
    def _mask_layer_from_geometry(geometry_4326):
        mask_layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "topofast_mask", "memory")
        feature = QgsFeature()
        feature.setGeometry(geometry_4326)
        mask_layer.dataProvider().addFeature(feature)
        mask_layer.updateExtents()
        return mask_layer

    def _clip_raster_to_mask(self, layer, mask_geometry_4326, output_path, layer_name):
        mask_layer = self._mask_layer_from_geometry(mask_geometry_4326)
        processing.run("gdal:cliprasterbymasklayer", {
            "INPUT": layer,
            "MASK": mask_layer,
            "SOURCE_CRS": None,
            "TARGET_CRS": None,
            "TARGET_EXTENT": None,
            "NODATA": NODATA_VALUE,
            "ALPHA_BAND": False,
            "CROP_TO_CUTLINE": True,
            "KEEP_RESOLUTION": False,
            "SET_RESOLUTION": False,
            "X_RESOLUTION": None,
            "Y_RESOLUTION": None,
            "MULTITHREADING": False,
            "OPTIONS": "",
            "DATA_TYPE": 0,
            "EXTRA": "",
            "OUTPUT": output_path,
        })
        return QgsRasterLayer(output_path, layer_name)

    DOWNLOAD_TIMEOUT_SECONDS = 120
    DOWNLOAD_CHUNK_BYTES = 256 * 1024

    def _download_dem(self, rect_4326, params, dem_raw_path):
        query = {
            "demtype": params["demtype"],
            "south": rect_4326.yMinimum(),
            "north": rect_4326.yMaximum(),
            "west": rect_4326.xMinimum(),
            "east": rect_4326.xMaximum(),
            "outputFormat": "GTiff",
            "API_Key": params["api_key"],
        }
        url = "https://portal.opentopography.org/API/globaldem?" + urllib.parse.urlencode(query)

        # Con "Cancelar" habilitado y timeout: antes, si OpenTopography no
        # respondía, urlretrieve() podía quedar colgado indefinidamente y la
        # única salida era forzar el cierre de QGIS. Ahora se descarga en
        # trozos, con progreso real (si el servidor manda Content-Length) y
        # chequeo de cancelación en cada trozo.
        progress = QProgressDialog(
            "Descargando DEM desde OpenTopography...", "Cancelar", 0, 0, self.iface.mainWindow()
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setWindowTitle("TopoFast")
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()

        cancelled = False
        try:
            with urllib.request.urlopen(url, timeout=self.DOWNLOAD_TIMEOUT_SECONDS) as response:
                content_length = response.getheader("Content-Length")
                total_bytes = int(content_length) if content_length and content_length.isdigit() else 0
                progress.setMaximum(total_bytes)
                downloaded = 0
                with open(dem_raw_path, "wb") as out_file:
                    while True:
                        if progress.wasCanceled():
                            cancelled = True
                            break
                        chunk = response.read(self.DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        out_file.write(chunk)
                        downloaded += len(chunk)
                        if total_bytes > 0:
                            progress.setValue(downloaded)
                        QApplication.processEvents()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            if "rate limit" in body.lower():
                raise RuntimeError(
                    "Alcanzaste el límite gratuito de OpenTopography (50 descargas cada "
                    "24 horas). Esperá a que se libere cupo (la ventana es rodante, no "
                    "un reinicio fijo a medianoche) y volvé a intentar más tarde."
                ) from exc
            raise RuntimeError(
                f"OpenTopography respondió con error {exc.code}. "
                f"Revisá la API Key y el área seleccionada.\n{body[:300]}"
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(
                "OpenTopography no respondió a tiempo (se agotó el tiempo de espera). "
                "Probá de nuevo en un momento o con un área más chica."
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise RuntimeError(
                    "OpenTopography no respondió a tiempo (se agotó el tiempo de espera). "
                    "Probá de nuevo en un momento o con un área más chica."
                ) from exc
            raise RuntimeError(f"No se pudo conectar con OpenTopography: {exc.reason}") from exc
        finally:
            progress.close()

        if cancelled:
            if os.path.exists(dem_raw_path):
                os.remove(dem_raw_path)
            raise RuntimeError("Descarga cancelada.")

        if not os.path.exists(dem_raw_path) or os.path.getsize(dem_raw_path) < 1000:
            raise RuntimeError("El DEM descargado está vacío o incompleto. Probá con un área más chica.")

    def _warn_if_many_voids(self, dem_layer):
        provider = dem_layer.dataProvider()
        stats = provider.bandStatistics(1)
        total_pixels = dem_layer.width() * dem_layer.height()
        if total_pixels <= 0 or stats.elementCount <= 0:
            return
        void_fraction = 1.0 - (stats.elementCount / total_pixels)
        if void_fraction > 0.1:
            self.iface.messageBar().pushMessage(
                "TopoFast",
                f"El {void_fraction * 100:.0f}% del área no tiene datos de elevación "
                f"(vacíos del DEM, típico en glaciares/nieve). Si necesitás cobertura "
                f"completa probá con el modelo Copernicus GLO-30 (COP30).",
                level=Qgis.MessageLevel.Warning, duration=12
            )

    def _style_dem(self, layer):
        provider = layer.dataProvider()
        stats = provider.bandStatistics(1)
        vmin, vmax = stats.minimumValue, stats.maximumValue
        if vmin is None or vmax is None or vmin >= vmax:
            return

        span = vmax - vmin
        stops = [
            (vmin, QColor(40, 90, 40)),
            (vmin + span * 0.35, QColor(160, 200, 90)),
            (vmin + span * 0.6, QColor(235, 220, 130)),
            (vmin + span * 0.8, QColor(180, 120, 80)),
            (vmax, QColor(255, 255, 255)),
        ]
        ramp_items = [QgsColorRampShader.ColorRampItem(value, color) for value, color in stops]

        shader_function = QgsColorRampShader()
        shader_function.setColorRampType(QgsColorRampShader.Type.Interpolated)
        shader_function.setColorRampItemList(ramp_items)

        shader = QgsRasterShader()
        shader.setRasterShaderFunction(shader_function)

        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def _style_contours(self, layer, params):
        # "Equidistancia de curvas" es el intervalo de las curvas maestras
        # (gruesas, con la cota). Si "curvas intermedias" está desactivado,
        # todas las líneas (generadas directo a esa equidistancia) se ven
        # iguales. Si está activo, la extracción ya vino más fina (ver
        # process_area) y acá se separan: las que caen justo en un múltiplo
        # de la equidistancia quedan como maestras, el resto como
        # intermedias finas sin etiqueta.
        if not params["intermediate_contours"]:
            simple_symbol = QgsLineSymbol.createSimple({"color": "115,77,38", "width": "0.26"})
            layer.setRenderer(QgsSingleSymbolRenderer(simple_symbol))
            layer.setLabelsEnabled(False)
            layer.triggerRepaint()
            return

        index_step = params["interval"]
        # Comparar "ELEV" % index_step = 0 falla por errores de redondeo de
        # punto flotante (p. ej. una cota que debería ser exactamente 3130.0
        # puede llegar como 3129.9999999998) y hacía que curvas que sí eran
        # maestras se clasificaran como intermedias de forma intermitente.
        # Con una tolerancia chica alcanza, porque gdal:contour calcula las
        # cotas como múltiplos exactos del intervalo (el único margen de
        # error posible es de punto flotante, no de varios metros).
        master_expr = f'abs("ELEV" - round("ELEV" / {index_step}) * {index_step}) < 0.001'

        root_rule = QgsRuleBasedRenderer.Rule(None)

        master_symbol = QgsLineSymbol.createSimple({"color": "101,67,33", "width": "0.5"})
        master_rule = QgsRuleBasedRenderer.Rule(master_symbol)
        master_rule.setLabel("Curva maestra")
        master_rule.setFilterExpression(master_expr)
        root_rule.appendChild(master_rule)

        intermediate_symbol = QgsLineSymbol.createSimple({"color": "153,102,51", "width": "0.15"})
        intermediate_rule = QgsRuleBasedRenderer.Rule(intermediate_symbol)
        intermediate_rule.setLabel("Curva intermedia")
        intermediate_rule.setFilterExpression("ELSE")
        root_rule.appendChild(intermediate_rule)

        layer.setRenderer(QgsRuleBasedRenderer(root_rule))

        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = 'to_string(round("ELEV"))'
        label_settings.isExpression = True
        label_settings.placement = QgsPalLayerSettings.Placement.Line
        label_settings.dataDefinedProperties().setProperty(
            QgsPalLayerSettings.Property.Show,
            QgsProperty.fromExpression(master_expr)
        )

        text_format = QgsTextFormat()
        text_format.setSize(8)
        text_format.setColor(QColor(80, 50, 20))
        label_settings.setFormat(text_format)

        layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()
