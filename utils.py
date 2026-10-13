# -*- coding: utf-8 -*-
"""Small helpers shared by all modules."""
import logging
import datetime

from qgis.PyQt.QtCore import QVariant
from . import config
from qgis.core import (NULL, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsField, QgsProject,
                       QgsVectorDataProvider, QgsVectorLayer)

LAYER_KEYS = ("point", "line", "polygon")


def is_null(v):
    if v is None:
        return True
    try:
        if v == NULL:
            return True
    except Exception:
        logging.getLogger(__name__).debug("Ignored error", exc_info=True)
    if isinstance(v, str) and not v.strip():
        return True
    return False


def val(feature, name):
    """Attribute value or None (also None when the field does not exist)."""
    if not name:
        return None
    try:
        v = feature[name]
    except (KeyError, IndexError):
        return None
    return None if is_null(v) else v


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def run_dialog(dlg):
    return dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()


def layer_by_id(lid):
    return QgsProject.instance().mapLayer(lid) if lid else None


def get_layers(cfg):
    return {k: layer_by_id(cfg["layers"].get(k)) for k in LAYER_KEYS}


def rep_point(feature):
    """A point that lies on/in the feature (used to assign it to an area)."""
    g = feature.geometry()
    if g is None or g.isNull() or g.isEmpty():
        return None
    try:
        p = g.pointOnSurface()
        if p is None or p.isNull():
            p = g.centroid()
    except Exception:
        p = g.centroid()
    if p is None or p.isNull():
        return None
    return p.asPoint()


def qc_field_specs(cfg):
    """Fields to create: the shared ones plus the QC fields of EVERY review level."""
    base = cfg.get("base_fields", cfg["fields"])
    specs = {base["area"]: (QVariant.String, 20), base["auto"]: (QVariant.String, 60)}
    for _code, _label, f in config.level_list(cfg):
        specs.update({
            f["status"]: (QVariant.String, 20),
            f["error_type"]: (QVariant.String, 40),
            f["remark"]: (QVariant.String, 120),
            f["date"]: (QVariant.String, 20),
            f["reviewer"]: (QVariant.String, 30),
            f["round"]: (QVariant.Int, 0),
            f["qcround"]: (QVariant.String, 20),
        })
    return specs


# ------------------------------------------------------------------ status texts
STATUS_KEYS = ("accepted", "rejected", "doubtful", "undetectable", "missed")
HANDLED_KINDS = ("accepted", "rejected", "doubtful", "undetectable")   # the reviewer has dealt with it


def status_alias_map(cfg):
    """casefolded status text -> kind. Accepts the configured text, the English default and the older
    Dutch text, so results written by earlier versions keep working."""
    out = {}
    for key in STATUS_KEYS:
        for text in (config.LEGACY_STATUS.get(key), config.DEFAULT_STATUS.get(key),
                     cfg.get("status_values", {}).get(key)):
            if text:
                out[str(text).strip().casefold()] = key
    return out


def status_values_for(cfg, key):
    """Every stored text that means `key` (for QGIS expressions)."""
    seen = []
    for text in (cfg.get("status_values", {}).get(key), config.DEFAULT_STATUS.get(key),
                 config.LEGACY_STATUS.get(key)):
        if text and text not in seen:
            seen.append(text)
    return seen


def status_kind(amap, value):
    return None if value is None else amap.get(str(value).strip().casefold())


def status_field_names(cfg):
    """The status field of every review level."""
    names = {cfg.get("base_fields", cfg["fields"])["status"]}
    for _code, _label, f in config.level_list(cfg):
        names.add(f["status"])
    return names


def is_missed(cfg, feat, amap, names=None):
    """True for a 'missed mutation' point added by a reviewer (at any review level)."""
    for n in (names or status_field_names(cfg)):
        if status_kind(amap, val(feat, n)) == "missed":
            return True
    return False


