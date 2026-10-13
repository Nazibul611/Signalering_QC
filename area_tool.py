# -*- coding: utf-8 -*-
"""Manual review areas: the reviewer draws the area to be reviewed on the map (polygon or rectangle) or
picks it from a grid (generated regular grid, or the cells of an existing polygon layer / "grid shape").

Contents
* AreaDrawTool - map tool: click vertices (polygon) or drag (rectangle).
* PickTool     - map tool: click a cell, or drag a rectangle over several cells.
* AreaPanel    - small non-modal window that holds the chosen areas (with live pin counts) and saves them.
"""
import math
from collections import Counter

from qgis.PyQt.QtCore import QEvent, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                                 QLabel, QListWidget, QPushButton, QVBoxLayout)
from qgis.core import (QgsFeatureRequest, QgsGeometry, QgsMapLayerProxyModel, QgsPointXY, QgsRectangle,
                       QgsWkbTypes)
from qgis.gui import QgsMapLayerComboBox, QgsMapTool, QgsRubberBand

from . import config, qc_rounds, utils, workflow
from .utils import val

MAX_GRID_LINES = 300       # do not draw a grid overlay finer than this many lines per direction
MAX_DRAG_CELLS = 500       # a drag-select never adds more cells than this


def _band(canvas, geom_type, line=(0, 110, 90), fill_alpha=45, width=2):
    rb = QgsRubberBand(canvas, geom_type)
    rb.setColor(QColor(line[0], line[1], line[2], 255))
    rb.setFillColor(QColor(line[0], line[1], line[2], fill_alpha))
    rb.setWidth(width)
    return rb


def _clear(canvas, rb):
    if rb is None:
        return
    try:
        rb.reset(QgsWkbTypes.PolygonGeometry)
        canvas.scene().removeItem(rb)
    except Exception:
        pass


# ====================================================================== map tools
class AreaDrawTool(QgsMapTool):
    """mode "polygon": left-click = add vertex, right-click / double-click / Enter = finish,
    Backspace = remove last vertex, Esc = cancel.   mode "rect": press-drag-release.
    Emits finished(QgsGeometry in canvas CRS)."""
    finished = pyqtSignal(QgsGeometry)

    def __init__(self, canvas, mode="polygon"):
        super().__init__(canvas)
        self.mode = mode
        self._pts = []
        self._cur = None
        self._start = None
        self._rb = None
        self._ignore_release = False
        self.setCursor(Qt.CrossCursor)

    def set_mode(self, mode):
        self._reset()
        self.mode = mode

    # -- helpers
    def _ensure_band(self):
        if self._rb is None:
            self._rb = _band(self.canvas(), QgsWkbTypes.PolygonGeometry, (0, 110, 90), 40, 2)
        return self._rb

    def _reset(self):
        self._pts, self._cur, self._start = [], None, None
        _clear(self.canvas(), self._rb)
        self._rb = None

    def _draw_polygon(self):
        rb = self._ensure_band()
        rb.reset(QgsWkbTypes.PolygonGeometry)
        pts = list(self._pts) + ([self._cur] if self._cur is not None else [])
        for i, p in enumerate(pts):
            rb.addPoint(p, i == len(pts) - 1)

    def _finish_polygon(self):
        pts = []
        for p in self._pts:                       # drop consecutive duplicates (double-click)
            if not pts or (abs(p.x() - pts[-1].x()) > 1e-9 or abs(p.y() - pts[-1].y()) > 1e-9):
                pts.append(p)
        if len(pts) < 3:
            self._reset()
            return
        geom = QgsGeometry.fromPolygonXY([pts])
        self._reset()
        self.finished.emit(geom)

    # -- events
    def canvasPressEvent(self, e):
        if self.mode == "rect" and e.button() == Qt.LeftButton:
            self._reset()
            self._start = self.toMapCoordinates(e.pos())

    def canvasMoveEvent(self, e):
        if self.mode == "rect":
            if self._start is None:
                return
            rect = QgsRectangle(self._start, self.toMapCoordinates(e.pos()))
            self._ensure_band().setToGeometry(QgsGeometry.fromRect(rect), None)
        elif self._pts:
            self._cur = self.toMapCoordinates(e.pos())
            self._draw_polygon()

    def canvasReleaseEvent(self, e):
        if self.mode == "rect":
            if self._start is None or e.button() != Qt.LeftButton:
                return
            rect = QgsRectangle(self._start, self.toMapCoordinates(e.pos()))
            tol = self.canvas().mapUnitsPerPixel() * 4
            self._reset()
            if rect.width() > tol and rect.height() > tol:      # ignore a plain click
                self.finished.emit(QgsGeometry.fromRect(rect))
            return
        if self._ignore_release:
            self._ignore_release = False
            return
        if e.button() == Qt.LeftButton:
            self._pts.append(self.toMapCoordinates(e.pos()))
            self._cur = None
            self._draw_polygon()
        elif e.button() == Qt.RightButton:
            self._finish_polygon()

    def canvasDoubleClickEvent(self, e):
        if self.mode == "polygon" and e.button() == Qt.LeftButton:
            self._ignore_release = True      # the release that follows a double-click adds no vertex
            self._finish_polygon()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self._reset()
            e.accept()
        elif k in (Qt.Key_Backspace, Qt.Key_Delete) and self.mode == "polygon":
            if self._pts:
                self._pts.pop()
                self._cur = None
                self._draw_polygon() if self._pts else self._reset()
            e.accept()
        elif k in (Qt.Key_Return, Qt.Key_Enter) and self.mode == "polygon":
            self._finish_polygon()
            e.accept()

    def deactivate(self):
        self._reset()
        super().deactivate()


