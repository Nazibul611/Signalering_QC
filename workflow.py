# -*- coding: utf-8 -*-
"""Sampling workflow (collect -> build cells -> draw -> commit) and QC symbology."""
import os
from collections import Counter, defaultdict

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (QgsExpression, QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsPoint,
                       QgsPalLayerSettings, QgsProject, QgsRuleBasedRenderer,
                       QgsSingleSymbolRenderer, QgsSymbol, QgsTextFormat, QgsVectorFileWriter,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes)

from . import qc_rounds, sampling
from .sampling import FeatInfo
from .utils import (LAYER_KEYS, is_missed, is_null, now_str, rep_point, status_alias_map,
                    status_field_names, status_values_for, transformer, val, write_values)

SAMPLE_LAYER = "QC_Sample"
SAMPLE_FIELDS = [
    ("area_id", QVariant.String, 20), ("round", QVariant.Int, 0),
    ("method", QVariant.String, 20), ("seed", QVariant.Int, 0),
    ("n_feat", QVariant.Int, 0), ("operators", QVariant.String, 254), ("qc_rounds", QVariant.String, 254),
    ("status", QVariant.String, 10), ("drawn", QVariant.String, 20),
]


# ------------------------------------------------------------------ collect / plan
def primary_layer(layers):
    return layers.get("point") or next((l for l in layers.values() if l is not None), None)


def collect_infos(cfg, layers):
    area_f, op_f = cfg["fields"]["area"], cfg["operator_field"]
    amap, status_names = status_alias_map(cfg), status_field_names(cfg)
    primary = primary_layer(layers)
    crs = primary.crs()
    infos = []
    for key in LAYER_KEYS:
        lyr = layers.get(key)
        if lyr is None:
            continue
        xf = transformer(lyr.crs(), crs)
        for f in lyr.getFeatures():
            p = rep_point(f)
            if p is None:
                continue
            if xf is not None:
                p = xf.transform(p)
            # features added by QC as "missed mutations" are never part of a sample
            if is_missed(cfg, f, amap, status_names):
                continue
            infos.append(FeatInfo(key, f.id(), p.x(), p.y(),
                                  str(val(f, op_f) or "(unknown)"),
                                  val(f, area_f) is not None, val(f, area_f)))
    return infos, crs


def plan(cfg, layers, params):
    infos, crs = collect_infos(cfg, layers)
    if not infos:
        raise RuntimeError("No features found in the configured layers.")
    method = params["method"]
    if method == "grid":
        cells = sampling.build_grid_cells(infos, params["grid_size"])
    elif method == "polygon":
        pl = params["polygon_layer"]
        xf = transformer(pl.crs(), crs)
        feats = []
        for f in pl.getFeatures():
            g = f.geometry()
            if g is not None and not g.isNull() and xf is not None:
                g = QgsGeometry(g)
                g.transform(xf)
                f.setGeometry(g)
            feats.append(f)
        cells = sampling.build_polygon_cells(infos, feats)
    else:
        cells = sampling.build_balanced_cells(infos, params["target_pins"])
    res = sampling.draw(cells, infos, params["operators"], params["mode"], params["value"],
                        params["seed"], params["min_pins"], params.get("min_op_pins", 1),
                        params["exclude_sampled"])
    res["crs"] = crs
    res["n_cells_total"] = len(cells)
    return res


