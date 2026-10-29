"""Column level: resolve every column of a query against real schemas and trace where its values come from.

`Tracer` is mixed into the analyzer, which provides the state it reads: `schema`, `variables`, `provider` and
the helpers `fqn`, `_cols`, `_canon`, `_v`, `issue`, `_no_schema`.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope, find_all_in_scope
from sqlglot.schema import MappingSchema

from .compat import D, lineage, star_friendly_resolver, to_node
from .model import ColumnRecord

_UNRESOLVED = re.compile(r"Column '([^']+)' could not be resolved(?: for table: '([^']+)')?")
_POSITION = re.compile(r"\s*Line: (\d+), Col: (\d+)\.?")


class Tracer:
    def _qualify(self, query, mapping):
        """(qualified query or None, first error or None).
        Names qualify() can't resolve that are script variables (DECLARE / FOR) become values - BigQuery's own
        order: a name is a column if any source has it, else a variable. Any other unresolvable name is returned
        as the error, with the query qualified again without validation so the other columns keep their lineage."""
        base = query.copy()
        # quote_identifiers=False keeps the expressions shown in `steps` readable
        opts = dict(schema=mapping, dialect=D, quote_identifiers=False)
        with star_friendly_resolver():
            while True:
                try:
                    return qualify(base.copy(), validate_qualify_columns=True, **opts), None
                except SqlglotError as e:
                    m = _UNRESOLVED.search(str(e))
                    name, table = (m[1].lower(), (m[2] or "").lower()) if m else ("", "")
                    hits = [c for c in base.find_all(exp.Column)
                            if (table and table in self.variables and c.table.lower() == table)
                            or (not table and name in self.variables and not c.table and c.name.lower() == name)]
                    if hits:
                        for c in hits:
                            c.replace(exp.Parameter(this=exp.Var(this=c.sql(D))))
                        continue
                    try:
                        return qualify(base.copy(), validate_qualify_columns=False, **opts), e
                    except SqlglotError:
                        return None, e

    def _query_lineage(self, query, idx) -> list[ColumnRecord]:
        """One record per output column of `query`, in order."""
        if isinstance(query, exp.Subquery):  # CREATE ... AS ( WITH ... SELECT ... ): sqlglot loses the CTEs
            query = query.unnest()  # behind a parenthesized root, so lineage stopped at every CTE column
        mapping = MappingSchema(self.schema, dialect=D)
        q, err = self._qualify(query, mapping)
        if err is not None:
            pos = _POSITION.search(str(err))
            severity, msg = self._explain(_POSITION.sub("", str(err)), q, query, pos)
            self.issue(severity, msg, idx, *((int(pos[1]), int(pos[2])) if pos else ()))
        if q is None:
            return []
        try:
            annotate_types(q, schema=mapping, dialect=D)
        except Exception:  # types only feed temp-table schemas; lineage doesn't need them
            pass
        # qualify lower-cases names; report them as written (aliases) or as in the schema (columns)
        case = {c.lower(): c for t in query.find_all(exp.Table) for c in self._cols(t)}
        case.update({s.alias_or_name.lower(): s.alias_or_name for s in query.selects if s.alias_or_name})
        root = build_scope(q)
        scopes = {id(s.expression): s for s in root.traverse()}
        top = {id(root.expression)}  # the statement's own SELECT, and each branch of a top-level UNION
        stack = [root]
        while stack:
            s = stack.pop()
            top.add(id(s.expression))
            stack += s.set_operation_scopes if isinstance(s.expression, exp.SetOperation) else []
        # all columns in one pass share sqlglot's cache; trim_selects=False skips a deep copy of the
        # whole SELECT per column per scope (quadratic on wide tables), and keeps each node's own SELECT
        kw = dict(schema=None, dialect=D, scope=root, copy=False, trim_selects=False)
        try:
            nodes = lineage(None, q, **kw)
        except Exception:  # one bad column fails the batch; redo column by column below
            nodes = {}
        # conds: per SELECT, shared by every column passing through it; nodes: sqlglot's to_node cache
        ctx = SimpleNamespace(scopes=scopes, mapping=mapping, conds={}, nodes={})
        stuck = set()
        out = []
        for sel in q.selects:
            name = sel.alias_or_name
            rec = ColumnRecord(case.get(name.lower(), name), set())
            try:
                node = nodes.get(name) or lineage(name, q, **kw)
            except Exception as e:  # sqlglot internals; keep the column's position, carry on
                self.issue("warning", f"Column lineage failed for `{name}`: {e}", idx)
            else:
                keys = {}  # id(lineage node) -> its node in rec.flow
                for n in node.walk():
                    if isinstance(n.expression, exp.Table):
                        rec.sources.add(self._col_key(n.expression, n.name))
                        keys[id(n)] = ("table", *self._col_key(n.expression, n.name).rsplit(".", 1))
                    elif isinstance(n.expression, exp.Placeholder):
                        rec.unresolved.add(n.name)
                    elif isinstance(n.source, exp.Select) and id(n.source) in scopes:
                        s = scopes[id(n.source)]
                        shape, why = self._conditions(s, ctx)
                        expr = n.expression.this if isinstance(n.expression, exp.Alias) else n.expression
                        label = n.name if not n.name.isdigit() else n.expression.alias_or_name  # UNION branch: index
                        col = label.split(".")[-1]
                        if id(n.source) in top:
                            label, key = None, OUT  # the statement's target column
                        elif s.scope_type == ScopeType.SUBQUERY:
                            label = f"(subquery).{label}"
                            key = ("subquery", s.expression.parent.alias_or_name, col)
                        elif isinstance(s.expression.parent, exp.CTE):  # "c.x" -> "eu_customers.x"
                            label = f"{s.expression.parent.alias}.{col}"
                            key = ("cte", s.expression.parent.alias, col)
                        else:  # a UNION branch: a column of the CTE / subquery around the UNION
                            p, first = s.expression.parent, s.expression.parent
                            while isinstance(p, exp.SetOperation):
                                p = p.parent
                            while isinstance(first, exp.SetOperation):
                                first = first.this
                            if isinstance(first, exp.Select) and n.name.isdigit() and int(n.name) < len(first.selects):
                                col = first.selects[int(n.name)].alias_or_name  # as the first branch names it
                            key = ("cte", p.alias, col) if isinstance(p, exp.CTE) else ("subquery", p.alias_or_name if p else "", col)
                            label = f"{p.alias}.{col}" if isinstance(p, exp.CTE) else f"(subquery).{col}"
                        step = {"label": label, "expression": expr.sql(D), **shape}
                        if isinstance(expr, exp.Column) and not shape.keys() - {"from"}:
                            step["same"] = True  # one-to-one copy, nothing filtered or joined: compactable
                        if step not in rec.steps:
                            rec.steps.append(step)
                        rec.indirect |= why
                        keys[id(n)] = key
                        f = rec.flow.setdefault(key, {"inputs": set(), "steps": [], "same": True, "filters": set()})
                        plain = {k: v for k, v in step.items() if k not in ("label", "same")}
                        if plain not in f["steps"]:
                            f["steps"].append(plain)
                        # a diagram can skip a copy, but not a UNION: that's where inputs come together
                        f["same"] = f["same"] and "same" in step and s.scope_type != ScopeType.SET_OPERATION
                        f["filters"] |= {c.rsplit(".", 1)[0] for c in why}  # tables its conditions read
                _link(node, None, keys, rec.flow, set())
            if name == "*":
                self.issue("warning", f"SELECT * not expanded: table schema unknown{self._no_schema()}", idx)
            if sel.type and not sel.type.is_type(exp.DataType.Type.UNKNOWN):
                rec.type = sel.type.sql(D)
            out.append(rec)
            stuck |= rec.unresolved
        if stuck and err is None:  # with an error, that message already says why these are stuck
            self.issue("warning", f"Lineage stops at {', '.join(sorted(stuck))}: source not found", idx)
        return out

    def _col_key(self, t: exp.Table, name: str) -> str:
        """"table.column" for a column of table node `t` ("o.amount" -> "proj.ds.orders.amount")."""
        return f"{self._v(self.fqn(t))}.{self._canon(t, name.split('.')[-1].strip(chr(34)))}"

    def _conditions(self, scope: Scope, ctx) -> tuple[dict, set[str]]:
        """What shapes the rows of one SELECT - its FROM, JOINs, WHERE, GROUP BY, HAVING, QUALIFY as SQL -
        and the base columns those conditions read (including columns of subqueries inside them)."""
        if id(scope.expression) in ctx.conds:
            return ctx.conds[id(scope.expression)]
        ctx.conds[id(scope.expression)] = ({}, set())  # in progress: a recursive CTE refers to itself
        sel = scope.expression
        shape = {"from": [self._source_label(a, node, src) for a, (node, src) in scope.selected_sources.items()]}
        parts = []
        joins = []
        for j in sel.args.get("joins") or []:
            if j.args.get("on"):
                kind = " ".join(x for x in (j.side, j.kind) if x)
                joins.append(f"{kind + ' ' if kind else ''}JOIN {j.this.alias_or_name} ON {j.args['on'].sql(D)}")
                parts.append(j.args["on"])
        if joins:
            shape["joins"] = joins
        for key in ("where", "group", "having", "qualify"):
            clause = sel.args.get(key)
            if clause:
                parts.append(clause)
                if key == "group":
                    shape["group_by"] = [g.sql(D) for g in clause.expressions] or [clause.sql(D)]
                else:
                    shape[key] = clause.this.sql(D)
        if not shape["from"]:
            del shape["from"]
        indirect = set()
        for part in parts:
            for col in find_all_in_scope(part, exp.Column):
                indirect |= self._resolve(col, scope, ctx)
            for sub in part.find_all(exp.Select):  # e.g. WHERE id NOT IN (SELECT ...): all it reads counts
                sub_scope = ctx.scopes.get(id(sub))
                for col in sub_scope.columns if sub_scope else ():
                    indirect |= self._resolve(col, sub_scope, ctx)
        ctx.conds[id(scope.expression)] = (shape, indirect)
        return shape, indirect

    def _source_label(self, alias: str, node, src) -> str:
        """A FROM/JOIN item as a reader would name it: "proj.ds.t AS o", "eu_customers AS c", "(subquery) AS x"."""
        if isinstance(src, exp.Table):
            what = self.fqn(src)
            return what if src.name == alias else f"{what} AS {alias}"
        what = node.name if isinstance(node, exp.Table) else "(subquery)"  # CTE reference, or derived table
        return what if what == alias else f"{what} AS {alias}"

    def _resolve(self, col: exp.Column, scope: Scope, ctx) -> set[str]:
        """Base table columns behind a column reference, looking outward for correlated references."""
        if isinstance(col, exp.Pseudocolumn) and not col.table:  # _TABLE_SUFFIX, _PARTITIONTIME: of the one table
            tables = [src for src in scope.sources.values() if isinstance(src, exp.Table)]
            return {self._col_key(tables[0], col.name)} if len(tables) == 1 else set()
        s = scope
        while s is not None and col.table not in s.sources:
            s = s.parent
        src = s.sources[col.table] if s is not None else None
        if isinstance(src, exp.Table):
            return {self._col_key(src, col.name)}
        if isinstance(src, Scope):  # a CTE / subquery column: its tables, plus the conditions that filtered it
            try:
                node = to_node(col.name, src, D, ctx.mapping, trim_selects=False, _cache=ctx.nodes)
            except Exception:
                return set()
            leaves = {self._col_key(n.expression, n.name) for n in node.walk() if isinstance(n.expression, exp.Table)}
            return leaves | (self._conditions(src, ctx)[1] if isinstance(src.expression, exp.Select) else set())
        return set()

    def _explain(self, msg, q, query, pos) -> tuple[str, str]:
        """(severity, message) for a name qualify() couldn't resolve, judged by the sources visible where it
        appears in `q` (qualified without validation, so stars are expanded). An error only when certain;
        a warning when some source's columns are unknown."""
        unknown_tables = sorted({self.fqn(t) for t in query.find_all(exp.Table) if t.db and not self._cols(t)})
        hint = (f" (schema unknown for {', '.join(unknown_tables)})" if unknown_tables else "") + self._no_schema()
        m = _UNRESOLVED.search(msg)
        scope = None
        if m and q is not None and pos:
            line, col = int(pos[1]), int(pos[2])
            scope = next((s for s in build_scope(q).traverse()
                          if any(c.this.meta.get("line") == line and c.this.meta.get("col") == col for c in s.columns)),
                         None)
        if scope is None:  # e.g. "Cannot automatically join": blame missing schemas when there are any
            return ("warning", msg + hint) if unknown_tables else ("error", msg)

        def describe(s, n):  # ("cte1 AS c1" / "proj.ds.t", its lower-cased columns or None if unknown)
            node, src = s.selected_sources[n]
            if isinstance(src, exp.Table):
                cols = {c.lower() for c in self._cols(src)} or None
            else:
                names = src.expression.named_selects if isinstance(src, Scope) else []
                cols = None if not names or "*" in names else {c.lower() for c in names}
            return self._source_label(n, node, src), cols

        name, alias = m[1], (m[2] or "").lower()
        if alias:
            s = scope
            while s is not None and alias not in s.selected_sources:  # outward: correlated references
                s = s.parent
            if s is None:
                if any(describe(scope, n)[1] is None for n in scope.selected_sources):
                    return "warning", f"`{alias}.{name}` not resolved: `{alias}` is no table alias here and some columns are unknown{hint}"
                return "error", f"`{alias}.{name}`: `{alias}` is not a table alias or column in this query"
            what, cols = describe(s, alias)
            if cols is None:
                return "warning", f"Column `{alias}.{name}` not resolved: columns of {what} unknown{hint}"
            if name.lower() not in cols:
                return "error", f"Column `{name}` not found in {what}"
            return "warning", f"Column `{alias}.{name}` could not be resolved{hint}"
        here = [describe(scope, n) for n in scope.selected_sources]
        hits = [what for what, cols in here if cols and name.lower() in cols]
        if len(hits) > 1:
            return "error", f"Column `{name}` is ambiguous, present in: {', '.join(hits)}. Qualify it with a table alias."
        unknown = [what for what, cols in here if cols is None]
        if unknown:
            return "warning", f"Column `{name}` not resolved: columns unknown for {', '.join(unknown)}{hint}"
        if not hits:
            return "error", f"Column `{name}` not found in {', '.join(what for what, _ in here) or 'this query'}"
        return "warning", f"Column `{name}` could not be resolved{hint}"


OUT = ("out", "", "")  # in ColumnRecord.flow: the record's own column


def _link(n, parent, keys, flow, seen):
    """Fill flow[x]["inputs"] from sqlglot's lineage tree; nodes without a key (placeholders ...) pass through."""
    key = keys.get(id(n))
    if key is None:
        for child in n.downstream:
            _link(child, parent, keys, flow, seen)
        return
    if parent is not None and key != parent:  # a recursive CTE reads itself
        flow[parent]["inputs"].add(key)
    if key[0] != "table" and (id(n), parent) not in seen:
        seen.add((id(n), parent))
        for child in n.downstream:
            _link(child, key, keys, flow, seen)
