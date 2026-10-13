# -*- coding: utf-8 -*-
"""QC rounds (TL-01, TL-02, OL-01, ...).

A QC round is one review pass of one review level. Which areas belong to a round is kept in the
`qc_rounds` field of the QC_Sample layer (text like ";TL-01;TL-02;"). The result of every feature in every
round is kept in the table QC_Results (same .gpkg as QC_Sample), so reviewing an area again in another round
is a recheck that never overwrites the earlier round. The QC fields on the pins always show the latest
judgement of the review level (so symbology and the attribute table keep working).
"""
import os
import re

from qgis.PyQt.QtCore import QVariant, pyqtSignal
from qgis.PyQt.QtWidgets import (QComboBox, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton,
                                 QVBoxLayout, QWidget)
from qgis.core import (QgsExpression, QgsFeature, QgsFeatureRequest, QgsField, QgsProject, QgsSettings,
                       QgsVectorFileWriter, QgsVectorLayer)

from . import config, utils
from .utils import LAYER_KEYS, val

RESULTS_LAYER = "QC_Results"
ROUND_KEY = "signalering_qc/round/%s"
CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,20}$")
RESULT_FIELDS = [("qc_round", QVariant.String, 20), ("level", QVariant.String, 10),
                 ("pin_layer", QVariant.String, 10), ("feat_id", QVariant.Int, 0),
                 ("area_id", QVariant.String, 20), ("status", QVariant.String, 20),
                 ("error_type", QVariant.String, 40), ("remark", QVariant.String, 120),
                 ("judged", QVariant.String, 20), ("reviewer", QVariant.String, 30)]
VALUE_KEYS = ("status", "error_type", "remark", "judged", "reviewer")


# ====================================================================== rounds in the project config
def rounds_of(cfg, level=None):
    return [r for r in cfg.get("qc_rounds", []) if level is None or r.get("level") == level]


def round_codes(cfg, level=None):
    return [r["code"] for r in rounds_of(cfg, level)]


def level_of_round(cfg, code):
    for r in cfg.get("qc_rounds", []):
        if r["code"] == code:
            return r.get("level")
    return None


def active_round(cfg):
    """The selected QC round of the active review level (None when the level has no round yet)."""
    level = cfg.get("active_level") or cfg.get("default_level", "TL")
    codes = round_codes(cfg, level)
    saved = QgsSettings().value(ROUND_KEY % level, "")
    if saved in codes:
        return saved
    return codes[-1] if codes else None


def set_active_round(level, code):
    QgsSettings().setValue(ROUND_KEY % level, code or "")


def next_code(cfg, level):
    n = len(rounds_of(cfg, level)) + 1
    used = set(round_codes(cfg))
    while "%s-%02d" % (level, n) in used:
        n += 1
    return "%s-%02d" % (level, n)


def _stored_cfg():
    return config.load_config(resolved=False)


def _store_layer_id(cfg, key, lid):
    cfg["layers"][key] = lid
    stored = _stored_cfg()
    stored["layers"][key] = lid
    config.save_config(stored)


# ====================================================================== sample layer: round membership
def area_rounds(value):
    return {x for x in str(value or "").split(";") if x}


def fmt_rounds(codes):
    return ";" + ";".join(sorted(codes)) + ";" if codes else None


def ensure_sample_fields(sample):
    if sample is not None:
        utils.ensure_fields(sample, {"qc_rounds": (QVariant.String, 254)})


def update_area_rounds(sample, add=None, remove=None, only_ids=None):
    """Add / remove a round code in the qc_rounds field of the sample areas (only_ids = area_id list)."""
    if sample is None:
        return 0
    ensure_sample_fields(sample)
    changes = {}
    for f in sample.getFeatures():
        if only_ids is not None and val(f, "area_id") not in only_ids:
            continue
        codes = area_rounds(val(f, "qc_rounds"))
        new = set(codes)
        if add:
            new.add(add)
        if remove:
            new.discard(remove)
        if new != codes:
            changes[f.id()] = {"qc_rounds": fmt_rounds(new)}
    if changes:
        utils.write_values(sample, changes)
    return len(changes)


