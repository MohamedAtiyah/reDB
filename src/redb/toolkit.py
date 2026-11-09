"""Toolkit: the single façade over every Db2 operation the agent performs (one connection)."""
from __future__ import annotations

from typing import Any

from . import advisor, explain, ledger, monitor, verify
from .config import Settings, get_settings
from .db import Db
from .memory import Memory, hash_embed


class Toolkit:
    def __init__(self, settings: Settings | None = None, embed=None, db: Db | None = None):
        self.s = settings or get_settings()
        self.schema = self.s.target_schema
        self.db = db or Db(self.s)
        explain.ensure_explain_tables(self.db)
        self.memory = Memory(self.db, embed or (lambda t: hash_embed(t, self.s.embed_dim)),
                             self.s.embed_dim)
        self.memory.ensure()

    # Observe
    def top_statements(self, limit: int = 8) -> list[dict[str, Any]]:
        return monitor.top_statements(self.db, self.schema, limit)

    def health(self) -> dict[str, Any]:
        return monitor.health(self.db, self.schema)

    # Diagnose
    def explain(self, sql: str) -> dict[str, Any]:
        plan = explain.explain(self.db, sql)
        plan["summary"] = explain.summarize(plan)
        plan["dot"] = explain.to_dot(plan)
        plan["nodes"] = {str(k): v for k, v in plan["nodes"].items()}  # JSON/checkpoint friendly
        return plan

    def explain_cost(self, sql: str) -> float:
        return explain.explain(self.db, sql)["total_cost"]

    def catalog(self, tables: list[str]) -> dict[str, Any]:
        lines, not_null = [], set()
        for t in tables:
            schema, _, name = t.partition(".")
            cols = self.db.query("""SELECT COLNAME, TYPENAME, LENGTH, NULLS FROM SYSCAT.COLUMNS
                                    WHERE TABSCHEMA = ? AND TABNAME = ? ORDER BY COLNO""", (schema, name))
            card = self.db.scalar("SELECT CARD FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = ?", (schema, name))
            idx = self.db.query("""SELECT INDNAME, COLNAMES, UNIQUERULE FROM SYSCAT.INDEXES
                                   WHERE TABSCHEMA = ? AND TABNAME = ?""", (schema, name))
            lines.append(f"{t} (card={card}): " + ", ".join(
                f"{c['COLNAME']} {c['TYPENAME']}({c['LENGTH']}){'' if c['NULLS'] == 'Y' else ' NOT NULL'}" for c in cols))
            lines += [f"  index {i['INDNAME'].strip()} {i['COLNAMES']} ({ {'P': 'primary key', 'U': 'unique'}.get(i['UNIQUERULE'], 'non-unique') })"
                      for i in idx]
            not_null |= {c["COLNAME"] for c in cols if c["NULLS"] == "N"}
        return {"text": "\n".join(lines), "not_null": sorted(not_null)}

    # Prescribe / prove
    def recommend_indexes(self, sql: str) -> dict[str, Any]:
        return advisor.recommend(self.db, sql, self.schema)

    def prove_equivalent(self, original: str, rewrite: str) -> dict[str, Any]:
        return verify.prove(self.db, original, rewrite)

    def timed(self, sql: str, runs: int = 3) -> float:
        return self.db.timed(sql, runs)

    def runstats_sql(self, table: str) -> str:
        schema, _, name = table.partition(".")
        if not (advisor.valid_ident(schema) and advisor.valid_ident(name)):
            raise ValueError(f"unsafe table name {table!r}")
        return (f"CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE {schema}.{name} "
                "WITH DISTRIBUTION AND DETAILED INDEXES ALL')")

    def run_ddl(self, ddl: str) -> None:
        self.db.execute(ddl)

    # Ledger
    def ledger_record(self, **kw) -> int:
        return ledger.record(self.db, **kw)

    def ledger_status(self, change_id: int, status: str, ms_after: float | None = None) -> None:
        ledger.set_status(self.db, change_id, status, ms_after)

    def ledger_current(self):
        return ledger.current(self.db)

    def ledger_timeline(self):
        return ledger.timeline(self.db)

    def ledger_as_of(self, ts: str):
        return ledger.as_of(self.db, ts)

    def rollback(self, change_id: int) -> str:
        return ledger.rollback(self.db, change_id)

    # Memory
    def recall(self, sig: str, k: int = 3):
        return self.memory.search(sig, k)

    def remember(self, sig: str, sql: str, kind: str, detail: str, gain: float):
        return self.memory.remember(sig, sql, kind, detail, gain)
