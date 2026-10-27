"""Fill template placeholders before parsing: {{ name }} / {{ params.name }} (Airflow, dbt, Jinja) and ${name}."""
from __future__ import annotations

import re

_JINJA = re.compile(r"\{\{\s*(.*?)\s*\}\}")
_SHELL = re.compile(r"\$\{\s*([A-Za-z_][\w.]*)\s*\}")
_TAG = re.compile(r"\{%.*?%\}", re.S)
_NAME = re.compile(r"[A-Za-z_]\w*(?:\.\w+)*")
_TRIPLE = re.compile(r"(r?(?:\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''))", re.I)  # e.g. JavaScript UDF bodies


def placeholder(expr: str) -> str:
    """The name left where a placeholder has no value: keeps the SQL parseable and says what's missing."""
    name = expr.split(".")[-1] if _NAME.fullmatch(expr) else expr
    return "__" + (re.sub(r"\W+", "_", name).strip("_")[:40] or "value") + "__"


def render(sql: str, vars: dict[str, str] | None = None) -> tuple[str, list[str], bool]:
    """(SQL with placeholders filled, placeholders that had no value, whether {% %} template logic was removed).

    A placeholder's value is looked up by its full name, then by its last part, so {{ params.project }} takes
    "params.project" or "project". Template logic ({% if %} ...) is removed, not evaluated; line numbers are kept.
    """
    vars = vars or {}
    missing: list[str] = []

    def fill(m):
        expr = m.group(1).strip()
        if _NAME.fullmatch(expr):
            for key in (expr, expr.split(".")[-1]):
                if key in vars:
                    return str(vars[key])
        missing.append(expr)
        return placeholder(expr)

    tags = bool(_TAG.search(sql))
    sql = _TAG.sub(lambda m: "\n" * m.group(0).count("\n"), sql)
    parts = _TRIPLE.split(sql)  # odd parts are triple-quoted strings: ${...} there is JavaScript, not a template
    for i in range(0, len(parts), 2):
        parts[i] = _SHELL.sub(fill, _JINJA.sub(fill, parts[i]))
    for i in range(1, len(parts), 2):
        parts[i] = _JINJA.sub(fill, parts[i])
    return "".join(parts), list(dict.fromkeys(missing)), tags
