## Code Review Results

**Reviewed:** 2026-10-13, using `ce:review` in Codex.  
**Scope:** follow-up to the full stacked-PR review, from `4f397da87fc731efe9fe940b0b1c3c3bde901b37` to pushed head **`2b9832c65b300048ae09cc96561b47cb4f93c1f0`** of [PR #13](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/13). Four new commits change 20 paths, with 645 insertions and 125 deletions, including the previous review document. Local HEAD matches GitHub's PR head.  
**Intent:** independently verify CR01–CR07, check the fixes for correctness, security, performance and test regressions, and confirm the earlier Ponytail simplifications remain applied.  
**Mode:** report-only. The user's requested saved report is the explicit exception to the skill's no-artifact rule. This review changed no implementation or tests and created no todos, internal review artifacts, commits, pushes or GitHub comments. Untracked `.claude/` content is excluded.

**Result:** all seven actionable findings are resolved. No new actionable finding was identified in the follow-up diff. All twelve earlier Ponytail recommendations remain applied.

**Reviewers:** correctness, testing, maintainability, project-standards, agent-native and learnings; security, performance, API contract, reliability, adversarial, Python and frontend races. Migration coverage was reviewed for invalid writes and populated upgrade/downgrade. Following the supplied AGENTS mapping, these were sequential review lenses in the main chat; no independent-agent agreement boost was used.

- Security/API contract: actual request-byte limits and replay after configuration changes.
- Reliability/adversarial: lost publication confirmations, results arriving before publication errors, stale release and untouched batch remainder.
- Performance: payload projection and transaction isolation during concurrent status reads.
- Testing/migrations: regression assertions, all four enum constraints, nullable values and populated rollback.
- Frontend races: submission key retention across a lost response and replacement when the body changes.

### Findings

None at P0–P3. No unresolved actionable work is routed to a fixer or downstream resolver.

### Previous findings: verification at the pushed head

Source: the historical findings in [round two](2026-10-13-ce-review-round-two.md). This report supplies the independent verification of that document's newly added outcome paragraph; its original finding tables and verdict describe the older `4f397da87fc7` head.

| Finding | Original severity / origin | Status | Fix and verification |
|---|---|---|---|
| CR01 — understated request length bypasses the body cap | P1 / #13 | Resolved | `services/orchestrator/src/orchestrator/app/main.py:14` uses Starlette's `RequestBodyLimitMiddleware` with a 64 KiB limit. The committed real-Uvicorn test at `services/orchestrator/tests/app/test_http_server.py:23` sends chunked data with `Content-Length: 1`, expects 413 and verifies that no order is stored. API tests cover the exact boundary, one byte over it and actual streamed bytes. All pass. |
| CR02 — ambiguous publication reuses an attempt identity | P1 / #13 | Resolved | `packages/bus/src/bus/port.py:30` distinguishes provably unsent `NotPublished` failures. `services/orchestrator/src/orchestrator/app/dispatcher.py:73` releases that attempt only for this exception; other failures retain its identity/deadline and release only the untouched remainder. The RabbitMQ precheck raises `NotPublished` before sending, while later errors remain conservative. Tests cover enqueue-then-timeout, rejection of the old result after the next attempt, settlement before a lost confirmation and both batch-remainder cases. The shared bus contract checks publication on a closed bus. |
| CR03 — release after settlement reschedules the order | P1 / #13 | Resolved | `services/orchestrator/src/orchestrator/adapters/memory_store.py:63` returns when no open attempt was removed; the Postgres adapter at `:176` checks the deleted row count before updating the order. The shared contract at `tests/contract/test_store_contract.py:263` compares the entire order/history after successful, rejected, retry and timed-out settlement and checks an unrelated due order remains claimable. Both adapters pass. An additional Postgres probe independently confirmed stale release leaves the completed order/history unchanged. |
| CR04 — current configuration rejects an existing replay | P2 / #6, extended in #13 | Resolved | `services/orchestrator/src/orchestrator/app/api.py:44` looks up an existing key before validating today's stages/caps; the race fallback uses the same `_replay` helper at `:81`. Tests verify replay after removal of a scripted stage and after lowering the attempt cap, conflict for a changed body, and continued validation for a new key. |
| CR05 — status loads the order payload once per attempt | P2 / #13 | Resolved | `services/orchestrator/src/orchestrator/adapters/postgres_store.py:130` performs separate order/history selects in one `REPEATABLE READ` transaction. A disposable Postgres probe committed a settlement between those reads: the returned pair consistently contained the prior running order and open attempt, while a later ordinary read saw completion. Captured statements confirmed two queries and only one order-payload projection. This verifies query shape and consistency, not a latency benchmark. |
| CR06 — browser retry generates a different key | P2 / #8 | Resolved | `services/orchestrator/src/orchestrator/static/index.html:186,203` retains the serialized body/key pair across failures. A Node harness executed the actual page script with mocked DOM/fetch: two unchanged submissions after lost responses had identical keys/bodies; changing the body produced a different key. This validates submission behavior, not visual rendering. |
| CR07 — enum checks lack enforcement/rollback tests | P2 / #13, schema introduced in #4 | Resolved | `tests/contract/test_migration.py:87` rejects invalid values for all four enum columns with the expected constraint names. The populated upgrade/downgrade test at `:107` preserves order/attempt data, accepts the nullable fields and verifies the checks are removed on downgrade. These tests run against real Postgres and pass. |

The publication fix deliberately favors preserving a possibly delivered attempt over assuming an exception proves non-delivery. This prevents identity reuse at the cost of waiting for its existing deadline after an ambiguous failure; that tradeoff is now stated in the bus contract and design documentation.

### Ponytail follow-up

The twelve cuts from [the original Ponytail review](2026-10-13-ponytail-stack-review.md) remain present: shared Docker targets; directional transition handling; dataclass insert values; shared overdue-order projection; channel context managers; Pydantic attempt projection; Result identity reuse; `aclosing` fixture; command identity projection; direct runner handler; text-node helper; and native CI matrix.

CR05 retains the overdue-query projection while removing the separate status endpoint's repeated payload projection. CR01 uses the installed framework middleware instead of keeping a custom body limiter. The prior [library evidence](2026-10-13-ponytail-library-evidence.md) remains the simplification reference; this follow-up introduces no new library replacement recommendation.

### Requirements Completeness

The plan identified in round two remains the requirements reference. The qualifications attached to R7 (stale-result protection), R9 (replay), R11–R14 (terminal scheduling), R15–R16 (status consistency/cost) and R17 (submission retry) are addressed by the verified fixes above. No planned implementation unit was removed by these commits. This is a review of the corrective diff and its interactions, building on the prior full-stack review; it does not claim a second independent read of every unchanged stack file.

### Learnings & Past Solutions

- The round-two probes supplied the failure cases; this pass verified the corrective implementation independently of the author's outcome summary.
- No `docs/solutions/` knowledge base exists. The earlier review and Ponytail reports provide the available project context.
- REST parity remains intact: the page's submit/status actions continue to use the same public API available to an agent. No additional agent-native gap was found.

### Coverage

| Verification | Result |
|---|---|
| Local tests without Docker: `pytest -m 'not contract and not e2e' -q` | **181 passed**, 59 deselected; includes the real-HTTP body-limit regression and memory contract variants |
| Local Docker-backed adapter contracts: `pytest -m contract -q` | **44 passed**, 196 deselected; Postgres, RabbitMQ and migration checks |
| Local lint / format | Clean; **70 files** already formatted |
| Actual page script, mocked DOM/fetch | Unchanged retry retains key/body; changed body gets a new key |
| Additional disposable Postgres interleaving probe | Consistent status snapshot during settlement; payload selected once; stale release leaves completed state/history intact |
| GitHub CI at exact reviewed head | **All four jobs succeeded:** lint/format, unit, adapter contracts and end-to-end |

CI evidence: [run 38421327051](https://github.com/HubDev-AI/flowforge-order-orchestrator/actions/runs/38421327051), associated with `2b9832c65b300048ae09cc96561b47cb4f93c1f0`. End-to-end verification here comes from CI; that suite was not rerun locally in this follow-up. The local passed counts total 225; deselected counts are exclusions, not additional tests.

- No surviving findings, malformed findings or below-confidence candidates; no independent-agreement boost. No reviewer-lens failure affected the verdict.
- The browser retry and concurrent status interleaving probes were review-time checks, not new committed regression tests. Preserving them in the suite would improve future protection; their absence does not reopen a reproduced defect after the implementation passed these checks.
- Full visual browser comparison and a dependency-vulnerability database audit were not performed. Existing documented local-demo constraints from round two remain outside this corrective diff.
- No implementation fixes were applied by this review. No deployment or merge was performed.

---

> **Verdict:** Ready to merge.
>
> **Reasoning:** All seven previous findings are resolved at the pushed PR #13 head, the corrective diff introduces no confirmed actionable regression, targeted local checks pass, and all CI jobs pass at that same head. The twelve Ponytail simplifications remain applied.
