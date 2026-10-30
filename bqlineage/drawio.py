"""Export lineage as a draw.io (diagrams.net) file: a "Tables" page and/or a "Columns" page.

Boxes flow left to right, from sources to targets, through every intermediate step: CTEs, subqueries, temp tables
and views, each in its own style (see the legend on each page). On the Columns page each box lists only the columns
in the lineage, arrows run source column -> column, and hovering a column shows how it was built (expressions,
JOIN / WHERE / GROUP BY). Unless resolve=False (--raw), CTEs and subqueries that only copy a column are left out.
A solid arrow comes from a driving table, the one that decides the rows (FROM, a RIGHT JOIN's right side, both
sides of a FULL JOIN); a dashed one from a table joined in for more columns, or one that only filters.
Arrows run behind the boxes and never through one: a long arrow passes each column of boxes in a lane kept free
for it, and every bend sits in the gap between two columns, on a track of its own.
Pages are stored compressed, the way draw.io itself saves them, so every version of the desktop app can open it.
"""
from __future__ import annotations

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from itertools import accumulate, pairwise
from statistics import median_low
from types import SimpleNamespace

from .trace import OUT

HEAD, ROW, CHAR = 30, 24, 7  # px; CHAR ~ width of one label character
# px: least gap between columns of boxes, between boxes, from a lane to its neighbours, between bends in a gap,
# between arrows into a box (Tables page)
GAP_X, GAP_Y, LANE, TRACK, ENTRY = 100, 40, 16, 12, 14
KINDS = {  # box kind -> (style, legend label), in legend order
    "source": ("fillColor=#dae8fc;strokeColor=#6c8ebf;", "source table"),
    "view": ("fillColor=#e1d5e7;strokeColor=#9673a6;dashed=1;", "view"),
    "temp": ("fillColor=#f5f5f5;strokeColor=#666666;dashed=1;", "temp table"),
    "cte": ("fillColor=#fff2cc;strokeColor=#d6b656;", "CTE"),
    "subquery": ("fillColor=#ffe6cc;strokeColor=#d79b00;dashed=1;dashPattern=1 3;", "subquery"),
    "target": ("fillColor=#d5e8d4;strokeColor=#82b366;", "target"),
}
LINES = {  # drives the rows? -> (style, legend label)
    True: ("strokeWidth=2;strokeColor=#333333;", "driving table (FROM)"),
    False: ("dashed=1;strokeColor=#999999;", "joined in / filter"),
}
TABLE = "rounded=1;whiteSpace=wrap;fontStyle=1;"
LEGEND = "rounded=1;whiteSpace=wrap;fontSize=11;"
# horizontalStack=0: draw.io re-stacks the rows whenever a box moves, and stacks sideways by default
CONTAINER = ("swimlane;startSize=30;fontStyle=1;childLayout=stackLayout;horizontal=1;horizontalStack=0;"
             "resizeParent=1;resizeParentMax=0;resizeLast=0;marginBottom=0;collapsible=1;")
FIELD = "text;align=left;verticalAlign=middle;spacingLeft=8;points=[[0,0.5],[1,0.5]];portConstraint=eastwest;"
EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=1;endArrow=block;endFill=1;jumpStyle=arc;jumpSize=6;"
_NOT_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # control characters XML forbids (pasted SQL can carry them)


def to_drawio(result, level: str = "both", resolve: bool = True, table: str | None = None,
              columns: list[str] | None = None) -> str:
    """draw.io XML for a LineageResult. level: "table", "column" or "both" (two pages)."""
    mxfile = ET.Element("mxfile", host="bqlineage", type="device")
    if level in ("table", "both"):
        _page(mxfile, "Tables", _tables_page(result, resolve, table))
    if level in ("column", "both"):
        _page(mxfile, "Columns", _columns_page(result, resolve, table, columns))
    return ET.tostring(mxfile, encoding="unicode")


