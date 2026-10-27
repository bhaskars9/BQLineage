"""LineageResult: per-statement lineage stitched together through temp tables and views."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import JSON_VERSION, Issue, StatementLineage


@dataclass
class LineageResult:
    statements: list[StatementLineage]
    views: list[StatementLineage]  # definitions of views fetched from the schema provider
    temp_tables: set[str]  # re-created temp tables are versioned: _SESSION.t, _SESSION.t#2, ...
    intermediate: set[str]  # temp tables + expanded views: lineage is traced through these
    issues: list[Issue] = field(default_factory=list)
    schemas: dict[str, dict] = field(default_factory=dict)  # table -> the columns the analysis used for it
    sql: str = ""  # the analyzed SQL, after templates were rendered

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def targets(self, table: str | None = None) -> list[str]:
        """Tables/queries the script writes, optionally only those matching `table` (see table_lineage)."""
        return sorted({s.target for s in self.statements if _is_table(s.target, table)})

    def table_lineage(self, resolve: bool = True, table: str | None = None) -> dict[str, list[str]]:
        """target table -> source tables. resolve=True traces through temp tables and views.
        table: only this target, as "project.dataset.table", "dataset.table" or "table"."""
        graph: dict[str, set[str]] = {}
        for s in self.statements + self.views:
            graph.setdefault(s.target, set()).update(s.sources)
        return self._collect(graph, set(self.targets(table)), resolve, lambda k: k)

    def column_lineage(self, resolve: bool = True, table: str | None = None,
                       columns: list[str] | None = None) -> dict[str, list[str]]:
        """"table.column" -> source "table.column"s its values come from. resolve=True traces through temp tables
        and views. table / columns: only these targets (columns match case-insensitively, with or without table)."""
        return {k: v["sources"] for k, v in self.column_details(resolve, table, columns).items()}

    def column_details(self, resolve: bool = True, table: str | None = None,
                       columns: list[str] | None = None) -> dict[str, dict]:
        """Per target column (same filters as column_lineage):
        sources          - columns its values come from
        indirect_sources - columns the JOIN / WHERE / GROUP BY / HAVING / QUALIFY along the way read
        steps            - how it was built: the expression and conditions at each step, from the target back
                           through CTEs, subqueries, temp tables and views. With resolve=True, one-to-one hops
                           (a CTE / temp table that only copies the column, no filter or join) are left out;
                           the target's own step always stays
        unresolved       - only when lineage got stuck: the references whose source couldn't be found"""
        direct: dict[str, set[str]] = {}
        indirect: dict[str, set[str]] = {}
        unresolved: dict[str, set[str]] = {}
        steps: dict[str, list[dict]] = {}
        for s in self.statements + self.views:
            for c, srcs in s.columns.items():
                k = f"{s.target}.{c}"
                direct.setdefault(k, set()).update(srcs)
                info = s.details.get(c, {})
                indirect.setdefault(k, set()).update(info.get("indirect", ()))
                unresolved.setdefault(k, set()).update(info.get("unresolved", ()))
                own = steps.setdefault(k, [])
                for st in info.get("steps", ()):
                    rest = {x: y for x, y in st.items() if x != "label"}
                    st = {"column": k, **rest} if st["label"] is None else {"column": st["label"], "in": s.target, **rest}
                    if st not in own:
                        own.append(st)

        def table_of(key):
            return key.rsplit(".", 1)[0]

        def traced(key):
            return resolve and table_of(key) in self.intermediate and key in direct

        memo: dict[str, tuple] = {}

        def trace(key):  # (direct, indirect, steps, unresolved), expanded through temp tables / views
            if key in memo:
                return memo[key]
            memo[key] = (set(), set(), [], set())  # in progress: a self-referencing temp table adds nothing
            d_out, i_out, s_out, u_out = set(), set(), list(steps.get(key, [])), set(unresolved.get(key, ()))
            for src in direct.get(key, ()):
                if traced(src):
                    d, i, s, u = trace(src)
                    d_out |= d
                    i_out |= i  # the conditions that built a temp column apply to what reads it
                    s_out += [x for x in s if x not in s_out]
                    u_out |= u
                else:
                    d_out.add(src)
            for src in indirect.get(key, ()):
                if traced(src):
                    d, i, _, _ = trace(src)
                    i_out |= d | i
                else:
                    i_out.add(src)
            memo[key] = (d_out, i_out, s_out, u_out)
            return memo[key]

        wanted = {c.lower() for c in columns} if columns else None
        out = {}
        for s in self.statements:
            if not _is_table(s.target, table) or (resolve and s.target in self.temp_tables):
                continue
            for c in s.columns:
                if wanted is None or c.lower() in wanted:
                    k = f"{s.target}.{c}"
                    d, i, st, u = trace(k)
                    st = [{x: y for x, y in step.items() if x != "same"} for step in st
                          if not (resolve and step.get("same") and step["column"] != k)]
                    out[k] = {"sources": sorted(d), "indirect_sources": sorted(i), "steps": st}
                    if u:
                        out[k]["unresolved"] = sorted(u)
        return dict(sorted(out.items()))

    def _collect(self, graph, targets, resolve, table_of):
        def expand(key, seen):
            if table_of(key) not in self.intermediate or key not in graph:
                return {key}
            if key in seen:  # self-referencing temp table, e.g. INSERT INTO tmp SELECT ... FROM tmp
                return set()
            return set().union(*(expand(s, seen | {key}) for s in graph[key]))

        out = {}
        for t in sorted(targets):
            if not resolve:
                out[t] = sorted(graph[t])
            elif table_of(t) not in self.temp_tables:
                out[t] = sorted(set().union(*(expand(s, {t}) for s in graph[t])))
        return out

    def to_dict(self, resolve: bool = True, table: str | None = None, columns: list[str] | None = None) -> dict:
        return {
            "version": JSON_VERSION,
            "tables": self.table_lineage(resolve, table),
            "columns": self.column_details(resolve, table, columns),
            "issues": [str(i) for i in self.issues],
        }


def _is_table(name: str, table: str | None) -> bool:
    """`table` picks a target by full name or trailing parts: "p.ds.t" matches "ds.t" and "t"."""
    return table is None or name == table or name.endswith("." + table)