def error_severity(cfg, label):
    """'major' / 'minor' for an error type label (also understands the older Dutch labels)."""
    sev = {str(e["label"]).casefold(): e.get("severity", "major") for e in cfg.get("error_types", [])}
    lab = str(label or "").strip()
    s = sev.get(lab.casefold())
    if s is None:
        s = sev.get(config.LEGACY_ERRORS.get(lab, "").casefold())
    return s or "major"


def ensure_fields(layer, specs):
    """Add the fields in specs {name: (QVariant type, length)} that are missing."""
    missing = [QgsField(n, t, "", ln) for n, (t, ln) in specs.items()
               if layer.fields().indexOf(n) < 0]
    if not missing:
        return True
    if layer.isEditable():
        ok = True
        for fld in missing:
            ok = layer.addAttribute(fld) and ok
        layer.updateFields()
        return ok
    prov = layer.dataProvider()
    if not (prov.capabilities() & QgsVectorDataProvider.AddAttributes):
        return False
    ok = prov.addAttributes(missing)
    layer.updateFields()
    return ok


def write_values(layer, changes):
    """changes = {fid: {field_name: value}}; writes through the edit buffer if the
    layer is being edited, otherwise straight to the data provider."""
    if not changes:
        return True
    fields = layer.fields()
    ok = True
    if layer.isEditable():
        for fid, vals in changes.items():
            for name, v in vals.items():
                idx = fields.indexOf(name)
                if idx >= 0:
                    ok = layer.changeAttributeValue(fid, idx, v) and ok
    else:
        prov_changes = {}
        for fid, vals in changes.items():
            d = {fields.indexOf(n): v for n, v in vals.items() if fields.indexOf(n) >= 0}
            if d:
                prov_changes[fid] = d
        ok = layer.dataProvider().changeAttributeValues(prov_changes)
    layer.triggerRepaint()
    return bool(ok)


def add_feature(layer, feat):
    if layer.isEditable():
        ok = layer.addFeature(feat)
    else:
        ok, _ = layer.dataProvider().addFeatures([feat])
    layer.triggerRepaint()
    return bool(ok)


def transformer(src_crs, dst_crs):
    if src_crs == dst_crs:
        return None
    return QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())


COPY_COORD_DEFAULT = {"enabled": True, "crs": "EPSG:28992", "format": "{x:.2f},{y:.2f}"}


def pin_location(feature):
    """Point used for the oblique check: the point itself, the middle of a line, or a point inside a polygon."""
    g = feature.geometry()
    if g is None or g.isNull() or g.isEmpty():
        return None
    if g.type() == 0:                                   # point
        return g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
    if g.type() == 1:                                   # line
        mid = g.interpolate(g.length() / 2.0)
        if mid is not None and not mid.isNull():
            return mid.asPoint()
    return rep_point(feature)


def coordinate_text(layer, feature, copy_cfg=None):
    """Coordinate of a pin as text in the configured CRS (default RD 'x,y' in metres)."""
    c = dict(COPY_COORD_DEFAULT)
    c.update(copy_cfg or {})
    p = pin_location(feature)
    if p is None:
        return None
    dst = QgsCoordinateReferenceSystem(c["crs"])
    if dst.isValid() and layer.crs() != dst:
        p = QgsCoordinateTransform(layer.crs(), dst, QgsProject.instance()).transform(p)
    return c["format"].format(x=p.x(), y=p.y())


def get_sample_layer(cfg):
    """The sample layer from the project config; if the id is missing or stale, any layer that looks
    like one (fields area_id, round, n_feat - e.g. QC_Sample) so existing areas are never 'lost'."""
    lyr = layer_by_id(cfg["layers"].get("sample"))
    if lyr is not None:
        return lyr
    found = []
    for l in QgsProject.instance().mapLayers().values():
        if isinstance(l, QgsVectorLayer) and all(l.fields().indexOf(n) >= 0 for n in ("area_id", "round", "n_feat")):
            found.append(l)
    found.sort(key=lambda l: (l.name() != "QC_Sample", l.name()))
    if found:
        cfg["layers"]["sample"] = found[0].id()
        return found[0]
    return None
