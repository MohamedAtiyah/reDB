"""Prescribe + what-if proof: Db2's index advisor with virtual indexes."""
from __future__ import annotations

import hashlib
import re
from typing import Any

IDENT = re.compile(r"^[A-Z0-9_#@$]+$")


def valid_ident(name: str) -> bool:
    return bool(name) and len(name) <= 128 and bool(IDENT.match(name))


def parse_colnames(colnames: str) -> list[tuple[str, str]]:
    """'+CUSTOMER_ID-ORDER_DATE' -> [('CUSTOMER_ID','ASC'), ('ORDER_DATE','DESC')]."""
    cols = re.findall(r"([+-])([^+-]+)", (colnames or "").strip())
    if not cols:
        raise ValueError(f"cannot parse COLNAMES {colnames!r}")
    return [(c.strip(), "ASC" if sign == "+" else "DESC") for sign, c in cols]


def index_ddl(schema: str, table: str, cols: list[tuple[str, str]]) -> tuple[str, str, str]:
    """Return (index_name, create_ddl, undo_ddl). Raises on any unsafe identifier."""
    for ident in [schema, table, *[c for c, _ in cols]]:
        if not valid_ident(ident):
            raise ValueError(f"unsafe identifier {ident!r}")
    h = hashlib.sha1(f"{schema}.{table}:{cols}".encode()).hexdigest()[:6].upper()
    name = f"REDB_IX_{table[:40]}_{h}"
    collist = ", ".join(f'"{c}" {d}' for c, d in cols)
    return (name, f'CREATE INDEX "{schema}"."{name}" ON "{schema}"."{table}" ({collist})',
            f'DROP INDEX "{schema}"."{name}"')


def has_parameter_markers(sql: str) -> bool:
    bare = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return "?" in bare or bool(re.search(r"(?<![\w:]):[A-Za-z_]\w*", bare))


def recommend(db, sql: str, schema: str) -> dict[str, Any]:
    """Ask Db2 for virtual indexes. Returns est cost with them and the DDL to build them."""
    if has_parameter_markers(sql):
        return {"skipped": "statement has parameter markers", "indexes": [], "cost_with_indexes": None}
    marker = db.scalar("SELECT CHAR(CURRENT TIMESTAMP) FROM SYSIBM.SYSDUMMY1")
    try:
        db.exec_immediate("SET CURRENT EXPLAIN MODE RECOMMEND INDEXES")
        try:
            db.exec_immediate(sql)  # compiled, not executed: SQL0217W is expected
        except Exception as e:
            if "SQL0217W" not in str(e):
                raise
    finally:
        db.exec_immediate("SET CURRENT EXPLAIN MODE NO")
    rows = db.query("""SELECT TOTAL_COST FROM EXPLAIN_STATEMENT
                       WHERE EXPLAIN_LEVEL = 'P' AND EXPLAIN_TIME >= TIMESTAMP(CAST(? AS VARCHAR(32)))
                       ORDER BY EXPLAIN_TIME DESC FETCH FIRST 1 ROW ONLY""", (marker,))
    cost = float(rows[0]["TOTAL_COST"]) if rows else None
    advised = db.query("""SELECT NAME, TBCREATOR, TBNAME, COLNAMES, UNIQUERULE FROM ADVISE_INDEX
                          WHERE EXISTS = 'N' AND EXPLAIN_TIME >= TIMESTAMP(CAST(? AS VARCHAR(32)))""", (marker,))
    indexes, seen = [], set()
    for r in advised:
        tbschema, table = r["TBCREATOR"].strip(), r["TBNAME"].strip()
        if tbschema.upper() != schema.upper():
            continue
        cols = parse_colnames(r["COLNAMES"])
        name, ddl, undo = index_ddl(tbschema, table, cols)
        if name in seen:
            continue
        seen.add(name)
        indexes.append({"name": name, "table": f"{tbschema}.{table}", "columns": cols, "ddl": ddl, "undo": undo})
    return {"skipped": None, "indexes": indexes, "cost_with_indexes": cost}