def _page(mxfile, name, root):
    model = ET.Element("mxGraphModel", grid="1", gridSize="10", guides="1", tooltips="1", connect="1",
                       arrows="1", fold="1", page="0", pageScale="1", math="0", shadow="0")
    model.append(root)
    # draw.io's own page encoding: raw deflate of the URI-encoded XML, base64 (what "Compressed" saves)
    data = urllib.parse.quote(ET.tostring(model, encoding="unicode"), safe="!~*'()")
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = deflate.compress(data.encode("ascii")) + deflate.flush()
    ET.SubElement(mxfile, "diagram", id=name.lower(), name=name).text = base64.b64encode(packed).decode("ascii")


def read_page(diagram) -> ET.Element:
    """The mxGraphModel inside a <diagram> element of a draw.io file (compressed or not)."""
    if len(diagram):
        return diagram[0]
    xml = zlib.decompress(base64.b64decode(diagram.text), -15).decode("ascii")
    return ET.fromstring(urllib.parse.unquote(xml))


def _graph(result, resolve, table, columns):
    """The column graph behind the selected target columns, through CTEs, subqueries, temp tables and views.
    A node is (box, column); a box is ("table", name), or ("cte" | "subquery", name, i) inside statement i.
    resolve: leave out CTE / subquery columns that only copy a column (no filter, join or expression).
    links: (input, node) -> whether the input's box drives the node's rows."""
    stmts = result.statements + result.views
    inputs: dict[tuple, set[tuple]] = {}
    steps: dict[tuple, list[dict]] = {}
    filters: dict[tuple, set[str]] = {}
    same: dict[tuple, bool] = {}
    drives: dict[tuple, set[tuple]] = {}  # box -> boxes that decide its rows
    for i, s in enumerate(stmts):
        def box(k, s=s, i=i):
            if k == OUT:
                return "table", s.target
            return ("table", k[1]) if k[0] == "table" else (k[0], k[1], i)

        for c, d in s.details.items():
            # a CLONE / COPY has no query to trace: its column comes straight from its sources
            flow = d.get("flow") or {OUT: {"inputs": {("table", *x.rsplit(".", 1)) for x in s.columns.get(c, ())},
                                           "steps": [], "same": False, "filters": set()}}
            for k, f in flow.items():
                n = box(k), c if k == OUT else k[2]
                inputs.setdefault(n, set()).update((box(x), x[2]) for x in f["inputs"])
                inputs[n].discard(n)  # INSERT INTO tmp ... FROM tmp
                steps.setdefault(n, []).extend(st for st in f["steps"] if st not in steps[n])
                filters.setdefault(n, set()).update(f["filters"])
                same[n] = same.get(n, True) and f["same"]
                drives.setdefault(n[0], set()).update(box(b) for b in f.get("driving", ()))
    wanted = {(("table", t), c) for t, c in (k.rsplit(".", 1) for k in result.column_details(resolve, table, columns))}

    def hidden(n):  # temp tables and views always show: they are steps the script wrote
        return resolve and n[0][0] != "table" and same.get(n, False)

    def lead(src, box):  # no FROM known (a CLONE, SELECT without FROM): everything counts as driving
        return not drives.get(box) or src in drives[box]

    memo: dict[tuple, dict[tuple, bool]] = {}

    def ins(n):  # inputs -> driving?, looking through hidden nodes (driving only if every hop is)
        if n not in memo:
            memo[n] = {}  # in progress: a cycle adds nothing
            out = {}
            for x in inputs.get(n, ()):
                for y, d in (ins(x).items() if hidden(x) else [(x, True)]):
                    out[y] = out.get(y, False) or (d and lead(x[0], n[0]))
            memo[n] = out
        return memo[n]

    nodes, todo = set(), list(wanted)
    while todo:
        n = todo.pop()
        if n not in nodes:
            nodes.add(n)
            todo += ins(n)
    return SimpleNamespace(nodes=nodes, links={(x, n): d for n in nodes for x, d in ins(n).items()}, steps=steps,
                           filters=filters, wanted=wanted, owner={i: s.target for i, s in enumerate(stmts)}, lead=lead)


