# Code review of the pull request stack, 2026-10-13

**Verdict: ready with fixes. No blocker. 5 should-fix (1 documentation, 4 code), 15 advisory.**

## Outcome

Resolved on the review-fixes branch after this report was written. Fixed: 1, 2, 3, 4, 5, 6, 8, 10, 11, 12, 13, 14, 15, 16, 19, 20, and the class and clock renames of 17. Recorded in `docs/design.md` as accepted: 7 (a store error while settling acknowledges the result), 9 (batch-atomic sweeps), 18 (the test-only handler ships in the image), and the `orders.payload` column name from 17. The twelve cuts of the simplification review in `2026-10-13-ponytail-stack-review.md` were applied on the same branch.

Reviewed head: `fd47c6d48a9644bff4e85969eb595cfd4beff9c3` (PR 12, `docs/ai-conversation-logs`). Base: `6a4fe499401c087036325c379f2a71197a430bbd` (`main`, the merge base). The twelve PRs are a linear stack, PR 1 on `main` and each next PR on the previous head, so the stack diff is `main..fd47c6d`: 95 files, 32,498 added lines, of which 1,911 are executable non-test lines (the rest is docs, conversation logs, `uv.lock`, tests, config). Every PR is a draft; none has review comments. Fixes land in a separate PR on top of PR 12, as agreed.

## How this was verified

- `uv run ruff check . && uv run ruff format --check .`: clean, 67 files formatted.
- `uv run pytest -m "not contract and not e2e"`: 159 passed locally on `fd47c6d`.
- Contract and end-to-end suites were not run locally (Docker on this machine hangs on image pulls). They ran in CI on PR 10's head `443ddf0`, run <https://github.com/HubDev-AI/flowforge-order-orchestrator/actions/runs/38408036229>, started 2026-10-13T13:48:46Z, completed, conclusion success: Lint and format, Unit tests, Adapter contract tests (13:48:51Z to 13:49:49Z), End-to-end tests (13:48:50Z to 13:51:38Z). `git diff --stat 443ddf0..HEAD` is `README.md`, `.gitignore`, and the two conversation logs (3 files, 24,214 insertions, 7 deletions), so that run stands for the code at `fd47c6d`. PRs 11 and 12 ran lint only (tests skipped as drafts). PRs 1 to 9 predate the workflow and have no checks.
- Six mutation probes in a disposable worktree at `fd47c6d`, each run against the unit suite and reverted before the next: 6 of 6 killed. M1 `memory_store.settle` without the `is_open` filter: 4 tests fail (`test_settle_closes_an_attempt_once[memory]`, `test_the_same_result_delivered_twice_advances_the_order_once`, two more). M2 `number <= policy.max_attempts` in `_transient_failure`: 4 fail (`test_deadline_on_the_final_attempt_is_exhausted_not_timed_out`, three more). M3 `late = False` in `results.handle`: 1 fails (`test_a_result_after_its_deadline_counts_as_a_timeout_before_the_sweep_runs`). M4 memory bus yields expired commands: 2 fail. M5 `submit_order` without `nudge.set()`: 1 fails. M6 uncapped backoff: 1 fails. No survivors.
- Three behaviour probes against the application with in-memory adapters (not committed): results under each finding below.
- The working tree carries one uncommitted line in `.gitignore` (`.claude/`); it is in no PR and is not reviewed.
- The conversation logs were grepped for key, token, and private-key patterns: no hits.

## Findings

Severity: P1 must fix before the stack is judged, P2 should fix in the fix PR, P3 advisory. Each names the PR that introduced the code.

### P1

**1. The README names a review that did not produce this review.** `README.md:131` says the independent code review is "Codex with GPT-6.1 Sol" and that its log "is added with the review fixes"; `README.md:133` builds on that. This document was produced by the `ce-code-review` skill in Claude Code on Claude Fable 5.1, run inline. The brief (`prescreen-brief.md:16`) requires the README to say which tools were used for what, so the row must name whatever actually reviews the stack. Either rewrite the row for this review and link this file, or run the named tool as well and add its log. Introduced in PR 11 (`d2f9be8`), amended in PR 12 (`fd47c6d`). Route: manual.

### P2