# ====================================================================== results table
def get_results_layer(cfg):
    lyr = utils.layer_by_id(cfg["layers"].get("results"))
    if lyr is not None:
        return lyr
    for l in QgsProject.instance().mapLayers().values():
        if isinstance(l, QgsVectorLayer) and l.name() == RESULTS_LAYER and l.fields().indexOf("qc_round") >= 0:
            cfg["layers"]["results"] = l.id()
            return l
    return None


def ensure_results_layer(cfg):
    """The QC_Results table; created next to the QC_Sample layer when it does not exist yet."""
    lyr = get_results_layer(cfg)
    if lyr is not None:
        return lyr
    sample = utils.get_sample_layer(cfg)
    if sample is None:
        raise RuntimeError("There is no sample layer yet - define review areas first.")
    path = sample.source().split("|")[0]
    uri = "%s|layername=%s" % (path, RESULTS_LAYER)
    lyr = QgsVectorLayer(uri, RESULTS_LAYER, "ogr") if os.path.exists(path) else None
    if lyr is None or not lyr.isValid():
        mem = QgsVectorLayer("None", RESULTS_LAYER, "memory")
        mem.dataProvider().addAttributes([QgsField(n, t, "", ln) for n, t, ln in RESULT_FIELDS])
        mem.updateFields()
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = RESULTS_LAYER
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
        res = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsProject.instance().transformContext(), opts)
        if res[0] != QgsVectorFileWriter.NoError:
            raise RuntimeError("Could not create the table %s in %s: %s" % (RESULTS_LAYER, path, res[1]))
        lyr = QgsVectorLayer(uri, RESULTS_LAYER, "ogr")
        if not lyr.isValid():
            raise RuntimeError("Could not open the table %s in %s" % (RESULTS_LAYER, path))
    QgsProject.instance().addMapLayer(lyr)
    _store_layer_id(cfg, "results", lyr.id())
    return lyr


def _row_from_feature(f):
    d = {"rid": f.id()}
    for n, _t, _l in RESULT_FIELDS:
        d[n] = val(f, n)
    d["feat_id"] = int(d["feat_id"]) if d["feat_id"] is not None else None
    return d


def load_rows(rl, code=None, area_id=None, level=None, extra_expr=None):
    """{(pin_layer, feat_id): row} (the latest row wins when there are several)."""
    if rl is None:
        return {}
    q = QgsExpression.quotedValue
    col = QgsExpression.quotedColumnRef
    parts = []
    if code:
        parts.append("%s = %s" % (col("qc_round"), q(code)))
    if area_id:
        parts.append("%s = %s" % (col("area_id"), q(area_id)))
    if level:
        parts.append("%s = %s" % (col("level"), q(level)))
    if extra_expr:
        parts.append(extra_expr)
    req = QgsFeatureRequest()
    if parts:
        req.setFilterExpression(" AND ".join(parts))
    req.setFlags(QgsFeatureRequest.NoGeometry)
    out = {}
    for f in rl.getFeatures(req):
        r = _row_from_feature(f)
        out[(r["pin_layer"], r["feat_id"], r["qc_round"]) if code is None else (r["pin_layer"], r["feat_id"])] = r
    return out


def history(rl, key, fid, level):
    """All rows (every round of the level) of one feature, oldest first."""
    if rl is None:
        return []
    q, col = QgsExpression.quotedValue, QgsExpression.quotedColumnRef
    req = QgsFeatureRequest().setFilterExpression("%s = %s AND %s = %d AND %s = %s" % (
        col("pin_layer"), q(key), col("feat_id"), int(fid), col("level"), q(level)))
    req.setFlags(QgsFeatureRequest.NoGeometry)
    rows = [_row_from_feature(f) for f in rl.getFeatures(req)]
    rows.sort(key=lambda r: (r.get("judged") or "", r["rid"]))
    return rows


def save_rows(rl, rows):
    """Insert (no 'rid') or update (has 'rid') result rows; inserted rows get their 'rid' back."""
    prov = rl.dataProvider()
    fields = rl.fields()
    changes, new_rows, new_feats = {}, [], []
    for r in rows:
        if r.get("rid") is not None:
            changes[r["rid"]] = {fields.indexOf(n): r.get(n) for n, _t, _l in RESULT_FIELDS
                                 if fields.indexOf(n) >= 0}
        else:
            f = QgsFeature(fields)
            for n, _t, _l in RESULT_FIELDS:
                f.setAttribute(n, r.get(n))
            new_feats.append(f)
            new_rows.append(r)
    ok = True
    if changes:
        ok = prov.changeAttributeValues(changes) and ok
    if new_feats:
        res, added = prov.addFeatures(new_feats)
        ok = bool(res) and ok
        for r, f in zip(new_rows, added or []):
            r["rid"] = f.id()
    rl.triggerRepaint()
    return ok


