"""Export lineage as a draw.io (diagrams.net) file: a "Tables" page and/or a "Columns" page.

Tables flow left to right (sources first). On the Columns page each table lists only the columns in the lineage,
arrows run source column -> target column, and hovering a target column shows how it was built (expressions,
JOIN / WHERE / GROUP BY per step). Re-layout big diagrams in draw.io with Arrange > Layout > Horizontal Flow.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

HEAD, ROW, GAP_X, GAP_Y, CHAR = 30, 24, 140, 40, 7  # px; CHAR ~ width of one label character
COLORS = {
    "source": "fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "target": "fillColor=#d5e8d4;strokeColor=#82b366;",
    "temp": "fillColor=#f5f5f5;strokeColor=#666666;dashed=1;",  # temp tables / expanded views (--raw)
}
TABLE = "rounded=1;whiteSpace=wrap;fontStyle=1;"
CONTAINER = "swimlane;startSize=30;fontStyle=1;childLayout=stackLayout;horizontal=1;resizeParent=1;collapsible=1;"
FIELD = "text;align=left;verticalAlign=middle;spacingLeft=8;points=[[0,0.5],[1,0.5]];portConstraint=eastwest;"
EDGE_TABLE = "edgeStyle=orthogonalEdgeStyle;rounded=1;endArrow=block;endFill=1;"
EDGE_COLUMN = "edgeStyle=entityRelationEdgeStyle;endArrow=block;endFill=1;"


def to_drawio(result, level: str = "both", resolve: bool = True, table: str | None = None,
              columns: list[str] | None = None) -> str:
    """draw.io XML for a LineageResult. level: "table", "column" or "both" (two pages)."""
    mxfile = ET.Element("mxfile", host="bqlineage")
    if level in ("table", "both"):
        _page(mxfile, "Tables", _tables_page(result, resolve, table))
    if level in ("column", "both"):
        _page(mxfile, "Columns", _columns_page(result, resolve, table, columns))
    return ET.tostring(mxfile, encoding="unicode")


def _page(mxfile, name, root):
    diagram = ET.SubElement(mxfile, "diagram", id=name.lower(), name=name)
    model = ET.SubElement(diagram, "mxGraphModel", grid="1", gridSize="10", guides="1", tooltips="1",
                          connect="1", arrows="1", fold="1", page="0")
    model.append(root)


def _kind(result, name):
    if name in result.intermediate:
        return "temp"
    return "target" if name in set(result.targets()) else "source"


def _levels(edges: dict[str, set[str]]) -> dict[str, int]:
    """Layout column per node: 0 for pure sources, else one right of its deepest source."""
    level: dict[str, int] = {}

    def depth(n):
        if n not in level:
            level[n] = 0  # guards self-references (INSERT INTO t ... FROM t)
            level[n] = 1 + max((depth(s) for s in edges.get(n, ()) if s != n), default=-1)
        return level[n]

    for n in set(edges) | {s for srcs in edges.values() for s in srcs}:
        depth(n)
    return level


def _place(boxes: dict[str, tuple[int, int, int]]) -> dict[str, tuple[int, int]]:
    """boxes: name -> (level, width, height). Returns name -> (x, y): one column per level, stacked."""
    widths: dict[int, int] = {}
    for lvl, w, _ in boxes.values():
        widths[lvl] = max(widths.get(lvl, 0), w)
    x_of, x = {}, 40
    for lvl in sorted(widths):
        x_of[lvl], x = x, x + widths[lvl] + GAP_X
    pos, y_of = {}, {}
    for name in sorted(boxes, key=lambda n: (boxes[n][0], n)):
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
        cid = f"c{self.n}"
        if tooltip:  # draw.io shows a UserObject's "tooltip" attribute on hover
            holder = ET.SubElement(self.root, "UserObject", id=cid, label=label, tooltip=tooltip)
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


def _tables_page(result, resolve, table):
    edges = {t: set(srcs) for t, srcs in result.table_lineage(resolve, table).items()}
    level = _levels(edges)
    boxes = {n: (level[n], max(160, len(n) * CHAR + 40), 44) for n in level}
    pos, cells, ids = _place(boxes), _Cells(), {}
    for n, (x, y) in pos.items():
        ids[n] = cells.vertex(n, TABLE + COLORS[_kind(result, n)], x, y, boxes[n][1], boxes[n][2])
    for t, srcs in sorted(edges.items()):
        for s in sorted(srcs):
            cells.edge(ids[s], ids[t], EDGE_TABLE)
    return cells.root


def _columns_page(result, resolve, table, columns):
    details = result.column_details(resolve, table, columns)
    fields: dict[str, dict[str, str]] = {}  # table -> {column: tooltip}
    edges: dict[str, set[str]] = {}  # table-level, for the layout
    links = []
    for key, d in details.items():
        t, c = key.rsplit(".", 1)
        fields.setdefault(t, {})[c] = _tooltip(d)
        edges.setdefault(t, set())
        for src in d["sources"]:
            st, sc = src.rsplit(".", 1)
            fields.setdefault(st, {}).setdefault(sc, "")
            edges[t].add(st)
            links.append((src, key))
    level = _levels(edges)
    boxes = {t: (level[t], max(180, max(len(x) for x in [t, *cols]) * CHAR + 40), HEAD + ROW * len(cols))
             for t, cols in fields.items()}
    pos, cells, ids = _place(boxes), _Cells(), {}
    for t, (x, y) in pos.items():
        w = boxes[t][1]
        box = cells.vertex(t, CONTAINER + COLORS[_kind(result, t)], x, y, w, boxes[t][2])
        for i, (c, tip) in enumerate(sorted(fields[t].items())):
            ids[f"{t}.{c}"] = cells.vertex(c, FIELD, 0, HEAD + ROW * i, w, ROW, parent=box, tooltip=tip)
    for src, key in sorted(set(links)):
        cells.edge(ids[src], ids[key], EDGE_COLUMN)
    return cells.root


def _tooltip(d: dict) -> str:
    """How a target column was built: each step's expression and conditions, then indirect sources."""
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
