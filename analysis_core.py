# -*- coding: utf-8 -*-
"""Initial analysis (core logic, no QGIS imports).

Two groups of checks, both run on *all* pins before any sampling:

* Point pins (Nieuw / Verwijderen / Gewijzigd): what lies underneath the pin in the BGT
  reference layers (hitbgt, hitbgtoth)?  Codes A01..A05.
* Line pins (Gewijzigd, "hervormen"): how far does the drawn line lie from the boundary of the
  matching BGT object?  Codes B01..B04.

The QGIS side (analysis_qgis.py) supplies the geometry work through two callbacks:

    under_fn(pin) -> [ {layer, type, tmain, tsub, fmain, fsub, bron, bgtid}, ... ]
    shift_fn(pin) -> {"max": m, "mean": m, "length": m, "bgtid": str, "bron": str} or None

so the same code can be run and tested with any geometry library.
"""
import csv
import html
import re
from collections import Counter, OrderedDict, defaultdict

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}


# ---------------------------------------------------------------- helpers
def norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def parse_target(opmerking, patterns):
    """'Gewijzigd naar open verharding: tegels' -> ('open verharding', 'tegels').
    Returns None when the remark does not name a target class."""
    txt = str(opmerking or "").strip()
    for pat in patterns:
        m = re.match(pat, txt, re.I)
        if m:
            tgt = m.group(1).strip()
            main, _, sub = tgt.partition(":")
            return norm(main), (norm(sub) or None)
    return None


DESCRIPTORS = (("bgttype", "type"), ("otype", "otype"), ("tmain", "tmain"), ("tsub", "tsub"),
               ("fmain", "fmain"), ("fsub", "fsub"))


def match_fields(target, obj):
    """Which BGT descriptor fields (bgttype, otype, tmain, tsub, fmain, fsub) carry the class/function
    named in the remark? [] = no match. A remark 'main: sub' needs both parts to be found."""
    main, sub = target
    vals = {label: norm(obj.get(key)) for label, key in DESCRIPTORS}
    hit_main = [l for l, v in vals.items() if v and v == main]
    if not hit_main:
        return []
    if sub is None:
        return hit_main
    hit_sub = [l for l, v in vals.items() if v and v == sub]
    return hit_main + hit_sub if hit_sub else []


def target_equals_current(target, obj):
    return bool(match_fields(target, obj))


def first_match(target, objs):
    """(object, matched field labels) for the first object matching the remark, else (None, [])."""
    for o in objs:
        f = match_fields(target, o)
        if f:
            return o, f
    return None, []


def describe(obj):
    bits = [obj.get("type") or "?"]
    if obj.get("otype"):
        bits[0] += " [%s]" % obj["otype"]
    cls = " / ".join(x for x in (obj.get("tmain"), obj.get("tsub"), obj.get("fmain"), obj.get("fsub")) if x)
    if cls:
        bits.append("(%s)" % cls)
    return " ".join(bits)


def shift_class(max_shift, acfg):
    """Measured shift (m) -> (label, expected Categorie or None)."""
    t = acfg["shift"]
    if max_shift < t["noise_m"]:
        return "<%d cm" % round(t["noise_m"] * 100), None
    for lim, cat, label in ((t["cat3_max_m"], 3, "%d-%d cm" % (round(t["noise_m"] * 100), round(t["cat3_max_m"] * 100))),
                            (t["cat2_max_m"], 2, "%d-%d cm" % (round(t["cat3_max_m"] * 100), round(t["cat2_max_m"] * 100)))):
        if max_shift < lim:
            return label, cat
    return ">%d cm" % round(t["cat2_max_m"] * 100), 1


def _as_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- point pins
def removal_covers(pin, same, removals, acfg):
    """True when a Verwijderen pin of an accepted type removes (part of) what lies underneath
    this Nieuw pin: it sits on the same BGT object, or within replace_distance_m of the pin."""
    if not removals:
        return False
    otype = (pin["objecttype"] or "").upper()
    accept = set(t.upper() for t in acfg["related"].get(otype, {}).get("removal_types", [otype]))
    ids = {o.get("bgtid") for o in same if o.get("bgtid")}
    rad = float(acfg.get("replace_distance_m", 3.0))
    for rtype in accept:
        if ids & removals["ids"].get(rtype, set()):
            return True
        for x, y in removals["pts"].get(rtype, ()):
            if (x - pin["x"]) ** 2 + (y - pin["y"]) ** 2 <= rad * rad:
                return True
    return False


