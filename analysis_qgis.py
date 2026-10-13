# -*- coding: utf-8 -*-
"""Initial analysis, QGIS side: reads pins and BGT reference layers, feeds analysis_core,
writes HTML/CSV and a map layer of flagged pins. Read-only on the signalering layers."""
import os

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor
from qgis.core import (QgsCategorizedSymbolRenderer, QgsFeature, QgsFeatureRequest, QgsField,
                       QgsGeometry, QgsMarkerSymbol, QgsPointXY, QgsProject, QgsRectangle,
                       QgsRendererCategory, QgsSpatialIndex, QgsVectorFileWriter, QgsVectorLayer,
                       QgsWkbTypes)

from . import analysis_core as core
from .utils import is_missed, rep_point, status_alias_map, status_field_names, transformer, val

REF_KEYS = ("hitbgt", "oth_polygon", "oth_point")


class RefLayer(object):
    """In-memory copy (geometry + a few attributes) of the part of a reference layer near the pins."""

    def __init__(self, key, layer, fmap, ignore_plus, extent, target_crs):
        self.key, self.layer, self.f = key, layer, fmap
        self.index, self.geoms, self.attrs, self.bounds = QgsSpatialIndex(), {}, {}, {}
        self.xf_in = transformer(target_crs, layer.crs())      # analysis crs -> layer crs
        self.xf_out = transformer(layer.crs(), target_crs)     # layer crs -> analysis crs
        # field names are matched case-insensitively (shapefile: BGTTYPE, gpkg/PostGIS: bgttype)
        flds = layer.fields()
        self.real, self.missing = {}, []
        for k, n in fmap.items():
            if not n:
                continue
            i = flds.lookupField(n)
            if i < 0:
                self.missing.append(n)
            else:
                self.real[k] = flds.at(i).name()
        rect = QgsRectangle(extent)
        if self.xf_in is not None:
            rect = self.xf_in.transformBoundingBox(rect)
        req = QgsFeatureRequest().setFilterRect(rect)
        req.setSubsetOfAttributes([flds.lookupField(n) for n in self.real.values()])
        plus = self.real.get("plus")
        ignore = set(str(x).lower() for x in ignore_plus)
        for feat in layer.getFeatures(req):
            if plus and str(val(feat, plus) or "").lower() in ignore:
                continue
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            g = QgsGeometry(g)
            if self.xf_out is not None:
                g.transform(self.xf_out)
            self.geoms[feat.id()] = g
            self.attrs[feat.id()] = {k: val(feat, n) for k, n in self.real.items()}
            nf = QgsFeature(feat.id())
            nf.setGeometry(g)
            self.index.addFeature(nf)

    def obj(self, fid):
        a = self.attrs[fid]
        return {"layer": self.key, "type": a.get("type"), "tmain": a.get("tmain"), "tsub": a.get("tsub"),
                "fmain": a.get("fmain"), "fsub": a.get("fsub"), "otype": a.get("otype"), "bron": a.get("bron"),
                "bgtid": a.get("bgtid")}

    def boundary(self, fid):
        b = self.bounds.get(fid)
        if b is None:
            b = self.geoms[fid].convertToType(QgsWkbTypes.LineGeometry, True)
            self.bounds[fid] = b if (b is not None and not b.isNull()) else False
            b = self.bounds[fid]
        return b or None


def _pins(layer, cfg, xf, amap):
    f_area = cfg["fields"]["area"]
    status_names = status_field_names(cfg)
    op_f = cfg["operator_field"]
    out = []
    for feat in layer.getFeatures():
        if is_missed(cfg, feat, amap, status_names):       # QC-added "missed mutation" points
            continue
        g = feat.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        g = QgsGeometry(g)
        if xf is not None:
            g.transform(xf)
        p = rep_point(feat)
        if p is None:
            continue
        if xf is not None:
            p = xf.transform(p)
        out.append({"fid": feat.id(), "operator": val(feat, op_f), "objecttype": val(feat, "Objecttype"),
                    "status": val(feat, "Status"), "bronhouder": val(feat, "Bronhouder"),
                    "opmerking": val(feat, "Opmerking"), "categorie": val(feat, "Categorie"),
                    "mutatietype": val(feat, "MutatiType"), "area": val(feat, f_area),
                    "x": p.x(), "y": p.y(), "geom": g})
    return out


