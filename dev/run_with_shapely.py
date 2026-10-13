# -*- coding: utf-8 -*-
"""Developer harness: runs analysis_core on shapefiles with shapely instead of QGIS.
usage: python dev/run_with_shapely.py <pinpoint.shp> <pinline.shp> <BGT_dir> <out_dir> [config.json]"""
import importlib.util, json, os, sys
import geopandas as gpd, numpy as np, pandas as pd, pyogrio, shapely
from shapely import STRtree
from shapely.geometry import Point

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("analysis_core", os.path.join(here, "..", "analysis_core.py"))
core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)

pt_path, ln_path, bgt_dir, out = sys.argv[1:5]
cfg = json.load(open(sys.argv[5] if len(sys.argv) > 5 else os.path.join(here, "..", "default_config.json"), encoding="utf-8"))
A = cfg["analysis"]
os.makedirs(out, exist_ok=True)

pp, pl = gpd.read_file(pt_path), gpd.read_file(ln_path)
bb = tuple(pd.concat([pp, pl]).total_bounds)
pad = 5
bb = (bb[0]-pad, bb[1]-pad, bb[2]+pad, bb[3]+pad)

def load_ref(key, fn):
    f = A["reference_fields"][key]
    cols = [v for v in f.values()]
    df = pyogrio.read_dataframe(os.path.join(bgt_dir, fn), bbox=bb, columns=[c for c in cols if c])
    ok = ~df[f["plus"]].isin(A["ignore_plusstatus"]) if f.get("plus") in df else np.ones(len(df), bool)
    df = df[ok].reset_index(drop=True)
    df.attrs["f"] = f
    return df

refs = {"hitbgt": load_ref("hitbgt", "hitbgt.shp"), "oth_polygon": load_ref("oth_polygon", "v_hitbgtoth_polygon.shp"),
        "oth_point": load_ref("oth_point", "v_hitbgtoth_point.shp")}
trees = {k: STRtree(d.geometry.values) for k, d in refs.items()}
bounds = {}
def objdict(key, row):
    f = refs[key].attrs["f"]
    g = lambda n: (None if not f.get(n) or pd.isna(row.get(f[n])) else row[f[n]])
    return {"layer": key, "type": g("type"), "tmain": g("tmain"), "tsub": g("tsub"), "fmain": g("fmain"),
            "fsub": g("fsub"), "otype": g("otype"), "bron": g("bron"), "bgtid": g("bgtid")}

def under_fn(pin):
    p = Point(pin["x"], pin["y"]); res = []
    for key, df in refs.items():
        tol = A["point_tolerance_m"] if key == "oth_point" else 0
        for i in trees[key].query(p.buffer(tol) if tol else p, predicate="intersects" if not tol else "intersects"):
            geom = df.geometry.values[i]
            if (tol and geom.distance(p) <= tol) or (not tol and geom.contains(p)):
                res.append(objdict(key, df.iloc[i]))
    return res

S = A["shift"]
bnd = {}
for key in ("hitbgt", "oth_polygon"):
    d = refs[key]; bnd[key] = (STRtree(shapely.boundary(d.geometry.values)), shapely.boundary(d.geometry.values))
def shift_fn(pin):
    L = pin["geom"]; t = pin["objecttype"]
    n = max(int(L.length / S["step_m"]), 2)
    pts = shapely.line_interpolate_point(L, np.linspace(0, L.length, n))
    best = None
    for key in ("hitbgt", "oth_polygon"):
        d = refs[key]; tree, bl = bnd[key]; tcol = d.attrs["f"]["type"]
        for i in tree.query(L.buffer(S["search_m"])):
            if d[tcol].values[i] != t: continue
            dist = shapely.distance(pts, bl[i])
            m = {"max": float(dist.max()), "mean": float(dist.mean()), "length": L.length,
                 "bgtid": d[d.attrs["f"]["bgtid"]].values[i], "bron": d[d.attrs["f"]["bron"]].values[i]}
            if best is None or m["mean"] < best["mean"]: best = m
    return best

def pins(df, geom_line=False):
    out = []
    for i, r in df.iterrows():
        g = r.geometry
        c = g.interpolate(0.5, normalized=True) if geom_line else g
        out.append({"fid": i, "operator": r["Operator"], "objecttype": r["Objecttype"], "status": r["Status"],
                    "bronhouder": r["Bronhouder"], "opmerking": r["Opmerking"], "categorie": r["Categorie"],
                    "mutatietype": r["MutatiType"], "x": c.x, "y": c.y, "geom": g})
    return out

rows = core.analyse(pins(pp), pins(pl, True), under_fn, shift_fn, A)
summ = core.summarize(rows, A)
core.write_csv(rows, os.path.join(out, "analysis_flags.csv"))
core.write_html(rows, summ, A, core.RULE_NAMES, os.path.join(out, "analysis_report.html"))
import pickle; pickle.dump((rows, summ), open(os.path.join(out, "rows.pkl"), "wb"))
print("rows", len(rows), "flagged", sum(core.flagged(r, A) for r in rows))
print(dict(summ["rules"]))