def check_point(pin, under, acfg, removals=None):
    """Return (underneath_text, [(code, severity, message), ...]) for one point pin."""
    flags = []
    status, otype = norm(pin["status"]), (pin["objecttype"] or "").strip().upper()
    rel = acfg["related"].get(otype)
    own_bron = acfg["bronhouder"]
    text = "; ".join(sorted({describe(o) + "@" + str(o.get("layer")) for o in under})) or "niets"

    def add(code, sev, msg):
        flags.append((code, sev, msg))

    if rel is None:
        return text, [("A00", "info", "Objecttype '%s' is not in the 'related' mapping - not checked" % otype)]
    related_types = set(rel["types"])
    if not related_types:                      # e.g. BAG-only pins: no BGT class to compare with
        return text, flags

    if not under:
        add("A02", "warning", "No BGT object found at this location")
        return text, flags

    same = [o for o in under if (o.get("type") or "").upper() in related_types]
    ground = [o for o in under if o.get("layer") == "hitbgt"]

    # bronhouder of what lies underneath (ground layer decides who is responsible)
    foreign = sorted({str(o.get("bron")) for o in (same or ground) if o.get("bron") and o.get("bron") != own_bron})
    if foreign:
        add("A03", "warning", "Underlying object has bronhouder %s (expected %s)" % (", ".join(foreign), own_bron))

    if status == "nieuw":
        if same and not removal_covers(pin, same, removals, acfg):
            if rel.get("compare_class"):
                # ground classes: same BGT type underneath is normal (a parkeervlak is cut out of a
                # voetpad). Only "already exists" when it already has the class/function of the remark.
                target = parse_target(pin["opmerking"], acfg.get("new_target_patterns", []))
                if target is None:
                    add("A01", rel.get("same_severity", "info"),
                        "Same type underneath and remark names no class (%s)" % describe(same[0]))
                else:
                    obj, fields = first_match(target, same)
                    if obj is not None:
                        add("A01", "warning", "Not new: BGT already has '%s' underneath (%s; matched on %s)" %
                            (": ".join(x for x in target if x), describe(obj), "+".join(fields)))
            else:
                add("A01", rel.get("same_severity", "warning"),
                    "Not new: related object already exists underneath (%s)" % describe(same[0]))
    elif status == "verwijderen":
        if not same:
            add("A04", "warning", "Nothing of type %s underneath (found: %s)" %
                ("/".join(sorted(related_types)), text))
        elif rel.get("compare_class"):
            # ground classes: the object being removed must have the class/function the remark names
            target = parse_target(pin["opmerking"], acfg.get("remove_target_patterns", []))
            if target is not None and not any(target_equals_current(target, o) for o in same):
                add("A04", "warning", "Remark says remove '%s' but the BGT object underneath is %s" %
                    (": ".join(x for x in target if x), describe(same[0])))
    elif status == "gewijzigd":
        # Objecttype holds the NEW class, so a different class underneath is the normal case for a
        # class change. Only flag when the BGT object already has what the remark changes it to.
        target = parse_target(pin["opmerking"], acfg["target_patterns"])
        obj, fields = first_match(target, same) if target is not None else (None, [])
        if obj is not None:
            add("A05", "warning", "Remark says change to '%s' but the BGT object already is that (%s; matched on %s)" %
                (": ".join(x for x in target if x), describe(obj), "+".join(fields)))
    return text, flags


# ---------------------------------------------------------------- line pins
def check_line(pin, metrics, acfg):
    """Return (flags, measured_class_label, expected_categorie)."""
    flags = []
    if (pin["objecttype"] or "").upper() in acfg["shift"]["skip_types"]:
        return [("B00", "info", "Objecttype %s has no BGT geometry to compare with" % pin["objecttype"])], None, None
    if metrics is None:
        return [("B03", "warning", "No BGT object of type %s within %.0f m" %
                 (pin["objecttype"], acfg["shift"]["search_m"]))], None, None
    mx = metrics["max"]
    label, expected = shift_class(mx, acfg)
    if expected is None:
        flags.append(("B02", "warning", "Max shift %.0f cm: probably map improvement, not a signalering" % (mx * 100)))
    else:
        op = _as_int(pin["categorie"])
        if op is not None and op != expected:
            diff = abs(op - expected)
            flags.append(("B01", "warning" if diff >= 2 else "info",
                          "Categorie %s given, measured %.0f cm = Categorie %s" % (op, mx * 100, expected)))
    if mx > acfg["shift"]["large_m"]:
        flags.append(("B04", "warning", "Shift %.1f m is very large: check that the right BGT object was matched" % mx))
    return flags, label, expected