def _kind(result, box) -> str:
    if box[0] != "table":
        return box[0]
    if box[1] in result.temp_tables:
        return "temp"
    if box[1] in result.intermediate:  # an expanded view
        return "view"
    return "target" if box[1] in set(result.targets()) else "source"


def _label(box) -> str:
    return f"(subquery) {box[1]}".strip() if box[0] == "subquery" else box[1]


def _about(g, box) -> str | None:
    """Hover text for a CTE / subquery box: which statement it belongs to."""
    if box[0] == "table":
        return None
    return f"{'CTE' if box[0] == 'cte' else 'subquery'} in the query that writes {g.owner[box[2]]}"


def _levels(edges: dict) -> dict:
    """Layout column per node: one right of its deepest source. A source table sits one left of the first step
    that reads it, so a table joined in late in a long CTE chain doesn't send arrows across the whole page."""
    level: dict = {}

    def depth(n):
        if n not in level:
            level[n] = 0  # guards cycles
            level[n] = 1 + max((depth(s) for s in edges.get(n, ()) if s != n), default=-1)
        return level[n]

    feeds: dict = {}
    for n, srcs in edges.items():
        for s in srcs:
            if s != n:
                feeds.setdefault(s, set()).add(n)
    for n in set(edges) | set(feeds):
        depth(n)
    for n in feeds:
        if not set(edges.get(n, ())) - {n}:
            level[n] = max(0, min(level[f] for f in feeds[n]) - 1)
    return level


def _ranks(level: dict, feeds: dict) -> dict:
    """Vertical order within each layout column: the rightmost by name, then every node next to the nodes it
    feeds (average of their positions), so arrows run mostly straight instead of crossing."""
    rank: dict = {}
    for lvl in sorted(set(level.values()), reverse=True):
        def key(n):
            down = [rank[d] for d in feeds.get(n, ()) if d in rank]
            return (sum(down) / len(down) if down else float("inf"), n)
        for i, n in enumerate(sorted((n for n in level if level[n] == lvl), key=key)):
            rank[n] = i
    return rank


