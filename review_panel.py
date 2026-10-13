# -*- coding: utf-8 -*-
"""Dock panel: step through every feature of a sampled area and judge it."""
import getpass
from collections import Counter, defaultdict

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QGridLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
try:
    from qgis.PyQt.QtWidgets import QShortcut
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QShortcut
from qgis.core import (Qgis, QgsCoordinateTransform, QgsExpression, QgsFeature, QgsFeatureRequest,
                       QgsGeometry, QgsProject, QgsSettings, QgsWkbTypes)
from qgis.gui import QgsMapToolEmitPoint

from . import config, qc_rounds, utils, workflow
from .select_tool import RectSelectTool
from .utils import LAYER_KEYS, now_str, val

LAYER_LABEL = {"point": "Pinpoint", "line": "Pinline", "polygon": "Pinpolygon"}


class ReviewPanel(QDockWidget):
    def __init__(self, iface, plugin):
        super().__init__("Signalering QC - Review", iface.mainWindow())
        self.setObjectName("SignaleringQCReviewPanel")
        self.iface, self.plugin, self.canvas = iface, plugin, iface.mapCanvas()
        self.cfg, self.layers, self.sample = {}, {}, None
        self.items, self.status_cache, self.idx = [], {}, 0
        self._theme_state = 0
        self._static_sc, self._dyn_sc = [], []
        self._amap, self._status_names = {}, set()   # status text -> kind (see utils.status_alias_map)
        self.batch_sel = set()   # (layer_key, fid) chosen in multi-select mode
        self._undo = None        # last judgement, so it can be undone
        self.round, self.rl, self.results = None, None, {}   # active QC round, QC_Results layer, its rows

        self.missed_tool = QgsMapToolEmitPoint(self.canvas)
        self.missed_tool.canvasClicked.connect(self._on_missed_click)
        self.sel_tool = RectSelectTool(self.canvas)
        self.sel_tool.selected.connect(self._on_rect_selected)
        self.canvas.mapToolSet.connect(self._on_maptool_set)

        self._build_ui()
        self._add_static_shortcuts()
        self.visibilityChanged.connect(self._on_visibility)

    # ================================================================== UI
    def _build_ui(self):
        body = QWidget()
        lay = QVBoxLayout(body)

        lvl = QHBoxLayout()
        lvl.addWidget(QLabel("Review as:"))
        self.cmb_level = QComboBox()
        self.cmb_level.setToolTip("Each review level stores its own result, error type and remark in separate fields.")
        self.cmb_level.currentIndexChanged.connect(self._on_level_changed)
        lvl.addWidget(self.cmb_level, 1)
        lay.addLayout(lvl)
        self.round_bar = qc_rounds.RoundBar()
        self.round_bar.changed.connect(self._on_round_changed)
        lay.addWidget(self.round_bar)

        row = QHBoxLayout()
        self.cmb_area = QComboBox()
        self.cmb_area.currentIndexChanged.connect(self._on_area_changed)
        row.addWidget(self.cmb_area, 1)
        b = QPushButton("Zoom area")
        b.clicked.connect(lambda _=False: self._zoom_area())
        row.addWidget(b)
        lay.addLayout(row)

        self.chk_open = QCheckBox("Only open areas")
        self.chk_open.setChecked(True)
        self.chk_open.toggled.connect(lambda _=None: self._fill_areas())
        lay.addWidget(self.chk_open)

        nav = QHBoxLayout()
        self.btn_prev = QPushButton("<  Prev (Ctrl+Left)")
        self.btn_next = QPushButton("Next  > (Ctrl+Right)")
        self.btn_unrev = QPushButton("Next unreviewed")
        self.btn_prev.clicked.connect(lambda _=False: self._step(-1))
        self.btn_next.clicked.connect(lambda _=False: self._step(1))
        self.btn_unrev.clicked.connect(lambda _=False: self._go_unreviewed())
        for b in (self.btn_prev, self.btn_next, self.btn_unrev):
            nav.addWidget(b)
        lay.addLayout(nav)

        self.lbl_pos = QLabel("-")
        self.lbl_pos.setStyleSheet("font-weight:bold")
        lay.addWidget(self.lbl_pos)
        self.lbl_flags = QLabel()
        self.lbl_flags.setWordWrap(True)
        self.lbl_flags.setStyleSheet("color:#b00020")
        lay.addWidget(self.lbl_flags)

        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["Field", "Value"])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setMinimumHeight(170)
        lay.addWidget(self.tbl)

        self.lbl_state = QLabel()
        lay.addWidget(self.lbl_state)
        self.lbl_other = QLabel()
        self.lbl_other.setWordWrap(True)
        self.lbl_other.setStyleSheet("color:#475569")
        lay.addWidget(self.lbl_other)
        self.lbl_hist = QLabel()
        self.lbl_hist.setWordWrap(True)
        self.lbl_hist.setStyleSheet("color:#7c3aed")
        lay.addWidget(self.lbl_hist)
        self.txt_remark = QLineEdit()
        self.txt_remark.setPlaceholderText("QC remark (optional). In multi-select mode it is applied to every selected feature.")
        lay.addWidget(self.txt_remark)

        self.chk_batch = QCheckBox("Multi-select mode: judge several features at once")
        self.chk_batch.toggled.connect(self._toggle_batch)
        lay.addWidget(self.chk_batch)
        self.batch_box = QWidget()
        bl = QVBoxLayout(self.batch_box)
        bl.setContentsMargins(0, 0, 0, 0)
        self.btn_sel_map = QPushButton("Select on map  (drag a rectangle; Shift adds, Ctrl removes)")
        self.btn_sel_map.setCheckable(True)
        self.btn_sel_map.toggled.connect(self._toggle_sel_tool)
        bl.addWidget(self.btn_sel_map)
        sel_row = QHBoxLayout()
        for text, fn in (("All unreviewed", self._sel_all_unreviewed), ("All in area", self._sel_all),
                         ("Clear selection", self._sel_clear)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            sel_row.addWidget(b)
        bl.addLayout(sel_row)
        self.lbl_sel = QLabel("0 selected")
        self.lbl_sel.setStyleSheet("font-weight:bold;color:#1d4ed8")
        bl.addWidget(self.lbl_sel)
        self.batch_box.setVisible(False)
        lay.addWidget(self.batch_box)

        self.row_ok = QHBoxLayout()
        self.btn_ok, self.btn_doubt = QPushButton(), QPushButton()
        self.btn_ok.setStyleSheet("background:#bbf7d0")
        self.btn_doubt.setStyleSheet("background:#fde68a")
        self.btn_ok.clicked.connect(lambda _=False: self._judge("accepted"))
        self.btn_doubt.clicked.connect(lambda _=False: self._judge("doubtful"))
        self.row_ok.addWidget(self.btn_ok)
        self.row_ok.addWidget(self.btn_doubt)
        lay.addLayout(self.row_ok)
        self.grid_err = QGridLayout()
        lay.addLayout(self.grid_err)
        self.btn_undet = QPushButton()
        self.btn_undet.setStyleSheet("background:#e2e8f0")
        self.btn_undet.setToolTip("Not wrong, but it cannot be detected / assessed (e.g. not visible on the imagery).\n"
                                  "Counts as handled, but is EXCLUDED from the QC statistics (error rate).")
        self.btn_undet.clicked.connect(lambda _=False: self._judge("undetectable"))
        lay.addWidget(self.btn_undet)

        self.chk_adv = QCheckBox("Auto-advance after judging")
        self.btn_undo = QPushButton("Undo last judgement")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(lambda _=False: self._undo_last())
        adv = QHBoxLayout()
        adv.addWidget(self.chk_adv, 1)
        adv.addWidget(self.btn_undo)
        lay.addLayout(adv)

        th = QHBoxLayout()
        self.cmb_a, self.cmb_b = QComboBox(), QComboBox()
        self.btn_theme = QPushButton("Switch (Ctrl+Alt+T)")
        self.btn_theme.clicked.connect(lambda _=False: self._toggle_theme())
        th.addWidget(QLabel("Map themes:"))
        th.addWidget(self.cmb_a, 1)
        th.addWidget(self.cmb_b, 1)
        th.addWidget(self.btn_theme)
        lay.addLayout(th)

        ms = QHBoxLayout()
        self.btn_missed = QPushButton("Add missed mutation (click map)")
        self.btn_missed.setCheckable(True)
        self.btn_missed.toggled.connect(self._toggle_missed)
        self.cmb_missed_op = QComboBox()
        self.cmb_missed_op.setEditable(True)
        ms.addWidget(self.btn_missed, 1)
        ms.addWidget(QLabel("for operator"))
        ms.addWidget(self.cmb_missed_op, 1)
        lay.addLayout(ms)

        rv = QHBoxLayout()
        rv.addWidget(QLabel("Reviewer:"))
        self.txt_reviewer = QLineEdit()
        st = QgsSettings()
        try:
            default_name = getpass.getuser()
        except Exception:
            default_name = ""
        self.txt_reviewer.setText(st.value("signalering_qc/reviewer", default_name))
        self.txt_reviewer.editingFinished.connect(
            lambda: QgsSettings().setValue("signalering_qc/reviewer", self.txt_reviewer.text().strip()))
        rv.addWidget(self.txt_reviewer, 1)
        lay.addLayout(rv)
        lay.addStretch(1)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(body)
        self.setWidget(sc)

    # ================================================================== shortcuts
    def _shortcut(self, seq, fn, bucket):
        s = QShortcut(QKeySequence(seq), self.iface.mainWindow())
        s.activated.connect(fn)
        s.setEnabled(self.isVisible())
        bucket.append(s)

    def _add_static_shortcuts(self):
        self._shortcut("Ctrl+Right", lambda: self._step(1), self._static_sc)
        self._shortcut("Ctrl+Left", lambda: self._step(-1), self._static_sc)
        self._shortcut("Ctrl+Alt+T", self._toggle_theme, self._static_sc)

    def _on_visibility(self, visible):
        for s in self._static_sc + self._dyn_sc:
            s.setEnabled(bool(visible))

    def remove_shortcuts(self):
        for s in self._static_sc + self._dyn_sc:
            s.setEnabled(False)
            s.deleteLater()
        self._static_sc, self._dyn_sc = [], []

    def _rebuild_error_buttons(self):
        for s in self._dyn_sc:
            s.setEnabled(False)
            s.deleteLater()
        self._dyn_sc = []
        while self.grid_err.count():
            w = self.grid_err.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        sv = self.cfg["status_values"]
        self.btn_ok.setText("%s  (Ctrl+1)" % sv["accepted"])
        self.btn_doubt.setText("%s  (Ctrl+2)" % sv["doubtful"])
        self.btn_undet.setText("%s - not in QC  (Ctrl+0)" % sv["undetectable"])
        self._shortcut("Ctrl+1", lambda: self._judge("accepted"), self._dyn_sc)
        self._shortcut("Ctrl+2", lambda: self._judge("doubtful"), self._dyn_sc)
        self._shortcut("Ctrl+0", lambda: self._judge("undetectable"), self._dyn_sc)
        for i, et in enumerate(self.cfg["error_types"]):
            label = et["label"]
            hint = "  (Ctrl+%d)" % (i + 3) if i < 7 else ""
            b = QPushButton("%s%s" % (label, hint))
            b.setStyleSheet("background:%s" % ("#fecaca" if et.get("severity", "major") == "major" else "#fed7aa"))
            b.setToolTip("Reject - severity: %s" % et.get("severity", "major"))
            b.clicked.connect(lambda _=False, l=label: self._judge("rejected", l))
            self.grid_err.addWidget(b, i // 2, i % 2)
            if i < 7:
                self._shortcut("Ctrl+%d" % (i + 3), lambda l=label: self._judge("rejected", l), self._dyn_sc)

    # ================================================================== data loading
    def reload(self):
        self.cfg = config.load_config()
        self._amap = utils.status_alias_map(self.cfg)
        self._status_names = utils.status_field_names(self.cfg)
        self._fill_levels()
        self.layers = utils.get_layers(self.cfg)
        self.sample = utils.get_sample_layer(self.cfg)
        qc_rounds.ensure_sample_fields(self.sample)
        for lyr in self.layers.values():                 # fields added by newer versions (e.g. the QC round)
            if lyr is not None:
                utils.ensure_fields(lyr, utils.qc_field_specs(self.cfg))
        self.round = qc_rounds.active_round(self.cfg)
        self.rl = qc_rounds.get_results_layer(self.cfg)
        self.round_bar.refresh(self.cfg)
        self.chk_adv.setChecked(bool(self.cfg["review"].get("auto_advance", True)))
        self._rebuild_error_buttons()
        self._fill_themes()
        self._fill_areas()

    def _on_round_changed(self):
        self._undo = None
        self.btn_undo.setEnabled(False)
        self.reload()
        self._push("QC round %s." % (self.round or "- (none yet)"))

    def _fill_themes(self):
        names = QgsProject.instance().mapThemeCollection().mapThemes()
        for cb in (self.cmb_a, self.cmb_b):
            cur = cb.currentText()
            cb.clear()
            cb.addItems(names)
            if cur in names:
                cb.setCurrentText(cur)
        if len(names) > 1 and self.cmb_a.currentText() == self.cmb_b.currentText():
            self.cmb_b.setCurrentIndex(1)

    def current_area(self):
        d = self.cmb_area.currentData()
        return d if isinstance(d, dict) else None

    def _progress(self):
        """{area_id: (features, reviewed in the ACTIVE QC round)}; also loads the round's results."""
        f = self.cfg["fields"]
        self.results = qc_rounds.load_rows(self.rl, self.round) if (self.rl is not None and self.round) else {}
        out = {}
        for key in LAYER_KEYS:
            lyr = self.layers.get(key)
            if lyr is None or lyr.fields().indexOf(f["area"]) < 0:
                continue
            req = QgsFeatureRequest().setFilterExpression(
                "%s IS NOT NULL" % QgsExpression.quotedColumnRef(f["area"]))
            req.setFlags(QgsFeatureRequest.NoGeometry)
            for feat in lyr.getFeatures(req):
                if utils.is_missed(self.cfg, feat, self._amap, self._status_names):
                    continue
                row = self.results.get((key, feat.id()))
                kind = utils.status_kind(self._amap, row["status"]) if row else None
                n, r = out.get(val(feat, f["area"]), (0, 0))
                out[val(feat, f["area"])] = (n + 1, r + (1 if kind in utils.HANDLED_KINDS else 0))
        return out

    @staticmethod
    def _area_sort(r):
        try:
            return (r["round"], int(str(r["area_id"]).split("-A")[-1]))
        except ValueError:
            return (r["round"], 0)

    def _fill_areas(self, pick_open=False):
        keep = None if pick_open else (self.current_area() or {}).get("area_id")
        self.cmb_area.blockSignals(True)
        self.cmb_area.clear()
        if self.sample is None:
            self.cmb_area.blockSignals(False)
            self._clear_items("No areas yet - use 'Define review areas (draw / grid)'.")
            return
        if not self.round:
            self.cmb_area.blockSignals(False)
            self._clear_items("No QC round yet - click 'Add' next to 'QC round' (for example TL-01).")
            return
        prog = self._progress()
        rows = []
        for f in self.sample.getFeatures():
            aid = val(f, "area_id")
            if aid is None or self.round not in qc_rounds.area_rounds(val(f, "qc_rounds")):
                continue
            n, rev = prog.get(aid, (0, 0))
            rows.append({"fid": f.id(), "area_id": aid, "round": int(val(f, "round") or 1),
                         "operators": val(f, "operators") or "", "n": n, "reviewed": rev,
                         "status": val(f, "status") or "Open", "geom": QgsGeometry(f.geometry())})
        rows.sort(key=self._area_sort)
        only_open = self.chk_open.isChecked()
        sel, first_open = 0, None
        for r in rows:
            complete = r["n"] > 0 and r["reviewed"] >= r["n"]
            if only_open and complete and r["area_id"] != keep:
                continue
            self.cmb_area.addItem(self._area_text(r), r)
            i = self.cmb_area.count() - 1
            if r["area_id"] == keep:
                sel = i
            if first_open is None and not complete:
                first_open = i
        if not self.cmb_area.count() and rows and only_open:
            # every area is fully reviewed: list them all anyway so they can still be opened
            for r in rows:
                self.cmb_area.addItem(self._area_text(r), r)
            self.iface.messageBar().pushMessage("Signalering QC",
                                                "All areas are fully reviewed - showing all of them.", level=Qgis.Info, duration=5)
        if pick_open and first_open is not None:
            sel = first_open
        if self.cmb_area.count():
            self.cmb_area.setCurrentIndex(sel)
        self.cmb_area.blockSignals(False)
        if self.cmb_area.count():
            self._on_area_changed()
        else:
            self._clear_items("No open areas left - open the Dashboard." if rows else
                              "No areas in QC round %s yet - use 'Define review areas'." % self.round)

    @staticmethod
    def _area_text(r):
        return "%s  [%d/%d]  %s" % (r["area_id"], r["reviewed"], r["n"], r["operators"])

    def _clear_items(self, msg):
        self.items, self.status_cache, self.idx = [], {}, 0
        self.batch_sel = set()
        self._update_sel_label()
        self.lbl_pos.setText(msg)
        self.lbl_flags.clear()
        self.lbl_state.clear()
        self.lbl_hist.clear()
        self.tbl.setRowCount(0)

    def _on_area_changed(self, *_):
        self._load_items()
        if self.chk_batch.isChecked():
            self._set_batch(set())
        first = self._first_unreviewed(-1)
        self.idx = first if first is not None else 0
        self._show_current()

    def _load_items(self):
        area = self.current_area()
        self.items, self.status_cache = [], {}
        ops = set()
        if area is None:
            return
        f = self.cfg["fields"]
        expr = "%s = %s" % (QgsExpression.quotedColumnRef(f["area"]), QgsExpression.quotedValue(area["area_id"]))
        for key in LAYER_KEYS:
            lyr = self.layers.get(key)
            if lyr is None or lyr.fields().indexOf(f["area"]) < 0:
                continue
            for feat in lyr.getFeatures(QgsFeatureRequest().setFilterExpression(expr)):
                if utils.is_missed(self.cfg, feat, self._amap, self._status_names):
                    continue
                self.items.append((key, feat.id()))
                row = self.results.get((key, feat.id()))
                self.status_cache[(key, feat.id())] = row["status"] if row else None
                ops.add(str(val(feat, self.cfg["operator_field"]) or ""))
        self.items.sort(key=lambda t: (LAYER_KEYS.index(t[0]), t[1]))
        self.cmb_missed_op.clear()
        self.cmb_missed_op.addItems(sorted(o for o in ops if o))

    def _is_reviewed(self, item):
        return utils.status_kind(self._amap, self.status_cache.get(item)) in utils.HANDLED_KINDS

    def _refresh_status(self):
        """Re-read the results of this area in the active QC round from QC_Results (they may have been changed
        outside the panel, by an earlier session or by another reviewer)."""
        area = self.current_area()
        if self.rl is None or not self.round or area is None:
            return
        rows = qc_rounds.load_rows(self.rl, self.round, area_id=area["area_id"])
        for it in self.items:
            if it in rows:
                self.results[it] = rows[it]
                self.status_cache[it] = rows[it]["status"]
            else:
                self.results.pop(it, None)
                self.status_cache[it] = None

    def _first_unreviewed(self, after):
        n = len(self.items)
        for k in range(1, n + 1):
            i = (after + k) % n if n else 0
            if i < n and not self._is_reviewed(self.items[i]):
                return i
        return None

    # ================================================================== display
    def _show_current(self):
        area = self.current_area()
        if not self.items or area is None:
            self._clear_items("No features in this area.")
            return
        self.idx = max(0, min(self.idx, len(self.items) - 1))
        key, fid = self.items[self.idx]
        layer = self.layers[key]
        feat = layer.getFeature(fid)
        if not feat.isValid():
            return
        f = self.cfg["fields"]
        self._refresh_status()
        reviewed = sum(1 for it in self.items if self._is_reviewed(it))
        area["reviewed"], area["n"] = reviewed, len(self.items)
        self.cmb_area.setItemText(self.cmb_area.currentIndex(), self._area_text(area))
        self.lbl_pos.setText("%s  |  %s  |  %d/%d  |  %s #%d  |  reviewed %d/%d" % (
            self.round, area["area_id"], self.idx + 1, len(self.items), LAYER_LABEL[key], fid, reviewed, len(self.items)))

        qc_names = set(f.values())
        names = [fl.name() for fl in layer.fields() if fl.name() not in qc_names] + \
                [fl.name() for fl in layer.fields() if fl.name() in qc_names]
        self.tbl.setRowCount(len(names))
        for r, n in enumerate(names):
            v = val(feat, n)
            self.tbl.setItem(r, 0, QTableWidgetItem(n))
            self.tbl.setItem(r, 1, QTableWidgetItem("" if v is None else str(v)))
        self.tbl.resizeColumnToContents(0)

        auto = val(feat, f["auto"])
        if auto:
            rn = {r["id"]: r.get("name", r["id"]) for r in self.cfg.get("rules", [])}
            self.lbl_flags.setText("Pre-check: " + "; ".join("%s %s" % (c, rn.get(c, "")) for c in str(auto).split(";")))
        else:
            self.lbl_flags.clear()
        row = self.results.get((key, fid))
        lvl_label = "%s %s" % (self.cfg.get("active_level_label", "QC"), self.round)
        if row and row.get("status"):
            self.lbl_state.setText("%s review: %s %s  (%s, %s)" % (
                lvl_label, row["status"], row.get("error_type") or "", row.get("reviewer") or "?",
                row.get("judged") or "?"))
        else:
            self.lbl_state.setText("%s review: not reviewed yet" % lvl_label)
        earlier = [r for r in qc_rounds.history(self.rl, key, fid, self.cfg.get("active_level"))
                   if r["qc_round"] != self.round]
        self.lbl_hist.setText("Earlier rounds:  " + "   |   ".join(
            "%s: %s%s (%s)" % (r["qc_round"], r.get("status") or "?",
                               " - %s" % r["error_type"] if r.get("error_type") else "", r.get("judged") or "?")
            for r in earlier) if earlier else "")
        others = []                          # what the other review level(s) decided about this feature
        for _code, label, lf in config.level_list(self.cfg):
            if lf["status"] == f["status"]:
                continue
            s2 = val(feat, lf["status"])
            if s2:
                et2 = val(feat, lf["error_type"])
                others.append("%s: %s%s" % (label, s2, " - %s" % et2 if et2 else ""))
        self.lbl_other.setText("   |   ".join(others))
        if self.chk_batch.isChecked():
            return  # multi-select: keep the typed remark, the selection and the map view as they are
        self.txt_remark.setText(str((row or {}).get("remark") or ""))

        for lyr in self.layers.values():
            if lyr is not None:
                lyr.removeSelection()
        layer.selectByIds([fid])
        self._zoom_to_feature(layer, feat)

    def _zoom_to_feature(self, layer, feat):
        g = QgsGeometry(feat.geometry())
        if g.isNull():
            return
        dest = self.canvas.mapSettings().destinationCrs()
        if layer.crs() != dest:
            g.transform(QgsCoordinateTransform(layer.crs(), dest, QgsProject.instance()))
        box = g.boundingBox()
        scale = float(self.cfg["review"].get("zoom_scale", 500))
        if box.width() < 0.01 or box.height() < 0.01:
            self.canvas.setCenter(box.center())
            self.canvas.zoomScale(scale)
        else:
            box.scale(2.0)
            self.canvas.setExtent(box)
            if self.canvas.scale() < scale:
                self.canvas.zoomScale(scale)
        self.canvas.refresh()
        try:
            self.canvas.flashFeatureIds(layer, [feat.id()])
        except Exception:
            pass

    def _zoom_area(self):
        area = self.current_area()
        if area is None or self.sample is None:
            return
        g = QgsGeometry(area["geom"])
        dest = self.canvas.mapSettings().destinationCrs()
        if self.sample.crs() != dest:
            g.transform(QgsCoordinateTransform(self.sample.crs(), dest, QgsProject.instance()))
        box = g.boundingBox()
        box.scale(1.15)
        self.canvas.setExtent(box)
        self.canvas.refresh()

    # ================================================================== navigation
    def _step(self, d):
        if not self.items:
            return
        self.idx = (self.idx + d) % len(self.items)
        self._show_current()

    def _go_unreviewed(self):
        nxt = self._first_unreviewed(self.idx)
        if nxt is None:
            self._push("Every feature in this area is reviewed.")
            return
        self.idx = nxt
        self._show_current()

    def _push(self, msg, level=None):
        self.iface.messageBar().pushMessage("Signalering QC", msg,
                                            level=level if level is not None else Qgis.Info, duration=4)

    # ================================================================== judging
    def _qc_value_fields(self):
        f = self.cfg["fields"]
        return [f["status"], f["error_type"], f["remark"], f["date"], f["reviewer"], f["round"], f["qcround"]]

    def _judge(self, status_key, error_label=None):
        area = self.current_area()
        if not self.items or area is None or not self.isVisible():
            return
        if not self.round:
            self._push("Add a QC round first (button 'Add' next to 'QC round').", Qgis.Warning)
            return
        batch = self.chk_batch.isChecked()
        if batch:
            targets = [it for it in self.items if it in self.batch_sel]
            if not targets:
                self._push("Multi-select: select features first ('Select on map' or the buttons below it).",
                           Qgis.Warning)
                return
        else:
            targets = [self.items[self.idx]]
        f, sv = self.cfg["fields"], self.cfg["status_values"]
        for key in sorted({k for k, _ in targets}):
            if self.layers[key].fields().indexOf(f["status"]) < 0:
                self._push("Field %s is missing on the %s layer - open Settings and create the QC fields." %
                           (f["status"], LAYER_LABEL[key]), Qgis.Warning)
                return
        vals = {f["status"]: sv[status_key], f["error_type"]: error_label,
                f["remark"]: self.txt_remark.text().strip() or None, f["date"]: now_str(),
                f["reviewer"]: self.txt_reviewer.text().strip() or None, f["round"]: int(area["round"]),
                f["qcround"]: self.round}
        names = self._qc_value_fields()
        try:
            self.rl = qc_rounds.ensure_results_layer(self.cfg)
        except Exception as exc:
            self._push("Could not open the QC_Results table: %s" % exc, Qgis.Warning)
            return
        undo, changes, rows = [], defaultdict(dict), []
        for key, fid in targets:
            feat = self.layers[key].getFeature(fid)
            old_row = self.results.get((key, fid))
            undo.append((key, fid, {n: val(feat, n) for n in names}, dict(old_row) if old_row else None))
            row = dict(old_row) if old_row else {"qc_round": self.round, "level": self.cfg["active_level"],
                                                 "pin_layer": key, "feat_id": fid, "area_id": area["area_id"]}
            row.update({"status": vals[f["status"]], "error_type": vals[f["error_type"]],
                        "remark": vals[f["remark"]], "judged": vals[f["date"]], "reviewer": vals[f["reviewer"]]})
            rows.append(row)
            changes[key][fid] = vals
        if not qc_rounds.save_rows(self.rl, rows):
            self._push("Could not write the QC result to QC_Results (locked or read-only?).", Qgis.Warning)
            return
        for r in rows:
            self.results[(r["pin_layer"], r["feat_id"])] = r
        for key, ch in changes.items():
            if not utils.write_values(self.layers[key], ch):
                self._push("The result was saved in QC_Results, but the pin layer could not be updated "
                           "(read-only or locked?).", Qgis.Warning)
        for key, fid in targets:
            self.status_cache[(key, fid)] = sv[status_key]
        self._undo = {"area_id": area["area_id"], "items": undo}
        self.btn_undo.setEnabled(True)
        self._after_change(area)
        if batch:
            self.txt_remark.clear()
            self._set_batch(set())
            self._push("%d features set to %s." % (len(targets), sv[status_key]), Qgis.Success)
            if self._first_unreviewed(-1) is None:
                self._advance()
            else:
                self._show_current()
        elif self.chk_adv.isChecked():
            self._advance()
        else:
            self._show_current()

    def _after_change(self, area):
        """Refresh the progress text of the area; the Open/Done status in the sample layer follows the
        default review level (Team Lead)."""
        reviewed = sum(1 for it in self.items if self._is_reviewed(it))
        area["reviewed"], area["n"] = reviewed, len(self.items)
        self.cmb_area.setItemText(self.cmb_area.currentIndex(), self._area_text(area))
        new_status = "Done" if reviewed == len(self.items) else "Open"
        if (area.get("status") != new_status and self.sample is not None
                and self.cfg.get("active_level") == self.cfg.get("default_level", "TL")):
            utils.write_values(self.sample, {area["fid"]: {"status": new_status}})
            area["status"] = new_status

    def _undo_last(self):
        u, area = self._undo, self.current_area()
        if not u:
            self._push("Nothing to undo.")
            return
        if area is None or area["area_id"] != u["area_id"]:
            self._push("Switch back to area %s to undo the last judgement." % u["area_id"], Qgis.Warning)
            return
        changes = defaultdict(dict)
        for key, fid, old, _row in u["items"]:
            changes[key][fid] = old
        for key, ch in changes.items():
            utils.write_values(self.layers[key], ch)
        to_delete, to_restore = [], []
        for key, fid, old, old_row in u["items"]:
            cur = self.results.get((key, fid))
            if old_row is None:
                if cur is not None:
                    to_delete.append(cur.get("rid"))
                self.results.pop((key, fid), None)
                self.status_cache[(key, fid)] = None
            else:
                if cur is not None:
                    old_row["rid"] = cur.get("rid", old_row.get("rid"))
                to_restore.append(old_row)
                self.results[(key, fid)] = old_row
                self.status_cache[(key, fid)] = old_row.get("status")
        if self.rl is not None:
            qc_rounds.delete_rows(self.rl, to_delete)
            qc_rounds.save_rows(self.rl, to_restore)
        self._undo = None
        self.btn_undo.setEnabled(False)
        self._after_change(area)
        self._push("Undid %d judgement(s)." % len(u["items"]))
        self._show_current()

    def _advance(self):
        nxt = self._first_unreviewed(self.idx)
        if nxt is not None:
            self.idx = nxt
            self._show_current()
            return
        area = self.current_area()
        self._push("Area %s is fully reviewed." % area["area_id"], Qgis.Success)
        self._fill_areas(pick_open=True)

    # ================================================================== themes
    def _toggle_theme(self):
        a, b = self.cmb_a.currentText(), self.cmb_b.currentText()
        if not a or not b or a == b:
            self._push("Create two map themes (e.g. '2025' and '2026') and pick them in the panel.", Qgis.Warning)
            return
        target = b if self._theme_state == 0 else a
        self._theme_state ^= 1
        QgsProject.instance().mapThemeCollection().applyTheme(
            target, QgsProject.instance().layerTreeRoot(), self.iface.layerTreeView().layerTreeModel())

    # ================================================================== missed mutations
    def _toggle_missed(self, on):
        if on:
            if self.current_area() is None or self.layers.get("point") is None:
                self.btn_missed.blockSignals(True)
                self.btn_missed.setChecked(False)
                self.btn_missed.blockSignals(False)
                self._push("Select an area and configure a pinpoint layer first.", Qgis.Warning)
                return
            self.canvas.setMapTool(self.missed_tool)
        elif self.canvas.mapTool() is self.missed_tool:
            self.canvas.unsetMapTool(self.missed_tool)

    def _on_maptool_set(self, new, old):
        if new is not self.missed_tool and self.btn_missed.isChecked():
            self.btn_missed.blockSignals(True)
            self.btn_missed.setChecked(False)
            self.btn_missed.blockSignals(False)
        if new is not self.sel_tool and self.btn_sel_map.isChecked():
            self.btn_sel_map.blockSignals(True)
            self.btn_sel_map.setChecked(False)
            self.btn_sel_map.blockSignals(False)

    def _on_missed_click(self, point, button):
        layer, area = self.layers.get("point"), self.current_area()
        if layer is None or area is None:
            return
        f, sv = self.cfg["fields"], self.cfg["status_values"]
        if layer.fields().indexOf(f["status"]) < 0:
            self._push("QC fields are missing on the pinpoint layer - open Settings.", Qgis.Warning)
            return
        if not self.round:
            self._push("Add a QC round first (button 'Add' next to 'QC round').", Qgis.Warning)
            self.btn_missed.setChecked(False)
            return
        dest = self.canvas.mapSettings().destinationCrs()
        p = point
        if layer.crs() != dest:
            p = QgsCoordinateTransform(dest, layer.crs(), QgsProject.instance()).transform(point)
        feat = QgsFeature(layer.fields())
        if QgsWkbTypes.isMultiType(layer.wkbType()):
            feat.setGeometry(QgsGeometry.fromMultiPointXY([p]))
        else:
            feat.setGeometry(QgsGeometry.fromPointXY(p))
        values = {self.cfg["operator_field"]: self.cmb_missed_op.currentText().strip() or None,
                  f["status"]: sv["missed"], f["error_type"]: self.cfg.get("missed_label", "Missed mutation"),
                  f["remark"]: self.txt_remark.text().strip() or None, f["date"]: now_str(),
                  f["reviewer"]: self.txt_reviewer.text().strip() or None,
                  f["round"]: int(area["round"]), f["area"]: area["area_id"], f["qcround"]: self.round}
        for name, v in values.items():
            feat.setAttribute(name, v)
        if utils.add_feature(layer, feat):
            self._push("Missed mutation added to %s." % area["area_id"], Qgis.Success)
            self.txt_remark.clear()
        else:
            self._push("Could not add the missed mutation.", Qgis.Warning)
        self.btn_missed.setChecked(False)

    # ================================================================== multi-select
    def _toggle_batch(self, on):
        self.batch_box.setVisible(on)
        if on:
            self.txt_remark.clear()
            self._set_batch(set())
            self._push("Multi-select: select features, type an optional remark, then click Accepted / Doubtful / an error type.")
        else:
            if self.btn_sel_map.isChecked():
                self.btn_sel_map.setChecked(False)
            self._set_batch(set())
            self._show_current()

    def _toggle_sel_tool(self, on):
        if on:
            if self.current_area() is None:
                self.btn_sel_map.blockSignals(True)
                self.btn_sel_map.setChecked(False)
                self.btn_sel_map.blockSignals(False)
                self._push("Select an area first.", Qgis.Warning)
                return
            self.canvas.setMapTool(self.sel_tool)
        elif self.canvas.mapTool() is self.sel_tool:
            self.canvas.unsetMapTool(self.sel_tool)

    def _set_batch(self, items):
        """Set the multi-select selection (a set of (layer_key, fid)) and mirror it on the map."""
        self.batch_sel = set(items)
        by = defaultdict(list)
        for key, fid in self.batch_sel:
            by[key].append(fid)
        for key, lyr in self.layers.items():
            if lyr is not None:
                lyr.removeSelection()
                if by.get(key):
                    lyr.selectByIds(by[key])
        self._update_sel_label()

    def _update_sel_label(self):
        n = len(self.batch_sel)
        parts = ", ".join("%s %d" % (LAYER_LABEL[k], c) for k, c in sorted(Counter(k for k, _ in self.batch_sel).items()))
        self.lbl_sel.setText("%d selected%s" % (n, "  (%s)" % parts if n else ""))

    def _sel_all(self):
        self._set_batch(set(self.items))

    def _sel_all_unreviewed(self):
        self._refresh_status()
        self._set_batch(set(it for it in self.items if not self._is_reviewed(it)))

    def _sel_clear(self):
        self._set_batch(set())

    def _on_rect_selected(self, rect, mods):
        if self.current_area() is None or not self.items:
            return
        dest = self.canvas.mapSettings().destinationCrs()
        boxes = {}
        for key in {k for k, _ in self.items}:
            g = QgsGeometry.fromRect(rect)
            lyr = self.layers[key]
            if lyr.crs() != dest:
                g.transform(QgsCoordinateTransform(dest, lyr.crs(), QgsProject.instance()))
            boxes[key] = g
        hits = set()
        for key, fid in self.items:  # only features of the current area can be selected
            fg = self.layers[key].getFeature(fid).geometry()
            if fg is not None and not fg.isNull() and fg.intersects(boxes[key]):
                hits.add((key, fid))
        if bool(mods & Qt.ShiftModifier):
            new = self.batch_sel | hits
        elif bool(mods & Qt.ControlModifier):
            new = self.batch_sel - hits
        else:
            new = hits
        self._set_batch(new)

    # ================================================================== review level
    def _fill_levels(self):
        self.cmb_level.blockSignals(True)
        self.cmb_level.clear()
        for code, label, _f in config.level_list(self.cfg):
            self.cmb_level.addItem(label, code)
        self.cmb_level.setCurrentIndex(max(0, self.cmb_level.findData(self.cfg.get("active_level"))))
        self.cmb_level.blockSignals(False)
        self.setWindowTitle("Signalering QC - Review (%s)" % self.cfg.get("active_level_label", ""))

    def _on_level_changed(self, *_):
        code = self.cmb_level.currentData()
        if not code or code == self.cfg.get("active_level"):
            return
        config.set_active_level(code)
        cfg = config.load_config()
        for lyr in utils.get_layers(cfg).values():       # make sure this level's QC fields exist
            if lyr is not None:
                utils.ensure_fields(lyr, utils.qc_field_specs(cfg))
        self._undo = None
        self.btn_undo.setEnabled(False)
        self.reload()
        if any(l is not None and "pre_qc" in l.styleManager().styles() for l in self.layers.values()):
            workflow.apply_qc_symbology(self.iface, self.cfg, self.layers)   # colours follow the level
        self._push("Now reviewing as %s." % self.cfg.get("active_level_label", ""))