# ---------------------------------------------------------------- driver
def build_removals(points, unders, acfg):
    """Index of the Verwijderen pins: BGT ids lying under them and their locations, per Objecttype."""
    ids, pts = defaultdict(set), defaultdict(list)
    for pin, under in zip(points, unders):
        if norm(pin["status"]) != "verwijderen":
            continue
        t = (pin["objecttype"] or "").upper()
        rel_types = set(acfg["related"].get(t, {}).get("types", []))
        pts[t].append((pin["x"], pin["y"]))
        for o in under:
            if (o.get("type") or "").upper() in rel_types and o.get("bgtid"):
                ids[t].add(o["bgtid"])
    return {"ids": ids, "pts": pts}


def analyse(points, lines, under_fn, shift_fn, acfg, progress=None):
    """points/lines: lists of pin dicts. Returns list of result rows."""
    rows = []
    n = len(points) + len(lines)
    unders = []
    for i, pin in enumerate(points):                 # pass 1: what lies under every point pin
        unders.append(under_fn(pin))
        if progress and (i + 1) % 50 == 0:
            progress(i + 1, n)
    removals = build_removals(points, unders, acfg)
    done = len(points)
    for pin, under in zip(points, unders):           # pass 2: the checks
        text, flags = check_point(pin, under, acfg, removals)
        rows.append(_row(pin, "point", text, None, None, None, flags))
    for pin in lines:
        m = shift_fn(pin)
        flags, label, expected = check_line(pin, m, acfg)
        rows.append(_row(pin, "line", "", m, label, expected, flags))
        done += 1
        if progress and done % 50 == 0:
            progress(done, n)
    return rows


def _row(pin, kind, under_text, metrics, label, expected, flags):
    return OrderedDict([
        ("layer", kind), ("fid", pin["fid"]), ("area", pin.get("area") or ""),
        ("operator", pin["operator"] or "(unknown)"), ("objecttype", pin["objecttype"]),
        ("status", pin["status"]), ("categorie_operator", pin["categorie"]),
        ("opmerking", pin["opmerking"]), ("underneath", under_text),
        ("max_shift_m", None if not metrics else round(metrics["max"], 3)),
        ("mean_shift_m", None if not metrics else round(metrics["mean"], 3)),
        ("shift_class", label or ""), ("categorie_measured", expected if expected else ""),
        ("flags", flags), ("x", pin["x"]), ("y", pin["y"]),
    ])


def flagged(row, acfg):
    thr = SEVERITY_ORDER[acfg.get("count_severity", "warning")]
    return any(SEVERITY_ORDER[s] >= thr for _, s, _ in row["flags"])


# ---------------------------------------------------------------- summaries
def summarize(rows, acfg):
    out = {"operators": OrderedDict(), "rules": Counter(), "areas": OrderedDict(), "total": len(rows)}
    for r in rows:
        op = out["operators"].setdefault(r["operator"], {
            "pins": 0, "flagged": 0, "status": Counter(), "rules": Counter(), "shift": Counter()})
        op["pins"] += 1
        op["status"][r["status"]] += 1
        if flagged(r, acfg):
            op["flagged"] += 1
        for code, sev, _ in r["flags"]:
            op["rules"][code] += 1
            out["rules"][code] += 1
        if r["shift_class"]:
            op["shift"][r["shift_class"]] += 1
        if r["area"]:
            a = out["areas"].setdefault(r["area"], {"pins": 0, "flagged": 0, "flags": 0})
            a["pins"] += 1
            a["flagged"] += 1 if flagged(r, acfg) else 0
            thr = SEVERITY_ORDER[acfg.get("count_severity", "warning")]
            a["flags"] += sum(1 for _, s, _ in r["flags"] if SEVERITY_ORDER[s] >= thr)
    for a in out["areas"].values():
        a["per100"] = round(100.0 * a["flags"] / a["pins"], 1) if a["pins"] else 0.0
    return out


# ---------------------------------------------------------------- export
CSV_COLS = ["layer", "fid", "area", "operator", "objecttype", "status", "categorie_operator", "opmerking",
            "underneath", "max_shift_m", "mean_shift_m", "shift_class", "categorie_measured",
            "flag_codes", "flag_reason", "x", "y"]


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(CSV_COLS)
        for r in rows:
            if not r["flags"]:
                continue
            d = dict(r)
            d["flag_codes"] = ",".join(c for c, _, _ in r["flags"])
            d["flag_reason"] = " | ".join(m for _, _, m in r["flags"])
            w.writerow(["" if d[c] is None else d[c] for c in CSV_COLS])