def _layout(level: dict, size: dict, wires: list) -> tuple[dict, list]:
    """Place boxes in columns by level and route the wires between them so none crosses a box.
    size: box -> (width, height). wires: (src, y in src, dst, y in dst, or None to share dst's left side out among
    the wires into it, line style). A wire that skips columns passes each through a lane: a placeholder stacked
    with that column's boxes, shared by the wires from the same port in the same style (so does a bend). Boxes then move up or down to line up with what they
    connect to, keeping their order, and runs of lanes level out where there's room. Every bend sits in the gap
    after a column, on a track of its own.
    Returns box -> (x, y), and per wire (y in dst, bend points), or None for a wire against the flow (a cycle),
    which draw.io routes itself."""
    lvl, h = dict(level), {b: hb for b, (_, hb) in size.items()}
    chains, into = {}, {}  # wire -> items it passes; box -> wires sharing its left side
    for i, (s, so, t, to, style) in enumerate(wires):
        if level[t] > level[s]:
            lanes = [("~", s, so, style, k) for k in range(level[s] + 1, level[t])]
            for lane in lanes:
                lvl[lane], h[lane] = lane[-1], 0
            chains[i] = [s, *lanes, t]
            if to is None:
                into.setdefault(t, []).append(i)
    entry = {i: h[t] / 2 if to is None else to for i, (_, _, t, to, _) in enumerate(wires)}

    def hops(i):  # (item, y in it, next item, y in that, style) along wire i
        offs = [wires[i][1], *[0] * (len(chains[i]) - 2), entry[i]]
        return [(a, ao, b, bo, wires[i][4]) for (a, b), (ao, bo) in zip(pairwise(chains[i]), pairwise(offs))]

    def links():
        return {hop for i in chains for hop in hops(i)}

    feeds: dict = {}
    for a, _, b, _, _ in links():
        feeds.setdefault(a, set()).add(b)
    rank = _ranks(lvl, feeds)
    cols: dict[int, list] = {}  # level -> items, top to bottom
    for n in sorted(lvl, key=lambda n: (lvl[n], rank[n])):
        cols.setdefault(lvl[n], []).append(n)

    def gaps(items):
        return [h[a] + (GAP_Y if "~" not in (a[0], b[0]) else LANE) for a, b in pairwise(items)]

    y = {}
    for items in cols.values():
        y.update(zip(items, accumulate([0, *gaps(items)])))

    def sweep(order, up, pin=False):  # each column in turn lines up with its neighbours on one side; pin: lanes stay
        near: dict = {}
        for a, ao, b, bo, _ in links():
            if up:
                near.setdefault(b, []).append((bo, a, ao))
            else:
                near.setdefault(a, []).append((ao, b, bo))
        for k in order:
            items = cols[k]
            want = [[] if pin and n[0] == "~" else [y[m] + mo - no for no, m, mo in near.get(n, ())] for n in items]
            weight = [1e9 if pin and n[0] == "~" else len(w) or 0.01 for w, n in zip(want, items)]
            # the median, not the mean: most rows line up exactly and one wire bends, rather than all bending
            y.update(zip(items, _pack([median_low(w) if w else y[n] for w, n in zip(want, items)], weight,
                                      gaps(items))))

    def share():  # arrows into a box, spread down its left side in the order they come from
        for t, idx in into.items():
            idx.sort(key=lambda i: y[chains[i][-2]] + hops(i)[-1][1])
            for k, i in enumerate(idx):
                entry[i] = round(h[t] * (k + 1) / (len(idx) + 1))

    def fits(n, v):  # could lane n sit at height v, between its neighbours in the column?
        items = cols[lvl[n]]
        j = items.index(n)
        return ((j == 0 or v >= y[items[j - 1]] + gaps(items[j - 1:j + 1])[0])
                and (j == len(items) - 1 or y[items[j + 1]] >= v + gaps(items[j:j + 2])[0]))

    def straighten():  # each run of lanes at one height where there's room, level with an end if possible
        for i in sorted(chains, key=lambda i: -len(chains[i])):  # longest first: shorter runs share their start
            run, (_, so, _, _, _), (_, _, _, bo, _) = chains[i][1:-1], hops(i)[0], hops(i)[-1]
            ends = (y[chains[i][0]] + so, y[chains[i][-1]] + bo)
            for v in sorted({*ends, *(y[n] for n in run)},
                            key=lambda v: ((v != ends[0]) + (v != ends[1]), sum(abs(v - y[n]) for n in run))):
                if all(fits(n, v) for n in run):
                    y.update((n, v) for n in run)
                    break

    right, left = sorted(cols, reverse=True), sorted(cols)
    for _ in range(4):
        sweep(right, False)
        sweep(left, True)
    share()
    sweep(right, False)
    share()
    straighten()
    sweep(right, False, pin=True)
    share()
    straighten()
    top = min(y.values(), default=0)
    y = {n: round(v - top) + 40 for n, v in y.items()}

    bends: dict = {}  # column -> {(item, y, style): {y, ...}}: the bends in the gap after it, by where they start
    for a, ao, b, bo, style in links():
        if y[a] + ao != y[b] + bo:
            bends.setdefault(lvl[a], {}).setdefault((a, y[a] + ao, style), set()).add(y[b] + bo)
    x_of, track, x = {}, {}, 40
    for k in range(min(cols, default=0), max(cols, default=0) + 1):
        x_of[k] = x
        x += max((size[n][0] for n in cols.get(k, ()) if n in size), default=0)
        order = _order_bends(bends.get(k, {}))
        gap = max(GAP_X, TRACK * (len(order) + 1))
        for j, start in enumerate(order):
            track[k, start] = round(x + gap * (j + 1) / (len(order) + 1))
        x += gap

    routes = []
    for i in range(len(wires)):
        if i not in chains:
            routes.append(None)
            continue
        points = []
        for a, ao, b, bo, style in hops(i):
            if y[a] + ao != y[b] + bo:
                tx = track[lvl[a], (a, y[a] + ao, style)]
                points += [(tx, y[a] + ao), (tx, y[b] + bo)]
        routes.append((entry[i], points))
    return {b: (x_of[lvl[b]], y[b]) for b in size}, routes