def run(cfg, layers, out_dir, progress=None, add_to_project=True):
    acfg = cfg["analysis"]
    warnings = []
    pl_point, pl_line = layers.get("point"), layers.get("line")
    if pl_point is None and pl_line is None:
        raise RuntimeError("Choose the pinpoint and/or pinline layer first (1. Settings).")

    # reference layers (found by name in the project, like the existing spatial_ref rules)
    refs_layers = {}
    for key in REF_KEYS:
        name = acfg["reference_layers"].get(key)
        found = QgsProject.instance().mapLayersByName(name) if name else []
        if found:
            refs_layers[key] = found[0]
        else:
            warnings.append("Reference layer '%s' not found in the project - skipped." % name)
    if "hitbgt" not in refs_layers and "oth_polygon" not in refs_layers:
        raise RuntimeError("Load the BGT reference layers (hitbgt, v_hitbgtoth_polygon) in the project first.")

    crs = (refs_layers.get("hitbgt") or refs_layers["oth_polygon"]).crs()
    amap = status_alias_map(cfg)
    points = _pins(pl_point, cfg, transformer(pl_point.crs(), crs), amap) if pl_point else []
    lines = _pins(pl_line, cfg, transformer(pl_line.crs(), crs), amap) if pl_line else []
    if not points and not lines:
        raise RuntimeError("No pins found in the configured layers.")

    boxes = [p["geom"].boundingBox() for p in points + lines]
    ext = QgsRectangle(min(b.xMinimum() for b in boxes), min(b.yMinimum() for b in boxes),
                       max(b.xMaximum() for b in boxes), max(b.yMaximum() for b in boxes))
    ext.grow(max(acfg["shift"]["search_m"], acfg["point_tolerance_m"]) + 2)

    refs = {}
    for i, (key, lyr) in enumerate(refs_layers.items()):
        if progress:
            progress(i, len(refs_layers), "Reading reference layer %s..." % lyr.name())
        refs[key] = RefLayer(key, lyr, acfg["reference_fields"][key], acfg["ignore_plusstatus"], ext, crs)
        if "type" not in refs[key].real:
            raise RuntimeError("Layer '%s' has no field '%s' (type). Fields found: %s.\nAdjust analysis.reference_fields.%s in Settings." %
                               (lyr.name(), acfg["reference_fields"][key].get("type"),
                                ", ".join(f.name() for f in lyr.fields()), key))
        if refs[key].missing:
            warnings.append("Layer '%s': fields not found (ignored): %s" % (lyr.name(), ", ".join(refs[key].missing)))
        if not refs[key].geoms:
            warnings.append("No features of '%s' near the pins - is it the right layer/area?" % lyr.name())

    tol = float(acfg["point_tolerance_m"])
    S = acfg["shift"]

    def under_fn(pin):
        res = []
        pt = QgsGeometry.fromPointXY(QgsPointXY(pin["x"], pin["y"]))
        for key, ref in refs.items():
            is_pt = key == "oth_point"
            d = tol if is_pt else 0.0
            box = QgsRectangle(pin["x"] - d - 0.001, pin["y"] - d - 0.001, pin["x"] + d + 0.001, pin["y"] + d + 0.001)
            for fid in ref.index.intersects(box):
                g = ref.geoms[fid]
                if (g.distance(pt) <= tol) if is_pt else g.intersects(pt):
                    res.append(ref.obj(fid))
        return res

    def shift_fn(pin):
        line = pin["geom"]
        length = line.length()
        if length <= 0:
            return None
        n = max(int(length / S["step_m"]), 2)
        samples = [line.interpolate(length * i / (n - 1)) for i in range(n)]
        box = line.boundingBox()
        box.grow(S["search_m"])
        best = None
        for key in ("hitbgt", "oth_polygon"):
            ref = refs.get(key)
            if ref is None:
                continue
            for fid in ref.index.intersects(box):
                a = ref.attrs[fid]
                if str(a.get("type") or "").upper() != str(pin["objecttype"] or "").upper():
                    continue
                bnd = ref.boundary(fid)
                if bnd is None:
                    continue
                dists = [s.distance(bnd) for s in samples]
                m = {"max": max(dists), "mean": sum(dists) / len(dists), "length": length,
                     "bgtid": a.get("bgtid"), "bron": a.get("bron")}
                if best is None or m["mean"] < best["mean"]:
                    best = m
        return best

    rows = core.analyse(points, lines, under_fn, shift_fn, acfg,
                        progress=(lambda d, n: progress(d, n, "Analysing pins...")) if progress else None)
    summary = core.summarize(rows, acfg)

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "analysis_flags.csv")
    html_path = os.path.join(out_dir, "analysis_report.html")
    core.write_csv(rows, csv_path)
    core.write_html(rows, summary, acfg, core.RULE_NAMES, html_path)

    flag_layer = _flag_layer(rows, crs, os.path.join(out_dir, "analysis_flags.gpkg"), warnings)
    if flag_layer is not None and add_to_project:
        for old in QgsProject.instance().mapLayersByName("QC_analysis"):
            QgsProject.instance().removeMapLayer(old.id())
        QgsProject.instance().addMapLayer(flag_layer)
    return {"rows": rows, "summary": summary, "html": html_path, "csv": csv_path,
            "layer": flag_layer, "warnings": warnings,
            "n_flagged": sum(1 for r in rows if core.flagged(r, acfg))}