def delete_rows(rl, rids):
    rids = [r for r in rids if r is not None]
    if not rids:
        return True
    ok = rl.dataProvider().deleteFeatures(rids)
    rl.triggerRepaint()
    return bool(ok)


# ====================================================================== feature fields (latest result)
def mirror_values(fields, row):
    """Feature field values for a result row (fields = QC field names of the review level)."""
    return {fields["status"]: row.get("status"), fields["error_type"]: row.get("error_type"),
            fields["remark"]: row.get("remark"), fields["date"]: row.get("judged"),
            fields["reviewer"]: row.get("reviewer"), fields["qcround"]: row.get("qc_round")}


def cleared_values(fields):
    return {fields[k]: None for k in ("status", "error_type", "remark", "date", "reviewer", "qcround")}



# ====================================================================== add / remove
def add_round(cfg, level, code, layers):
    """Create a QC round. Returns (ok, message). The first round of a level takes over the results that
    were stored on the pins before rounds existed."""
    code = (code or "").strip()
    if not CODE_RE.match(code):
        return False, "Use 1-20 letters, digits, '-', '_' or '.' (for example TL-02)."
    if code.casefold() in {c.casefold() for c in round_codes(cfg)}:
        return False, "A QC round called '%s' already exists." % code
    first = not round_codes(cfg, level)
    stored = _stored_cfg()
    stored.setdefault("qc_rounds", []).append({"code": code, "level": level, "created": utils.now_str()})
    config.save_config(stored)
    cfg.setdefault("qc_rounds", []).append({"code": code, "level": level, "created": utils.now_str()})
    set_active_round(level, code)
    note = ""
    if first:
        n_areas, n_res = adopt_existing(cfg, level, code, layers)
        if n_areas or n_res:
            note = " It took over %d existing areas and %d earlier results." % (n_areas, n_res)
    return True, "QC round %s added.%s" % (code, note)


def adopt_existing(cfg, level, code, layers):
    """First round of a level: existing areas and results (made before rounds existed) belong to it."""
    sample = utils.get_sample_layer(cfg)
    n_areas = 0
    if sample is not None:
        ensure_sample_fields(sample)
        mine = set(round_codes(cfg, level))
        changes = {}
        for f in sample.getFeatures():
            codes = area_rounds(val(f, "qc_rounds"))
            if not (codes & mine):
                changes[f.id()] = {"qc_rounds": fmt_rounds(codes | {code})}
        if changes:
            utils.write_values(sample, changes)
        n_areas = len(changes)
    fields = config.level_fields(cfg, level)
    amap = utils.status_alias_map(cfg)
    for lyr in layers.values():
        if lyr is not None:
            utils.ensure_fields(lyr, utils.qc_field_specs(cfg))
    rows, stamp = [], {}
    names = utils.status_field_names(cfg)
    for key in LAYER_KEYS:
        lyr = layers.get(key)
        if lyr is None or lyr.fields().indexOf(fields["status"]) < 0:
            continue
        req = QgsFeatureRequest().setFilterExpression(
            "%s IS NOT NULL" % QgsExpression.quotedColumnRef(fields["status"]))
        req.setFlags(QgsFeatureRequest.NoGeometry)
        for f in lyr.getFeatures(req):
            stamp.setdefault(key, {})[f.id()] = {fields["qcround"]: code}
            if utils.status_kind(amap, val(f, fields["status"])) == "missed":
                continue
            if val(f, fields["area"]) is None:
                continue
            rows.append({"qc_round": code, "level": level, "pin_layer": key, "feat_id": f.id(),
                         "area_id": val(f, fields["area"]), "status": val(f, fields["status"]),
                         "error_type": val(f, fields["error_type"]), "remark": val(f, fields["remark"]),
                         "judged": val(f, fields["date"]), "reviewer": val(f, fields["reviewer"])})
    if rows:
        rl = ensure_results_layer(cfg)
        save_rows(rl, rows)
    for key, ch in stamp.items():
        utils.write_values(layers[key], ch)
    return n_areas, len(rows)


