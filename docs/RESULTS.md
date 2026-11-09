# Measured results

Environment: Db2 Community Edition **v12.1.5.0** in Docker (`icr.io/db2_community/db2:latest`, linux/amd64),
4 vCPU / 15 GB host, `--scale 1.0` (100k customers, 2k products, 400k orders, 1.2M order items),
offline mode (no Gemini key; deterministic heuristics). All numbers below come from
`python -m redb setup --reset && python -m redb workload --repeat 10 && python -m redb analyze --approve-all`.
Latency is the **median of 3 timed runs after 1 warm-up, all rows fetched**, measured by reDB itself.

## Virtual-index proof (before anything is built)

Estimated cost from Db2's optimizer, `CURRENT EXPLAIN MODE RECOMMEND INDEXES` (timerons):

| Stmt | What it does | Cost now | Cost with advised (virtual) indexes | Est. gain |
|---|---|---:|---:|---:|
| W1 | last 20 orders of one customer | 806.5 | 7.1 | 99% |
| W2 | revenue of one product (join) | 1,708.4 | 14.3 | 99% |
| W3 | March 2024 revenue by channel (`YEAR()`/`MONTH()`) | 836.5 | 89.8 | 89% |
| W4 | pending June-2025 orders + customer, top 50 | 1,154.3 | 343.5 | 70% |
| W5 | enterprise customers without 2025 orders (`NOT IN`) | 1,139.3 | 471.3 | 59% |
| W6 | primary-key lookup (control) | 14.1 | - | left alone (< 100 timerons) |

## Measured after applying the approved indexes (run 1)

| Stmt | Before (ms) | After (ms) | Gain |
|---|---:|---:|---:|
| W2 | 35.4 | 0.5 | 98.6% |
| W5 | 24.7 | 10.0 | 59.4% |
| W4 | 24.0 | 9.0 | 62.4% |
| W3 | 18.1 | 3.2 | 82.6% |
| W1 | 14.6 | 0.4 | 97.2% |
| W6 (control) | 0.5 | 0.3 | noise |
| **Total** | **117.3** | **23.5** | **80.0%** |

Repeat runs: 112.3 → 21.9 ms (80.5%), and 165.3 → 22.0 ms (86.7%) in the Streamlit run
(screenshot `docs/screenshots/04-results.png`). Sub-millisecond statements (W6) are within timing noise.

## Rewrites: proven equivalent, then honestly rejected

| Stmt | Rewrite | Equivalence (EXCEPT ALL both ways) | EXPLAIN cost | Measured | Verdict |
|---|---|---|---|---|---|
| W3 | `YEAR()/MONTH()` → half-open date range | 0 / 0 differing rows | 836.5 → 834.1 | 18.1 → 26.3 ms / 18.0 → 17.6 ms | rejected: no gain |
| W5 | `NOT IN` → `NOT EXISTS` | 0 / 0 differing rows | 1,139.3 → 1,139.3 | 24.7 → 24.7 ms | rejected: no gain |

Db2 12.1's optimizer already turns the `NOT IN` on NOT NULL columns into an anti-join and handles the
date predicates, so the classic textbook rewrites buy nothing here. reDB's prove step requires
**equivalent AND faster** (cheaper plan by ≥1% or ≥5% faster measured), so it refused to "fix" these.
This is the point of the project: LLM advice is not trusted until Db2's own numbers back it.