**2. A broker or publish outage longer than the attempt budget turns every in-flight order into `compensation_failed`, with no recovery path.** `dispatcher.py:65-73` logs a failed publish and leaves the attempt open; `claim_overdue` later records it `timed_out` and `transitions.py:141-153` counts it against `max_attempts` exactly like a stage failure. With `config/pipeline.toml:4-7` (3 attempts, 1 s base backoff, 10 s deadline) the execute budget is 10 + 1 + 10 + 2 + 10 = 33 s, then the undo of the same stage burns another 33 s while the broker is still down, so an outage of about 66 s plus sweep latency ends the order. Probe: a bus whose `publish_command` always raises, one order, one tick per simulated second: `compensation_failed` after 74 s, history `inventory execute 1,2 timed_out, 3 exhausted; inventory undo 1,2 timed_out, 3 exhausted`, terminal reason `inventory exhausted: deadline passed; undo of inventory exhausted: deadline passed`. The docs accept "one attempt and one deadline window" per lost publish (`docs/design.md:33`, `README.md:75`), not a terminal business failure from an infrastructure outage, and there is no manual retry (`docs/design.md:116`). Cheap fixes, pick one in the fix PR: skip `claim_due` while the bus connection reports closed (aio-pika exposes `connection.is_closed`); or close a never-published attempt at once with an outcome that does not count toward the cap and keep `next_due_at = now + interval`. Introduced in PR 6 (`f44d1ac`). Covered by: a unit test with a dead bus asserting the order is still `running` after the budget. Route: manual.

**3. No size cap on the submission body or the outcome script.** `api.py:36-38` caps only the header. `schemas.py:15-16`, `messages.py:21-22`, and `script.py:16-17` put no bound on `payload` or on the token lists, and no ASGI body-size middleware exists. Probe: a 9,077,526-byte payload returned 201, and a script with 200,000 tokens for one stage returned 201. The payload and script are copied into every command (`dispatcher.py:89-96`), stored as JSONB, and re-sent on each undo, so one submission can be amplified by stages x attempts x 2 and a payload over RabbitMQ's default `max_message_size` (16 MiB since 4.0; the stack runs 4.3.6) fails to publish and then hits finding 2. Auth is out of scope by the plan; a size limit is not auth. Fix: a request-body cap (for example 64 KiB) as a middleware or a `Content-Length` check, plus `max_length` on the script token lists (at most `max_attempts` tokens is all a script can use). Introduced in PR 2 (`34351fa`) and PR 6 (`f44d1ac`). Route: manual.

**4. The status read is not atomic, so a client can see the order and its attempts from two different moments.** `api.py:86-89` calls `get_order` and then `list_attempts`, and each opens its own connection and snapshot (`postgres_store.py:122-142`). Between the two reads the results consumer or the sweep can commit a settle, so the page can render `state: running, current_stage: inventory` next to an attempt list that already shows `fraud execute 1` open, or a `completed` state with an attempt still open. R15 and R16 promise one consistent view. Fix: read both inside one transaction (one `engine.connect()` with a `REPEATABLE READ` snapshot, or a single query joining attempts) and add a store-port method `get_order_with_attempts`. Introduced in PR 4 (`f1e2334`) and PR 6 (`f44d1ac`). Route: manual.

**5. Under a backlog the dispatcher drains at most one batch per sweep interval: 50 orders per second with the shipped config.** `dispatcher.py:19` sets `CLAIM_BATCH_SIZE = 50`; `run` at `dispatcher.py:54-63` sleeps for `interval_seconds` (or a nudge) after every tick, full batch or not. A settled result also nudges the loop (`results.py:64`), so with healthy stages the next tick follows the first answer, not the interval. The bound bites while no results arrive: stages down, the broker recovering, or stages slower than the interval, which is exactly when the backlog exists; 1,000 due orders then take about 20 s to start. Probe (no stages consuming): 120 submitted orders, interval 0.2 s, running count sampled every 0.2 s: `[0, 100, 120, 120]`, that is 50 per sweep. Fix: loop again without waiting when `len(claims) == CLAIM_BATCH_SIZE` (or when `claim_overdue` returned a full batch). Introduced in PR 6 (`f44d1ac`). Route: manual.

### P3