def ensure_round(cfg, layers):
    """The active QC round of the active level; creates '<level>-01' when the level has none yet.
    Returns (code, message or None)."""
    code = active_round(cfg)
    if code:
        return code, None
    level = cfg.get("active_level") or cfg.get("default_level", "TL")
    code = next_code(cfg, level)
    ok, msg = add_round(cfg, level, code, layers)
    return (code, msg) if ok else (None, msg)


def describe_round(cfg, code, layers):
    """Counts shown before a round is removed."""
    level = level_of_round(cfg, code)
    rl = get_results_layer(cfg)
    n_res = len(load_rows(rl, code)) if rl is not None else 0
    missed = _missed_features(cfg, level, code, layers)
    sample = utils.get_sample_layer(cfg)
    only = 0
    if sample is not None and "qc_rounds" in [x.name() for x in sample.fields()]:
        for f in sample.getFeatures():
            if area_rounds(val(f, "qc_rounds")) == {code}:
                only += 1
    n_missed = sum(len(v) for v in missed.values())
    return {"results": n_res, "missed": n_missed, "only_areas": only}


def _missed_features(cfg, level, code, layers):
    fields = config.level_fields(cfg, level)
    amap = utils.status_alias_map(cfg)
    out = {}
    for key in LAYER_KEYS:
        lyr = layers.get(key)
        if lyr is None or lyr.fields().indexOf(fields["qcround"]) < 0 or lyr.fields().indexOf(fields["status"]) < 0:
            continue
        req = QgsFeatureRequest().setFilterExpression("%s = %s" % (
            QgsExpression.quotedColumnRef(fields["qcround"]), QgsExpression.quotedValue(code)))
        req.setFlags(QgsFeatureRequest.NoGeometry)
        ids = [f.id() for f in lyr.getFeatures(req)
               if utils.status_kind(amap, val(f, fields["status"])) == "missed"]
        if ids:
            out[key] = ids
    return out


def remove_round(cfg, code, layers):
    """Delete a round: its results, the missed mutations added in it and its link to the areas. The latest
    result on the pins falls back to the previous round of the same level. Returns a message."""
    level = level_of_round(cfg, code)
    fields = config.level_fields(cfg, level)
    rl = get_results_layer(cfg)
    affected = set()
    n_res = 0
    if rl is not None:
        mine = load_rows(rl, code)
        affected = set(mine)
        n_res = len(mine)
        delete_rows(rl, [r["rid"] for r in mine.values()])
    n_missed = 0
    for key, ids in _missed_features(cfg, level, code, layers).items():
        lyr = layers[key]
        n_missed += len(ids)
        if lyr.isEditable():
            lyr.deleteFeatures(ids)
        else:
            lyr.dataProvider().deleteFeatures(ids)
        lyr.triggerRepaint()
    if rl is not None and affected:                     # pins fall back to their previous round
        latest = {}
        for r in load_rows(rl, None, level=level).values():
            k = (r["pin_layer"], r["feat_id"])
            if k in affected and (k not in latest or (r.get("judged") or "", r["rid"]) >=
                                  (latest[k].get("judged") or "", latest[k]["rid"])):
                latest[k] = r
        by = {}
        for k in affected:
            vals = mirror_values(fields, latest[k]) if k in latest else cleared_values(fields)
            by.setdefault(k[0], {})[k[1]] = vals
        for key, ch in by.items():
            if layers.get(key) is not None:
                utils.write_values(layers[key], ch)
    update_area_rounds(utils.get_sample_layer(cfg), remove=code)
    stored = _stored_cfg()
    stored["qc_rounds"] = [r for r in stored.get("qc_rounds", []) if r["code"] != code]
    config.save_config(stored)
    cfg["qc_rounds"] = [r for r in cfg.get("qc_rounds", []) if r["code"] != code]
    if QgsSettings().value(ROUND_KEY % level, "") == code:
        set_active_round(level, "")
    return "QC round %s removed (%d results, %d missed mutations deleted)." % (code, n_res, n_missed)


