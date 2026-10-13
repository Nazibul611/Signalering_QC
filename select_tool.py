# -*- coding: utf-8 -*-
"""Rectangle / click selection map tool used by the review panel's multi-select mode."""
import logging
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand


class RectSelectTool(QgsMapTool):
    """Drag a rectangle (or just click). Emits selected(rect_in_canvas_crs, keyboard_modifiers)."""
    selected = pyqtSignal(QgsRectangle, object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self._start = None
        self._rb = None
        self.setCursor(Qt.CrossCursor)

    def _band(self):
        rb = QgsRubberBand(self.canvas(), QgsWkbTypes.PolygonGeometry)
        rb.setColor(QColor(37, 99, 235, 255))
        rb.setFillColor(QColor(37, 99, 235, 50))
        rb.setWidth(2)
        return rb

    def _reset(self):
        self._start = None
        if self._rb is not None:
            try:
                self._rb.reset(QgsWkbTypes.PolygonGeometry)
                self.canvas().scene().removeItem(self._rb)
            except Exception:
                logging.getLogger(__name__).debug("Ignored error", exc_info=True)
            self._rb = None

    def canvasPressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._reset()
        self._start = self.toMapCoordinates(e.pos())
        self._rb = self._band()

    def canvasMoveEvent(self, e):
        if self._start is None or self._rb is None:
            return
        rect = QgsRectangle(self._start, self.toMapCoordinates(e.pos()))
        self._rb.setToGeometry(QgsGeometry.fromRect(rect), None)

    def canvasReleaseEvent(self, e):
        if self._start is None:
            return
        rect = QgsRectangle(self._start, self.toMapCoordinates(e.pos()))
        tol = self.canvas().mapUnitsPerPixel() * 6  # a plain click selects what is within ~6 pixels
        if rect.width() < tol and rect.height() < tol:
            c = rect.center()
            rect = QgsRectangle(c.x() - tol, c.y() - tol, c.x() + tol, c.y() + tol)
        self._reset()
        self.selected.emit(rect, e.modifiers())

    def deactivate(self):
        self._reset()
        super().deactivate()
