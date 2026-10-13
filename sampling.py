# -*- coding: utf-8 -*-
"""Random-area sampling for signalering QC (no GUI code).

Every feature is represented by one point (see utils.rep_point) and belongs to exactly
one cell, so a feature is never reviewed twice and never falls between two cells.
"""
import math
import random
from collections import Counter, defaultdict, namedtuple

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsSpatialIndex

FeatInfo = namedtuple("FeatInfo", "layer fid x y operator sampled area", defaults=(None,))


class Cell(object):
    def __init__(self, geometry, members):
        self.geometry = geometry
        self.members = members


def build_grid_cells(infos, size):
    if not infos:
        return []
    size = float(size)
    x0 = min(i.x for i in infos)
    y0 = min(i.y for i in infos)
    buckets = defaultdict(list)
    for i in infos:
        buckets[(int((i.x - x0) // size), int((i.y - y0) // size))].append(i)
    cells = []
    for (cx, cy), members in sorted(buckets.items()):
        rect = QgsRectangle(x0 + cx * size, y0 + cy * size,
                            x0 + (cx + 1) * size, y0 + (cy + 1) * size)
        cells.append(Cell(QgsGeometry.fromRect(rect), members))
    return cells


def build_balanced_cells(infos, target):
    """Recursive median split: cells tile the extent and hold ~equal numbers of
    features, so every sampled area costs about the same review effort."""
    if not infos:
        return []
    target = max(2, int(target))
    xs = [i.x for i in infos]
    ys = [i.y for i in infos]
    root = QgsRectangle(min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
    cells = []
    stack = [(list(infos), root)]
    while stack:
        items, rect = stack.pop()
        if len(items) <= target:
            cells.append(Cell(QgsGeometry.fromRect(rect), items))
            continue
        horizontal = rect.width() >= rect.height()
        if horizontal:
            items.sort(key=lambda i: (i.x, i.layer, i.fid))
        else:
            items.sort(key=lambda i: (i.y, i.layer, i.fid))
        mid = len(items) // 2
        a, b = items[mid - 1], items[mid]
        if horizontal:
            c = (a.x + b.x) / 2.0
            r1 = QgsRectangle(rect.xMinimum(), rect.yMinimum(), c, rect.yMaximum())
            r2 = QgsRectangle(c, rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
        else:
            c = (a.y + b.y) / 2.0
            r1 = QgsRectangle(rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), c)
            r2 = QgsRectangle(rect.xMinimum(), c, rect.xMaximum(), rect.yMaximum())
        stack.append((items[:mid], r1))
        stack.append((items[mid:], r2))
    return cells


def build_polygon_cells(infos, polygon_features):
    """Use existing polygons (e.g. buurten / work areas) as cells."""
    index = QgsSpatialIndex()
    geoms = {}
    for f in polygon_features:
        g = f.geometry()
        if g is None or g.isNull():
            continue
        index.addFeature(f)
        geoms[f.id()] = QgsGeometry(g)
    buckets = defaultdict(list)
    for i in infos:
        pg = QgsGeometry.fromPointXY(QgsPointXY(i.x, i.y))
        for pid in index.intersects(QgsRectangle(i.x, i.y, i.x, i.y)):
            if geoms[pid].intersects(pg):
                buckets[pid].append(i)
                break
    return [Cell(QgsGeometry(geoms[pid]), members) for pid, members in sorted(buckets.items())]


def draw(cells, infos, operators, mode, value, seed, min_pins=1, min_op_pins=1,
         exclude_sampled=True):
    """Stratified random draw of cells.

    mode "count": add `value` NEW cells per operator (cells already drawn for another
                  operator in this run count towards it).
    mode "pct"  : top up until `value` % of the operator's features are in a sampled cell.
    Returns {"cells", "per_operator", "warnings"}.
    """
    rng = random.Random(seed)
    total = Counter(i.operator for i in infos)
    already = Counter(i.operator for i in infos if i.sampled)

    pool = [c for c in cells if len(c.members) >= min_pins]
    n_small = len(cells) - len(pool)
    n_used = 0
    if exclude_sampled:
        keep = [c for c in pool if not any(m.sampled for m in c.members)]
        n_used = len(pool) - len(keep)
        pool = keep

    op_cells = defaultdict(dict)
    for ci, c in enumerate(pool):
        for op, n in Counter(m.operator for m in c.members).items():
            op_cells[op][ci] = n

    chosen, chosen_set, warnings = [], set(), []
    order = sorted(operators, key=lambda o: (sum(op_cells[o].values()) if o in op_cells else 0, o))
    for op in order:
        if op not in op_cells:
            warnings.append("%s: no eligible area left." % op)
            continue
        cand = sorted(ci for ci, n in op_cells[op].items() if n >= min_op_pins)
        rng.shuffle(cand)
        have_cells = sum(1 for x in chosen_set if x in op_cells[op])
        have_feats = already[op] + sum(n for ci, n in op_cells[op].items() if ci in chosen_set)
        target_feats = int(math.ceil(total[op] * float(value) / 100.0))
        cells0 = have_cells
        for ci in cand:
            if mode == "count":
                if have_cells >= int(value):
                    break
            elif have_feats >= target_feats:
                break
            if ci in chosen_set:
                continue
            chosen_set.add(ci)
            chosen.append(ci)
            have_cells += 1
            have_feats += op_cells[op][ci]
        if have_cells == cells0:                         # nothing added for this operator: say why
            if mode == "count":
                if int(value) <= 0:
                    warnings.append("%s: the number of areas is 0." % op)
                else:
                    warnings.append("%s: no new area needed - areas drawn in this run already count." % op)
            elif have_feats >= target_feats:
                warnings.append("%s: target of %s%% is reached - %d of %d features (%.1f%%) are already in sampled "
                                "areas (earlier rounds or areas drawn for other operators). "
                                "To draw more, raise the percentage: it is cumulative." %
                                (op, value, have_feats, total[op], 100.0 * have_feats / max(total[op], 1)))
        if mode == "count" and have_cells < int(value):
            warnings.append("%s: only %d of %d areas available." % (op, have_cells, int(value)))
        elif mode != "count" and have_feats < target_feats:
            warnings.append("%s: could only reach %d of %d features." % (op, have_feats, target_feats))

    if not chosen:
        warnings.append("%d candidate areas: %d skipped for having fewer than %d pins, %d skipped because they "
                        "already contain sampled pins (%s)." %
                        (len(cells), n_small, min_pins, n_used,
                         "excluded" if exclude_sampled else "not excluded"))
    sel_cells = [pool[ci] for ci in chosen]
    per_op = {}
    for op in operators:
        cnt = sum(1 for c in sel_cells if any(m.operator == op for m in c.members))
        feats = sum(1 for c in sel_cells for m in c.members if m.operator == op)
        per_op[op] = {"cells": cnt, "new_features": feats, "already": already[op],
                      "total": total[op]}
    return {"cells": sel_cells, "per_operator": per_op, "warnings": warnings}
