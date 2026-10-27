"""Splitting a BigQuery script into statements, including the SQL inside procedural blocks."""
from __future__ import annotations

from sqlglot.tokens import TokenType

NO_LINEAGE = {"END", "LEAVE", "BREAK", "ITERATE", "CONTINUE", "RETURN", "RAISE", "UNTIL"}


def kw(tok) -> str:
    """Upper-cased token text when it can be a keyword (not a string or quoted identifier)."""
    quoted = tok.token_type == TokenType.IDENTIFIER or tok.token_type.name.endswith("STRING")
    return "" if quoted else tok.text.upper()


def _after(chunk, word):
    """Tokens after the first top-level `word` (THEN / DO), skipping brackets and CASE..END; None if absent."""
    depth = 0
    for i, tok in enumerate(chunk[1:], 1):
        k = kw(tok)
        if tok.token_type in (TokenType.L_PAREN, TokenType.L_BRACKET) or k == "CASE":
            depth += 1
        elif tok.token_type in (TokenType.R_PAREN, TokenType.R_BRACKET) or (k == "END" and depth):
            depth -= 1
        elif k == word and not depth:
            return chunk[i + 1:]
    return None


def _params(chunk) -> list[str]:
    """Parameter names of CREATE PROCEDURE p(IN a INT64, OUT b STRUCT<x INT64, y STRING>, c STRING)."""
    start = next((i for i, t in enumerate(chunk) if t.token_type == TokenType.L_PAREN), None)
    names, depth, expect = [], 0, True
    for t in chunk[start + 1:] if start is not None else ():
        if t.token_type == TokenType.L_PAREN or t.text == "<":
            depth += 1
        elif t.token_type == TokenType.R_PAREN or t.text in (">", ">>"):
            if depth == 0:
                break
            depth -= len(t.text) if t.text != ")" else 1
        elif t.token_type == TokenType.COMMA and depth == 0:
            expect = True
        elif expect and kw(t) not in ("IN", "OUT", "INOUT"):
            names.append(t.text)
            expect = False
    return names


def split(tokens):
    """Split tokens on ';' and peel BigQuery procedural wrappers (BEGIN, IF/ELSEIF..THEN, ELSE, LOOP, REPEAT,
    WHILE..DO, FOR..DO, CASE/WHEN..THEN, EXCEPTION WHEN ERROR THEN, labels, CREATE PROCEDURE ... BEGIN) so the
    SQL inside blocks is analyzed like straight-line code. Yields (statement tokens, names it introduces as
    script variables: a FOR-loop variable, procedure parameters)."""
    chunk = []
    for tok in [*tokens, None]:
        if tok is not None and tok.token_type != TokenType.SEMICOLON:
            chunk.append(tok)
            continue
        names = []
        while chunk:
            k = kw(chunk[0])
            if len(chunk) > 2 and chunk[1].token_type == TokenType.COLON:  # label: BEGIN / LOOP / ...
                rest = chunk[2:]
            elif k == "BEGIN" and len(chunk) > 1 and kw(chunk[1]) != "TRANSACTION":
                rest = chunk[1:]
            elif k in ("ELSE", "LOOP", "REPEAT"):
                rest = chunk[1:]
            elif k in ("IF", "ELSEIF", "CASE", "WHEN", "EXCEPTION"):
                rest = _after(chunk, "THEN")
            elif k in ("WHILE", "FOR"):
                rest = _after(chunk, "DO")
                names += [chunk[1].text] if k == "FOR" else []
            elif k == "CREATE" and any(kw(t) == "PROCEDURE" for t in chunk[:5]):  # the body is the SQL
                rest = _after(chunk, "BEGIN")
                names += _params(chunk)
            else:
                break
            if rest is None:
                break
            chunk = rest
        if chunk and kw(chunk[0]) not in NO_LINEAGE:  # END IF, LEAVE, ... carry no lineage
            yield chunk, names
        chunk = []
