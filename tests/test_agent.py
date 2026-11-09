"""The LangGraph agent with a FakeToolkit: pauses before apply and applies only approved proposals."""
from redb.agent import build_graph, graph_dot, resume, run_until_approval
from redb.config import Settings
from redb.llm import Brain

W1 = "SELECT ORDER_ID FROM RETAIL.ORDERS WHERE CUSTOMER_ID = 4242"
W3 = "SELECT CHANNEL, COUNT(*) FROM RETAIL.ORDERS WHERE YEAR(ORDER_DATE) = 2024 AND MONTH(ORDER_DATE) = 3 GROUP BY CHANNEL"


class FakeToolkit:
    schema = "RETAIL"

    def __init__(self):
        self.ddl_run, self.ledger, self.memory = [], {}, []

    def top_statements(self, limit):
        return [{"sql": s, "avg_ms": 20.0, "executions": 5, "rows_read": 2_000_000, "rows_returned": 5,
                 "read_per_returned": 400_000, "total_ms": 100.0} for s in (W1, W3)][:limit]

    def health(self):
        return {"bufferpool_hit_pct": 99.0, "tables": [], "table_scans": 10, "unused_indexes": [],
                "stale_tables": ["PRODUCTS"]}

    def explain(self, sql):
        return {"total_cost": 800.0, "summary": "#1 RETURN\n  #2 TBSCAN on RETAIL.ORDERS",
                "dot": "digraph{}", "flags": [{"op": 2, "kind": "TABLE_SCAN", "detail": "full scan of RETAIL.ORDERS"}],
                "tables": ["RETAIL.ORDERS"], "operators": ["RETURN", "TBSCAN"], "nodes": {}}

    def explain_cost(self, sql):
        return 790.0

    def catalog(self, tables):
        return {"text": "RETAIL.ORDERS ...", "not_null": ["CUSTOMER_ID", "ORDER_DATE"]}

    def recommend_indexes(self, sql):
        col = "CUSTOMER_ID" if "CUSTOMER_ID" in sql else "ORDER_DATE"
        return {"skipped": None, "cost_with_indexes": 10.0, "indexes": [{
            "name": f"REDB_IX_ORDERS_{col}", "table": "RETAIL.ORDERS", "columns": [(col, "ASC")],
            "ddl": f'CREATE INDEX "RETAIL"."REDB_IX_ORDERS_{col}" ON "RETAIL"."ORDERS" ("{col}" ASC)',
            "undo": f'DROP INDEX "RETAIL"."REDB_IX_ORDERS_{col}"'}]}

    def prove_equivalent(self, a, b):
        return {"proven": True, "detail": "EXCEPT ALL both ways: 0 / 0 differing rows"}

    def timed(self, sql, runs=3):
        if "ORDER_DATE >=" in sql:
            return 15.0
        return 2.0 if any(d.startswith("CREATE INDEX") for d in self.ddl_run) else 20.0

    def runstats_sql(self, table):
        return f"CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE {table} WITH DISTRIBUTION AND DETAILED INDEXES ALL')"

    def run_ddl(self, ddl):
        self.ddl_run.append(ddl)

    def ledger_record(self, **kw):
        cid = len(self.ledger) + 1
        self.ledger[cid] = dict(kw)
        return cid

    def ledger_status(self, cid, status, ms_after=None):
        self.ledger[cid]["status"] = status

    def recall(self, sig, k=3):
        return []

    def remember(self, *a):
        self.memory.append(a)


def make():
    tk = FakeToolkit()
    return tk, build_graph(tk, Brain(Settings(google_api_key="")))


def test_pauses_before_apply_and_applies_only_approved():
    tk, graph = make()
    state = run_until_approval(graph, "t1")
    assert graph.get_state({"configurable": {"thread_id": "t1"}}).next == ("apply",)
    assert tk.ddl_run == [] and tk.ledger == {}  # nothing touched before approval
    kinds = sorted(p["kind"] for p in state["proposals"])
    assert kinds.count("INDEX") == 2 and "REWRITE" in kinds and "RUNSTATS" in kinds
    w1_index = next(p for p in state["proposals"] if p["kind"] == "INDEX" and p["case_id"] == state["cases"][0]["case_id"])
    assert w1_index["verified"] and w1_index["est_cost_after"] == 10.0

    final = resume(graph, "t1", [w1_index["id"]])
    creates = [d for d in tk.ddl_run if d.startswith("CREATE INDEX")]
    assert creates == w1_index["ddl"]                       # only the approved one
    assert any("RUNSTATS" in d for d in tk.ddl_run)         # stats refreshed after the index
    assert len(tk.ledger) == 1 and tk.ledger[1]["status"] == "APPLIED" and tk.ledger[1]["kind"] == "INDEX"
    assert tk.ledger[1]["undo_ddl"].startswith("DROP INDEX")
    assert final["applied"][0]["gain_pct"] == 90.0
    assert len(tk.memory) == 1
    assert final["report"]["reduction_pct"] is not None


def test_rewrite_is_guarded_and_adopted():
    tk, graph = make()
    state = run_until_approval(graph, "t2")
    rw = next(p for p in state["proposals"] if p["kind"] == "REWRITE")
    assert "ORDER_DATE >= '2024-03-01'" in rw["rewrite_sql"] and rw["verified"]
    resume(graph, "t2", [rw["id"]])
    assert tk.ddl_run == [] and tk.ledger[1]["status"] == "ADOPTED"


def test_nothing_approved_changes_nothing():
    tk, graph = make()
    run_until_approval(graph, "t3")
    final = resume(graph, "t3", [])
    assert tk.ddl_run == [] and tk.ledger == {} and final["applied"] == []


def test_unverified_proposals_are_never_applied():
    tk, graph = make()
    tk.recommend_indexes = lambda sql: {"skipped": None, "cost_with_indexes": 900.0, "indexes": [{
        "name": "REDB_IX_X", "table": "RETAIL.ORDERS", "columns": [], "ddl": "CREATE INDEX X", "undo": "DROP INDEX X"}]}
    state = run_until_approval(graph, "t4")
    bad = [p for p in state["proposals"] if p["kind"] == "INDEX"]
    assert bad and not any(p["verified"] for p in bad)
    resume(graph, "t4", [p["id"] for p in bad])
    assert not any(d.startswith("CREATE INDEX") for d in tk.ddl_run)


def test_equivalent_but_slower_rewrite_is_rejected():
    tk, graph = make()
    tk.explain_cost = lambda sql: 800.0
    tk.timed = lambda sql, runs=3: 25.0 if "ORDER_DATE >=" in sql else 20.0
    state = run_until_approval(graph, "t5")
    rw = next(p for p in state["proposals"] if p["kind"] == "REWRITE")
    assert rw["equivalent"] and not rw["verified"] and "REJECTED" in rw["evidence"]


def test_graph_dot():
    _, graph = make()
    dot = graph_dot(graph)
    assert '"recall" -> "apply"' in dot and "human approval" in dot