**6. A degraded broker stretches one tick to minutes and delays the sweep.** `dispatcher.py:44-45` publishes the batch serially after the claim commits and `rabbitmq_bus.py:15,93` waits up to 5 s per publish, so a batch of 50 can hold the tick for 250 s before `claim_overdue` runs. Fix: stop publishing the batch after the first failure in a tick. PR 6.

**7. A store error while settling discards a valid result.** `consume_forever` (`consume.py:14-18`) logs the handler error and asks for the next item, which acknowledges the message (`rabbitmq_bus.py:62-63`); the attempt then waits for its deadline and the retry counts against the cap. A transient database error is thereby charged to the order. Fix: let infrastructure errors propagate out of the handler so the stream reopens without the acknowledgement (the robust connection redelivers), and keep the catch for stale or malformed results. PR 5 (`d844339`) and PR 7 (`1cb016c`).

**8. The healthcheck proves the static page is served, not that the loops run, and the orchestrator has no restart policy.** `docker-compose.yml:63-64` requests `GET /`, which `api.py:92-95` answers from a file; a dead dispatcher task or a lost database still reports healthy. `docker-compose.yml:46-67` has no `restart:`, and `aio_pika.connect_robust` raises if the first connect fails, so a startup race leaves the container stopped. Health endpoints beyond Compose are out of scope by the plan; a `restart: unless-stopped` line is not. PR 1 (`ca13e9c`), PR 6.

**9. `claim_due` and `claim_overdue` are batch-atomic, so one bad row would stall the sweep on every tick.** `postgres_store.py:145-176` roll back the whole batch when any callback raises, and `test_claim_that_fails_midway_claims_nothing` pins that. The invariants (`_require_current`, stage list snapshot) currently make the raise unreachable, so this is a note for the design record, not a change. PR 4.

**10. A dropped expired command leaves no trace.** `rabbitmq_bus.py:77-78` and `memory_bus.py:41` discard it silently, so AE14 ("discards attempts 1 and 2 without running the handler") cannot be seen in the stage log. Log one line at INFO with the attempt key; the end-to-end test at `tests/e2e/test_acceptance.py:129-131` filters stage log lines by order id, so give the line a distinct wording or filter on `attempt N:`. PR 5, PR 3.

**11. The timeline page is found by walking up from the package and only works with an editable install.** `api.py:22` resolves `services/orchestrator/static/index.html` via `parents[3]`; a wheel install would not ship it. Move it under `src/orchestrator/static/` and load it with `importlib.resources`. PR 8 (`4939a03`).

**12. Containers run as root and the actions are pinned by tag, not by commit.** Neither Dockerfile sets `USER` (`services/orchestrator/Dockerfile:26-27`, `services/stage/Dockerfile:27`). `.github/workflows/ci.yml:26-27` and the three repeats pin `actions/checkout@v7.0.1` and `astral-sh/setup-uv@v10.3.0` by tag; a moved tag changes CI silently. PR 1, PR 10 (`443ddf0`).

**13. `state`, `current_kind`, `kind`, and `outcome` are free text in the schema.** `0001_initial.py:17,19,37,39` have no `CHECK` against the enums, so a bad write is a 500 on the next read instead of a rejected write. PR 4.

**14. End-to-end Compose failures hide their output.** `tests/e2e/conftest.py:59-67` captures stdout and stderr and raises `CalledProcessError`, whose message carries only the command and exit code, so a failed `up --build --wait` in CI shows no Compose log. Print `stderr` in the exception or pass it to `pytest.fail`. PR 9 (`ef26464`).

**15. One test asserts nothing.** `services/orchestrator/tests/app/test_dispatcher.py:29-30` (`test_a_tick_with_nothing_to_do_changes_nothing`) only calls `tick()`. Assert the store is unchanged and no command was published, or delete it. PR 6.

**16. Three documented behaviours have no test.** (a) `docs/design.md:80` claims `settle` blocks on a held order lock and then drops the result as stale; the contract tests pin `SKIP LOCKED` for the two claims only. (b) A late `rejected` result is recorded as `timed_out` (probe confirmed: late `REJECTED` gave `timed_out, deadline passed`); `test_a_result_after_its_deadline_counts_as_a_timeout_before_the_sweep_runs` uses `succeeded` only. (c) The consistency of one status read (finding 4) once fixed. PR 4, PR 6.

