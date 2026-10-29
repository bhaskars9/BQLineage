"""Export lineage as a draw.io (diagrams.net) file: a "Tables" page and/or a "Columns" page.

Boxes flow left to right, from sources to targets, through every intermediate step: CTEs, subqueries, temp tables
and views, each in its own style (see the legend on each page). On the Columns page each box lists only the columns
in the lineage, arrows run source column -> column, and hovering a column shows how it was built (expressions,
JOIN / WHERE / GROUP BY). Unless resolve=False (--raw), CTEs and subqueries that only copy a column are left out.
Pages are stored compressed, the way draw.io itself saves them, so every version of the desktop app can open it.
"""
from __future__ import annotations

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from types import SimpleNamespace

from .trace import OUT

HEAD, ROW, GAP_X, GAP_Y, CHAR = 30, 24, 140, 40, 7  # px; CHAR ~ width of one label character
KINDS = {  # box kind -> (style, legend label), in legend order
    "source": ("fillColor=#dae8fc;strokeColor=#6c8ebf;", "source table"),
    "view": ("fillColor=#e1d5e7;strokeColor=#9673a6;dashed=1;", "view"),
    "temp": ("fillColor=#f5f5f5;strokeColor=#666666;dashed=1;", "temp table"),
    "cte": ("fillColor=#fff2cc;strokeColor=#d6b656;", "CTE"),
    "subquery": ("fillColor=#ffe6cc;strokeColor=#d79b00;dashed=1;dashPattern=1 3;", "subquery"),
    "target": ("fillColor=#d5e8d4;strokeColor=#82b366;", "target"),
}
TABLE = "rounded=1;whiteSpace=wrap;fontStyle=1;"
LEGEND = "rounded=1;whiteSpace=wrap;fontSize=11;"
CONTAINER = "swimlane;startSize=30;fontStyle=1;childLayout=stackLayout;horizontal=1;resizeParent=1;collapsible=1;"
FIELD = "text;align=left;verticalAlign=middle;spacingLeft=8;points=[[0,0.5],[1,0.5]];portConstraint=eastwest;"
EDGE_TABLE = "edgeStyle=orthogonalEdgeStyle;rounded=1;endArrow=block;endFill=1;"
EDGE_COLUMN = "edgeStyle=entityRelationEdgeStyle;endArrow=block;endFill=1;"
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
    resolve: leave out CTE / subquery columns that only copy a column (no filter, join or expression)."""
    stmts = result.statements + result.views
    inputs: dict[tuple, set[tuple]] = {}
    steps: dict[tuple, list[dict]] = {}
    filters: dict[tuple, set[str]] = {}
    same: dict[tuple, bool] = {}
    for i, s in enumerate(stmts):
        for c, d in s.details.items():
            def node(k, s=s, c=c, i=i):
                if k == OUT:
                    return ("table", s.target), c
                return (("table", k[1]), k[2]) if k[0] == "table" else ((k[0], k[1], i), k[2])

            # a CLONE / COPY has no query to trace: its column comes straight from its sources
            flow = d.get("flow") or {OUT: {"inputs": {("table", *x.rsplit(".", 1)) for x in s.columns.get(c, ())},
                                           "steps": [], "same": False, "filters": set()}}
            for k, f in flow.items():
                n = node(k)
                inputs.setdefault(n, set()).update(node(x) for x in f["inputs"])
                inputs[n].discard(n)  # INSERT INTO tmp ... FROM tmp
                steps.setdefault(n, []).extend(st for st in f["steps"] if st not in steps[n])
                filters.setdefault(n, set()).update(f["filters"])
                same[n] = same.get(n, True) and f["same"]
    wanted = {(("table", t), c) for t, c in (k.rsplit(".", 1) for k in result.column_details(resolve, table, columns))}

    def hidden(n):  # temp tables and views always show: they are steps the script wrote
        return resolve and n[0][0] != "table" and same.get(n, False)

    memo: dict[tuple, set[tuple]] = {}

    def ins(n):  # inputs, looking through hidden nodes
        if n not in memo:
            memo[n] = set()  # in progress: a cycle adds nothing
            memo[n] = set().union(*(ins(x) if hidden(x) else {x} for x in inputs.get(n, ())))
        return memo[n]

    nodes, todo = set(), list(wanted)
    while todo:
        n = todo.pop()
        if n not in nodes:
            nodes.add(n)
            todo += ins(n)
    return SimpleNamespace(nodes=nodes, links={(x, n) for n in nodes for x in ins(n)}, steps=steps, filters=filters,
                           wanted=wanted, owner={i: s.target for i, s in enumerate(stmts)})


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


def _place(boxes: dict, rank: dict) -> dict:
    """boxes: id -> (level, width, height). Returns id -> (x, y): one column per level, stacked by rank."""
    widths: dict[int, int] = {}
    for lvl, w, _ in boxes.values():
        widths[lvl] = max(widths.get(lvl, 0), w)
    x_of, x = {}, 40
    for lvl in sorted(widths):
        x_of[lvl], x = x, x + widths[lvl] + GAP_X
    pos, y_of = {}, {}
    for name in sorted(boxes, key=lambda n: (boxes[n][0], rank[n], n)):
        lvl, _, h = boxes[name]
        y = y_of.get(lvl, 40)
        pos[name], y_of[lvl] = (x_of[lvl], y), y + h + GAP_Y
    return pos


class _Cells:
    def __init__(self):
        self.root = ET.Element("root")
        ET.SubElement(self.root, "mxCell", id="0")
        ET.SubElement(self.root, "mxCell", id="1", parent="0")
        self.n = 1

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

    def edge(self, source, target, style):
        self.n += 1
        cell = ET.SubElement(self.root, "mxCell", id=f"c{self.n}", style=style, edge="1", parent="1",
                             source=source, target=target)
        ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})

    def legend(self, kinds):
        """One small box per kind on the page, above the diagram."""
        x = 40
        for k, (style, label) in KINDS.items():
            if k in kinds:
                w = len(label) * CHAR + 30
                self.vertex(label, LEGEND + style, x, -30, w, 24)
                x += w + 10


def _tables_page(result, resolve, table):
    g = _graph(result, resolve, table, None)
    edges: dict[tuple, set[tuple]] = {n[0]: set() for n in g.nodes}
    for x, n in g.links:
        if x[0] != n[0]:
            edges[n[0]].add(x[0])
    # tables that only filter, e.g. JOIN ... ON / WHERE x IN (SELECT ...): from the step that reads them; else
    # (no column reached that step) from what the statement writes
    known = {src for s in result.statements + result.views for src in s.sources} - result.temp_tables

    def add(box, name):
        if name in known and (not resolve or name not in result.intermediate or ("table", name) in edges):
            edges[box].add(("table", name))

    for n in g.nodes:
        for st in g.steps.get(n, ()):
            for item in st.get("from", ()):
                add(n[0], item.split(" AS ")[0])
    shown = set(edges) | {s for srcs in edges.values() for s in srcs}
    for n in sorted(g.nodes):
        for name in sorted(g.filters.get(n, ())):
            if ("table", name) not in shown:
                add(n[0], name)
    shown = set(edges) | {s for srcs in edges.values() for s in srcs}
    for t, srcs in result.table_lineage(resolve, table).items():
        edges.setdefault(("table", t), set())
        edges[("table", t)] |= {("table", s) for s in srcs if ("table", s) not in shown}
    for t in list(edges):
        edges[t].discard(t)
    feeds: dict[tuple, set[tuple]] = {}
    for t, srcs in edges.items():
        for s in srcs:
            feeds.setdefault(s, set()).add(t)
    level = _levels(edges)
    boxes = {n: (level[n], max(160, len(_label(n)) * CHAR + 40), 44) for n in level}
    pos, cells, ids = _place(boxes, _ranks(level, feeds)), _Cells(), {}
    cells.legend({_kind(result, n) for n in boxes})
    for n, (x, y) in pos.items():
        style = TABLE + KINDS[_kind(result, n)][0]
        ids[n] = cells.vertex(_label(n), style, x, y, boxes[n][1], boxes[n][2], tooltip=_about(g, n))
    for t, srcs in sorted(edges.items()):
        for s in sorted(srcs):
            cells.edge(ids[s], ids[t], EDGE_TABLE)
    return cells.root


def _columns_page(result, resolve, table, columns):
    g = _graph(result, resolve, table, columns)
    details = result.column_details(resolve, table, columns)
    order = {(("table", s.target), c): i for s in result.statements for i, c in enumerate(s.columns)}  # as written
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
        def key(c, b=b):
            down = [row_pos[d] for d in feeds_c.get((b, c), ()) if d in row_pos]
            return (sum(down) / len(down) if down else float("inf"), order.get((b, c), 0), c)
        rows[b] = sorted(fields[b], key=key)
        for i, c in enumerate(rows[b]):
            row_pos[(b, c)] = rank[b] * 10000 + i
    boxes = {b: (level[b], max(180, max(len(x) for x in [_label(b), *cols]) * CHAR + 40), HEAD + ROW * len(cols))
             for b, cols in fields.items()}
    pos, cells, ids = _place(boxes, rank), _Cells(), {}
    cells.legend({_kind(result, b) for b in boxes})
    for b, (x, y) in pos.items():
        w = boxes[b][1]
        box = cells.vertex(_label(b), CONTAINER + KINDS[_kind(result, b)][0], x, y, w, boxes[b][2],
                           tooltip=_about(g, b))
        for i, c in enumerate(rows[b]):
            ids[(b, c)] = cells.vertex(c, FIELD, 0, HEAD + ROW * i, w, ROW, parent=box, tooltip=fields[b][c])
    for x, n in sorted(g.links):
        cells.edge(ids[x], ids[n], EDGE_COLUMN)
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
