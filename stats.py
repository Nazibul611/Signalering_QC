# -*- coding: utf-8 -*-
"""Acceptance statistics and report export (HTML / CSV)."""
import csv
import datetime
import os
from collections import Counter
from html import escape

from qgis.core import QgsExpression, QgsFeatureRequest

from . import qc_rounds, utils
from .utils import LAYER_KEYS, val

LAYER_LABEL = {"point": "Pinpoint", "line": "Pinline", "polygon": "Pinpolygon"}


def _new_rec():
    return {"sampled": 0, "reviewed": 0, "accepted": 0, "doubtful": 0,
            "major": 0, "minor": 0, "missed": 0, "undetectable": 0, "rate": None, "verdict": ""}


def _finish(rec, cfg):
    denom = rec["reviewed"] + rec["missed"]
    rec["rate"] = (100.0 * (rec["major"] + rec["missed"]) / denom) if denom else None
    acc = cfg["acceptance"]
    if rec["reviewed"] < acc["min_reviewed_for_verdict"]:
        rec["verdict"] = "Too few reviewed"
    elif rec["rate"] <= acc["max_major_error_pct"]:
        rec["verdict"] = "Accepted"
    else:
        rec["verdict"] = "Not accepted"
    return rec


def compute(cfg, layers, round_code=None, results_layer=None, sample=None):
    """Statistics of the active review level. round_code=None: the latest result of every pin (as stored on
    the pins). round_code="TL-01": only the areas and results of that QC round (from QC_Results)."""
    f = cfg["fields"]                       # QC fields of the active review level
    amap = utils.status_alias_map(cfg)
    status_names = utils.status_field_names(cfg)
    missed_label = cfg.get("missed_label", "Missed mutation")
    extra = cfg.get("report_fields", [])
    ops, areas, errors, findings = {}, {}, Counter(), []
    rows, round_areas = {}, None
    if round_code:
        rows = qc_rounds.load_rows(results_layer, round_code)
        round_areas = set()
        if sample is not None:
            for sf in sample.getFeatures():
                if round_code in qc_rounds.area_rounds(val(sf, "qc_rounds")):
                    round_areas.add(val(sf, "area_id"))

    def finding(key, feat, op, st, et, area, src=None):
        src = src or {"remark": val(feat, f["remark"]), "reviewer": val(feat, f["reviewer"]),
                      "judged": val(feat, f["date"])}
        d = {"layer": LAYER_LABEL.get(key, key), "fid": feat.id(), "operator": op, "area": area,
             "status": st, "error_type": et or "", "remark": src.get("remark") or "",
             "reviewer": src.get("reviewer") or "", "date": src.get("judged") or ""}
        for x in extra:
            d["f_" + x] = val(feat, x) or ""
        findings.append(d)

    for key in LAYER_KEYS:
        lyr = layers.get(key)
        if lyr is None:
            continue
        names = [x.name() for x in lyr.fields()]
        if f["area"] not in names or f["status"] not in names:
            continue
        req = QgsFeatureRequest().setFilterExpression(
            "%s IS NOT NULL" % QgsExpression.quotedColumnRef(f["area"]))
        for feat in lyr.getFeatures(req):
            st = val(feat, f["status"])
            kind = utils.status_kind(amap, st)
            if kind != "missed" and utils.is_missed(cfg, feat, amap, status_names):
                continue                    # a missed mutation added by another review level
            src = None
            if round_code:
                if kind == "missed":
                    if val(feat, f.get("qcround")) != round_code:
                        continue            # a missed mutation added in another QC round
                else:
                    if val(feat, f["area"]) not in round_areas:
                        continue            # this area is not part of the round
                    src = rows.get((key, feat.id())) or {}
                    st = src.get("status")
                    kind = utils.status_kind(amap, st)
            op = str(val(feat, cfg["operator_field"]) or "(unknown)")
            et = (src or {}).get("error_type") if src is not None else val(feat, f["error_type"])
            area = val(feat, f["area"])
            rec = ops.setdefault(op, _new_rec())
            arec = areas.setdefault(area, {"n": 0, "reviewed": 0})
            if kind == "missed":
                rec["missed"] += 1
                errors[et or missed_label] += 1
                finding(key, feat, op, st, et or missed_label, area, src)
                continue
            rec["sampled"] += 1
            arec["n"] += 1
            if kind == "undetectable":      # handled, but NOT part of the QC
                rec["undetectable"] += 1
                arec["reviewed"] += 1       # the area counts as handled, the feature stays out of the error rate
                finding(key, feat, op, st, et, area, src)
                continue
            if kind in ("accepted", "rejected", "doubtful"):
                rec["reviewed"] += 1
                arec["reviewed"] += 1
                if kind == "accepted":
                    rec["accepted"] += 1
                elif kind == "doubtful":
                    rec["doubtful"] += 1
                    finding(key, feat, op, st, et, area, src)
                else:
                    rec["minor" if utils.error_severity(cfg, et) == "minor" else "major"] += 1
                    errors[et or "(no error type)"] += 1
                    finding(key, feat, op, st, et, area, src)

    totals = _new_rec()
    for rec in ops.values():
        for k in ("sampled", "reviewed", "accepted", "doubtful", "major", "minor", "missed", "undetectable"):
            totals[k] += rec[k]
        _finish(rec, cfg)
    _finish(totals, cfg)
    return {"operators": dict(sorted(ops.items())), "totals": totals, "errors": errors,
            "areas": areas, "findings": findings,
            "level": cfg.get("active_level_label", "") + (" - round %s" % round_code if round_code else
                                                           " - latest result"),
            "round": round_code}