**17. Naming and duplication nits.** Two unrelated classes are both named `StageSettings` (`packages/stage_kit/src/stage_kit/settings.py:11`, `services/orchestrator/src/orchestrator/app/config.py:18`). The `orders.payload` column holds the whole submission including the script, not the payload. `_utc_now` is defined three times (`simulator.py:20`, `memory_bus.py:10`, `rabbitmq_bus.py:20`) next to `orchestrator/app/clock.py`; one home in `contracts` would do. PR 3, PR 5, PR 6, PR 7.

**18. A test-only stage handler ships in the production image.** `services/stage/handlers/gift_wrap.py` exists for `tests/e2e/test_restarts.py:96-115`; the test then proves the wiring with a handler that was already built in. Acceptable for the brief; say so in the design record or build the handler into the image from the test overlay. PR 7, PR 9.

**19. The design and README trade-off tables do not state the consistency, concurrency, and load properties.** `README.md:71-82` and `docs/design.md:54-76` give what each decision buys and costs but not the guarantee level (at-least-once, effectively-once settlement, read consistency), the ceilings (one results consumer, one dispatcher, batch per interval), or what scales (stage replicas) and what does not. The next section is written so it can be moved into those tables in the fix PR. PR 9, PR 11.

**20. Removing a stage from the config is safe only while that stage's service keeps running.** `README.md:81` says an in-flight order "keeps its old stage list"; it does not say the removed stage's consumer must stay up until those orders finish, or they fail after the cap via finding 2's path. One sentence in the README and the design record. PR 11.

## Consistency, concurrency, and load against the requirements

What the code guarantees today, where the ceiling is, and the trade-off taken. Numbers come from `config/pipeline.toml`, `dispatcher.py:19`, `rabbitmq_bus.py:15-16`, and `docker-compose.yml`.

| Requirement | Property | What the code does | Ceiling or trade-off |
|---|---|---|---|
| R7, R23 delivery | At-least-once commands and results; effectively-once settlement | Persistent messages, durable queues, publisher confirms (`rabbitmq_bus.py:85-93`). Commands are acknowledged before the handler (`rabbitmq_bus.py:74`), results after the handler (`rabbitmq_bus.py:62-63`). The guarded close (`postgres_store.py:242-243`, `memory_store.py` `settle`) applies one outcome per attempt | A stage that crashes mid-handler loses that command until the deadline (10 s). A duplicate command runs the handler twice (R23 makes handlers idempotent). A duplicate result is dropped |
| R8 durability | Order and attempt row change in one transaction | `claim_due`, `claim_overdue`, `settle` each commit the attempt row and the order row together (`postgres_store.py:145-196`) | Postgres is a single node with a named volume; no replication. A crash between commit and publish costs one attempt and one deadline window (finding 2 is the sustained case) |
| R9, R26 idempotent submission | Linearizable per key | Unique constraint `uq_orders_idempotency_key` (`0001_initial.py:24`); a concurrent duplicate loses the insert and re-reads (`api.py:66-73`); SHA-256 of canonical JSON decides replay vs conflict | Replay returns the current state, not the original response body. No expiry of keys |
| R11, R22 retry and recovery | Backoff and due time persisted; recovery by polling | `next_due_at` on the order row, partial index `ix_orders_next_due_at`; the sweep claims due orders and times out overdue attempts every `interval_seconds` = 1 s | Recovery latency is one sweep interval plus one deadline window; a lost publish and a dead stage are indistinguishable. Retry policy (`max_attempts`, deadlines) is read from the current config at use, not snapshotted with the stage list, so a config change mid-order changes an in-flight order's budget (R24 snapshots the stage list only) |
| R15, R16 status | Read-committed per query | Two queries on two connections (finding 4) | A reader can see state and history from different instants until finding 4 is fixed. Status is served from the primary; polling at 1 s per open page is 2 queries per second per page |
| R5 per-stage queues | Stages run concurrently across orders, one order is strictly sequential | One durable queue per stage; a stage container consumes with prefetch 1 (`rabbitmq_bus.py:16,70`) | One container processes about 1 / latency commands per second (2 to 10 per second with the Compose profile of 0.1 to 0.5 s). Stage services scale horizontally: competing consumers on the same queue, no shared state, idempotent handlers. Nothing in the repo exercises replicas |
| Orchestrator concurrency | One process, two asyncio tasks, serialized per order by row locks | `claim_due` and `claim_overdue` take `FOR UPDATE SKIP LOCKED` (`postgres_store.py:155,168`); `settle` takes a blocking `FOR UPDATE` (`postgres_store.py:182`), so a result waits for a concurrent timeout of the same order and is then dropped as stale | The results consumer is serial (prefetch 1, one transaction per result), so throughput is about 1 / settle latency, in the low hundreds per second on a local Postgres. Dispatch drains at most `CLAIM_BATCH_SIZE / interval_seconds` = 50 per second under backlog (finding 5). Two orchestrator instances are declared unsupported; the locks would in fact serialize them, but the in-process nudge and the serial consumer were never tested that way |
| Load amplification | Payload travels in every command | The submission is copied into each execute and undo command (`dispatcher.py:89-96`) | Bytes on the bus per order = payload x (stages x attempts x 2) worst case; unbounded until finding 3 |
| Backpressure and growth | None on submission; append-only history | The API accepts any rate; RabbitMQ queues are durable and disk-backed; `attempts` grows without retention | Per-order history is bounded at stages x max_attempts x 2 rows (18 with defaults). No archival or TTL; out of scope by the plan |
| Clocks | Deadlines are absolute wall-clock times compared across containers | `Command.deadline_at` set by the orchestrator, checked on the stage's clock (`rabbitmq_bus.py:77`) with a 10 ms margin in the simulator | Correct only while containers share a clock (the plan's assumption); a skew larger than the margin makes a `timeout` script token succeed early |
| Availability split | Submission needs the database only; progress needs both | The API writes and nudges without touching the bus (`api.py:66-77`) | Orders accepted during a broker outage are dispatched later but burn attempts while it lasts (finding 2) |

