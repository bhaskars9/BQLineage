"""Every sqlglot internal bqlineage relies on, in one place.

sqlglot's lineage helper `to_node` and its column `Resolver` aren't public API, and two of its BigQuery behaviours
need working around. tests/test_compat.py pins each assumption, so a sqlglot upgrade that changes one fails there
first instead of showing up as wrong lineage somewhere else.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlglot.dialects.dialect import Dialect
from sqlglot.lineage import lineage, to_node  # noqa: F401  (to_node: internal, used to trace one CTE column)
from sqlglot.optimizer import qualify_columns as _qc
from sqlglot.tokens import TokenType

D = "bigquery"
BQ = Dialect.get_or_raise(D)
PSEUDOCOLUMNS = BQ.PSEUDOCOLUMNS  # _PARTITIONTIME, _TABLE_SUFFIX, ...


class Tokenizer(BQ.tokenizer_class):
    # sqlglot tokenizes these as COMMAND and swallows the rest of the statement into one string token,
    # hiding the SQL inside procedural blocks. As plain words, script.split() can peel them off instead.
    KEYWORDS = {
        **BQ.tokenizer_class.KEYWORDS,
        **dict.fromkeys(("BEGIN", "ELSEIF", "EXCEPTION", "LOOP", "REPEAT", "WHILE"), TokenType.VAR),
    }


def tokenize(sql: str):
    return Tokenizer(dialect=BQ).tokenize(sql)


class _Resolver(_qc.Resolver):
    # 1. BigQuery lets a CTE repeat a column name - e.g. SELECT * (or c1.*, c2.*) over a USING join whose sides
    #    share non-key columns like load_ts. sqlglot then refuses to expand any SELECT * over that CTE, which cuts
    #    lineage for every column after it. Listing each name once lets the star expand; only the repeated name
    #    itself (which BigQuery won't let you reference anyway) maps to its first occurrence.
    # 2. A source whose columns are unknown (an unexpanded *) is reported as having no known columns. sqlglot
    #    treats the two alike everywhere except USING expansion, which raised "Cannot automatically join" when
    #    the right side was SELECT * over a table without a schema.
    def get_source_columns(self, name, only_visible=False):
        columns = super().get_source_columns(name, only_visible)
        return [] if "*" in columns else list(dict.fromkeys(columns))


@contextmanager
def star_friendly_resolver():
    """Use _Resolver for our own qualify() calls only; other sqlglot users in the process are unaffected."""
    original, _qc.Resolver = _qc.Resolver, _Resolver
    try:
        yield
    finally:
        _qc.Resolver = original