def sample_info(sample_layer):
    rows = []
    if sample_layer is None:
        return rows
    for f in sample_layer.getFeatures():
        rows.append({"area_id": val(f, "area_id") or "", "round": val(f, "round") or 1,
                     "method": val(f, "method") or "", "seed": val(f, "seed"),
                     "drawn": val(f, "drawn") or "", "n_feat": val(f, "n_feat") or 0,
                     "operators": val(f, "operators") or "", "status": val(f, "status") or "",
                     "qc_rounds": ", ".join(sorted(qc_rounds.area_rounds(val(f, "qc_rounds"))))})
    rows.sort(key=lambda r: (r["round"], r["area_id"]))
    return rows


def _pct(v):
    return "-" if v is None else "%.1f%%" % v


# ------------------------------------------------------------------ exports
def export_csv(base_path, data, cfg):
    base = os.path.splitext(base_path)[0]
    p1, p2 = base + "_operators.csv", base + "_findings.csv"
    with open(p1, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Level", "Operator", "Sampled", "Reviewed", "Accepted", "Doubtful", "Major errors",
                    "Minor errors", "Missed", "Not detectable", "Error rate %", "Verdict"])
        for op, r in list(data["operators"].items()) + [("TOTAL", data["totals"])]:
            w.writerow([data.get("level", ""), op, r["sampled"], r["reviewed"], r["accepted"], r["doubtful"], r["major"],
                        r["minor"], r["missed"], r["undetectable"], "" if r["rate"] is None else round(r["rate"], 1),
                        r["verdict"]])
    extra = cfg.get("report_fields", [])
    with open(p2, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Level", "Layer", "FID", "Operator", "Area", "QC status", "Error type", "QC remark",
                    "Reviewer", "Date"] + extra)
        for d in data["findings"]:
            w.writerow([data.get("level", ""), d["layer"], d["fid"], d["operator"], d["area"], d["status"], d["error_type"],
                        d["remark"], d["reviewer"], d["date"]] + [d["f_" + x] for x in extra])
    return [p1, p2]


def export_html(path, cfg, data, sample_rows, title):
    e = escape
    acc = cfg["acceptance"]
    extra = cfg.get("report_fields", [])
    css = ("body{font-family:Segoe UI,Arial,sans-serif;margin:2rem;color:#111}"
           "table{border-collapse:collapse;margin:.6rem 0 1.6rem}"
           "th,td{border:1px solid #ccc;padding:4px 10px;text-align:left;font-size:13px}"
           "th{background:#f0f2f5}.ok{background:#dcfce7}.bad{background:#fee2e2}.na{background:#f3f4f6}"
           "h1{margin-bottom:0}.muted{color:#555}")

    def vcls(v):
        return "ok" if v == "Accepted" else ("bad" if v == "Not accepted" else "na")

    def row(op, r):
        return ("<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td>"
                "<td>%d</td><td>%d</td><td>%s</td><td class='%s'>%s</td></tr>" % (
                    e(op), r["sampled"], r["reviewed"], r["accepted"], r["doubtful"], r["major"],
                    r["minor"], r["missed"], r["undetectable"], _pct(r["rate"]), vcls(r["verdict"]), e(r["verdict"])))

    out = ["<html><head><meta charset='utf-8'><title>%s</title><style>%s</style></head><body>" % (e(title), css),
           "<h1>%s</h1><p class='muted'>Generated %s &middot; acceptance: max. %.1f%% major errors "
           "(incl. missed), at least %d reviewed features. <i>Not detectable</i> = not wrong but not detectable: "
           "counted as handled, excluded from the error rate.</p>" % (
               e(title), datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               acc["max_major_error_pct"], acc["min_reviewed_for_verdict"]),
           "<h2>Result per operator</h2><table><tr><th>Operator</th><th>Sampled</th><th>Reviewed</th>"
           "<th>Accepted</th><th>Doubtful</th><th>Major</th><th>Minor</th><th>Missed</th><th>Not detectable</th>"
           "<th>Error rate</th><th>Verdict</th></tr>"]
    out += [row(op, r) for op, r in data["operators"].items()]
    out.append(row("TOTAL", data["totals"]).replace("<td>TOTAL</td>", "<th>TOTAL</th>"))
    out.append("</table><h2>Error types</h2><table><tr><th>Error type</th><th>Count</th></tr>")
    out += ["<tr><td>%s</td><td>%d</td></tr>" % (e(k), v) for k, v in data["errors"].most_common()]
    out.append("</table><h2>Sample draws</h2><table><tr><th>Area</th><th>Round</th><th>Method</th>"
               "<th>Seed</th><th>Drawn</th><th>Features</th><th>Reviewed</th><th>Operators</th><th>QC rounds</th></tr>")
    for r in sample_rows:
        a = data["areas"].get(r["area_id"], {"n": 0, "reviewed": 0})
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td>"
                   "<td>%s</td><td>%s</td></tr>" % (e(r["area_id"]), r["round"], e(r["method"]), r["seed"],
                                                       e(r["drawn"]), r["n_feat"], a["reviewed"],
                                                       e(r["operators"]), e(r.get("qc_rounds", ""))))
    out.append("</table><h2>Findings (rejected, doubtful, missed, not detectable)</h2><table><tr><th>Layer</th><th>FID</th>"
               "<th>Operator</th><th>Area</th><th>QC status</th><th>Error type</th><th>QC remark</th>"
               "<th>Reviewer</th>%s</tr>" % "".join("<th>%s</th>" % e(x) for x in extra))
    for d in data["findings"]:
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td>%s</td>%s</tr>" % (e(d["layer"]), d["fid"], e(d["operator"]), e(str(d["area"])),
                                           e(str(d["status"])), e(d["error_type"]), e(str(d["remark"])),
                                           e(str(d["reviewer"])),
                                           "".join("<td>%s</td>" % e(str(d["f_" + x])) for x in extra)))
    out.append("</table></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return path
