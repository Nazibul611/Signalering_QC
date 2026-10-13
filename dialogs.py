# -*- coding: utf-8 -*-
import json
import os
import random
from collections import Counter

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                                 QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                                 QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout)
from qgis.core import QgsProject
from qgis.gui import QgsFileWidget, QgsMapLayerComboBox
from qgis.core import QgsMapLayerProxyModel

from . import config, qc_rounds, stats, utils, workflow


# ====================================================================== settings
class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signalering QC - Settings")
        self.resize(760, 700)
        self.cfg = cfg
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.combos = {}
        for key, label, flt in (("point", "Pinpoint layer", QgsMapLayerProxyModel.PointLayer),
                                ("line", "Pinline layer (optional)", QgsMapLayerProxyModel.LineLayer),
                                ("polygon", "Pinpolygon layer (optional)", QgsMapLayerProxyModel.PolygonLayer)):
            cb = QgsMapLayerComboBox()
            cb.setFilters(flt)
            cb.setAllowEmptyLayer(True)
            cb.setLayer(utils.layer_by_id(cfg["layers"].get(key)))
            self.combos[key] = cb
            form.addRow(label, cb)
        self.chk_fields = QCheckBox("Create missing QC fields on these layers now")
        self.chk_fields.setChecked(True)
        form.addRow("", self.chk_fields)
        lay.addLayout(form)
        lay.addWidget(QLabel("Advanced configuration (JSON): QC field names, status values, error types "
                             "and severity, acceptance threshold, sampling defaults, pre-check rules:"))
        self.txt = QPlainTextEdit()
        self.txt.setLineWrapMode(QPlainTextEdit.NoWrap)
        body = {k: v for k, v in cfg.items() if k != "layers"}
        self.txt.setPlainText(json.dumps(body, indent=2, ensure_ascii=False))
        lay.addWidget(self.txt, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _accept(self):
        try:
            self._parsed = json.loads(self.txt.toPlainText())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid JSON", str(exc))
            return
        if self.combos["point"].currentLayer() is None and not any(
                c.currentLayer() for c in self.combos.values()):
            QMessageBox.warning(self, "Settings", "Pick at least one signalering layer.")
            return
        self.accept()

    def result_config(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg.update(self._parsed)
        cfg["layers"] = {k: (c.currentLayer().id() if c.currentLayer() else None)
                         for k, c in self.combos.items()}
        cfg["layers"]["sample"] = self.cfg["layers"].get("sample")
        return cfg


# ====================================================================== sampling
class SamplingDialog(QDialog):
    def __init__(self, cfg, layers, preselect=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signalering QC - Draw random review areas")
        self.resize(560, 700)
        self.cfg = cfg
        sd = cfg["sampling"]
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.cmb_method = QComboBox()
        self.cmb_method.addItem("Balanced cells (equal number of pins per area)", "balanced")
        self.cmb_method.addItem("Regular grid", "grid")
        self.cmb_method.addItem("Polygon layer (e.g. buurten / work areas)", "polygon")
        self.cmb_method.setCurrentIndex(max(0, self.cmb_method.findData(sd["method"])))
        self.cmb_method.currentIndexChanged.connect(self._toggle)
        form.addRow("Area type", self.cmb_method)

        self.spn_target = QSpinBox()
        self.spn_target.setRange(5, 5000)
        self.spn_target.setValue(int(sd["target_pins"]))
        form.addRow("Max features per area", self.spn_target)

        self.spn_grid = QDoubleSpinBox()
        self.spn_grid.setRange(10, 100000)
        self.spn_grid.setSuffix(" m")
        self.spn_grid.setValue(float(sd["grid_size"]))
        form.addRow("Grid cell size", self.spn_grid)

        self.cmb_poly = QgsMapLayerComboBox()
        self.cmb_poly.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        form.addRow("Polygon layer", self.cmb_poly)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("Add N NEW areas per operator", "count")
        self.cmb_mode.addItem("Top up to X % of each operator's features", "pct")
        self.cmb_mode.setCurrentIndex(max(0, self.cmb_mode.findData(sd["mode"])))
        form.addRow("Selection", self.cmb_mode)
        self.spn_value = QDoubleSpinBox()
        self.spn_value.setRange(1, 100)
        self.spn_value.setValue(float(sd["value"]))
        form.addRow("N or %", self.spn_value)

        self.spn_min = QSpinBox()
        self.spn_min.setRange(1, 1000)
        self.spn_min.setValue(int(sd["min_pins"]))
        form.addRow("Skip areas with fewer features than", self.spn_min)

        self.chk_excl = QCheckBox("Exclude areas that already contain sampled features")
        self.chk_excl.setChecked(bool(sd["exclude_sampled"]))
        form.addRow("", self.chk_excl)

        row = QHBoxLayout()
        self.spn_seed = QSpinBox()
        self.spn_seed.setRange(1, 2147483647)
        self.spn_seed.setValue(random.randint(1, 999999))
        btn = QPushButton("New seed")
        btn.clicked.connect(lambda: self.spn_seed.setValue(random.randint(1, 999999)))
        row.addWidget(self.spn_seed, 1)
        row.addWidget(btn)
        form.addRow("Random seed", row)

        self.fw = QgsFileWidget()
        self.fw.setStorageMode(QgsFileWidget.SaveFile)
        self.fw.setFilter("GeoPackage (*.gpkg)")
        self.fw.setFilePath(self._default_path())
        form.addRow("Sample file (.gpkg)", self.fw)
        lay.addLayout(form)

        lay.addWidget(QLabel("Operators to draw for:"))
        self.lst = QListWidget()
        infos, _ = workflow.collect_infos(cfg, layers)
        tot = Counter(i.operator for i in infos)
        done = Counter(i.operator for i in infos if i.sampled)
        for op in sorted(tot):
            it = QListWidgetItem("%s  -  %d features, %d already sampled" % (op, tot[op], done[op]))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            on = (op in preselect) if preselect else True
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            it.setData(Qt.UserRole, op)
            self.lst.addItem(it)
        lay.addWidget(self.lst, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Draw")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._toggle()

    def _default_path(self):
        s = utils.get_sample_layer(self.cfg)
        if s is not None:
            return s.source().split("|")[0]
        base = QgsProject.instance().absolutePath() or os.path.expanduser("~")
        return os.path.join(base, "QC_Sample.gpkg")

    def _toggle(self, *_):
        m = self.cmb_method.currentData()
        self.spn_target.setEnabled(m == "balanced")
        self.spn_grid.setEnabled(m == "grid")
        self.cmb_poly.setEnabled(m == "polygon")

    def params(self):
        ops = [self.lst.item(i).data(Qt.UserRole) for i in range(self.lst.count())
               if self.lst.item(i).checkState() == Qt.Checked]
        return {"method": self.cmb_method.currentData(), "target_pins": self.spn_target.value(),
                "grid_size": self.spn_grid.value(), "polygon_layer": self.cmb_poly.currentLayer(),
                "mode": self.cmb_mode.currentData(), "value": self.spn_value.value(),
                "min_pins": self.spn_min.value(), "min_op_pins": 1,
                "exclude_sampled": self.chk_excl.isChecked(), "operators": ops,
                "seed": self.spn_seed.value(), "path": self.fw.filePath()}


# ====================================================================== dashboard
class DashboardDialog(QDialog):
    COLS = ["Operator", "Sampled", "Reviewed", "Accepted", "Doubtful", "Major", "Minor", "Missed",
            "Not detectable", "Error rate", "Verdict"]

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("Signalering QC - Dashboard")
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        lvl = QHBoxLayout()
        lvl.addWidget(QLabel("Review level:"))
        self.cmb_level = QComboBox()
        self.cmb_level.currentIndexChanged.connect(self._on_level)
        lvl.addWidget(self.cmb_level)
        lvl.addWidget(QLabel("QC round:"))
        self.cmb_round = QComboBox()
        self.cmb_round.currentIndexChanged.connect(lambda *_: self.refresh(keep_round=True))
        lvl.addWidget(self.cmb_round)
        lvl.addStretch(1)
        lay.addLayout(lvl)
        self.lbl = QLabel()
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        self.tbl = QTableWidget(0, len(self.COLS))
        self.tbl.setHorizontalHeaderLabels(self.COLS)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.tbl, 1)
        self.lbl_err = QLabel()
        self.lbl_err.setWordWrap(True)
        lay.addWidget(self.lbl_err)
        row = QHBoxLayout()
        for text, fn in (("Refresh", self.refresh), ("Export HTML...", self._html),
                         ("Export CSV...", self._csv),
                         ("Add extra review areas for failing operators...", self._extra)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
        row.addStretch(1)
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        row.addWidget(b)
        lay.addLayout(row)
        self.refresh()

    def refresh(self, keep_round=False):
        self.cfg = config.load_config()
        keep = self.cmb_round.currentData() if keep_round else None
        self.cmb_round.blockSignals(True)
        self.cmb_round.clear()
        self.cmb_round.addItem("Latest result of every pin", None)
        for code in qc_rounds.round_codes(self.cfg, self.cfg.get("active_level")):
            self.cmb_round.addItem(code, code)
        self.cmb_round.setCurrentIndex(max(0, self.cmb_round.findData(keep)))
        self.cmb_round.blockSignals(False)
        self.cmb_level.blockSignals(True)
        self.cmb_level.clear()
        for code, label, _f in config.level_list(self.cfg):
            self.cmb_level.addItem(label, code)
        self.cmb_level.setCurrentIndex(max(0, self.cmb_level.findData(self.cfg.get("active_level"))))
        self.cmb_level.blockSignals(False)
        self.layers = utils.get_layers(self.cfg)
        self.sample = utils.get_sample_layer(self.cfg)
        rl = qc_rounds.get_results_layer(self.cfg)
        self.data = stats.compute(self.cfg, self.layers, self.cmb_round.currentData(), rl, self.sample)
        acc = self.cfg["acceptance"]
        self.lbl.setText("Acceptance: max. %.1f%% major errors (incl. missed mutations), at least %d "
                         "reviewed features per operator for a verdict. 'Not detectable' features are excluded from the rate." %
                         (acc["max_major_error_pct"], acc["min_reviewed_for_verdict"]))
        rows = list(self.data["operators"].items()) + [("TOTAL", self.data["totals"])]
        self.tbl.setRowCount(len(rows))
        for r, (op, rec) in enumerate(rows):
            vals = [op, rec["sampled"], rec["reviewed"], rec["accepted"], rec["doubtful"],
                    rec["major"], rec["minor"], rec["missed"], rec["undetectable"], stats._pct(rec["rate"]),
                    rec["verdict"]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == len(vals) - 1:
                    colour = {"Accepted": "#bbf7d0", "Not accepted": "#fecaca"}.get(rec["verdict"], "#e5e7eb")
                    it.setBackground(QBrush(QColor(colour)))
                self.tbl.setItem(r, c, it)
        self.tbl.resizeColumnsToContents()
        self.lbl_err.setText("Error types: " + (", ".join("%s: %d" % kv for kv in
                             self.data["errors"].most_common()) or "none yet"))

    def _on_level(self, *_):
        code = self.cmb_level.currentData()
        if code:
            config.set_active_level(code)
            self.refresh()
            if getattr(self.plugin, "panel", None) is not None:
                self.plugin.panel.reload()

    def _title(self):
        return "Signalering QC report - %s - %s review (%s)" % (
            QgsProject.instance().title() or os.path.basename(QgsProject.instance().fileName()) or "project",
            self.cfg.get("active_level_label", ""), self.data.get("round") or "latest result")

    def _html(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export HTML report", "", "HTML (*.html)")
        if path:
            stats.export_html(path, self.cfg, self.data, stats.sample_info(self.sample), self._title())
            QMessageBox.information(self, "Report", "Saved:\n%s" % path)

    def _csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV (base name)", "", "CSV (*.csv)")
        if path:
            files = stats.export_csv(path, self.data, self.cfg)
            QMessageBox.information(self, "Report", "Saved:\n" + "\n".join(files))

    def _extra(self):
        bad = [op for op, r in self.data["operators"].items() if r["verdict"] == "Not accepted"]
        if not bad:
            QMessageBox.information(self, "Extra sample", "No operator is currently 'Not accepted'.")
            return
        self.accept()
        self.plugin.define_areas(hint_operators=bad)
