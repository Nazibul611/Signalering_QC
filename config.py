# -*- coding: utf-8 -*-
"""Project-level configuration (stored inside the QGIS project file) and review levels."""
import json
import os

from qgis.core import QgsProject, QgsSettings

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SCOPE = "signalering_qc"
LEVEL_KEY = "signalering_qc/level"          # active review level, remembered per QGIS user

DEFAULT_STATUS = {"accepted": "Accepted", "rejected": "Rejected", "doubtful": "Doubtful",
                  "missed": "Missed", "undetectable": "Not detectable"}
# Texts written by versions before 0.4.0 - still understood when reading old results.
LEGACY_STATUS = {"accepted": "Akkoord", "rejected": "Afgekeurd", "doubtful": "Twijfel",
                 "missed": "Gemist", "undetectable": "Niet detecteerbaar"}
LEGACY_ERRORS = {"Verkeerd objecttype": "Wrong object type", "Valse mutatie": "False mutation",
                 "Geometrie onjuist": "Wrong geometry", "Attribuut onjuist": "Wrong attribute",
                 "Opmerking onjuist/onduidelijk": "Unclear/incorrect remark",
                 "Gemiste mutatie": "Missed mutation"}


def merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def defaults():
    with open(os.path.join(PLUGIN_DIR, "default_config.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _migrate(cfg):
    """Project configs saved by older versions carry the Dutch default texts: switch those to English
    (customised texts are left alone)."""
    sv = cfg.get("status_values", {})
    for k, legacy in LEGACY_STATUS.items():
        if sv.get(k) == legacy:
            sv[k] = DEFAULT_STATUS[k]
    for et in cfg.get("error_types", []):
        if et.get("label") in LEGACY_ERRORS:
            et["label"] = LEGACY_ERRORS[et["label"]]
    if cfg.get("missed_label") in LEGACY_ERRORS:
        cfg["missed_label"] = LEGACY_ERRORS[cfg["missed_label"]]
    if not cfg.get("levels"):
        cfg["levels"] = defaults()["levels"]


# ------------------------------------------------------------------ review levels
def level_fields(cfg, code):
    """QC field names of one review level: the shared/base fields overridden by the level's own."""
    out = dict(cfg.get("base_fields", cfg["fields"]))
    out.update(cfg.get("levels", {}).get(code, {}).get("fields", {}))
    return out


def level_list(cfg):
    """[(code, label, resolved field names)] for every review level."""
    return [(code, lv.get("label", code), level_fields(cfg, code))
            for code, lv in cfg.get("levels", {}).items()]


def active_level(cfg):
    codes = list(cfg.get("levels", {}))
    code = QgsSettings().value(LEVEL_KEY, cfg.get("default_level", "TL"))
    if code not in codes:
        dflt = cfg.get("default_level")
        code = dflt if dflt in codes else (codes[0] if codes else "TL")
    return code


def set_active_level(code):
    QgsSettings().setValue(LEVEL_KEY, code)


def _resolve(cfg):
    """cfg["fields"] becomes the QC fields of the active level, so all other code keeps using
    cfg["fields"]. The untouched fields are kept in cfg["base_fields"]."""
    code = active_level(cfg)
    cfg["active_level"] = code
    cfg["active_level_label"] = cfg["levels"].get(code, {}).get("label", code)
    cfg["base_fields"] = dict(cfg["fields"])
    cfg["fields"] = level_fields(cfg, code)
    return cfg


def load_config(resolved=True):
    """resolved=False returns the config exactly as stored (use it for the Settings dialog)."""
    cfg = defaults()
    txt, ok = QgsProject.instance().readEntry(SCOPE, "config", "")
    if ok and txt:
        try:
            merge(cfg, json.loads(txt))
        except ValueError:
            pass
    _migrate(cfg)
    return _resolve(cfg) if resolved else cfg


def save_config(cfg):
    data = dict(cfg)
    if "base_fields" in data:               # never store the resolved (per-level) field names
        data["fields"] = data.pop("base_fields")
    data.pop("active_level", None)
    data.pop("active_level_label", None)
    prj = QgsProject.instance()
    prj.writeEntry(SCOPE, "config", json.dumps(data, ensure_ascii=False))
    prj.setDirty(True)
