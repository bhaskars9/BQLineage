"""Table- and column-level lineage for BigQuery SQL, built on sqlglot (github.com/tobymao/sqlglot)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.lineage import lineage as _sg_lineage
from sqlglot.lineage import to_node as _to_node
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope, find_all_in_scope
from sqlglot.schema import MappingSchema
from sqlglot.tokens import TokenType

D = "bigquery"
_BQ = Dialect.get_or_raise(D)
SESSION = "_SESSION"  # BigQuery's dataset name for script temp tables
_DEFAULT = "__default__"  # placeholder project/dataset; never shown in output
_UNRESOLVED = re.compile(r"Column '([^']+)' could not be resolved(?: for table: '([^']+)')?")
_POSITION = re.compile(r"\s*Line: (\d+), Col: (\d+)\.?")
_TOKEN = re.compile(r"<Token token_type: \S+, text: (.*?), line: \d+.*?>")
_NO_LINEAGE = {"END", "LEAVE", "BREAK", "ITERATE", "CONTINUE", "RETURN", "RAISE", "UNTIL"}


class _Tokenizer(_BQ.tokenizer_class):
    # sqlglot tokenizes these as COMMAND and swallows the rest of the statement into one string token,
    # hiding the SQL inside procedural blocks. As plain words, _statements() can peel them off instead.
    KEYWORDS = {
        **_BQ.tokenizer_class.KEYWORDS,
        **dict.fromkeys(("BEGIN", "ELSEIF", "EXCEPTION", "LOOP", "REPEAT", "WHILE"), TokenType.VAR),
    }


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
class StatementLineage:
    index: int | None
    kind: str  # SELECT, CREATE TABLE, CREATE TEMP TABLE, CREATE VIEW, INSERT, MERGE, UPDATE, EXPORT, VIEW
    target: str
    sources: set[str]  # every table read, including ones only used in JOIN/WHERE
    columns: dict[str, set[str]]  # target column -> {"table.column", ...} its values come from
    # target column -> {"steps": [{expression, from, joins, where, ...}], "indirect": {"table.column", ...}}
    details: dict[str, dict] = field(default_factory=dict)


@dataclass
class LineageResult:
    statements: list[StatementLineage]
    views: list[StatementLineage]  # definitions of views fetched from the schema provider
    temp_tables: set[str]  # re-created temp tables are versioned: _SESSION.t, _SESSION.t#2, ...
    intermediate: set[str]  # temp tables + expanded views: lineage is traced through these
    issues: list[Issue] = field(default_factory=list)

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
                           through CTEs, subqueries, temp tables and views"""
        direct: dict[str, set[str]] = {}
        indirect: dict[str, set[str]] = {}
        steps: dict[str, list[dict]] = {}
        for s in self.statements + self.views:
            for c, srcs in s.columns.items():
                k = f"{s.target}.{c}"
                direct.setdefault(k, set()).update(srcs)
                info = s.details.get(c, {})
                indirect.setdefault(k, set()).update(info.get("indirect", ()))
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

        def trace(key):  # (direct, indirect, steps), expanded through temp tables / views
            if key in memo:
                return memo[key]
            memo[key] = (set(), set(), [])  # in progress: a self-referencing temp table adds nothing
            d_out, i_out, s_out = set(), set(), list(steps.get(key, []))
            for src in direct.get(key, ()):
                if traced(src):
                    d, i, s = trace(src)
                    d_out |= d
                    i_out |= i  # the conditions that built a temp column apply to what reads it
                    s_out += [x for x in s if x not in s_out]
                else:
                    d_out.add(src)
            for src in indirect.get(key, ()):
                if traced(src):
                    d, i, _ = trace(src)
                    i_out |= d | i
                else:
                    i_out.add(src)
            memo[key] = (d_out, i_out, s_out)
            return memo[key]

        wanted = {c.lower() for c in columns} if columns else None
        out = {}
        for s in self.statements:
            if not _is_table(s.target, table) or (resolve and s.target in self.temp_tables):
                continue
            for c in s.columns:
                if wanted is None or c.lower() in wanted:
                    d, i, st = trace(f"{s.target}.{c}")
                    out[f"{s.target}.{c}"] = {"sources": sorted(d), "indirect_sources": sorted(i), "steps": st}
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
            "tables": self.table_lineage(resolve, table),
            "columns": self.column_details(resolve, table, columns),
            "issues": [str(i) for i in self.issues],
        }


