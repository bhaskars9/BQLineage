"""Statement level: walk a script, work out what each statement writes and reads, and collect its lineage."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.tokens import TokenType

from . import script
from .compat import BQ, D, PSEUDOCOLUMNS, tokenize
from .model import ColumnRecord, Issue, LineageError, StatementLineage
from .render import placeholder, render
from .result import LineageResult
from .trace import Tracer

SESSION = "_SESSION"  # BigQuery's dataset name for script temp tables
_DEFAULT = "__default__"  # placeholder project/dataset; never shown in output
_TOKEN = re.compile(r"<Token token_type: \S+, text: (.*?), line: \d+.*?>")
_SPEC = re.compile(r"%%|%[-+ #'0]*\d*(?:\.\d+)?([sdifeEgGtTxXo])")  # BigQuery FORMAT() specifiers


def analyze(
    sql: str,
    schema=None,
    project: str | None = None,
    dataset: str | None = None,
    expand_views: bool = True,
    strict: bool = False,
    vars: dict[str, str] | None = None,
) -> LineageResult:
    """Parse a BigQuery query/script and compute its lineage.

    schema: a provider with get_table(fqn) -> TableInfo | None (DictSchemaProvider, BigQuerySchemaProvider, Catalog).
    project/dataset: defaults for unqualified table names.
    vars: values for template placeholders - {{ name }}, {{ params.name }}, ${name} (see docs/variables.md).
    strict: raise LineageError instead of returning a result with error issues.
    """
    sql, missing, logic = render(sql, vars)
    a = _Analyzer(schema, project, dataset, expand_views)
    a.variables |= {placeholder(m).lower() for m in missing}  # left in an expression, a placeholder is a value
    for m in missing:
        a.issue("warning", f"Template placeholder `{m}` has no value: pass --var {m.split('.')[-1]}=... "
                           "or set it under [vars] in bqlineage.toml", None)
    if logic:
        a.issue("warning", "Template logic ({% ... %}) was removed, not evaluated: render the SQL first "
                           "for exact lineage", None)
    a.run(sql)
    result = LineageResult(a.statements, a.views, a.temps, a.intermediate | a.temps, a.issues, a.loaded, sql)
    if strict and not result.ok:
        raise LineageError(result.issues)
    return result


def validate(sql: str, **kwargs) -> list[Issue]:
    """Syntax check, plus (with a schema) unknown tables/columns, ambiguous columns, column-count mismatches."""
    return analyze(sql, **kwargs).issues


def _target(stmt) -> exp.Table:
    t = stmt.this
    return t.this if isinstance(t, exp.Schema) else t


def _format(fmt: str, args: list[str]) -> str | None:
    """BigQuery FORMAT() for the specifiers dynamic SQL uses (%s %d %t %T ...); None if the arguments don't fit."""
    it, short = iter(args), False

    def sub(m):
        nonlocal short
        if m.group(0) == "%%":
            return "%"
        v = next(it, None)
        if v is None:
            short = True
            return ""
        if m.group(1) == "T" and not re.fullmatch(r"-?\d+(?:\.\d+)?", v):  # %T: as a SQL literal
            return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return v

    out = _SPEC.sub(sub, fmt)
    return None if short else out