def _pack(want: list, weight: list, gap: list) -> list:
    """Positions as close as possible (weighted least squares) to `want`, in order, each at least gap[i] below the
    previous: isotonic regression (pool adjacent violators) after taking the gaps out."""
    off = list(accumulate([0, *gap]))
    blocks: list[list] = []  # [value, weight, count]
    for v, w in zip((a - o for a, o in zip(want, off)), weight):
        blocks.append([v, w, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop()
            v1, w1, c1 = blocks[-1]
            blocks[-1] = [(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2]
    return [z + o for z, o in zip((v for v, _, c in blocks for _ in range(c)), off)]


def _order_bends(starts: dict) -> list:
    """Left-to-right order of the bends in one gap, {(item, y from, style): {y to, ...}}, crossing as few wires as
    possible: going down, the one starting higher further right (going up, lower), then neighbours swapped
    while that crosses fewer."""
    def span(k):
        ys = [k[1], *starts[k]]
        return min(ys), max(ys)

    def cost(a, b):  # crossings with a's bend left of b's: b's way in and a's ways out, across the other's bend
        (lo, hi), (blo, bhi) = span(a), span(b)
        return (lo < b[1] < hi) + sum(blo < t < bhi for t in starts[a])

    order = sorted(starts, key=lambda k: (-k[1] if sum(starts[k]) > k[1] * len(starts[k]) else k[1], str(k)))
    swapped = True
    while swapped:  # each swap removes a crossing, so this ends
        swapped = False
        for j in range(len(order) - 1):
            if cost(order[j], order[j + 1]) > cost(order[j + 1], order[j]):
                order[j], order[j + 1], swapped = order[j + 1], order[j], True
    return order


class _Cells:
    def __init__(self):
        self.root = ET.Element("root")
        ET.SubElement(self.root, "mxCell", id="0")
        ET.SubElement(self.root, "mxCell", id="1", parent="0")
        self.n, self.edges, self.dashed = 1, 0, 0

    def vertex(self, label, style, x, y, w, h, parent="1", tooltip=None) -> str:
        self.n += 1
        cid, label = f"c{self.n}", _NOT_XML.sub("", label)
        if tooltip:  # draw.io shows a UserObject's "tooltip" attribute on hover
            holder = ET.SubElement(self.root, "UserObject", id=cid, label=label, tooltip=_NOT_XML.sub("", tooltip))
            cell = ET.SubElement(holder, "mxCell", style=style, vertex="1", parent=parent)
        else:
            cell = ET.SubElement(self.root, "mxCell", id=cid, value=label, style=style, vertex="1", parent=parent)
        ET.SubElement(cell, "mxGeometry", {"x": str(x), "y": str(y), "width": str(w), "height": str(h), "as": "geometry"})
        return cid

    def edge(self, source, target, driving, route, exit_y=0.5):
        """An arrow, drawn behind every box (cells earlier in the file are drawn first), a dashed one behind the
        solid ones, so where they meet the solid one shows. route: (y in target,
        bend points) from _layout, with exit_y / the y in target as fractions of the boxes' heights; None:
        draw.io's own routing."""
        self.n += 1
        style = EDGE + LINES[driving][0]
        if route:
            style += f"exitX=1;exitY={exit_y:.4g};entryX=0;entryY={route[0]:.4g};"
        cell = ET.Element("mxCell", id=f"c{self.n}", style=style, edge="1", parent="1", source=source, target=target)
        geo = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        if route and route[1]:
            points = ET.SubElement(geo, "Array", {"as": "points"})
            for px, py in route[1]:
                ET.SubElement(points, "mxPoint", x=str(px), y=str(py))
        self.root.insert(2 + (self.edges if driving else self.dashed), cell)
        self.edges, self.dashed = self.edges + 1, self.dashed + (not driving)

    def legend(self, kinds, lines):
        """Above the diagram: one small box per kind of box on the page, then a sample of each kind of arrow."""
        x = 40
        for k, (style, label) in KINDS.items():
            if k in kinds:
                w = len(label) * CHAR + 30
                self.vertex(label, LEGEND + style, x, -30, w, 24)
                x += w + 10
        for d, (style, label) in LINES.items():
            if d in lines:
                self.n += 1
                cell = ET.SubElement(self.root, "mxCell", id=f"c{self.n}", style=EDGE + style, edge="1", parent="1")
                geo = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
                ET.SubElement(geo, "mxPoint", {"x": str(x + 10), "y": "-18", "as": "sourcePoint"})
                ET.SubElement(geo, "mxPoint", {"x": str(x + 50), "y": "-18", "as": "targetPoint"})
                w = len(label) * CHAR + 10
                self.vertex(label, LEGEND + "strokeColor=none;fillColor=none;align=left;", x + 56, -30, w, 24)
                x += 56 + w + 10


def _tables_page(result, resolve, table):
    g = _graph(result, resolve, table, None)
    edges: dict[tuple, dict[tuple, bool]] = {n[0]: {} for n in g.nodes}  # box -> {box it reads: drives it?}

    def add(box, src, driving):
        if src != box:
            into = edges.setdefault(box, {})
            into[src] = into.get(src, False) or driving

    for (x, n), d in g.links.items():
        add(n[0], x[0], d)
    # tables that only filter, e.g. JOIN ... ON / WHERE x IN (SELECT ...): from the step that reads them; else
    # (no column reached that step) from what the statement writes
    known = {src for s in result.statements + result.views for src in s.sources} - result.temp_tables

    def add_table(box, name):
        if name in known and (not resolve or name not in result.intermediate or ("table", name) in edges):
            add(box, ("table", name), g.lead(("table", name), box))

    for n in g.nodes:
        for st in g.steps.get(n, ()):
            for item in st.get("from", ()):
                add_table(n[0], item.split(" AS ")[0])
    shown = set(edges) | {s for srcs in edges.values() for s in srcs}
    for n in sorted(g.nodes):
        for name in sorted(g.filters.get(n, ())):
            if ("table", name) not in shown:
                add_table(n[0], name)
    shown = set(edges) | {s for srcs in edges.values() for s in srcs}
    for t, srcs in result.table_lineage(resolve, table).items():
        edges.setdefault(("table", t), {})
        for s in srcs:
            if ("table", s) not in shown:
                add(("table", t), ("table", s), g.lead(("table", s), ("table", t)))
    level = _levels(edges)
    size = {n: (max(160, len(_label(n)) * CHAR + 40), max(44, ENTRY * (len(edges.get(n, ())) + 1))) for n in level}
    wires = [(s, size[s][1] / 2, t, None, d) for t, srcs in sorted(edges.items()) for s, d in sorted(srcs.items())]
    pos, routes = _layout(level, size, wires)
    cells, ids = _Cells(), {}
    cells.legend({_kind(result, n) for n in size}, {w[4] for w in wires})
    for n, (x, y) in pos.items():
        style = TABLE + KINDS[_kind(result, n)][0]
        ids[n] = cells.vertex(_label(n), style, x, y, size[n][0], size[n][1], tooltip=_about(g, n))
    for (s, _, t, _, d), route in zip(wires, routes):
        cells.edge(ids[s], ids[t], d, route and (route[0] / size[t][1], route[1]))
    return cells.root


def _columns_page(result, resolve, table, columns):
    g = _graph(result, resolve, table, columns)
    details = result.column_details(resolve, table, columns)
    stmts = result.statements + result.views
    order = {(("table", s.target), c): i for s in stmts for i, c in enumerate(s.columns)}  # as written
    written = {b for b, _ in order}
    fields: dict[tuple, dict[str, str]] = {}  # box -> {column: tooltip}
    for n in g.nodes:
        box, c = n
        if n in g.wanted:  # a target column: the whole story
            tip = _tooltip(details[f"{box[1]}.{c}"])
        elif g.steps.get(n):  # an intermediate column: its own step (one per UNION branch)
            tip = _tooltip({"steps": [{"column": c, **st} for st in g.steps[n]], "indirect_sources": []})
        else:
            tip = ""
        fields.setdefault(box, {})[c] = tip
    edges: dict[tuple, set[tuple]] = {b: set() for b in fields}  # box -> boxes it reads, for the layout
    feeds_t: dict[tuple, set[tuple]] = {}
    feeds_c: dict[tuple, set[tuple]] = {}
    for x, n in g.links:
        if x[0] != n[0]:
            edges[n[0]].add(x[0])
            feeds_t.setdefault(x[0], set()).add(n[0])
        feeds_c.setdefault(x, set()).add(n)
    level = _levels(edges)
    rank = _ranks(level, feeds_t)
    rows, row_pos = {}, {}
    for b in sorted(fields, key=lambda b: (-level[b], rank[b])):  # targets first, then what feeds them
        def key(c, b=b):  # a table the script writes keeps its column order; the rest line up with what they feed
            if b in written:
                return order.get((b, c), float("inf")), c
            down = [row_pos[d] for d in feeds_c.get((b, c), ()) if d in row_pos]
            return (sum(down) / len(down) if down else float("inf")), c
        rows[b] = sorted(fields[b], key=key)
        for i, c in enumerate(rows[b]):
            row_pos[(b, c)] = rank[b] * 10000 + i
    size = {b: (max(180, max(len(x) for x in [_label(b), *cols]) * CHAR + 40), HEAD + ROW * len(cols))
            for b, cols in fields.items()}
    at = {(b, c): HEAD + ROW * i + ROW // 2 for b, cols in rows.items() for i, c in enumerate(cols)}  # row middles
    links = sorted(g.links.items())
    pos, routes = _layout(level, size, [(x[0], at[x], n[0], at[n], d) for (x, n), d in links])
    cells, ids = _Cells(), {}
    cells.legend({_kind(result, b) for b in size}, set(g.links.values()))
    for b, (x, y) in pos.items():
        w = size[b][0]
        box = cells.vertex(_label(b), CONTAINER + KINDS[_kind(result, b)][0], x, y, w, size[b][1],
                           tooltip=_about(g, b))
        for i, c in enumerate(rows[b]):
            ids[(b, c)] = cells.vertex(c, FIELD, 0, HEAD + ROW * i, w, ROW, parent=box, tooltip=fields[b][c])
    for ((x, n), d), route in zip(links, routes):
        cells.edge(ids[x], ids[n], d, route and (0.5, route[1]))
    return cells.root


def _tooltip(d: dict) -> str:
    """How a column was built: each step's expression and conditions, then indirect sources."""
    lines = []
    for st in d["steps"]:
        where = f" (in {st['in']})" if "in" in st else ""
        lines.append(f"{st['column']}{where} = {st['expression']}")
        for k in ("joins", "where", "group_by", "having", "qualify"):
            if k in st:
                lines.append(f"    {k.replace('_', ' ').upper()}: {st[k] if isinstance(st[k], str) else '; '.join(st[k])}")
    if d["indirect_sources"]:
        lines.append("rows decided by: " + ", ".join(d["indirect_sources"]))
    if d.get("unresolved"):
        lines.append("UNRESOLVED: " + ", ".join(d["unresolved"]))
    return "\n".join(lines)
