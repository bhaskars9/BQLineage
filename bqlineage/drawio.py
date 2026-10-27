"""Export lineage as a draw.io (diagrams.net) file: a "Tables" page and/or a "Columns" page.

Tables flow left to right (sources first). On the Columns page each table lists only the columns in the lineage,
in the target's column order, arrows run source column -> target column, and hovering a target column shows how
it was built (expressions, JOIN / WHERE / GROUP BY per step). Pages are stored compressed, the way draw.io itself
saves them, so every version of the desktop app can open the file.
"""
from __future__ import annotations

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib

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


def _ranks(level: dict[str, int], feeds: dict[str, set[str]]) -> dict[str, float]:
    """Vertical order within each layout column: the rightmost by name, then every node next to the nodes it
    feeds (average of their positions), so arrows run mostly straight instead of crossing."""
    rank: dict[str, float] = {}
    for lvl in sorted(set(level.values()), reverse=True):
        def key(n):
            down = [rank[d] for d in feeds.get(n, ()) if d in rank]
            return (sum(down) / len(down) if down else float("inf"), n)
        for i, n in enumerate(sorted((n for n in level if level[n] == lvl), key=key)):
            rank[n] = i
    return rank


def _place(boxes: dict[str, tuple[int, int, int]], rank: dict[str, float]) -> dict[str, tuple[int, int]]:
    """boxes: name -> (level, width, height). Returns name -> (x, y): one column per level, stacked by rank."""
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


def _tables_page(result, resolve, table):
    edges = {t: set(srcs) for t, srcs in result.table_lineage(resolve, table).items()}
    feeds: dict[str, set[str]] = {}
    for t, srcs in edges.items():
        for s in srcs:
            feeds.setdefault(s, set()).add(t)
    level = _levels(edges)
    boxes = {n: (level[n], max(160, len(n) * CHAR + 40), 44) for n in level}
    pos, cells, ids = _place(boxes, _ranks(level, feeds)), _Cells(), {}
    for n, (x, y) in pos.items():
        ids[n] = cells.vertex(n, TABLE + COLORS[_kind(result, n)], x, y, boxes[n][1], boxes[n][2])
    for t, srcs in sorted(edges.items()):
        for s in sorted(srcs):
            cells.edge(ids[s], ids[t], EDGE_TABLE)
    return cells.root


def _columns_page(result, resolve, table, columns):
    details = result.column_details(resolve, table, columns)
    order = {f"{s.target}.{c}": i for s in result.statements for i, c in enumerate(s.columns)}  # as written
    fields: dict[str, dict[str, str]] = {}  # table -> {column: tooltip}
    edges: dict[str, set[str]] = {}  # table -> source tables, for the layout
    feeds_t: dict[str, set[str]] = {}
    feeds_c: dict[str, set[str]] = {}
    links = set()
    for key, d in details.items():
        t, c = key.rsplit(".", 1)
        fields.setdefault(t, {})[c] = _tooltip(d)
        edges.setdefault(t, set())
        for src in d["sources"]:
            st, sc = src.rsplit(".", 1)
            fields.setdefault(st, {}).setdefault(sc, "")
            edges[t].add(st)
            feeds_t.setdefault(st, set()).add(t)
            feeds_c.setdefault(src, set()).add(key)
            links.add((src, key))
    level = _levels(edges)
    rank = _ranks(level, feeds_t)
    rows, row_pos = {}, {}
    for t in sorted(fields, key=lambda t: (-level[t], rank[t])):  # targets first, then what feeds them
        def key(c, t=t):
            down = [row_pos[d] for d in feeds_c.get(f"{t}.{c}", ()) if d in row_pos]
            return (sum(down) / len(down) if down else float("inf"), order.get(f"{t}.{c}", 0), c)
        rows[t] = sorted(fields[t], key=key)
        for i, c in enumerate(rows[t]):
            row_pos[f"{t}.{c}"] = rank[t] * 10000 + i
    boxes = {t: (level[t], max(180, max(len(x) for x in [t, *cols]) * CHAR + 40), HEAD + ROW * len(cols))
             for t, cols in fields.items()}
    pos, cells, ids = _place(boxes, rank), _Cells(), {}
    for t, (x, y) in pos.items():
        w = boxes[t][1]
        box = cells.vertex(t, CONTAINER + COLORS[_kind(result, t)], x, y, w, boxes[t][2])
        for i, c in enumerate(rows[t]):
            ids[f"{t}.{c}"] = cells.vertex(c, FIELD, 0, HEAD + ROW * i, w, ROW, parent=box, tooltip=fields[t][c])
    for src, key in sorted(links):
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