## Per pull request

| PR | Head | Scope | Findings | Verdict |
|---|---|---|---|---|
| 1 | `ca13e9c` | Workspace, images, Compose, pipeline config | 8, 12 | ready |
| 2 | `34351fa` | Contracts: command, result, outcomes, script | 3 | ready with fixes |
| 3 | `3c6f0d7` | State machine, ports, in-memory adapters | 10, 17 | ready |
| 4 | `f1e2334` | Postgres store, migration, contract suite | 4, 9, 13, 16a | ready with fixes |
| 5 | `d844339` | RabbitMQ adapter | 7, 10, 17 | ready |
| 6 | `fb0344f` (head), `f44d1ac` (feature commit) | API, dispatcher, results consumer | 2, 3, 4, 5, 6, 8, 15, 16b, 17 | ready with fixes |
| 7 | `1cb016c` | Stage kit and the three stages | 7, 17, 18 | ready |
| 8 | `4939a03` | Timeline page | 11 | ready |
| 9 | `ef26464` | End-to-end suite, README, design record | 14, 18, 19 | ready |
| 10 | `443ddf0` | CI workflow | 12 | ready |
| 11 | `d2f9be8` | README architecture and trade-offs | 1, 19, 20 | ready with fixes |
| 12 | `fd47c6d` | Conversation logs, README tool table | 1 | ready with fixes |

## Requirements and plan

All 28 requirements (R1 to R28) and all nine units (U1 to U9) of `docs/plans/2026-10-13-1425-feat-async-order-orchestrator-plan.md` are implemented, and the 16 acceptance examples each map to a named test as `docs/design.md:91-112` lists; the mapping was spot-checked against the test files (AE1, AE3, AE6, AE13, AE14, AE16). Behaviour the plan did not ask for and the code adds: a late result of any outcome is recorded as `timed_out` (`results.py:58-59`, documented at `docs/design.md:81`); a malformed bus message is logged and dropped (`rabbitmq_bus.py:60,110-113`, documented in PR 5); a handler exception is a retry named by its class (KTD10). None changes the verdict.

## Not reviewed

The two conversation logs (24,142 lines) were scanned for secrets only, not read. Browser behaviour of the timeline page was not exercised; PR 8 records a manual check. Behaviour with two orchestrator instances or with stage replicas was not tested.