def _is_table(name: str, table: str | None) -> bool:
    """`table` picks a target by full name or trailing parts: "p.ds.t" matches "ds.t" and "t"."""
    return table is None or name == table or name.endswith("." + table)


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
    a.run(sql)
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


def _kw(tok) -> str:
    """Upper-cased token text when it can be a keyword (not a string or quoted identifier)."""
    quoted = tok.token_type == TokenType.IDENTIFIER or tok.token_type.name.endswith("STRING")
    return "" if quoted else tok.text.upper()


def _after(chunk, word):
    """Tokens after the first top-level `word` (THEN / DO), skipping brackets and CASE..END; None if absent."""
    depth = 0
    for i, tok in enumerate(chunk[1:], 1):
        kw = _kw(tok)
        if tok.token_type in (TokenType.L_PAREN, TokenType.L_BRACKET) or kw == "CASE":
            depth += 1
        elif tok.token_type in (TokenType.R_PAREN, TokenType.R_BRACKET) or (kw == "END" and depth):
            depth -= 1
        elif kw == word and not depth:
            return chunk[i + 1:]
    return None


def _statements(tokens):
    """Split tokens on ';' and peel BigQuery procedural wrappers (BEGIN, IF/ELSEIF..THEN, ELSE, LOOP, REPEAT,
    WHILE..DO, FOR..DO, CASE/WHEN..THEN, EXCEPTION WHEN ERROR THEN, labels) so the SQL inside blocks is
    analyzed like straight-line code. Yields (statement tokens, FOR-loop variable or None)."""
    chunk = []
    for tok in [*tokens, None]:
        if tok is not None and tok.token_type != TokenType.SEMICOLON:
            chunk.append(tok)
            continue
        loop_var = None
        while chunk:
            kw = _kw(chunk[0])
            if len(chunk) > 2 and chunk[1].token_type == TokenType.COLON:  # label: BEGIN / LOOP / ...
                rest = chunk[2:]
            elif kw == "BEGIN" and len(chunk) > 1 and _kw(chunk[1]) != "TRANSACTION":
                rest = chunk[1:]
            elif kw in ("ELSE", "LOOP", "REPEAT"):
                rest = chunk[1:]
            elif kw in ("IF", "ELSEIF", "CASE", "WHEN", "EXCEPTION"):
                rest = _after(chunk, "THEN")
            elif kw in ("WHILE", "FOR"):
                rest = _after(chunk, "DO")
                loop_var = chunk[1].text if kw == "FOR" else loop_var
            else:
                break
            if rest is None:
                break
            chunk = rest
        if chunk and _kw(chunk[0]) not in _NO_LINEAGE:  # END IF, LEAVE, ... carry no lineage
            yield chunk, loop_var
        chunk = []


