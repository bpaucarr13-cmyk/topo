from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsRectangle, QgsWkbTypes, QgsGeometry
from qgis.gui import QgsMapTool, QgsRubberBand


class ExtentMapTool(QgsMapTool):
    extentSelected = pyqtSignal(QgsRectangle)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 80, 0, 60))
        self.rubber_band.setStrokeColor(QColor(255, 80, 0, 200))
        self.rubber_band.setWidth(2)
        self.start_point = None
        self.is_dragging = False

    def canvasPressEvent(self, event):
        self.start_point = self.toMapCoordinates(event.pos())
        self.is_dragging = True
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

    def canvasMoveEvent(self, event):
        if not self.is_dragging or self.start_point is None:
            return
        current_point = self.toMapCoordinates(event.pos())
        self._show_rect(QgsRectangle(self.start_point, current_point))

    def canvasReleaseEvent(self, event):
        if not self.is_dragging or self.start_point is None:
            return
        self.is_dragging = False
        end_point = self.toMapCoordinates(event.pos())
        rect = QgsRectangle(self.start_point, end_point)
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.start_point = None
        if rect.width() == 0 or rect.height() == 0:
            return
        self.extentSelected.emit(rect)

    def _show_rect(self, rect):
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band.setToGeometry(QgsGeometry.fromRect(rect), None)

    def deactivate(self):
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        super().deactivate()