class _Analyzer(Tracer):
    def __init__(self, provider, project, dataset, expand_views):
        self.provider = provider
        self.project = project or getattr(provider, "project", None) or _DEFAULT
        self.dataset = dataset
        self.expand_views = expand_views
        self.schema: dict = {}  # catalog -> db -> table -> {column: type}
        self.fetched: set[str] = set()  # looked up in the provider, or created by the script
        self.loaded: dict[str, dict] = {}  # table -> columns, for what the provider returned
        self.statements: list[StatementLineage] = []
        self.views: list[StatementLineage] = []
        self.issues: list[Issue] = []
        self.versions: dict[str, int] = {}  # temp table -> number of times created
        self.temps: set[str] = set()  # versioned temp table names
        self.intermediate: set[str] = set()
        self.variables: set[str] = set()  # DECLAREd names and FOR-loop variables, lower-cased
        self.constants: dict[str, str] = {}  # variables whose value is known: DECLARE x DEFAULT 'literal', SET ...
        self.single = True
        self.line = None  # first line of the statement being analyzed

    def issue(self, severity, message, idx, line=None, col=None):
        self.issues.append(Issue(severity, message, idx, line or self.line, col))

    @staticmethod
    def fqn(t: exp.Table) -> str:
        if t.db == SESSION:
            return f"{SESSION}.{t.name}"
        return ".".join(p for p in (t.catalog, t.db, t.name) if p and p != _DEFAULT)

    def _v(self, name: str) -> str:
        """Temp tables can be re-created; each CREATE starts a new version so old readers keep old lineage."""
        n = self.versions.get(name, 1)
        return name if n == 1 else f"{name}#{n}"

    def _cols(self, t: exp.Table) -> dict:
        return self.schema.get(t.catalog, {}).get(t.db, {}).get(t.name, {})

    def _canon(self, t: exp.Table, col: str) -> str:
        """Column name as spelled in the table's schema (BigQuery column names are case-insensitive)."""
        if col.upper() in PSEUDOCOLUMNS:
            return col.upper()
        return next((c for c in self._cols(t) if c.lower() == col.lower()), col)

    def _put(self, t: exp.Table, columns: dict | None):
        if columns:
            self.schema.setdefault(t.catalog, {}).setdefault(t.db, {})[t.name] = dict(columns)

    def _no_schema(self) -> str:
        return "" if self.provider else " - no schema source given, pass --bq or --schema"

    # ---- script -----------------------------------------------------------------------------

    def run(self, sql: str, nested: int | None = None):
        """Analyze a script; `nested` is the statement number of the EXECUTE IMMEDIATE that runs `sql`."""
        sql = sql.removeprefix("﻿")  # UTF-8 BOM from Windows editors
        try:
            tokens = tokenize(sql)
        except SqlglotError as e:  # e.g. unterminated string
            self.issue("error", str(e), nested)
            return
        stmts = list(script.split(tokens))
        if nested is None:
            self.single = len(stmts) == 1
        for n, (toks, names) in enumerate(stmts, 1):
            idx = nested or n
            self.line = toks[0].line
            self.variables.update(v.lower() for v in names)  # FOR-loop variable, procedure parameters
            if script.kw(toks[0]) == "EXECUTE":
                self._execute(toks, idx)
                continue
            if script.kw(toks[0]) == "CALL":
                self.issue("warning", "CALL not analyzed (procedure body)", idx)
                continue
            try:
                parsed = BQ.parser().parse(toks, sql)  # one statement at a time: a bad one doesn't stop the rest
            except ParseError as e:
                for err in e.errors:
                    self.issue("error", _TOKEN.sub(r"'\1'", err["description"]), idx, err.get("line"), err.get("col"))
                continue
            for s in parsed:
                if s is not None:
                    self.statement(s, idx)

    def _execute(self, toks, idx):
        """EXECUTE IMMEDIATE: analyze the SQL it runs when that's built from literals and known constants."""
        m = re.match(r"(?is)\s*IMMEDIATE\s+(.+)", toks[1].text if len(toks) == 2 else "")
        sql = None
        if m:
            body = m.group(1)
            try:
                depth = 0
                for t in tokenize(body):  # cut the "INTO ..." / "USING ..." that follow the SQL expression
                    depth += {TokenType.L_PAREN: 1, TokenType.R_PAREN: -1}.get(t.token_type, 0)
                    if not depth and script.kw(t) in ("INTO", "USING"):
                        body = body[:t.start]
                        break
                sql = self._const(sqlglot.parse_one(body, read=D))
            except SqlglotError:
                sql = None
        if sql is None:
            self.issue("warning", "EXECUTE IMMEDIATE not analyzed: its SQL is only known at run time", idx)
            return
        line, before = self.line, len(self.issues)
        self.run(sql, idx)
        for i in self.issues[before:]:  # positions inside the generated SQL mean nothing in the file
            i.message, i.line, i.col = f"in EXECUTE IMMEDIATE: {i.message}", line, None
        self.line = line

    def _const(self, e) -> str | None:
        """The value of a string built from literals and known constants (DECLARE / SET), else None."""
        if isinstance(e, (exp.Literal, exp.RawString)):
            return str(e.this)
        if isinstance(e, exp.Column) and not e.table:
            return self.constants.get(e.name.lower())
        if isinstance(e, (exp.Paren, exp.Cast)):
            return self._const(e.this)
        if isinstance(e, exp.Format):
            fmt, args = self._const(e.this), [self._const(a) for a in e.expressions]
            return None if fmt is None or None in args else _format(fmt, args)
        if isinstance(e, (exp.DPipe, exp.Concat)):
            values = [self._const(p) for p in ([e.this, e.expression] if isinstance(e, exp.DPipe) else e.expressions)]
            return None if None in values else "".join(values)
        return None

    def statement(self, stmt, idx, sink=None, catalog=None):
        if isinstance(stmt, exp.Declare):
            for item in stmt.expressions:
                names = item.this if isinstance(item.this, list) else [item.this]
                default = item.args.get("default")
                value = self._const(default) if default is not None else None
                for n in names:
                    self.variables.add(n.name.lower())
                    if value is not None:
                        self.constants[n.name.lower()] = value
            return
        if isinstance(stmt, exp.Set):
            for item in stmt.expressions:
                eq = item.this
                if not isinstance(eq, exp.EQ):
                    continue
                if isinstance(eq.this, exp.Column) and not eq.this.table:  # SET x = ...: track known values
                    value, name = self._const(eq.expression), eq.this.name.lower()
                    if value is None:
                        self.constants.pop(name, None)
                    else:
                        self.constants[name] = value
                elif isinstance(eq.expression, exp.Literal):  # SET @@dataset_id / @@dataset_project_id
                    key = eq.this.sql(D).lower()
                    if key == "@@dataset_id":
                        self.dataset = eq.expression.name
                    elif key == "@@dataset_project_id":
                        self.project = eq.expression.name
            return
        if isinstance(stmt, exp.Query):
            kind = "SELECT"
        elif isinstance(stmt, exp.Export):
            kind = "EXPORT"
        elif isinstance(stmt, (exp.Insert, exp.Merge, exp.Update)):
            kind = stmt.key.upper()
        elif isinstance(stmt, exp.Create) and stmt.kind in ("TABLE", "VIEW"):
            props = stmt.args.get("properties")
            if props and props.find(exp.TemporaryProperty):
                kind = "CREATE TEMP TABLE"
                _target(stmt).set("db", exp.to_identifier(SESSION))
            else:
                kind = "VIEW" if sink is self.views else f"CREATE {stmt.kind}"
        else:
            if isinstance(stmt, exp.Command):
                self.issue("warning", f"Statement not understood, skipped: {stmt.sql(D)[:80]}", idx)
            return  # DROP/DELETE/TRUNCATE/ALTER/transactions/functions carry no lineage

        target = None if kind in ("SELECT", "EXPORT") else _target(stmt)
        creates = kind.startswith(("CREATE", "VIEW"))
        real = self._normalize(stmt, idx, catalog or self.project)
        for t in real:
            if not (t is target and creates):
                self._load(t, idx)
        sources = {self._v(self.fqn(t)) for t in real if t is not target}  # before a temp re-create bumps it

        handler = {"SELECT": self._select, "EXPORT": self._export, "INSERT": self._insert,
                   "MERGE": self._merge, "UPDATE": self._update}.get(kind, self._create)
        records = handler(stmt, idx, target)
        if creates:
            self.fetched.add(self.fqn(target))  # created by the script: never look it up in BigQuery
            if kind == "CREATE TEMP TABLE":
                self.versions[self.fqn(target)] = self.versions.get(self.fqn(target), 0) + 1
                self.temps.add(self._v(self.fqn(target)))
        if records is None:
            return

        if kind == "EXPORT":
            opts = stmt.args.get("options")
            uris = [p.args["value"].name for p in (opts.expressions if opts else [])
                    if isinstance(p, exp.Property) and p.name.lower() == "uri"]
            name = uris[0] if uris else "<export>"
        elif target is None:
            name = "<query>" if self.single else f"<query#{idx}>"
        else:
            name = self._v(self.fqn(target))
        columns: dict[str, set[str]] = {}
        details: dict[str, dict] = {}
        for rec in records:  # a MERGE can write one column from several WHEN branches
            columns.setdefault(rec.name, set()).update(rec.sources)
            d = details.setdefault(rec.name, {"steps": [], "indirect": set(), "unresolved": set(), "flow": {}})
            d["indirect"] |= rec.indirect
            d["unresolved"] |= rec.unresolved
            d["steps"] += [st for st in rec.steps if st not in d["steps"]]
            for node, f in rec.flow.items():
                g = d["flow"].setdefault(node, {"inputs": set(), "steps": [], "same": True, "filters": set(),
                                                "driving": set()})
                g["inputs"] |= f["inputs"]
                g["steps"] += [st for st in f["steps"] if st not in g["steps"]]
                g["same"] = g["same"] and f["same"]
                g["filters"] |= f["filters"]
                g["driving"] |= f["driving"]
        sink = self.statements if sink is None else sink
        sink.append(StatementLineage(idx, kind, name, sources, columns, details))

    def _normalize(self, stmt, idx, catalog) -> list[exp.Table]:
        """Fill in default project/dataset, map temp tables to _SESSION; return the real (non-CTE) tables."""
        ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}  # CTE names are case-insensitive
        real = []
        for t in stmt.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier) or (not t.db and t.name.lower() in ctes):
                continue
            if not t.db:
                if f"{SESSION}.{t.name}" in self.versions:
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
        if (t.db == SESSION or self._cols(t) or name in self.fetched or self.provider is None
                or "INFORMATION_SCHEMA" in t.name.upper()
                or any(p.startswith("__") and p.endswith("__") for p in (t.catalog, t.db, t.name))):
            return  # temp table, known, unanswerable, or named by a template placeholder that had no value
        self.fetched.add(name)
        try:
            info = self.provider.get_table(name)
        except Exception as e:  # permission denied, network, bad name: report it and keep going
            self.issue("error", f"Could not read schema of `{name}`: {str(e).splitlines()[0][:300]}", idx)
            return
        if info is None:
            offline = getattr(self.provider, "fallback", False) is None  # a catalog with nothing behind it
            self.issue("error", f"Table `{name}` not found" + (" in the catalog (pass --bq to look it up in BigQuery)"
                                                                if offline else ""), idx)
            return
        self.loaded[name] = dict(info.columns)
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

    # ---- per statement kind: each returns the target's column records, or None ----------------

    def _select(self, stmt, idx, target):
        return self._columns(stmt, idx)

    def _export(self, stmt, idx, target):
        return self._columns(stmt.this, idx)

    def _create(self, stmt, idx, target):
        props = stmt.args.get("properties")
        clone, like = stmt.args.get("clone"), props and props.find(exp.LikeProperty)
        if clone or like:  # CLONE / COPY copy the data column for column; LIKE copies only the schema
            src = (clone or like).this
            cols = self._cols(src)
            self._put(target, cols)
            if not clone:
                return None
            how = {"label": None, "expression": f"{'COPY' if clone.args.get('copy') else 'CLONE'} {self.fqn(src)}"}
            return [ColumnRecord(c, {f"{self._v(self.fqn(src))}.{c}"}, typ, [how]) for c, typ in cols.items()]
        declared = None
        if isinstance(stmt.this, exp.Schema):  # CREATE ... (a INT64, b STRING) [AS ...]
            declared = {c.name: c.args["kind"].sql(D) if c.args.get("kind") else "STRING" for c in stmt.this.expressions}
        query = stmt.expression
        if not isinstance(query, exp.Query):
            self._put(target, declared)
            return None
        records = self._columns(query, idx, list(declared) if declared else None, f"CREATE {self.fqn(target)}")
        names = [r.name for r in records]
        dups = [n for n, c in Counter(names).items() if c > 1 and n != "*"]
        if dups:
            self.issue("error", f"Duplicate column names in `{self.fqn(target)}`: {', '.join(dups)}", idx)
        if declared or "*" not in names:  # an unexpanded * leaves the new table's columns unknown
            self._put(target, declared or {r.name: r.type for r in records})
        if stmt.kind == "VIEW" and self.expand_views:
            self.intermediate.add(self.fqn(target))
        return records

    def _insert(self, stmt, idx, target):
        if isinstance(stmt.this, exp.Schema):
            cols = [self._canon(target, c.name) for c in stmt.this.expressions]
        else:
            cols = list(self._cols(target)) or None
            if cols is None:
                self.issue("warning", f"Schema of `{self.fqn(target)}` unknown; mapping INSERT by output column names{self._no_schema()}", idx)
        if not isinstance(stmt.expression, exp.Query):  # VALUES (...) - literals, no column lineage
            return []
        return self._columns(stmt.expression, idx, cols, f"INSERT {self.fqn(target)}")

    def _merge(self, stmt, idx, target):
        using = stmt.args["using"]
        # FROM the source: its rows are the ones merged in, so it's the driving table (the target is joined)
        base = exp.select().from_(using.copy()).join(target.copy(), on=stmt.args.get("on"))
        tcols = list(self._cols(target)) or None
        label = f"MERGE {self.fqn(target)}"
        records = []
        for when in stmt.args["whens"].expressions:
            then = when.args.get("then")
            if isinstance(then, exp.Update):
                names = [self._canon(target, eq.this.name) for eq in then.expressions]
                values = [eq.expression for eq in then.expressions]
            elif isinstance(then, exp.Insert) and isinstance(then.expression, exp.Tuple):  # INSERT (cols) VALUES
                names = [self._canon(target, c.name) for c in then.this.expressions]
                values = then.expression.expressions
            elif isinstance(then, exp.Insert) and isinstance(then.this, exp.Tuple):  # INSERT VALUES, no columns
                names, values = tcols, then.this.expressions
                if len(values) == 1 and isinstance(values[0], exp.Anonymous) and values[0].name.upper() == "VALUES":
                    values = values[0].expressions  # sqlglot reads this VALUES (...) as a function call
            elif isinstance(then, exp.Insert):  # INSERT ROW: every source column, by position
                if tcols is None:
                    self.issue("warning", f"{label}: target schema unknown; mapping INSERT ROW by source column names{self._no_schema()}", idx)
                star = exp.Column(this=exp.Star(), table=exp.to_identifier(using.alias_or_name))
                records += self._columns(exp.select(star).from_(using.copy()), idx, tcols, f"{label} INSERT ROW")
                continue
            else:  # DELETE
                continue
            if names is None:
                self.issue("warning", f"{label}: target schema unknown; INSERT without column list not mapped{self._no_schema()}", idx)
                continue
            q = base.copy().select(*(v.copy().as_(f"_c{i}") for i, v in enumerate(values)))
            if when.args.get("condition"):  # WHEN MATCHED AND <condition>
                q = q.where(when.args["condition"].copy())
            records += self._columns(q, idx, names, label)
        return records

    def _update(self, stmt, idx, target):
        q = exp.select(*(eq.expression.copy().as_(f"_c{i}") for i, eq in enumerate(stmt.expressions)))
        q = q.from_(target.copy())
        frm = stmt.args.get("from_") or stmt.args.get("from")
        if frm:
            q = q.join(frm.this.copy(), join_type="cross")
        if stmt.args.get("where"):
            q = q.where(stmt.args["where"].this.copy())
        names = [self._canon(target, eq.this.name) for eq in stmt.expressions]
        return self._columns(q, idx, names, f"UPDATE {self.fqn(target)}")

    def _columns(self, query, idx, target_cols=None, label=""):
        """The query's column records, renamed to `target_cols` by position when given."""
        records = self._query_lineage(query, idx)
        if target_cols is None or not records:
            return records
        if any(r.name == "*" for r in records):
            self.issue("warning", f"{label}: columns behind SELECT * are unknown, can't map them to the target", idx)
            return []
        if len(target_cols) != len(records):
            self.issue("error", f"{label}: query returns {len(records)} columns but target has {len(target_cols)}", idx)
            return []
        return [replace(r, name=tc) for tc, r in zip(target_cols, records)]