class PickTool(QgsMapTool):
    """Click = point, drag = rectangle. Emits picked(point, rect_or_None, modifiers) in canvas CRS and
    hovered(point) while the mouse moves."""
    picked = pyqtSignal(QgsPointXY, object, object)
    hovered = pyqtSignal(QgsPointXY)

    def __init__(self, canvas):
        super().__init__(canvas)
        self._press = None
        self._start = None
        self._rb = None
        self.setCursor(Qt.PointingHandCursor)

    def _reset(self):
        self._press = self._start = None
        _clear(self.canvas(), self._rb)
        self._rb = None

    def canvasPressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._reset()
        self._press = e.pos()
        self._start = self.toMapCoordinates(e.pos())

    def canvasMoveEvent(self, e):
        p = self.toMapCoordinates(e.pos())
        if self._start is None:
            self.hovered.emit(p)
            return
        if (e.pos() - self._press).manhattanLength() < 5:
            return
        if self._rb is None:
            self._rb = _band(self.canvas(), QgsWkbTypes.PolygonGeometry, (37, 99, 235), 40, 2)
        self._rb.setToGeometry(QgsGeometry.fromRect(QgsRectangle(self._start, p)), None)

    def canvasReleaseEvent(self, e):
        if self._start is None or e.button() != Qt.LeftButton:
            return
        end = self.toMapCoordinates(e.pos())
        dragged = (e.pos() - self._press).manhattanLength() >= 5
        start = self._start
        self._reset()
        if dragged:
            self.picked.emit(end, QgsRectangle(start, end), e.modifiers())
        else:
            self.picked.emit(end, None, e.modifiers())

    def deactivate(self):
        self._reset()
        super().deactivate()