def _flag_layer(rows, crs, gpkg_path, warnings):
    mem = QgsVectorLayer("Point?crs=%s" % crs.authid(), "QC_analysis", "memory")
    pr = mem.dataProvider()
    pr.addAttributes([QgsField("layer", QVariant.String), QgsField("fid", QVariant.LongLong),
                      QgsField("operator", QVariant.String), QgsField("objecttype", QVariant.String),
                      QgsField("status", QVariant.String), QgsField("severity", QVariant.String),
                      QgsField("codes", QVariant.String), QgsField("reason", QVariant.String, "", 254),
                      QgsField("underneath", QVariant.String, "", 254),
                      QgsField("max_cm", QVariant.Int), QgsField("mean_cm", QVariant.Int),
                      QgsField("cat_op", QVariant.String), QgsField("cat_meas", QVariant.String)])
    mem.updateFields()
    feats = []
    for r in rows:
        if not r["flags"]:
            continue
        worst = max(r["flags"], key=lambda f: core.SEVERITY_ORDER[f[1]])[1]
        f = QgsFeature(mem.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r["x"], r["y"])))
        f.setAttributes([r["layer"], int(r["fid"]), r["operator"], str(r["objecttype"] or ""),
                         str(r["status"] or ""), worst, ",".join(c for c, _, _ in r["flags"]),
                         " | ".join(m for _, _, m in r["flags"])[:254], r["underneath"][:254],
                         None if r["max_shift_m"] is None else int(round(r["max_shift_m"] * 100)),
                         None if r["mean_shift_m"] is None else int(round(r["mean_shift_m"] * 100)),
                         str(r["categorie_operator"] or ""), str(r["categorie_measured"] or "")])
        feats.append(f)
    pr.addFeatures(feats)
    mem.updateExtents()

    layer = mem
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "QC_analysis"
    if os.path.exists(gpkg_path):
        opts.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
    try:
        res = QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg_path, QgsProject.instance().transformContext(), opts)
        if res[0] == QgsVectorFileWriter.NoError:
            saved = QgsVectorLayer("%s|layername=QC_analysis" % gpkg_path, "QC_analysis", "ogr")
            if saved.isValid():
                layer = saved
        else:
            warnings.append("Could not write %s (%s); the map layer is temporary." % (gpkg_path, res[1]))
    except Exception as exc:
        warnings.append("Could not write %s (%s); the map layer is temporary." % (gpkg_path, exc))

    cats = []
    for name, col in (("error", "200,0,0"), ("warning", "255,140,0"), ("info", "130,130,130")):
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,255,255,0", "outline_color": col + ",255",
                                            "outline_width": "0.8", "size": "4.5"})
        cats.append(QgsRendererCategory(name, sym, name))
    layer.setRenderer(QgsCategorizedSymbolRenderer("severity", cats))
    return layer