def write_html(rows, summary, acfg, rule_names, path, title="Signalering QC - initial analysis"):
    e = html.escape
    thr = SEVERITY_ORDER[acfg.get("count_severity", "warning")]
    h = ["<!doctype html><html><head><meta charset='utf-8'><title>%s</title><style>"
         "body{font:14px/1.4 Segoe UI,Arial,sans-serif;margin:24px;color:#222}"
         "table{border-collapse:collapse;margin:8px 0 24px}th,td{border:1px solid #ccc;padding:4px 8px;"
         "text-align:left;vertical-align:top}th{background:#f0f0f0}td.n{text-align:right}"
         ".warning{background:#fff3cd}.error{background:#f8d7da}.info{color:#666}"
         "</style></head><body><h1>%s</h1>" % (e(title), e(title))]
    h.append("<p>%d pins analysed, %d flagged (warning or higher).</p>" %
             (summary["total"], sum(1 for r in rows if flagged(r, acfg))))
    h.append("<h2>Rules triggered</h2><table><tr><th>Code</th><th>Rule</th><th>Pins</th></tr>")
    for code, n in sorted(summary["rules"].items()):
        h.append("<tr><td>%s</td><td>%s</td><td class='n'>%d</td></tr>" % (e(code), e(rule_names.get(code, "")), n))
    h.append("</table><h2>Per operator</h2><table><tr><th>Operator</th><th>Pins</th><th>Flagged</th><th>%</th>"
             "<th>Status</th><th>Flags per rule</th><th>Shift classes (line pins)</th></tr>")
    for op, d in summary["operators"].items():
        pct = 100.0 * d["flagged"] / d["pins"] if d["pins"] else 0
        h.append("<tr><td>%s</td><td class='n'>%d</td><td class='n'>%d</td><td class='n'>%.1f</td><td>%s</td>"
                 "<td>%s</td><td>%s</td></tr>" % (
                     e(op), d["pins"], d["flagged"], pct,
                     e(", ".join("%s %d" % kv for kv in sorted(d["status"].items()))),
                     e(", ".join("%s %d" % kv for kv in sorted(d["rules"].items()))),
                     e(", ".join("%s: %d" % kv for kv in sorted(d["shift"].items())))))
    h.append("</table>")
    if summary["areas"]:
        h.append("<h2>Risk per sampled area</h2><table><tr><th>Area</th><th>Pins</th><th>Flagged pins</th>"
                 "<th>Flags per 100 pins</th></tr>")
        for a, d in sorted(summary["areas"].items(), key=lambda kv: -kv[1]["per100"]):
            h.append("<tr><td>%s</td><td class='n'>%d</td><td class='n'>%d</td><td class='n'>%.1f</td></tr>" %
                     (e(a), d["pins"], d["flagged"], d["per100"]))
        h.append("</table>")
    h.append("<h2>Detail (flagged pins)</h2><table><tr><th>Layer</th><th>Id</th><th>Operator</th><th>Type</th>"
             "<th>Status</th><th>Underneath</th><th>Max / mean shift (cm)</th><th>Categorie op / measured</th>"
             "<th>Flags</th></tr>")
    for r in rows:
        if not r["flags"]:
            continue
        worst = max(SEVERITY_ORDER[s] for _, s, _ in r["flags"])
        cls = {2: "error", 1: "warning", 0: "info"}[worst]
        shift = "" if r["max_shift_m"] is None else "%.0f / %.0f" % (r["max_shift_m"] * 100, r["mean_shift_m"] * 100)
        h.append("<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                 "<td>%s</td><td>%s</td></tr>" % (
                     cls, e(r["layer"]), e(str(r["fid"])), e(r["operator"]), e(str(r["objecttype"])),
                     e(str(r["status"])), e(r["underneath"]), shift,
                     e("%s / %s" % (r["categorie_operator"] or "-", r["categorie_measured"] or "-")),
                     e(" | ".join("%s: %s" % (c, m) for c, _, m in r["flags"]))))
    h.append("</table></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(h))


RULE_NAMES = {
    "A00": "Objecttype not in mapping",
    "A01": "Nieuw: related object already exists underneath (and is not removed by a Verwijderen pin)",
    "A02": "No BGT object at this location",
    "A03": "Underlying bronhouder differs",
    "A04": "Verwijderen: object of that type not found underneath",
    "A05": "Gewijzigd: BGT object already has the target class",
    "B00": "Line pin without BGT geometry (e.g. BAG)",
    "B01": "Categorie differs from measured shift",
    "B02": "Shift below noise threshold (kaartverbetering?)",
    "B03": "No matching BGT object found",
    "B04": "Shift unusually large",
}