# ------------------------------------------------------------------ manual areas
def clean_polygon(geom):
    """A valid (multi)polygon from a drawn / merged geometry, or None."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    g = QgsGeometry(geom)
    if not g.isGeosValid():
        g = g.makeValid()
    if g is None or g.isNull():
        return None
    if g.type() != QgsWkbTypes.PolygonGeometry:
        parts = [p for p in g.asGeometryCollection() if p.type() == QgsWkbTypes.PolygonGeometry]
        if not parts:
            return None
        g = QgsGeometry.unaryUnion(parts)
    return g if g is not None and not g.isNull() and g.area() > 0 else None


def assign_members(infos, geoms):
    """For every geometry (same CRS as the infos) the NEW features inside it, in order. A feature that
    already carries a sample area, or that lies in an earlier geometry of this list, is not taken again,
    so every feature belongs to exactly one area. Returns [{"members": [...], "already": n}, ...]."""
    taken, out = set(), []
    for g in geoms:
        res = {"members": [], "already": 0}
        out.append(res)
        if g is None or g.isNull() or g.isEmpty():
            continue
        bb = g.boundingBox()
        engine = QgsGeometry.createGeometryEngine(g.constGet())
        engine.prepareGeometry()
        for i in infos:
            if i.x < bb.xMinimum() or i.x > bb.xMaximum() or i.y < bb.yMinimum() or i.y > bb.yMaximum():
                continue
            if not engine.intersects(QgsPoint(i.x, i.y)):
                continue
            key = (i.layer, i.fid)
            if i.sampled or key in taken:
                res["already"] += 1
                continue
            taken.add(key)
            res["members"].append(i)
    return out


def plan_manual(infos, crs, entries):
    """Same result structure as plan(), for areas the reviewer drew / picked.
    entries: dicts with geom (CRS of the pins), label and method."""
    assigned = assign_members(infos, [e["geom"] for e in entries])
    cells, warnings = [], []
    for e, a in zip(entries, assigned):
        if not a["members"]:
            warnings.append("%s: no new pins inside - skipped." % e["label"])
            continue
        c = sampling.Cell(e["geom"], a["members"])
        c.method = e["method"]
        cells.append(c)
    total = Counter(i.operator for i in infos)
    already = Counter(i.operator for i in infos if i.sampled)
    per_op = {}
    for op in sorted(total):
        feats = sum(1 for c in cells for m in c.members if m.operator == op)
        if feats:
            per_op[op] = {"cells": sum(1 for c in cells if any(m.operator == op for m in c.members)),
                          "new_features": feats, "already": already[op], "total": total[op]}
    return {"cells": cells, "per_operator": per_op, "warnings": warnings, "crs": crs,
            "n_cells_total": len(entries)}


# ------------------------------------------------------------------ commit
def _find_sample_layer(path):
    norm = os.path.normcase(os.path.abspath(path))
    for lyr in QgsProject.instance().mapLayers().values():
        if isinstance(lyr, QgsVectorLayer) and lyr.providerType() == "ogr":
            src = lyr.source()
            if os.path.normcase(os.path.abspath(src.split("|")[0])) == norm \
                    and ("layername=%s" % SAMPLE_LAYER) in src:
                return lyr
    return None


def _get_or_create_sample_layer(path, crs):
    layer = _find_sample_layer(path)
    if layer is not None:
        return layer
    uri = "%s|layername=%s" % (path, SAMPLE_LAYER)
    if os.path.exists(path):
        test = QgsVectorLayer(uri, SAMPLE_LAYER, "ogr")
        if test.isValid():
            QgsProject.instance().addMapLayer(test)
            style_sample_layer(test)
            return test
    mem = QgsVectorLayer("MultiPolygon?crs=%s" % crs.authid(), SAMPLE_LAYER, "memory")
    mem.dataProvider().addAttributes([QgsField(n, t, "", ln) for n, t, ln in SAMPLE_FIELDS])
    mem.updateFields()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = SAMPLE_LAYER
    if os.path.exists(path):
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer
    res = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem, path, QgsProject.instance().transformContext(), opts)
    if res[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError("Could not create %s: %s" % (path, res[1]))
    layer = QgsVectorLayer(uri, SAMPLE_LAYER, "ogr")
    if not layer.isValid():
        raise RuntimeError("Could not open the new sample layer in %s" % path)
    QgsProject.instance().addMapLayer(layer)
    style_sample_layer(layer)
    return layer


def commit(cfg, layers, result, params, path):
    sample = _get_or_create_sample_layer(path, result["crs"])
    qc_rounds.ensure_sample_fields(sample)
    qc_round = params.get("qc_round")
    idx_round = sample.fields().indexOf("round")
    mx = sample.maximumValue(idx_round) if idx_round >= 0 else None
    rnd = (int(mx) if not is_null(mx) else 0) + 1
    area_field = cfg["fields"]["area"]
    stamp = now_str()
    feats, assignments, n_feat_total = [], defaultdict(dict), 0
    for n, cell in enumerate(result["cells"], 1):
        area_id = "R%d-A%02d" % (rnd, n)
        ops = Counter(m.operator for m in cell.members)
        g = QgsGeometry(cell.geometry)
        if not g.isMultipart():
            g.convertToMultiType()
        f = QgsFeature(sample.fields())
        f.setGeometry(g)
        f.setAttribute("area_id", area_id)
        f.setAttribute("round", rnd)
        f.setAttribute("method", (getattr(cell, "method", None) or params["method"])[:20])
        f.setAttribute("seed", int(params["seed"]))
        f.setAttribute("n_feat", len(cell.members))
        f.setAttribute("operators", ", ".join("%s (%d)" % (o, c) for o, c in sorted(ops.items()))[:250])
        f.setAttribute("status", "Open")
        if qc_round:
            f.setAttribute("qc_rounds", qc_rounds.fmt_rounds({qc_round}))
        f.setAttribute("drawn", stamp)
        feats.append(f)
        n_feat_total += len(cell.members)
        for m in cell.members:
            assignments[m.layer][m.fid] = {area_field: area_id}
    ok, _ = sample.dataProvider().addFeatures(feats)
    if not ok:
        raise RuntimeError("Could not write the sampled areas.")
    sample.updateExtents()
    sample.triggerRepaint()
    for key, changes in assignments.items():
        write_values(layers[key], changes)
    return {"sample_layer": sample, "round": rnd, "areas": len(feats), "features": n_feat_total}


# ------------------------------------------------------------------ symbology
def style_sample_layer(layer):
    sym = QgsFillSymbol.createSimple({"color": "255,140,0,30", "outline_color": "255,110,0,255",
                                      "outline_width": "0.8"})
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    ls = QgsPalLayerSettings()
    ls.fieldName = "area_id"
    fmt = QgsTextFormat()
    fmt.setSize(10)
    ls.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(ls))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def apply_qc_symbology(iface, cfg, layers):
    f = cfg["fields"]                       # QC fields of the active review level
    col = QgsExpression.quotedColumnRef
    q = QgsExpression.quotedValue

    def in_status(key):                     # the configured text, the English default and the older Dutch text
        return "%s IN (%s)" % (col(f["status"]), ", ".join(q(v) for v in status_values_for(cfg, key)))

    rules = [
        ("Accepted", in_status("accepted"), "46,160,67", 1.0),
        ("Rejected", in_status("rejected"), "220,38,38", 1.0),
        ("Doubtful", in_status("doubtful"), "245,158,11", 1.0),
        ("Missed", in_status("missed"), "147,51,234", 1.0),
        ("Not detectable", in_status("undetectable"), "100,116,139", 1.0),
        ("To review", "%s IS NOT NULL AND %s IS NULL" % (col(f["area"]), col(f["status"])), "37,99,235", 1.0),
        ("Not in sample", "ELSE", "150,150,150", 0.35),
    ]
    for lyr in layers.values():
        if lyr is None:
            continue
        sm = lyr.styleManager()
        if sm.currentStyle() == "pre_qc":
            sm.setCurrentStyle("default")
        elif "pre_qc" not in sm.styles():
            sm.addStyleFromLayer("pre_qc")
        root = QgsRuleBasedRenderer.Rule(None)
        for label, expr, rgb, opacity in rules:
            sym = QgsSymbol.defaultSymbol(lyr.geometryType())
            sym.setColor(QColor(*[int(x) for x in rgb.split(",")]))
            sym.setOpacity(opacity)
            if lyr.geometryType() == QgsWkbTypes.PointGeometry and opacity == 1.0:
                sym.setSize(3.2)
            root.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, expr, label))
        lyr.setRenderer(QgsRuleBasedRenderer(root))
        lyr.triggerRepaint()
        iface.layerTreeView().refreshLayerSymbology(lyr.id())


def restore_symbology(iface, layers):
    for lyr in layers.values():
        if lyr is None:
            continue
        sm = lyr.styleManager()
        if "pre_qc" in sm.styles():
            sm.setCurrentStyle("pre_qc")
            lyr.triggerRepaint()
            iface.layerTreeView().refreshLayerSymbology(lyr.id())

