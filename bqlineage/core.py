"""Table- and column-level lineage for BigQuery SQL, built on sqlglot (github.com/tobymao/sqlglot)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.lineage import lineage as _sg_lineage
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import build_scope
from sqlglot.schema import MappingSchema

D = "bigquery"
SESSION = "_SESSION"  # BigQuery's dataset name for script temp tables
_DEFAULT = "__default__"  # placeholder project/dataset; never shown in output


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    message: str
    statement: int | None = None  # 1-based statement number in the script
    line: int | None = None
    col: int | None = None

    def __str__(self):
        where = f" [statement {self.statement}]" if self.statement else ""
        where += f" [line {self.line}, col {self.col}]" if self.line else ""
        return f"{self.severity.upper()}{where}: {self.message}"


class LineageError(Exception):
    def __init__(self, issues: list[Issue]):
        super().__init__("\n".join(map(str, issues)))
        self.issues = issues


@dataclass
class StatementLineage:
    index: int | None
    kind: str  # SELECT, CREATE TABLE, CREATE TEMP TABLE, CREATE VIEW, INSERT, MERGE, UPDATE, VIEW
    target: str
    sources: set[str]  # every table read, including ones only used in JOIN/WHERE
    columns: dict[str, set[str]]  # target column -> {"table.column", ...}


@dataclass
class LineageResult:
    statements: list[StatementLineage]
    views: list[StatementLineage]  # definitions of views fetched from the schema provider
    temp_tables: set[str]
    intermediate: set[str]  # temp tables + expanded views: lineage is traced through these
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def table_lineage(self, resolve: bool = True) -> dict[str, list[str]]:
        """target table -> source tables. resolve=True traces through temp tables and views."""
        graph: dict[str, set[str]] = {}
        for s in self.statements + self.views:
            graph.setdefault(s.target, set()).update(s.sources)
        return self._collect(graph, {s.target for s in self.statements}, resolve, lambda k: k)

    def column_lineage(self, resolve: bool = True) -> dict[str, list[str]]:
        """"table.column" -> source "table.column"s. resolve=True traces through temp tables and views."""
        graph: dict[str, set[str]] = {}
        for s in self.statements + self.views:
            for c, srcs in s.columns.items():
                graph.setdefault(f"{s.target}.{c}", set()).update(srcs)
        targets = {f"{s.target}.{c}" for s in self.statements for c in s.columns}
        return self._collect(graph, targets, resolve, lambda k: k.rsplit(".", 1)[0])

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

    def to_dict(self, resolve: bool = True) -> dict:
        return {
            "tables": self.table_lineage(resolve),
            "columns": self.column_lineage(resolve),
            "issues": [str(i) for i in self.issues],
        }


def analyze(
    sql: str,
    schema=None,
    project: str | None = None,
    dataset: str | None = None,
    expand_views: bool = True,
    strict: bool = False,
) -> LineageResult:
    """Parse a BigQuery query/script and compute its lineage.

    schema: a provider with get_table(fqn) -> TableInfo | None (DictSchemaProvider, BigQuerySchemaProvider).
    project/dataset: defaults for unqualified table names.
    strict: raise LineageError instead of returning a result with error issues.
    """
    a = _Analyzer(schema, project, dataset, expand_views)
    try:
        stmts = [s for s in sqlglot.parse(sql, read=D) if s is not None]
    except ParseError as e:
        a.issues = [Issue("error", err["description"], line=err.get("line"), col=err.get("col")) for err in e.errors]
    except SqlglotError as e:  # tokenizer errors, e.g. unterminated string
        a.issues = [Issue("error", str(e))]
    else:
        a.single = len(stmts) == 1
        for i, s in enumerate(stmts, 1):
            a.statement(s, i)
    result = LineageResult(a.statements, a.views, a.temps, a.intermediate | a.temps, a.issues)
    if strict and not result.ok:
        raise LineageError(result.issues)
    return result


def validate(sql: str, **kwargs) -> list[Issue]:
    """Syntax check, plus (with a schema) unknown tables/columns, ambiguous columns, column-count mismatches."""
    return analyze(sql, **kwargs).issues


def _target(stmt) -> exp.Table:
    t = stmt.this
    return t.this if isinstance(t, exp.Schema) else t


class _Analyzer:
    def __init__(self, provider, project, dataset, expand_views):
        self.provider = provider
        self.project = project or getattr(provider, "project", None) or _DEFAULT
        self.dataset = dataset
        self.expand_views = expand_views
        self.schema: dict = {}  # catalog -> db -> table -> {column: type}
        self.fetched: set[str] = set()
        self.statements: list[StatementLineage] = []
        self.views: list[StatementLineage] = []
        self.issues: list[Issue] = []
        self.temps: set[str] = set()
        self.intermediate: set[str] = set()
        self.single = True

    def issue(self, severity, message, idx):
        self.issues.append(Issue(severity, message, idx))

    @staticmethod
    def fqn(t: exp.Table) -> str:
        if t.db == SESSION:
            return f"{SESSION}.{t.name}"
        return ".".join(p for p in (t.catalog, t.db, t.name) if p and p != _DEFAULT)

    def _cols(self, t: exp.Table) -> dict:
        return self.schema.get(t.catalog, {}).get(t.db, {}).get(t.name, {})

    def _put(self, t: exp.Table, columns: dict):
        if columns:
            self.schema.setdefault(t.catalog, {}).setdefault(t.db, {})[t.name] = dict(columns)

    # ---- statements -------------------------------------------------------------------------

    def statement(self, stmt, idx, sink=None, catalog=None):
        if isinstance(stmt, exp.Query):
            kind = "SELECT"
        elif isinstance(stmt, (exp.Insert, exp.Merge, exp.Update)):
            kind = stmt.key.upper()
        elif isinstance(stmt, exp.Create) and stmt.kind in ("TABLE", "VIEW"):
            props = stmt.args.get("properties")
            if props and props.find(exp.TemporaryProperty):
                kind = "CREATE TEMP TABLE"
                tgt = _target(stmt)
                tgt.set("db", exp.to_identifier(SESSION))
                self.temps.add(f"{SESSION}.{tgt.name}")
            else:
                kind = "VIEW" if sink is self.views else f"CREATE {stmt.kind}"
        else:
            if isinstance(stmt, exp.Command):
                self.issue("warning", f"Statement not understood, skipped: {stmt.sql(D)[:80]}", idx)
            return  # DECLARE/SET/DROP/DELETE/... carry no lineage

        target = None if kind == "SELECT" else _target(stmt)
        real = self._normalize(stmt, idx, catalog or self.project)
        for t in real:
            if not (t is target and kind.startswith(("CREATE", "VIEW"))):
                self._load(t, idx)
        name = self.fqn(target) if target is not None else ("<query>" if self.single else f"<query#{idx}>")

        handler = {"SELECT": self._select, "INSERT": self._insert, "MERGE": self._merge, "UPDATE": self._update}
        pairs = handler.get(kind, self._create)(stmt, idx, target)
        if pairs is None:
            return
        columns: dict[str, set[str]] = {}
        for n, srcs, _ in pairs:
            columns.setdefault(n, set()).update(srcs)
        sources = {self.fqn(t) for t in real if t is not target}
        (self.statements if sink is None else sink).append(StatementLineage(idx, kind, name, sources, columns))

    def _normalize(self, stmt, idx, catalog) -> list[exp.Table]:
        """Fill in default project/dataset, map temp tables to _SESSION; return the real (non-CTE) tables."""
        ctes = {c.alias_or_name for c in stmt.find_all(exp.CTE)}
        real = []
        for t in stmt.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier) or (not t.db and t.name in ctes):
                continue
            if not t.db:
                if f"{SESSION}.{t.name}" in self.temps:
                    t.set("db", exp.to_identifier(SESSION))
                elif self.dataset:
                    t.set("db", exp.to_identifier(self.dataset))
                else:
                    self.issue("warning", f"Table `{t.name}` has no dataset and no default dataset was given", idx)
                    t.set("db", exp.to_identifier(_DEFAULT))
            if not t.catalog:
                t.set("catalog", exp.to_identifier(catalog))
            real.append(t)
        return real

    def _load(self, t: exp.Table, idx):
        name = self.fqn(t)
        if t.db == SESSION or self._cols(t) or name in self.fetched or self.provider is None:
            return
        self.fetched.add(name)
        info = self.provider.get_table(name)
        if info is None:
            self.issue("error", f"Table `{name}` not found", idx)
            return
        self._put(t, info.columns)
        if info.view_sql and self.expand_views:
            try:
                query = sqlglot.parse_one(info.view_sql, read=D)
            except SqlglotError as e:
                self.issue("warning", f"Could not parse definition of view `{name}`: {e}", idx)
                return
            view = t.copy()
            view.set("alias", None)
            self.intermediate.add(name)
            self.statement(exp.Create(this=view, kind="VIEW", expression=query), idx, self.views, t.catalog)

    # ---- per statement kind: each returns [(target column, sources, type)] or None ----------

    def _select(self, stmt, idx, target):
        return self._columns(stmt, idx)

    def _create(self, stmt, idx, target):
        declared = None
        if isinstance(stmt.this, exp.Schema):  # CREATE ... (a INT64, b STRING) [AS ...]
            declared = {c.name: c.args["kind"].sql(D) if c.args.get("kind") else "STRING" for c in stmt.this.expressions}
        query = stmt.expression
        if not isinstance(query, exp.Query):
            self._put(target, declared)
            return None
        pairs = self._columns(query, idx, list(declared) if declared else None, f"CREATE {self.fqn(target)}")
        dups = [n for n, c in Counter(n for n, _, _ in pairs).items() if c > 1]
        if dups:
            self.issue("error", f"Duplicate column names in `{self.fqn(target)}`: {', '.join(dups)}", idx)
        self._put(target, declared or {n: t for n, _, t in pairs})
        if stmt.kind == "VIEW" and self.expand_views:
            self.intermediate.add(self.fqn(target))
        return pairs

    def _insert(self, stmt, idx, target):
        if isinstance(stmt.this, exp.Schema):
            cols = [c.name for c in stmt.this.expressions]
        else:
            cols = list(self._cols(target)) or None
            if cols is None:
                self.issue("warning", f"Schema of `{self.fqn(target)}` unknown; mapping INSERT by output column names", idx)
        if not isinstance(stmt.expression, exp.Query):  # VALUES (...) - literals, no column lineage
            return []
        return self._columns(stmt.expression, idx, cols, f"INSERT {self.fqn(target)}")

    def _merge(self, stmt, idx, target):
        using = stmt.args["using"]
        base = exp.select().from_(target.copy()).join(using.copy(), on=stmt.args.get("on"))
        tcols = list(self._cols(target))
        pairs = []
        for when in stmt.args["whens"].expressions:
            then = when.args.get("then")
            if isinstance(then, exp.Update):
                names = [eq.this.name for eq in then.expressions]
                values = [eq.expression for eq in then.expressions]
            elif isinstance(then, exp.Insert) and isinstance(then.expression, exp.Tuple):
                names = [c.name for c in then.this.expressions] if isinstance(then.this, exp.Tuple) else tcols
                values = then.expression.expressions
            elif isinstance(then, exp.Insert):  # INSERT ROW: all source columns, positionally
                q = exp.select(exp.Column(this=exp.Star(), table=exp.to_identifier(using.alias_or_name))).from_(using.copy())
                pairs += self._columns(q, idx, tcols, f"MERGE {self.fqn(target)} INSERT ROW")
                continue
            else:  # DELETE
                continue
            q = base.copy().select(*(v.copy().as_(f"_c{i}") for i, v in enumerate(values)))
            pairs += self._columns(q, idx, names, f"MERGE {self.fqn(target)}")
        return pairs

    def _update(self, stmt, idx, target):
        q = exp.select(*(eq.expression.copy().as_(f"_c{i}") for i, eq in enumerate(stmt.expressions)))
        q = q.from_(target.copy())
        frm = stmt.args.get("from_") or stmt.args.get("from")
        if frm:
            q = q.join(frm.this.copy(), join_type="cross")
        return self._columns(q, idx, [eq.this.name for eq in stmt.expressions], f"UPDATE {self.fqn(target)}")

    # ---- column lineage ---------------------------------------------------------------------

    def _columns(self, query, idx, target_cols=None, label=""):
        pairs = self._query_lineage(query, idx)
        if target_cols is None or not pairs:
            return pairs
        if len(target_cols) != len(pairs):
            self.issue("error", f"{label}: query returns {len(pairs)} columns but target has {len(target_cols)}", idx)
            return []
        return [(tc, s, t) for tc, (_, s, t) in zip(target_cols, pairs)]

    def _query_lineage(self, query, idx) -> list[tuple[str, set[str], str]]:
        mapping = MappingSchema(self.schema, dialect=D)
        try:
            q = qualify(query.copy(), schema=mapping, dialect=D, validate_qualify_columns=True)
        except SqlglotError as e:
            self.issue("error", self._explain(str(e), query), idx)
            return []
        try:
            annotate_types(q, schema=mapping, dialect=D)
        except Exception:  # types only feed temp-table schemas; lineage doesn't need them
            pass
        scope = build_scope(q)
        out = []
        for sel in q.selects:
            name = sel.alias_or_name
            if name == "*":
                self.issue("warning", "SELECT * could not be expanded: table schema unknown", idx)
                continue
            try:
                node = _sg_lineage(name, q, schema=None, dialect=D, scope=scope, copy=False)
            except Exception as e:  # sqlglot internals; keep going with the other columns
                self.issue("warning", f"Column lineage failed for `{name}`: {e}", idx)
                continue
            srcs = {
                f"{self.fqn(n.expression)}.{n.name.split('.')[-1].strip(chr(34))}"
                for n in node.walk()
                if isinstance(n.expression, exp.Table)
            }
            typ = sel.type.sql(D) if sel.type and not sel.type.is_type(exp.DataType.Type.UNKNOWN) else "STRING"
            out.append((name, srcs, typ))
        return out

    def _explain(self, msg, query) -> str:
        m = re.search(r"Column '([^']+)' could not be resolved", msg)
        if not m:
            return msg
        col = m.group(1).lower()
        tables = [t for t in query.find_all(exp.Table) if t.db]  # normalized real tables, not CTEs
        hits = [f"{t.alias_or_name} ({self.fqn(t)})" for t in tables if col in {c.lower() for c in self._cols(t)}]
        if len(hits) > 1:
            return f"Column `{col}` is ambiguous, present in: {', '.join(hits)}. Qualify it with a table alias."
        unknown = [self.fqn(t) for t in tables if not self._cols(t)]
        hint = f" (schema unknown for: {', '.join(sorted(set(unknown)))})" if unknown else ""
        return f"Column `{col}` not found in any referenced table{hint}. {msg}"