# ====================================================================== panel
class AreaPanel(QDialog):
    """Pending areas are kept in self.entries: dicts with key, geom (QgsGeometry in the CRS of the pin
    layer), label and method. Nothing is written until 'Save areas'."""

    MODES = (("polygon", "Draw polygon on the map"),
             ("rect", "Draw rectangle on the map"),
             ("grid", "Pick cells of a regular grid"),
             ("layer", "Pick cells from a polygon layer (grid shape)"),
             ("sample", "Re-select existing areas (recheck in this QC round)"))

    def __init__(self, plugin, cfg, layers, hint_operators=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signalering QC - Define review areas")
        self.setWindowFlags(self.windowFlags() | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.plugin = plugin
        self.cfg, self.layers = cfg, layers
        self.canvas = plugin.iface.mapCanvas()
        self.entries = []
        self._counter = 0
        self.infos, self.crs = [], None
        self.areas = []                       # existing sample areas (for rechecks)
        self.round = qc_rounds.active_round(cfg)
        self.resize(420, 600)

        self.draw_tool = AreaDrawTool(self.canvas, "polygon")
        self.draw_tool.finished.connect(self._on_drawn)
        self.pick_tool = PickTool(self.canvas)
        self.pick_tool.picked.connect(self._on_picked)
        self.pick_tool.hovered.connect(self._on_hover)
        self.rb_pending = None
        self.rb_hover = None
        self.rb_grid = None

        lay = QVBoxLayout(self)
        if hint_operators:
            lab = QLabel("Extra areas are needed for: <b>%s</b> (verdict 'Not accepted'). "
                         "Choose areas that contain many of their pins." % ", ".join(hint_operators))
            lab.setWordWrap(True)
            lab.setStyleSheet("background:#fff3e0; padding:6px; border:1px solid #e65100;")
            lay.addWidget(lab)

        self.round_bar = qc_rounds.RoundBar(show_level=True)
        self.round_bar.refresh(cfg)
        self.round_bar.changed.connect(self._on_round_changed)
        lay.addWidget(self.round_bar)
        self.lbl_round = QLabel()
        self.lbl_round.setWordWrap(True)
        self.lbl_round.setStyleSheet("color:#475569;")
        lay.addWidget(self.lbl_round)

        form = QFormLayout()
        self.cmb_mode = QComboBox()
        for code, text in self.MODES:
            self.cmb_mode.addItem(text, code)
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Tool", self.cmb_mode)

        self.spn_grid = QDoubleSpinBox()
        self.spn_grid.setRange(5, 100000)
        self.spn_grid.setDecimals(0)
        self.spn_grid.setSuffix(" m")
        self.spn_grid.setValue(float(cfg.get("sampling", {}).get("grid_size", 250)))
        self.spn_grid.valueChanged.connect(self._grid_changed)
        form.addRow("Grid cell size", self.spn_grid)

        self.cmb_layer = QgsMapLayerComboBox()
        self.cmb_layer.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.cmb_layer.layerChanged.connect(self._layer_changed)
        form.addRow("Polygon layer", self.cmb_layer)

        self.chk_merge = QCheckBox("Merge all picked cells into ONE area")
        form.addRow("", self.chk_merge)
        lay.addLayout(form)

        self.lbl_hint = QLabel()
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:#475569;")
        lay.addWidget(self.lbl_hint)

        row = QHBoxLayout()
        self.btn_tool = QPushButton("Activate tool on map")
        self.btn_tool.clicked.connect(self.activate_tool)
        row.addWidget(self.btn_tool)
        lay.addLayout(row)

        lay.addWidget(QLabel("Chosen areas (not saved yet):"))
        self.lst = QListWidget()
        self.lst.setSelectionMode(QListWidget.ExtendedSelection)
        lay.addWidget(self.lst, 1)

        row = QHBoxLayout()
        self.btn_remove = QPushButton("Remove selected")
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear = QPushButton("Clear all")
        self.btn_clear.clicked.connect(self._clear_all)
        row.addWidget(self.btn_remove)
        row.addWidget(self.btn_clear)
        lay.addLayout(row)

        self.lbl_total = QLabel()
        lay.addWidget(self.lbl_total)

        row = QHBoxLayout()
        self.btn_save = QPushButton("Save areas")
        self.btn_save.setStyleSheet("font-weight:bold;")
        self.btn_save.clicked.connect(self._save)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_save, 1)
        row.addWidget(self.btn_close)
        lay.addLayout(row)

        self.canvas.extentsChanged.connect(self._draw_grid)
        self.refresh_infos()
        self._mode_changed()

    # ------------------------------------------------------------------ data
    def refresh_infos(self):
        self.infos, self.crs = workflow.collect_infos(self.cfg, self.layers)
        self._load_areas()
        self._refresh()

    def _load_areas(self):
        self.areas = []
        sample = utils.get_sample_layer(self.cfg)
        if sample is None or self.crs is None:
            return
        xf = utils.transformer(sample.crs(), self.crs)
        for f in sample.getFeatures():
            g = QgsGeometry(f.geometry())
            if g.isNull():
                continue
            if xf is not None:
                g.transform(xf)
            self.areas.append({"area_id": val(f, "area_id"), "geom": g, "n": int(val(f, "n_feat") or 0),
                               "rounds": qc_rounds.area_rounds(val(f, "qc_rounds"))})

    def _on_round_changed(self):
        self.cfg = config.load_config()
        self.round = qc_rounds.active_round(self.cfg)
        self.round_bar.refresh(self.cfg)
        self.entries = []
        self.refresh_infos()
        if self.plugin.panel is not None:
            self.plugin.panel.reload()

    def _match_area(self, geom):
        """An existing area with (nearly) the same shape as `geom` - the same cell picked a second time."""
        ga = geom.area()
        for a in self.areas:
            if not a["geom"].boundingBox().intersects(geom.boundingBox()):
                continue
            sym = a["geom"].symDifference(geom)
            if sym is not None and not sym.isNull() and sym.area() <= 0.02 * max(ga, 1e-9):
                return a
        return None

    def mode(self):
        return self.cmb_mode.currentData()

    def _xf(self, to_primary):
        cc = self.canvas.mapSettings().destinationCrs()
        return utils.transformer(cc, self.crs) if to_primary else utils.transformer(self.crs, cc)

    def _to_primary(self, geom):
        g = QgsGeometry(geom)
        xf = self._xf(True)
        if xf is not None:
            g.transform(xf)
        return g

    def _to_canvas(self, geom):
        g = QgsGeometry(geom)
        xf = self._xf(False)
        if xf is not None:
            g.transform(xf)
        return g

    # ------------------------------------------------------------------ mode / tool
    def _mode_changed(self, *_):
        m = self.mode()
        self.spn_grid.setEnabled(m == "grid")
        self.cmb_layer.setEnabled(m == "layer")
        self.chk_merge.setEnabled(m in ("grid", "layer"))
        self.lbl_round.setText("New areas and rechecks are added to QC round %s. An area that is already in "
                               "this round is not added again; picking an existing area (or the same grid "
                               "cell) for ANOTHER round is a recheck - the earlier results are kept." %
                               (self.round or "(none yet - one is created when you save)"))
        self.lbl_hint.setText({
            "polygon": "Left-click to place corners. Right-click, double-click or Enter finishes the area. "
                       "Backspace removes the last corner, Esc cancels.",
            "rect": "Drag a rectangle on the map. Draw as many areas as you need, then click 'Save areas'.",
            "grid": "Click a grid cell to select it (click again to deselect). Drag a rectangle to select "
                    "many cells; hold Ctrl while dragging to deselect. Cells are multiples of the cell size "
                    "in the coordinate system of the pin layer.",
            "layer": "Click a polygon of the chosen layer to select it (click again to deselect). Drag a "
                     "rectangle to select all polygons it touches; hold Ctrl to deselect.",
            "sample": "Click an existing review area to review it again in this QC round (a recheck). Drag a "
                      "rectangle to select all areas it touches; Ctrl+drag deselects.",
        }[m])
        if m in ("polygon", "rect"):
            self.draw_tool.set_mode(m)
        self._clear_hover()
        self._draw_grid()
        self.activate_tool()

    def activate_tool(self):
        self.canvas.setMapTool(self.draw_tool if self.mode() in ("polygon", "rect") else self.pick_tool)

    def _grid_changed(self, *_):
        self._draw_grid()

    def _layer_changed(self, *_):
        self._clear_hover()

    # ------------------------------------------------------------------ cell lookup (grid / layer)
    def _grid_cell(self, cx, cy):
        s = self.spn_grid.value()
        key = ("grid", s, cx, cy)
        geom = QgsGeometry.fromRect(QgsRectangle(cx * s, cy * s, (cx + 1) * s, (cy + 1) * s))
        return key, geom, "Grid %d m (%d,%d)" % (s, cx, cy)

    def _cells_at(self, pt):
        """Cells containing the canvas point `pt` -> [(key, geom in primary CRS, label)]."""
        xf = self._xf(True)
        p = xf.transform(pt) if xf is not None else pt
        if self.mode() == "sample":
            pg = QgsGeometry.fromPointXY(p)
            return [self._area_cell(a) for a in self.areas if a["geom"].intersects(pg)][:1]
        if self.mode() == "grid":
            s = self.spn_grid.value()
            return [self._grid_cell(int(math.floor(p.x() / s)), int(math.floor(p.y() / s)))]
        lyr = self.cmb_layer.currentLayer()
        if lyr is None:
            return []
        lxf = utils.transformer(self.crs, lyr.crs())
        lp = lxf.transform(p) if lxf is not None else p
        pg = QgsGeometry.fromPointXY(lp)
        req = QgsFeatureRequest().setFilterRect(QgsRectangle(lp.x(), lp.y(), lp.x(), lp.y()))
        for f in lyr.getFeatures(req):
            g = f.geometry()
            if g is not None and not g.isNull() and g.intersects(pg):
                return [self._layer_cell(lyr, f, g)]
        return []

    def _area_cell(self, a):
        return ("area", a["area_id"]), QgsGeometry(a["geom"]), "Recheck %s" % a["area_id"]

    def _layer_cell(self, lyr, f, g):
        g = QgsGeometry(g)
        xf = utils.transformer(lyr.crs(), self.crs)
        if xf is not None:
            g.transform(xf)
        return (("layer", lyr.id(), f.id()), workflow.clean_polygon(g), "%s #%d" % (lyr.name(), f.id()))

    def _cells_in_rect(self, rect):
        xf = self._xf(True)
        r = xf.transformBoundingBox(rect) if xf is not None else rect
        out = []
        if self.mode() == "sample":
            rg = QgsGeometry.fromRect(r)
            return [self._area_cell(a) for a in self.areas if a["geom"].intersects(rg)]
        if self.mode() == "grid":
            s = self.spn_grid.value()
            x0, x1 = int(math.floor(r.xMinimum() / s)), int(math.floor(r.xMaximum() / s))
            y0, y1 = int(math.floor(r.yMinimum() / s)), int(math.floor(r.yMaximum() / s))
            if (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_DRAG_CELLS:
                self.plugin._msg("That rectangle covers too many cells (more than %d). Zoom in or use a "
                                 "larger cell size." % MAX_DRAG_CELLS)
                return []
            return [self._grid_cell(cx, cy) for cx in range(x0, x1 + 1) for cy in range(y0, y1 + 1)]
        lyr = self.cmb_layer.currentLayer()
        if lyr is None:
            return []
        lxf = utils.transformer(self.crs, lyr.crs())
        lr = lxf.transformBoundingBox(r) if lxf is not None else r
        rg = QgsGeometry.fromRect(lr)
        for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(lr)):
            g = f.geometry()
            if g is not None and not g.isNull() and g.intersects(rg):
                out.append(self._layer_cell(lyr, f, g))
                if len(out) > MAX_DRAG_CELLS:
                    self.plugin._msg("That rectangle touches too many polygons (more than %d)." % MAX_DRAG_CELLS)
                    return []
        return out

    # ------------------------------------------------------------------ events
    def _on_drawn(self, geom):
        g = workflow.clean_polygon(self._to_primary(geom))
        if g is None:
            self.plugin._msg("That area is not a valid polygon - draw it again.")
            return
        self._counter += 1
        mode = self.mode()
        self.entries.append({"key": ("draw", self._counter), "geom": g, "label": "Drawn area %d" % self._counter,
                             "method": "manual-polygon" if mode == "polygon" else "manual-rectangle",
                             "kind": "draw"})
        self._refresh()

    def _resolve(self, cell):
        """A picked cell becomes a recheck entry when it is an existing area (or the same cell picked
        again). Returns an entry dict, or None when it is already part of the active QC round."""
        key, geom, label = cell
        area = None
        if key[0] == "area":
            area = next((a for a in self.areas if a["area_id"] == key[1]), None)
        elif self.mode() in ("grid", "layer"):
            area = self._match_area(geom)
        if area is None:
            method = "manual-grid" if self.mode() == "grid" else "manual-layer"
            return {"key": key, "geom": geom, "label": label, "method": method, "kind": "cell"}
        if self.round and self.round in area["rounds"]:
            self.plugin._msg("%s is already in QC round %s." % (area["area_id"], self.round))
            return None
        return {"key": ("area", area["area_id"]), "geom": QgsGeometry(area["geom"]),
                "label": "Recheck %s" % area["area_id"], "method": "recheck", "kind": "recheck",
                "area_id": area["area_id"], "n": area["n"], "rounds": sorted(area["rounds"])}

    def _on_picked(self, pt, rect, mods):
        cells = self._cells_at(pt) if rect is None else self._cells_in_rect(rect)
        if not cells:
            return
        have = {e["key"] for e in self.entries}
        if rect is None:                                          # click: toggle
            e = self._resolve(cells[0])
            if e is None:
                return
            if e["key"] in have:
                self.entries = [x for x in self.entries if x["key"] != e["key"]]
            else:
                self.entries.append(e)
        elif mods & Qt.ControlModifier:                           # ctrl-drag: deselect
            drop = set()
            for c in cells:
                e = self._resolve(c)
                if e is not None:
                    drop.add(e["key"])
            self.entries = [x for x in self.entries if x["key"] not in drop]
        else:                                                     # drag: select
            for c in cells:
                e = self._resolve(c)
                if e is not None and e["key"] not in have:
                    self.entries.append(e)
                    have.add(e["key"])
        self._refresh()

    def _on_hover(self, pt):
        try:
            cells = self._cells_at(pt)
        except Exception:
            cells = []
        if not cells:
            self._clear_hover()
            return
        if self.rb_hover is None:
            self.rb_hover = _band(self.canvas, QgsWkbTypes.PolygonGeometry, (37, 99, 235), 25, 2)
        self.rb_hover.setToGeometry(self._to_canvas(cells[0][1]), None)

    def _clear_hover(self):
        _clear(self.canvas, self.rb_hover)
        self.rb_hover = None

    # ------------------------------------------------------------------ overlays
    def _draw_grid(self, *_):
        _clear(self.canvas, self.rb_grid)
        self.rb_grid = None
        if self.mode() != "grid" or not self.isVisible() or self.crs is None:
            return
        s = self.spn_grid.value()
        xf_in = self._xf(True)
        ext = self.canvas.extent()
        r = xf_in.transformBoundingBox(ext) if xf_in is not None else ext
        x0, x1 = int(math.floor(r.xMinimum() / s)), int(math.ceil(r.xMaximum() / s))
        y0, y1 = int(math.floor(r.yMinimum() / s)), int(math.ceil(r.yMaximum() / s))
        if (x1 - x0) > MAX_GRID_LINES or (y1 - y0) > MAX_GRID_LINES:
            self.lbl_hint.setText("Grid is too fine to draw at this zoom level - zoom in or enlarge the cell size. "
                                  "You can still click cells.")
            return
        lines = [[QgsPointXY(x * s, y0 * s), QgsPointXY(x * s, y1 * s)] for x in range(x0, x1 + 1)]
        lines += [[QgsPointXY(x0 * s, y * s), QgsPointXY(x1 * s, y * s)] for y in range(y0, y1 + 1)]
        g = QgsGeometry.fromMultiPolylineXY(lines)
        self.rb_grid = _band(self.canvas, QgsWkbTypes.LineGeometry, (90, 90, 90), 0, 1)
        self.rb_grid.setToGeometry(self._to_canvas(g), None)

    def _draw_pending(self):
        _clear(self.canvas, self.rb_pending)
        self.rb_pending = None
        if not self.entries:
            return
        g = QgsGeometry.collectGeometry([QgsGeometry(e["geom"]) for e in self.entries])
        self.rb_pending = _band(self.canvas, QgsWkbTypes.PolygonGeometry, (0, 110, 90), 70, 3)
        self.rb_pending.setToGeometry(self._to_canvas(g), None)

    # ------------------------------------------------------------------ list
    def _assignments(self):
        """Per entry: new pins (not for rechecks - those keep their pins). Rechecks do not take pins away
        from the new areas, so only the other entries take part in the assignment."""
        new = [e for e in self.entries if e["kind"] != "recheck"]
        it = iter(workflow.assign_members(self.infos, [e["geom"] for e in new]))
        return [({"members": [], "already": 0} if e["kind"] == "recheck" else next(it)) for e in self.entries]

    def _refresh(self):
        assigned = self._assignments()
        self.lst.clear()
        tot_new = tot_re = 0
        for e, a in zip(self.entries, assigned):
            n = len(a["members"])
            if e["kind"] == "recheck":
                tot_re += e["n"]
                self.lst.addItem("%s - %d pins, earlier rounds: %s" % (e["label"], e["n"],
                                                                      ", ".join(e["rounds"]) or "-"))
                continue
            tot_new += n
            ops = Counter(m.operator for m in a["members"])
            txt = "%s - %d new pins" % (e["label"], n)
            if ops:
                txt += " (%s)" % ", ".join("%s %d" % (o, c) for o, c in sorted(ops.items()))
            else:
                txt += " - nothing to review here, will be skipped"
            if a["already"]:
                txt += " - %d pins already in an earlier area (use 'Re-select existing areas' to recheck it)" % a["already"]
            self.lst.addItem(txt)
        self.lbl_total.setText("%d areas: %d new pins to review, %d pins to recheck" % (
            len(self.entries), tot_new, tot_re))
        self.btn_save.setEnabled(bool(self.entries))
        self.btn_save.setText("Save %d area%s" % (len(self.entries), "" if len(self.entries) == 1 else "s")
                              if self.entries else "Save areas")
        self.btn_remove.setEnabled(bool(self.entries))
        self.btn_clear.setEnabled(bool(self.entries))
        self._draw_pending()

    def _remove_selected(self):
        rows = {self.lst.row(i) for i in self.lst.selectedItems()}
        self.entries = [e for i, e in enumerate(self.entries) if i not in rows]
        self._refresh()

    def _clear_all(self):
        self.entries = []
        self._refresh()

    # ------------------------------------------------------------------ save
    def _save(self):
        entries = list(self.entries)
        if self.chk_merge.isChecked():
            cells = [e for e in entries if e["kind"] == "cell"]
            if len(cells) > 1:
                union = QgsGeometry.unaryUnion([QgsGeometry(e["geom"]) for e in cells])
                union = workflow.clean_polygon(union)
                merged = {"key": ("merged",), "geom": union, "kind": "cell", "method": cells[0]["method"],
                          "label": "Merged %d cells" % len(cells)}
                entries = [e for e in entries if e["kind"] != "cell"] + [merged]
        if self.plugin.commit_manual_areas(entries, self.infos, self.crs):
            self.entries = []
            self.refresh_infos()
            self._draw_grid()

    # ------------------------------------------------------------------ lifecycle
    def showEvent(self, e):
        super().showEvent(e)
        self._draw_grid()

    def event(self, e):
        if e.type() == QEvent.WindowActivate and self.round_bar.sync():
            self._on_round_changed()          # level / round was changed in another window
        return super().event(e)

    def closeEvent(self, e):
        self.release()
        super().closeEvent(e)

    def release(self):
        """Remove every overlay and give the map tool back."""
        try:
            self.canvas.extentsChanged.disconnect(self._draw_grid)
        except Exception:
            pass
        for tool in (self.draw_tool, self.pick_tool):
            try:
                if self.canvas.mapTool() is tool:
                    self.canvas.unsetMapTool(tool)
            except Exception:
                pass
        for name in ("rb_pending", "rb_hover", "rb_grid"):
            _clear(self.canvas, getattr(self, name))
            setattr(self, name, None)
