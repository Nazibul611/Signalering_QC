# -*- coding: utf-8 -*-
"""Configurable automatic pre-checks. Rules live in the JSON config (cfg["rules"])."""
import re
from collections import Counter, defaultdict

from qgis.PyQt.QtCore import QVariant
from qgis.core import (QgsDistanceArea, QgsExpression, QgsFeature, QgsFeatureRequest, QgsField,
                       QgsGeometry, QgsMarkerSymbol, QgsPointXY, QgsProject, QgsRectangle,
                       QgsSingleSymbolRenderer, QgsSpatialIndex, QgsVectorLayer)

from . import utils
from .utils import LAYER_KEYS, rep_point, val


class RuleEngine(object):
    def __init__(self, cfg, layers, only_sampled=False):
        self.cfg, self.layers, self.only_sampled = cfg, layers, only_sampled
        primary = layers.get("point") or next((l for l in layers.values() if l is not None), None)
        self.crs = primary.crs() if primary else None
        self.flags, self.warnings = [], []
        self.codes = defaultdict(list)
        self.counts = Counter()
        self._xf, self._ref_cache = {}, {}

    # ---------------------------------------------------------------- plumbing
    def _iter(self, layer, no_geometry=False):
        req = QgsFeatureRequest()
        af = self.cfg["fields"]["area"]
        if self.only_sampled and layer.fields().indexOf(af) >= 0:
            req.setFilterExpression("%s IS NOT NULL" % QgsExpression.quotedColumnRef(af))
        if no_geometry:
            req.setFlags(QgsFeatureRequest.NoGeometry)
        return layer.getFeatures(req)

    def _emit(self, key, layer, feat, rule, message):
        p = rep_point(feat)
        if p is not None and self.crs is not None and layer.crs() != self.crs:
            xf = self._xf.get(key)
            if xf is None:
                xf = self._xf[key] = utils.transformer(layer.crs(), self.crs)
            p = xf.transform(p)
        self.flags.append({"layer": key, "fid": feat.id(), "rule": rule["id"],
                           "name": rule.get("name", rule["id"]),
                           "severity": rule.get("severity", "warning"),
                           "message": message, "point": p})
        self.codes[(key, feat.id())].append(rule["id"])
        self.counts[rule["id"]] += 1

    def run(self):
        for rule in self.cfg.get("rules", []):
            if not rule.get("enabled", True):
                continue
            handler = getattr(self, "_rule_%s" % rule.get("type", ""), None)
            if handler is None:
                self.warnings.append("%s: unknown rule type '%s'" % (rule.get("id"), rule.get("type")))
                continue
            for key in rule.get("layers", LAYER_KEYS):
                layer = self.layers.get(key)
                if layer is None:
                    continue
                try:
                    handler(rule, key, layer)
                except Exception as exc:  # a broken rule must not stop the others
                    self.warnings.append("%s on %s layer: %s" % (rule.get("id"), key, exc))
        self.write_codes()
        return self

    def write_codes(self):
        af = self.cfg["fields"]["auto"]
        for key, layer in self.layers.items():
            if layer is None:
                continue
            utils.ensure_fields(layer, {af: (QVariant.String, 60)})
            if layer.fields().indexOf(af) < 0:
                self.warnings.append("Could not create field %s on %s layer." % (af, key))
                continue
            changes = {}
            for feat in self._iter(layer, no_geometry=True):
                new = ";".join(sorted(set(self.codes.get((key, feat.id()), [])))) or None
                if val(feat, af) != new:
                    changes[feat.id()] = {af: new}
            utils.write_values(layer, changes)

    # ---------------------------------------------------------------- rule types
    def _rule_geometry(self, rule, key, layer):
        for feat in self._iter(layer):
            g = feat.geometry()
            if g is None or g.isNull() or g.isEmpty():
                self._emit(key, layer, feat, rule, "Empty geometry")
            elif not g.isGeosValid():
                self._emit(key, layer, feat, rule, "Invalid geometry")

    def _rule_required(self, rule, key, layer):
        fields = [f for f in rule.get("fields", []) if layer.fields().indexOf(f) >= 0]
        for feat in self._iter(layer, True):
            missing = [f for f in fields if val(feat, f) is None]
            if missing:
                self._emit(key, layer, feat, rule, "Missing: %s" % ", ".join(missing))

    def _rule_domain(self, rule, key, layer):
        field = rule["field"]
        if layer.fields().indexOf(field) < 0:
            return
        cs = rule.get("case_sensitive", False)
        norm = (lambda s: str(s).strip()) if cs else (lambda s: str(s).strip().lower())
        allowed = set(norm(a) for a in rule.get("allowed", []))
        for feat in self._iter(layer, True):
            v = val(feat, field)
            if v is not None and norm(v) not in allowed:
                self._emit(key, layer, feat, rule, "%s = '%s' is not an allowed value" % (field, v))

    def _rule_status_remark(self, rule, key, layer):
        sf, rf = rule.get("status_field", "Status"), rule.get("remark_field", "Opmerking")
        if layer.fields().indexOf(sf) < 0 or layer.fields().indexOf(rf) < 0:
            return
        pats = {k.lower(): v for k, v in rule.get("patterns", {}).items()}
        for feat in self._iter(layer, True):
            st, rem = val(feat, sf), val(feat, rf)
            if st is None or rem is None:
                continue
            plist = pats.get(str(st).strip().lower())
            if plist and not any(re.search(p, str(rem), re.I) for p in plist):
                self._emit(key, layer, feat, rule, "Remark does not match status '%s'" % st)

    def _rule_duplicate(self, rule, key, layer):
        fields = [f for f in rule.get("fields", []) if layer.fields().indexOf(f) >= 0]
        d = float(rule.get("distance", 1.0))
        index, pts = QgsSpatialIndex(), {}
        for feat in self._iter(layer):
            p = rep_point(feat)
            if p is None:
                continue
            pts[feat.id()] = (p, tuple(str(val(feat, f)) for f in fields), feat)
            nf = QgsFeature(feat.id())
            nf.setGeometry(QgsGeometry.fromPointXY(p))
            index.addFeature(nf)
        for fid, (p, k, feat) in pts.items():
            box = QgsRectangle(p.x() - d, p.y() - d, p.x() + d, p.y() + d)
            for oid in index.intersects(box):
                if oid == fid or oid not in pts:
                    continue
                op, ok_, _ = pts[oid]
                if ok_ == k and p.distance(op) <= d:
                    self._emit(key, layer, feat, rule, "Possible duplicate of feature %s" % oid)
                    break

    def _rule_min_area(self, rule, key, layer):
        da = QgsDistanceArea()
        da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
        da.setEllipsoid(QgsProject.instance().ellipsoid())
        gf, sf = rule.get("group_field"), rule.get("status_field", "Status")
        over = {str(k).lower(): float(v) for k, v in rule.get("overrides", {}).items()}
        default = float(rule.get("default", 4.0))
        apply_status = [s.lower() for s in rule.get("apply_status", [])]
        for feat in self._iter(layer):
            if apply_status and str(val(feat, sf) or "").lower() not in apply_status:
                continue
            g = feat.geometry()
            if g is None or g.isNull():
                continue
            lim = over.get(str(val(feat, gf) or "").lower(), default) if gf else default
            area = da.measureArea(g)
            if area < lim:
                self._emit(key, layer, feat, rule, "Area %.1f m2 is below %.1f m2" % (area, lim))

    def _reference(self, name):
        refs = QgsProject.instance().mapLayersByName(name)
        if not refs:
            self.warnings.append("Reference layer '%s' not found in the project - rule skipped." % name)
            return None
        ref = refs[0]
        if ref.id() not in self._ref_cache:
            index, geoms = QgsSpatialIndex(), {}
            for f in ref.getFeatures():
                g = f.geometry()
                if g is None or g.isNull():
                    continue
                index.addFeature(f)
                geoms[f.id()] = QgsGeometry(g)
            self._ref_cache[ref.id()] = (ref, index, geoms)
        return self._ref_cache[ref.id()]

    def _rule_spatial_ref(self, rule, key, layer):
        where = {k: str(v).lower() for k, v in rule.get("where", {}).items()}
        if any(layer.fields().indexOf(k) < 0 for k in where):
            return
        cached = self._reference(rule["reference_layer"])
        if cached is None:
            return
        ref, index, geoms = cached
        xf = utils.transformer(layer.crs(), ref.crs())
        expect = rule.get("expect", "intersect")
        tol = float(rule.get("tolerance", 0.0)) if expect == "intersect" else 0.0
        for feat in self._iter(layer):
            if any(str(val(feat, k) or "").lower() != v for k, v in where.items()):
                continue
            p = rep_point(feat)
            if p is None:
                continue
            if xf is not None:
                p = xf.transform(p)
            pg = QgsGeometry.fromPointXY(QgsPointXY(p))
            box = QgsRectangle(p.x() - tol - 0.001, p.y() - tol - 0.001,
                               p.x() + tol + 0.001, p.y() + tol + 0.001)
            hit = False
            for rid in index.intersects(box):
                g = geoms[rid]
                if (g.distance(pg) <= tol) if tol > 0 else g.intersects(pg):
                    hit = True
                    break
            if expect == "intersect" and not hit:
                self._emit(key, layer, feat, rule, "Nothing found in '%s' at this location" % ref.name())
            elif expect == "no_intersect" and hit:
                self._emit(key, layer, feat, rule, "Object already exists in '%s'" % ref.name())


def build_flag_layer(flags, crs):
    lyr = QgsVectorLayer("Point?crs=%s" % crs.authid(), "QC_prechecks", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("layer", QVariant.String), QgsField("fid", QVariant.LongLong),
                      QgsField("rule", QVariant.String), QgsField("name", QVariant.String),
                      QgsField("severity", QVariant.String), QgsField("message", QVariant.String)])
    lyr.updateFields()
    feats = []
    for fl in flags:
        if fl["point"] is None:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(fl["point"])))
        f.setAttributes([fl["layer"], int(fl["fid"]), fl["rule"], fl["name"], fl["severity"], fl["message"]])
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,0,0,0",
                                        "outline_color": "220,0,0,255", "outline_width": "0.8",
                                        "size": "5"})
    lyr.setRenderer(QgsSingleSymbolRenderer(sym))
    return lyr
