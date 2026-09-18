from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsWkbTypes, QgsGeometry
from qgis.gui import QgsMapTool, QgsRubberBand


class PolygonMapTool(QgsMapTool):
    polygonSelected = pyqtSignal(QgsGeometry)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 80, 0, 60))
        self.rubber_band.setStrokeColor(QColor(255, 80, 0, 200))
        self.rubber_band.setWidth(2)
        self.points = []

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        point = self.toMapCoordinates(event.pos())
        self.points.append(point)
        self._redraw(point)

    def canvasMoveEvent(self, event):
        if not self.points:
            return
        preview_point = self.toMapCoordinates(event.pos())
        self._redraw(preview_point)

    def canvasDoubleClickEvent(self, event):
        self._finish()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._reset()

    def _redraw(self, preview_point):
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for point in self.points:
            self.rubber_band.addPoint(point, False)
        self.rubber_band.addPoint(preview_point, True)

    def _finish(self):
        points = list(self.points)
        self._reset()
        if len(points) < 3:
            return
        geometry = QgsGeometry.fromPolygonXY([points])
        self.polygonSelected.emit(geometry)

    def _reset(self):
        self.points = []
        self.rubber_band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

    def deactivate(self):
        self._reset()
        super().deactivate()