class _Analyzer:
    def __init__(self, provider, project, dataset, expand_views):
        self.provider = provider
        self.project = project or getattr(provider, "project", None) or _DEFAULT
        self.dataset = dataset
        self.expand_views = expand_views
        self.schema: dict = {}  # catalog -> db -> table -> {column: type}
        self.fetched: set[str] = set()  # looked up in the provider, or created by the script
        self.statements: list[StatementLineage] = []
        self.views: list[StatementLineage] = []
        self.issues: list[Issue] = []
        self.versions: dict[str, int] = {}  # temp table -> number of times created
        self.temps: set[str] = set()  # versioned temp table names
        self.intermediate: set[str] = set()
        self.variables: set[str] = set()  # DECLAREd names and FOR-loop variables, lower-cased
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
        if col.upper() in _BQ.PSEUDOCOLUMNS:
            return col.upper()
        return next((c for c in self._cols(t) if c.lower() == col.lower()), col)

    def _put(self, t: exp.Table, columns: dict | None):
        if columns:
            self.schema.setdefault(t.catalog, {}).setdefault(t.db, {})[t.name] = dict(columns)

    def _no_schema(self) -> str:
        return "" if self.provider else " - no schema source given, pass --bq or --schema"

    # ---- script -----------------------------------------------------------------------------

    def run(self, sql: str):
        sql = sql.removeprefix("﻿")  # UTF-8 BOM from Windows editors
        try:
            tokens = _Tokenizer(dialect=_BQ).tokenize(sql)
        except SqlglotError as e:  # e.g. unterminated string
            self.issue("error", str(e), None)
            return
        stmts = list(_statements(tokens))
        self.single = len(stmts) == 1
        for idx, (toks, loop_var) in enumerate(stmts, 1):
            self.line = toks[0].line
            if loop_var:
                self.variables.add(loop_var.lower())
            if _kw(toks[0]) in ("EXECUTE", "CALL"):
                self.issue("warning", f"{toks[0].text.upper()} not analyzed (dynamic SQL / procedure call)", idx)
                continue
            try:
                parsed = _BQ.parser().parse(toks, sql)  # one statement at a time: a bad one doesn't stop the rest
            except ParseError as e:
                for err in e.errors:
                    self.issue("error", _TOKEN.sub(r"'\1'", err["description"]), idx, err.get("line"), err.get("col"))
                continue
            for s in parsed:
                if s is not None:
                    self.statement(s, idx)

    def statement(self, stmt, idx, sink=None, catalog=None):
        if isinstance(stmt, exp.Declare):
            for item in stmt.expressions:
                names = item.this if isinstance(item.this, list) else [item.this]
                self.variables.update(n.name.lower() for n in names)
            return
        if isinstance(stmt, exp.Set):  # SET @@dataset_id / @@dataset_project_id change the defaults
            for item in stmt.expressions:
                eq = item.this
                if isinstance(eq, exp.EQ) and isinstance(eq.expression, exp.Literal):
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
        pairs = handler(stmt, idx, target)
        if creates:
            self.fetched.add(self.fqn(target))  # created by the script: never look it up in BigQuery
            if kind == "CREATE TEMP TABLE":
                self.versions[self.fqn(target)] = self.versions.get(self.fqn(target), 0) + 1
                self.temps.add(self._v(self.fqn(target)))
        if pairs is None:
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
        for n, srcs, _, info in pairs:  # a MERGE can write one column from several WHEN branches
            columns.setdefault(n, set()).update(srcs)
            d = details.setdefault(n, {"steps": [], "indirect": set()})
            d["indirect"] |= info["indirect"]
            d["steps"] += [st for st in info["steps"] if st not in d["steps"]]
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
        if (t.db in (SESSION, _DEFAULT) or self._cols(t) or name in self.fetched or self.provider is None
                or "INFORMATION_SCHEMA" in t.name.upper()):
            return
        self.fetched.add(name)
        try:
            info = self.provider.get_table(name)
        except Exception as e:  # permission denied, network, bad name: report it and keep going
            self.issue("error", f"Could not read schema of `{name}`: {str(e).splitlines()[0][:300]}", idx)
            return
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
            return [(c, {f"{self._v(self.fqn(src))}.{c}"}, typ, {"steps": [how], "indirect": set()})
                    for c, typ in cols.items()]
        declared = None
        if isinstance(stmt.this, exp.Schema):  # CREATE ... (a INT64, b STRING) [AS ...]
            declared = {c.name: c.args["kind"].sql(D) if c.args.get("kind") else "STRING" for c in stmt.this.expressions}
        query = stmt.expression
        if not isinstance(query, exp.Query):
            self._put(target, declared)
            return None
        pairs = self._columns(query, idx, list(declared) if declared else None, f"CREATE {self.fqn(target)}")
        names = [p[0] for p in pairs]
        dups = [n for n, c in Counter(names).items() if c > 1 and n != "*"]
        if dups:
            self.issue("error", f"Duplicate column names in `{self.fqn(target)}`: {', '.join(dups)}", idx)
        if declared or "*" not in names:  # an unexpanded * leaves the new table's columns unknown
            self._put(target, declared or {p[0]: p[2] for p in pairs})
        if stmt.kind == "VIEW" and self.expand_views:
            self.intermediate.add(self.fqn(target))
        return pairs

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
        base = exp.select().from_(target.copy()).join(using.copy(), on=stmt.args.get("on"))
        tcols = list(self._cols(target)) or None
        label = f"MERGE {self.fqn(target)}"
        pairs = []
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
                pairs += self._columns(exp.select(star).from_(using.copy()), idx, tcols, f"{label} INSERT ROW")
                continue
            else:  # DELETE
                continue
            if names is None:
                self.issue("warning", f"{label}: target schema unknown; INSERT without column list not mapped{self._no_schema()}", idx)
                continue
            q = base.copy().select(*(v.copy().as_(f"_c{i}") for i, v in enumerate(values)))
            if when.args.get("condition"):  # WHEN MATCHED AND <condition>
                q = q.where(when.args["condition"].copy())
            pairs += self._columns(q, idx, names, label)
        return pairs

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

    # ---- column lineage ---------------------------------------------------------------------

    def _columns(self, query, idx, target_cols=None, label=""):
        pairs = self._query_lineage(query, idx)
        if target_cols is None or not pairs:
            return pairs
        if any(p[0] == "*" for p in pairs):
            self.issue("warning", f"{label}: columns behind SELECT * are unknown, can't map them to the target", idx)
            return []
        if len(target_cols) != len(pairs):
            self.issue("error", f"{label}: query returns {len(pairs)} columns but target has {len(target_cols)}", idx)
            return []
        return [(tc, *rest) for tc, (_, *rest) in zip(target_cols, pairs)]

    def _qualify(self, query, mapping):
        """qualify(); names it can't resolve that are script variables (DECLARE / FOR) become values.
        That is BigQuery's own order: a name is a column if any source has it, else a variable."""
        base = query.copy()
        while True:
            try:
                # quote_identifiers=False keeps the expressions shown in `steps` readable
                return qualify(base.copy(), schema=mapping, dialect=D, validate_qualify_columns=True,
                               quote_identifiers=False)
            except SqlglotError as e:
                m = _UNRESOLVED.search(str(e))
                name, table = (m[1].lower(), (m[2] or "").lower()) if m else ("", "")
                hits = [c for c in base.find_all(exp.Column)
                        if (table and table in self.variables and c.table.lower() == table)
                        or (not table and name in self.variables and not c.table and c.name.lower() == name)]
                if not hits:
                    raise
                for c in hits:
                    c.replace(exp.Parameter(this=exp.Var(this=c.sql(D))))

    def _query_lineage(self, query, idx) -> list[tuple[str, set[str], str, dict]]:
        """Per output column: (name, source columns, type, {"steps": [...], "indirect": {...}})."""
        mapping = MappingSchema(self.schema, dialect=D)
        try:
            q = self._qualify(query, mapping)
        except SqlglotError as e:
            pos = _POSITION.search(str(e))
            severity, msg = self._explain(_POSITION.sub("", str(e)), query)
            self.issue(severity, msg, idx, *((int(pos[1]), int(pos[2])) if pos else ()))
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
            nodes = _sg_lineage(None, q, **kw)
        except Exception:  # one bad column fails the batch; redo column by column below
            nodes = {}
        # conds: per SELECT, shared by every column passing through it; nodes: sqlglot's to_node cache
        ctx = SimpleNamespace(scopes=scopes, mapping=mapping, conds={}, nodes={})
        unresolved = set()
        out = []
        for sel in q.selects:
            name = sel.alias_or_name
            srcs, steps, indirect = set(), [], set()
            try:
                node = nodes.get(name) or _sg_lineage(name, q, **kw)
            except Exception as e:  # sqlglot internals; keep the column's position, carry on
                self.issue("warning", f"Column lineage failed for `{name}`: {e}", idx)
            else:
                for n in node.walk():
                    if isinstance(n.expression, exp.Table):
                        srcs.add(self._col_key(n.expression, n.name))
                    elif isinstance(n.expression, exp.Placeholder):
                        unresolved.add(n.name)
                    elif isinstance(n.source, exp.Select) and id(n.source) in scopes:
                        s = scopes[id(n.source)]
                        shape, why = self._conditions(s, ctx)
                        expr = n.expression.this if isinstance(n.expression, exp.Alias) else n.expression
                        label = n.name if not n.name.isdigit() else n.expression.alias_or_name  # UNION branch: index
                        if id(n.source) in top:
                            label = None  # the statement's target column
                        elif s.scope_type == ScopeType.SUBQUERY:
                            label = f"(subquery).{label}"
                        step = {"label": label, "expression": expr.sql(D), **shape}
                        if step not in steps:
                            steps.append(step)
                        indirect |= why
            if name == "*":
                self.issue("warning", f"SELECT * not expanded: table schema unknown{self._no_schema()}", idx)
            typ = sel.type.sql(D) if sel.type and not sel.type.is_type(exp.DataType.Type.UNKNOWN) else "STRING"
            out.append((case.get(name.lower(), name), srcs, typ, {"steps": steps, "indirect": indirect}))
        if unresolved:
            self.issue("warning", f"Lineage stops at {', '.join(sorted(unresolved))}: source not found", idx)
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
        shape = {"from": [self.fqn(s) + (f" AS {a}" if a != s.name else "") if isinstance(s, exp.Table) else a
                          for a, (_, s) in scope.selected_sources.items()]}
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

    def _resolve(self, col: exp.Column, scope: Scope, ctx) -> set[str]:
        """Base table columns behind a column reference, looking outward for correlated references."""
        s = scope
        while s is not None and col.table not in s.sources:
            s = s.parent
        src = s.sources[col.table] if s is not None else None
        if isinstance(src, exp.Table):
            return {self._col_key(src, col.name)}
        if isinstance(src, Scope):  # a CTE / subquery column: its tables, plus the conditions that filtered it
            try:
                node = _to_node(col.name, src, D, ctx.mapping, trim_selects=False, _cache=ctx.nodes)
            except Exception:
                return set()
            leaves = {self._col_key(n.expression, n.name) for n in node.walk() if isinstance(n.expression, exp.Table)}
            return leaves | (self._conditions(src, ctx)[1] if isinstance(src.expression, exp.Select) else set())
        return set()

    def _explain(self, msg, query) -> tuple[str, str]:
        """(severity, message) for a qualify failure. Unresolved names are errors only when every schema is known."""
        m = _UNRESOLVED.search(msg)
        if not m:
            return "error", msg
        col, alias = m[1], m[2]
        tables = [t for t in query.find_all(exp.Table) if t.db]  # normalized real tables, not CTEs
        hits = [f"{t.alias_or_name} ({self.fqn(t)})" for t in tables if col.lower() in {c.lower() for c in self._cols(t)}]
        if len(hits) > 1 and not alias:
            return "error", f"Column `{col}` is ambiguous, present in: {', '.join(hits)}. Qualify it with a table alias."
        unknown = sorted({self.fqn(t) for t in tables if not self._cols(t)})
        if unknown:
            return "warning", f"Column `{col}` not resolved: schema unknown for {', '.join(unknown)}{self._no_schema()}"
        if alias:
            return "error", f"`{alias}.{col}`: `{alias}` is not a table alias or column in this query"
        return "error", f"Column `{col}` not found in any source of this query"