# ====================================================================== widget
class RoundBar(QWidget):
    """'QC round: [TL-01 v] [Add] [Remove]' - shared by the review panel and the area window."""
    changed = pyqtSignal()

    def __init__(self, parent=None, show_level=False):
        super().__init__(parent)
        self.cfg = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.level_row = QWidget()
        lv = QHBoxLayout(self.level_row)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Review as:"))
        self.cmb_level = QComboBox()
        self.cmb_level.currentIndexChanged.connect(self._on_level)
        lv.addWidget(self.cmb_level, 1)
        self.level_row.setVisible(show_level)
        outer.addWidget(self.level_row)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(row)
        lay.addWidget(QLabel("QC round:"))
        self.cmb = QComboBox()
        self.cmb.setToolTip("Reviewing an area again in another QC round is a recheck: the result of the "
                            "earlier round is kept.")
        self.cmb.currentIndexChanged.connect(self._on_select)
        lay.addWidget(self.cmb, 1)
        self.btn_add = QPushButton("Add")
        self.btn_add.clicked.connect(self._add)
        self.btn_del = QPushButton("Remove")
        self.btn_del.clicked.connect(self._remove)
        lay.addWidget(self.btn_add)
        lay.addWidget(self.btn_del)

    def refresh(self, cfg):
        self.cfg = cfg
        level = cfg.get("active_level")
        self.cmb_level.blockSignals(True)
        self.cmb_level.clear()
        for code, label, _f in config.level_list(cfg):
            self.cmb_level.addItem(label, code)
        self.cmb_level.setCurrentIndex(max(0, self.cmb_level.findData(level)))
        self.cmb_level.blockSignals(False)
        self.cmb.blockSignals(True)
        self.cmb.clear()
        for code in round_codes(cfg, level):
            self.cmb.addItem(code, code)
        cur = active_round(cfg)
        if cur:
            self.cmb.setCurrentIndex(max(0, self.cmb.findData(cur)))
        else:
            self.cmb.addItem("(no round yet - click Add)", "")
        self.cmb.blockSignals(False)
        self.btn_del.setEnabled(bool(cur))

    def code(self):
        return self.cmb.currentData() or None

    def _on_level(self, *_):
        code = self.cmb_level.currentData()
        if code and code != self.cfg.get("active_level"):
            config.set_active_level(code)
            self.changed.emit()

    def sync(self):
        """Re-read the level / round (they may have been changed in another window)."""
        cfg = config.load_config()
        if (cfg.get("active_level") != self.cfg.get("active_level")
                or active_round(cfg) != self.code() or round_codes(cfg) != round_codes(self.cfg)):
            self.refresh(cfg)
            return True
        return False

    def _on_select(self, *_):
        code = self.cmb.currentData()
        if code:
            set_active_round(self.cfg.get("active_level"), code)
            self.changed.emit()

    def _add(self):
        self.sync()
        cfg = config.load_config()
        level = cfg.get("active_level")
        code, ok = QInputDialog.getText(self, "Add QC round",
                                        "Code of the new %s round (for example %s):" % (
                                            cfg.get("active_level_label", level), next_code(cfg, level)),
                                        text=next_code(cfg, level))
        if not ok:
            return
        good, msg = add_round(cfg, level, code, utils.get_layers(cfg))
        if not good:
            QMessageBox.warning(self, "Add QC round", msg)
            return
        self.changed.emit()

    def _remove(self):
        self.sync()
        cfg = config.load_config()
        code = self.code()
        if not code:
            return
        layers = utils.get_layers(cfg)
        d = describe_round(cfg, code, layers)
        txt = ("Remove QC round %s?\n\nThis permanently deletes its %d results" % (code, d["results"]))
        if d["missed"]:
            txt += " and the %d missed mutations added in it" % d["missed"]
        txt += ". The result shown on the pins falls back to the previous round."
        if d["only_areas"]:
            txt += ("\n\n%d areas belong only to this round. They stay in the sample but are not in any round "
                    "until you add them to one (tool 'Re-select existing areas' in 'Define review areas')."
                    % d["only_areas"])
        if QMessageBox.question(self, "Remove QC round", txt, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        msg = remove_round(cfg, code, layers)
        QMessageBox.information(self, "Remove QC round", msg)
        self.changed.emit()
