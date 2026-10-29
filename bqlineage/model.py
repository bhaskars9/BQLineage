"""The records an analysis produces."""
from __future__ import annotations

from dataclasses import dataclass, field

JSON_VERSION = 1  # bump when the shape of LineageResult.to_dict() changes (see docs/output.md)


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    message: str
    statement: int | None = None  # 1-based statement number in the script
    line: int | None = None
    col: int | None = None

    def __str__(self):
        where = f" [statement {self.statement}]" if self.statement else ""
        where += f" [line {self.line}" + (f", col {self.col}]" if self.col else "]") if self.line else ""
        return f"{self.severity.upper()}{where}: {self.message}"


class LineageError(Exception):
    def __init__(self, issues: list[Issue]):
        super().__init__("\n".join(map(str, issues)))
        self.issues = issues


@dataclass
class ColumnRecord:
    """One output column of one query, as traced (before it is mapped onto a target table's column)."""
    name: str
    sources: set[str]  # "table.column"s its values come from
    type: str = "STRING"
    steps: list[dict] = field(default_factory=list)  # {"label", "expression", "from", "joins", "where", ...}
    indirect: set[str] = field(default_factory=set)  # columns the conditions along the way read
    unresolved: set[str] = field(default_factory=set)  # references whose source couldn't be found
    # the graph behind `sources`, for diagrams. node -> {"inputs": {node, ...}, "steps": [...], "same": bool,
    # "filters": {tables its JOIN / WHERE / ... read}}. A node is ("table", table, column),
    # ("cte" | "subquery", name, column), or ("out", "", "") for this column
    flow: dict[tuple, dict] = field(default_factory=dict)


@dataclass
class StatementLineage:
    index: int | None
    kind: str  # SELECT, CREATE TABLE, CREATE TEMP TABLE, CREATE VIEW, INSERT, MERGE, UPDATE, EXPORT, VIEW
    target: str
    sources: set[str]  # every table read, including ones only used in JOIN/WHERE
    columns: dict[str, set[str]]  # target column -> {"table.column", ...} its values come from
    # target column -> {"steps": [...], "indirect": {"table.column", ...}, "unresolved": {...}, "flow": {...}}
    details: dict[str, dict] = field(default_factory=dict)
