## Code Review Results

**Outcome.** All seven findings are fixed on PR #13: CR01 counts received bytes with Starlette's body-limit middleware; CR02 releases only commands the bus proves it never sent and keeps the identity of any other failed publish; CR03 makes a stale release a no-op in both adapters; CR04 replays before checking today's config; CR05 reads status in one `REPEATABLE READ` transaction without the per-attempt payload; CR06 reuses one key per body on the page; CR07 adds enum-rejection and populated upgrade/downgrade tests.

**Reviewed:** 2026-10-13, using `ce:review` in Codex.  
**Scope:** all 13 open draft PRs, reviewed cumulatively from `6a4fe499401c087036325c379f2a71197a430bbd` (`main`) to **`4f397da87fc731efe9fe940b0b1c3c3bde901b37`**, the head of [PR #13](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/13). The cumulative diff has 99 changed paths; #13 changes 40 paths relative to its merge base. Per-PR attribution uses each PR's merge base and committed changes.  
**Intent:** deliver the configurable asynchronous workflow, durable state/history, bounded retries, reverse compensation, idempotent submission, and a timeline page; independently verify the new recovery/security fixes and the twelve earlier Ponytail recommendations.  
**Mode:** report-only. The requested saved report is the explicit exception to the skill's no-artifact rule. No implementation fixes, internal `.context` artifacts, todos, commits, pushes, or GitHub comments were created.

**Result:** **3 P1 findings and 4 P2 findings remain. All 12 Ponytail cuts are implemented. All 221 existing tests pass.** Passing the existing suites does not cover the failures reproduced below.

**Reviewers:** correctness, testing, maintainability, project-standards, agent-native, learnings; security, performance, API contract, data migrations, reliability, adversarial, Python, frontend races; deployment verification for the Alembic change. Following the supplied AGENTS tool mapping, these were sequential review passes in the main chat, not independent subagents. Multiple lens names in a row indicate coverage; no independent-reviewer confidence boost was applied.

- Security: new request-body middleware, public input, broker messages, container and CI configuration.
- Performance: status joins, dispatch batches, retries, payload copying, database access.
- API contract: idempotent replay after configuration changes, body errors, response visibility, message identity.
- Data migrations/deployment: new `0002_enum_checks.py`, existing-data compatibility and rollback.
- Reliability/adversarial: lost confirms, results racing publication cleanup, expired messages, restart behavior.
- Python: typed store/bus contracts, async cleanup, serialization and exception boundaries.
- Frontend races: submission retry, asynchronous fetch/polling, DOM rendering and helper refactor.
- Previous-comments reviewer was not selected: GitHub reports no comments, reviews, or review threads on any of the 13 PRs. Rails/schema.rb and CLI-specific reviewers are not applicable.

Untracked `.claude/` content is excluded. The original review reports remain unchanged. Source locations below refer to the reviewed #13 head.

### P1 -- High

| # | File | Issue and concrete evidence | Origin | Reviewer | Confidence | Route |
|---|---|---|---|---|---:|---|
| CR01 | `services/orchestrator/src/orchestrator/app/main.py:26` | **Body cap trusts the header instead of received bytes.** A real HTTP request to the unchanged app under Uvicorn/h11, with `Transfer-Encoding: chunked`, `Content-Length: 1`, and a 66,585-byte JSON body returned **201**. The declared 64 KiB cap is bypassed before the submission is stored and amplified into commands. | #13 | security, adversarial, testing | 1.00 | `gated_auto -> downstream-resolver` |
| CR02 | `services/orchestrator/src/orchestrator/app/dispatcher.py:73` | **An ambiguous publish deletes its attempt and reuses its identity.** The broker can accept a command before its confirmation is lost. The catch-all release removes that attempt; a later claim reuses the number with a new deadline. Probe: after the original command's deadline expired, its old result was accepted for the replacement and completed the order. This breaks stale-result/deadline protection. | #13; interacts with #2/#5/#6 identity and confirms | correctness, reliability, adversarial, API contract | 1.00 | `manual -> downstream-resolver` |
| CR03 | `services/orchestrator/src/orchestrator/adapters/postgres_store.py:175` | **Releasing a settled claim changes its order anyway.** The guarded delete can remove zero rows, but the order's due time is updated unconditionally; memory release does the same at `memory_store.py:67`. Probe in both adapters: complete a one-stage order, release the old claim, then claim due work. The next batch raises `IllegalTransition`, leaving an unrelated due order pending. | #13 | correctness, reliability, testing | 1.00 | `safe_auto -> review-fixer` |

**CR01 — Fix and verification.** Replace the custom header-only `BodySizeLimit` with `starlette.middleware.body_limit.RequestBodyLimitMiddleware(max_body_size=MAX_BODY_BYTES)`, which exists in installed **Starlette 1.8.0**. Keep an early Content-Length rejection if desired, but actual received bytes must enforce the cap. A separate in-memory app using this native middleware returned **413 with an application/json response** for an understated streamed body. Add real HTTP tests for the conflicting-header/chunked case, exact-cap and cap-plus-one bodies, and streamed requests without a length. Confirm the intended 411-versus-supported-streaming policy rather than preserving an accidental restriction. Context7 returned the library API and received-byte enforcement; [Starlette middleware documentation](https://github.com/kludex/starlette/blob/main/docs/middleware.md) and [RFC 9112 §6.3](https://www.rfc-editor.org/rfc/rfc9112.html#section-6.3) explain why a header alone is insufficient.

**CR02 — Fix and verification.** Split definitely-unpublished commands from publications whose broker acceptance is unknown. Release only commands the adapter can prove were not accepted, plus the untouched remainder of the batch. Preserve the attempted command's identity and deadline on timeout/connection-loss ambiguity; do not delete and recreate that identity. If infrastructure failures must also avoid the business attempt cap, model that separately from delivery identity. This needs a bus-port/adapter decision, not another blanket catch. Test a bus that enqueues and then raises, a result arriving before confirmation, and a result arriving after the old deadline but during a replacement attempt. The current `FlakyBus` raises **before** enqueueing and cannot catch this regression. Context7 and the installed aio-pika 10.1.1 source explicitly document that losing a confirmation does not prove rejection. See [aio-pika exchange implementation](https://github.com/mosquito/aio-pika/blob/master/aio_pika/exchange.py) and [RabbitMQ publisher confirms](https://www.rabbitmq.com/docs/confirms).

**CR03 — Fix and verification.** Make stale release a complete no-op. In Postgres, update the order only if the exact matching open claim was deleted, in the same locked transaction. Apply equivalent checking in memory. Protect against reused generations/keys as part of CR02. Add assertions for state, stage, kind, due time, terminal reason, and timestamps after releasing a completed, rejected, retried, or timed-out claim. Include a second due order and assert the next dispatch succeeds. The existing `test_releasing_an_attempt_that_was_settled_meanwhile_keeps_its_row` checks only the attempt's outcome and therefore passes despite the corruption of the order. Conditional deletion alone is not enough.

### P2 -- Moderate

| # | File | Issue and concrete evidence | Origin | Reviewer | Confidence | Route |
|---|---|---|---|---|---:|---|
| CR04 | `services/orchestrator/src/orchestrator/app/api.py:44` | **Current configuration validation runs before idempotent replay.** Submit a three-token fraud script under a cap of three, lower the configured cap to one, then replay the unchanged body/key: the probe returned **201 then 422**, instead of the existing order. Removing a scripted stage has the same ordering problem. Existing orders must not be re-admitted against today's pipeline. | #6 ordering; #13 adds the cap trigger | API contract, correctness, testing | 1.00 | `gated_auto -> downstream-resolver` |
| CR05 | `services/orchestrator/src/orchestrator/adapters/postgres_store.py:132` | **The status join repeats the complete order payload for every attempt.** The shared projection includes `orders.payload`, although status does not expose it. Isolated Postgres probe: a 60,039-byte stored submission and 18 attempt rows produced **18 separately decoded payload dictionaries**, representing **1,080,702 JSON bytes** before response mapping. Every page poll repeats this work. | #13 | performance, maintainability | 0.97 | `manual -> downstream-resolver` |
| CR06 | `services/orchestrator/src/orchestrator/static/index.html:205` | **Retrying a failed browser submission generates a new key.** If the server commits the first POST but its response is lost, the button is re-enabled and the next identical submission creates another order. A harness executing the actual page script confirmed two different keys for the unchanged body after a simulated lost response. | #8; moved/refactored in #13 | frontend races, API contract, testing | 0.99 | `gated_auto -> downstream-resolver` |
| CR07 | `tests/contract/test_migration.py:19` | **No test checks enforcement of the four new enum constraints.** The migration smoke test upgrades twice, checks table existence and a revision-row count, and all other adapter writes use valid enums. Dropping the constraint-creation statements would leave those assertions satisfied. The probe confirmed the constraints exist now; the gap is regression coverage, not a claim that this migration is currently broken. | #4 test; new requirement in #13 | testing, data migrations | 0.98 | `safe_auto -> review-fixer` |

**CR04 — Fix and verification.** Normalize/hash the request, then replay or conflict-check an existing key against its stored submission before applying current pipeline and script-length validation to a genuinely new order. Keep the unique constraint and losing-insert reread for concurrent creates. Test lower caps, removed scripted stages, identical replay returning the original ID, and a changed payload returning 409. The request's Pydantic shape validation still applies; the issue is reapplying current pipeline policy to an already accepted order.

**CR05 — Fix and verification.** Keep the one-snapshot guarantee, but return the order fields once. Options include aggregating attempts in a single statement or using two reads inside one **REPEATABLE READ** transaction. A pair of ordinary READ COMMITTED statements would reintroduce the old consistency bug. Alternatively introduce a lean status projection that avoids loading the unused payload/hash/script fields. Test consistency during a concurrent settlement and verify that a large order payload is decoded/transferred once per status request. The byte number above is the sum of serialized payload values materialized by the real query, not a network-throughput or latency benchmark. Default history can reach 3 stages × 3 attempts × 2 kinds = 18 rows.

**CR06 — Fix and verification.** Retain one key and normalized body for a logical submission across transport/response-decoding failures. Reset them when the user deliberately starts another order or changes the body. Add a browser/script test in which the first response is lost after acceptance and the second POST uses the same key and resolves to the same order. The test suite exercises API replay, but never the page's retry key lifecycle. The harness verified control flow and headers, not visual browser rendering.

**CR07 — Fix and verification.** Add Postgres tests that reject an invalid value for each of `orders.state`, `orders.current_kind`, `attempts.kind`, and `attempts.outcome`, and accept the documented nullable values. Test upgrading a populated `0001` database to `0002`, then downgrading to `0001` without changing order/attempt rows. Retain the existing migration smoke test; it has a useful distinct purpose. A durable schema guarantee needs a write-rejection test, not just a table-presence assertion.

### Tests: missing coverage and weak assertions

| Area | Existing coverage | Specific missing check / action |
|---|---|---|
| Failed publish and outage | Three dispatcher tests verify exceptions before delivery, cap preservation, and releasing the untouched remainder. | Enqueue-then-timeout and result-before-confirmation cases for CR02. Keep the existing cases. |
| Release racing settlement | Shared store test keeps the closed attempt row. | Assert the entire order is unchanged and other due orders remain dispatchable; CR03. The existing assertion is insufficient, not useless. |
| Body cap | Oversized normal Content-Length and missing-length requests are tested through ASGITransport. | Real Uvicorn framing plus actual streamed byte count; CR01. Those existing tests pass while the bypass succeeds. |
| Status snapshot | Shared contract test reads an order/history pair after sequential operations. | Interleave settlement with status reading and assert one consistent view; the current test would also pass with two ordinary reads. Preserve snapshot correctness while fixing CR05. |
| Schema migration | Empty-database upgrade and rerun. | Invalid enum writes, populated upgrade, and downgrade; CR07. |
| Browser idempotency | Server-side replay/conflict tests. | Lost-response retry at the page boundary; CR06. |
| Page not-found behavior | `tests/e2e/test_acceptance.py:137–145` checks API 404, page 200, and the static presence of `id="not-found"`. | Name this a route/packaging smoke test. It does not execute JavaScript or prove the hidden not-found section becomes visible. Add a focused browser/script visibility test if claiming UI behavior coverage. |
| Existing state-machine, bus, simulator, handlers, restart tests | Separate pure, application, adapter, and E2E boundaries are covered. | No blanket deletion or consolidation recommendation: similar scenarios at these boundaries catch different faults. |

The previous assertion-free empty-tick test is gone. The full-batch test's bounded wait has a meaningful pass/fail condition even without a final explicit `assert`. Tests for a base no-op undo and the gift-wrap extension exercise promised behavior and should remain. No remaining test is recommended for deletion solely to reduce the count.

### Ponytail findings: verification against PR #13

Original source: [Ponytail stack review](2026-10-13-ponytail-stack-review.md). “Applied” means the suggested structural change is present; it does not claim every possible execution path or visual state was tested.

| Finding | Original PR | Status | Evidence at #13 head |
|---|---:|---|---|
| F01 shared Dockerfile prefix/targets | #1 | Applied | `Dockerfile:3,18,30`; Compose selects `orchestrator`/`stage`; old service Dockerfiles are removed. New images booted through the E2E suite. |
| F02 directional success walk | #3 | Applied | `domain/transitions.py:76,108`; one `_succeeded` retains direction, terminal state, reason, and current-attempt guards. Domain tests pass. |
| F03 dataclass insert values | #4 | Applied | `adapters/postgres_store.py:104` uses `asdict(order)` and the stage-list conversion. Both store contract variants pass. |
| F04 reuse joined order projection | #4 | Applied | `adapters/postgres_store.py:52,184–193`; no second order select or `by_id` map in overdue claims. Locking tests pass. CR05 concerns its separate new use for status reads. |
| F05 channel context management | #5 | Applied | `packages/bus/src/bus/rabbitmq_bus.py:47,61`; both consumer channels use `async with`. Broker restart and redelivery contract tests pass. |
| F06 Pydantic attempt-response projection | #6 | Applied | `app/schemas.py:65`; visibility decisions around the projection remain explicit. API tests pass. |
| F07 reuse Result identity | #6 | Applied | `app/results.py:44–48,58`; Result is passed directly; memory matching remains attribute-based. Duplicate/late results are tested. |
| F08 aclosing fixture | #6 | Applied | `services/orchestrator/tests/app/conftest.py:134`; the timeout remains. |
| F09 command identity projection | #7 | Applied | `packages/stage_kit/src/stage_kit/runner.py:55–56`; Python-mode dump includes only AttemptKey fields. Runner tests pass. |
| F10 remove forwarding handler | #7 | Applied | `runner.py:38`; passes `self.process`; `bus/consume.py:11` accepts `Awaitable[object]`. Shared-loop/runner tests pass. |
| F11 text-node helper | #8 | Applied | `orchestrator/static/index.html:229–252` under the package; `textElement` preserves `textContent` and element classes. Script logic inspected; no full visual comparison performed. |
| F12 native CI matrix | #10 | Applied | `.github/workflows/ci.yml:35–56`; separate lint, named suites, individual timeouts, `fail-fast: false`, and E2E-only Compose validation remain. #13's tests are skipped while draft; local equivalents all pass. |

All twelve are applied, with **zero outstanding Ponytail recommendations**. The three P1 findings above arise from the broader recovery/security work, not from missing simplification changes. The original estimates are not a measured net line reduction for #13, which also adds behavior and tests.

### Per-PR coverage

| PR | Head reviewed | Review scope / remaining issues |
|---|---|---|
| [#1](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/1) | `ca13e9cf7d82` | Workspace, package pins, Compose, Docker and config. Shared build, non-root execution and restart policies now implemented in #13. Development credentials and loopback-only API are deliberate demo configuration. |
| [#2](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/2) | `34351fa6735f` | Contracts, validation, script and round trips. Preserved enum/aware-time/extra-field checks. CR02 exposes a new identity-lifecycle problem in #13, not a missing identity field here. |
| [#3](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/3) | `3c6f0d7bf9b9` | Pure transitions, ports and memory adapters. Directional refactor passes; CR03 is in the newly added release method. |
| [#4](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/4) | `f1e2334d34be` | Schema, migration, transaction/locking boundaries, shared store suite. Live Postgres checks pass. CR07 requires coverage for #13's additional migration. |
| [#5](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/5) | `d84433906d01` | RabbitMQ confirms, unroutable errors, malformed/expired messages, ack order, durable queues and restarts. Contract tests pass. Ambiguous confirmation handling in the caller is CR02. |
| [#6](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/6) | `fb0344f60c4a` | API, status rules, dispatcher, results consumer, configuration and lifespan. CR04 inherits validation-before-replay ordering; #13 adds another trigger. |
| [#7](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/7) | `1cb016cd044a` | Runner, profiles, script timing, seeded randomness, handler contract, stage loading and shared consume loop. No additional unaccepted defect retained. |
| [#8](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/8) | `4939a03094eb` | Routing, submit/poll control flow, text-only DOM writes and responsive CSS. CR06 remains. Static HTML tests do not prove DOM behavior. |
| [#9](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/9) | `ef264642d5d5` | Acceptance/restart suites, helper error output, gift-wrap extension and design record. All 15 E2E cases pass. Weak not-found assertion noted in test coverage. |
| [#10](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/10) | `120cb62e5ce9` | Workflow triggers, draft policy, least permissions, pinned actions, matrix and cancellation. Matrix/action changes in #13 preserve policy. |
| [#11](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/11) | `d2f9be8c9587` | README only in the PR's three-dot diff. Latest #10 has a later ignore-file commit not in this head; review uses merge base `443ddf069c72`, not a two-tree comparison that would falsely report a deletion. |
| [#12](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/12) | `10fb3d4c7278` | README/AI attribution, conversation exports. No runtime additions; exports scanned for common key/private-key signatures, not fully reread as executable code. |
| [#13](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/13) | `4f397da87fc7` | All twelve Ponytail cuts plus recovery, body/script limits, snapshots, enum checks, container/CI changes. New CR01–CR05 and CR07 need the listed work; inherited CR06 remains. |

The stack is not currently a strict head-to-base ancestry chain at #11 because #10 advanced after #11 branched. That is handled by merge-base scope and is not reported as a runtime defect. Findings inherited from an earlier PR are counted once. None is “pre-existing” relative to the cumulative `main` scope: the relevant implementation and tests are all introduced by this stack.

### Requirements Completeness

**Plan source: explicit.** PR bodies link `docs/plans/2026-10-13-1425-feat-async-order-orchestrator-plan.md`; its 28 requirements and U1–U9 units were checked. All units and required mechanisms are present. These checks distinguish implementation presence from edge-case correctness:

- [x] R1–R4: configured composition, extension, empty/duplicate rejection — implemented and tested.
- [x] R5–R6: stage queues and message fields/prior undo output — implemented and tested.
- [ ] R7: stale result identity protection — partially correct; CR02 reuses an identity after an ambiguous publish.
- [x] R8: durable transactional state/history — implemented; restart suites pass.
- [ ] R9: same-key/same-body replay — partially correct; CR04 and CR06 cover config changes and browser retries.
- [x] R10: immediate ID/initial-state submission — implemented and tested.
- [x] R11–R14: backoff/caps/rejection/reverse undo and terminal failure states — present and covered; CR03 violates terminal scheduling during stale release.
- [x] R15–R16: status fields and ordered history — now one-snapshot reads; CR05 is a cost regression, not a missing field.
- [ ] R17: timeline submission/polling — implemented; ambiguous retry behavior needs CR06, and DOM behavior lacks automated verification.
- [x] R18–R20: profiles/scripts and infrastructure-free state-machine testing — implemented and tested.
- [x] R21–R22: one-command Compose and pending sweep recovery — existing E2E and application suites pass.
- [x] R23–R26: handler identity/idempotency, stage-list snapshots, expired-command drop, conflicting-key rejection — implemented and covered; delivery-generation issue remains under CR02.
- [x] R27–R28: URL addressing, not-found/retry/undo/in-progress/disconnect UI states — implementation present and inspected; browser execution coverage remains partial.
- [x] U1–U9: each planned implementation unit is present in its attributed PR.

Unchecked requirements above indicate concrete correctness/verification qualifications, not missing implementation units. Their defects are already in the severity tables; no duplicate “requirement missing” finding is added.

### Learnings & Past Solutions

No `docs/solutions/` knowledge base or critical-patterns file exists. The earlier general review and Ponytail report were used as prior context, then verified against actual #13 code. Previously reported status inconsistency, root containers, mutable action tags, missing late-rejection test, assertion-free empty tick, and hidden Compose stderr are addressed. The prior report's blanket claim that the body cap is fixed is qualified by CR01.

### Agent-Native Gaps

There is no native agent-tool registry or runtime system prompt. The page's domain operations are nevertheless available through the same REST API: submit payload/script/key, read state/history, and revisit an order by ID. API-level parity is present; a dedicated agent integration would be additional scope, not a reason to block this backend exercise.

### Deployment Notes

Deployment verification was performed as a read-only review lens. **Do not treat the passing suites as approval to deploy the current recovery/body-limit changes with CR01–CR03 unresolved.** No deployment was performed.

- Before applying `0002`, check existing enum values; all of the following counts must be zero. The migration validates existing rows and takes table locks, so schedule it for the actual deployment's traffic/size rather than assuming an online migration.

```sql
SELECT count(*) FROM orders
WHERE state NOT IN ('pending','running','compensating','completed','failed','compensation_failed')
   OR (current_kind IS NOT NULL AND current_kind NOT IN ('execute','undo'));

SELECT count(*) FROM attempts
WHERE kind NOT IN ('execute','undo')
   OR (outcome IS NOT NULL AND outcome NOT IN ('succeeded','retry','timed_out','rejected','exhausted'));
```

- After migration, verify `alembic_version = '0002'` and four validated constraints. The isolated probe confirmed the four constraints exist in a fresh upgraded database.

```sql
SELECT version_num FROM alembic_version;
SELECT conname, convalidated FROM pg_constraint
WHERE conname IN ('ck_orders_state','ck_orders_current_kind','ck_attempts_kind','ck_attempts_outcome');

-- Must remain zero; catches CR03's terminal order being rescheduled.
SELECT count(*) FROM orders
WHERE state IN ('completed','failed','compensation_failed') AND next_due_at IS NOT NULL;
```

- `0002`'s downgrade drops the checks without dropping columns/data. Verify it with CR07 before relying on it. Rolling back the application cannot reconstruct attempt rows already deleted by the new release path; existing affected orders need inspection/reconciliation, not merely a code rollback.
- Observe publish/release errors, `IllegalTransition` on dispatcher ticks, and stale/late results during rollout. Keep removed stage services alive for snapshotted in-flight orders, as the updated README now explains.

### Coverage

| Verification | Result |
|---|---|
| No-Docker tests, including memory contract variants | **171 passed**, 50 deselected |
| Docker-backed Postgres/RabbitMQ contract tests | **35 passed**, 186 deselected |
| Compose E2E suite, built images and restart/extension cases | **15 passed**, 206 deselected |
| Lint and format | Clean; **69 files** already formatted |
| Real Uvicorn/h11 chunked body-cap probe | Bypass reproduced: **66,585 bytes → 201** |
| Native Starlette middleware probe | Same understated-stream pattern rejected: **413 application/json** |
| Ambiguous-publish identity probe | Expired original result accepted for replacement: **reproduced** |
| Stale-release probe | Terminal order requeued and another due order blocked: **reproduced in memory and Postgres** |
| Replay after cap reduction | **201 → 422** for unchanged request/key |
| Actual page script in a small mocked DOM/fetch harness | Different keys on unchanged retry: **reproduced** |
| Isolated Postgres status query | **18 decoded payload copies; 1,080,702 serialized payload bytes** |
| Fresh migration probe | Four enum checks present; enforcement/downgrade gaps remain CR07 |
| GitHub | #13 lint succeeded; test job skipped because draft; no prior PR review threads |

The three test-suite totals partition the same collected suite: **221 passed**, not 221 plus the deselected counts. Tests ran against unchanged implementation. Temporary probes used in-memory state, loopback ephemeral servers, or new disposable Testcontainers; they did not write repository implementation or operate on existing application data.

Findings were validated against the `ce:review` merge-tier fields, deduplicated, and checked against source lines. Suppressed below the confidence gate: **0 retained candidates**; malformed findings: **0**. No independent-agreement boost was used. Existing intentional limits were excluded from the verdict: no authentication for the local demo, one orchestrator, settlement errors acknowledged by the shared consume loop, batch-atomic callbacks, and the test extension handler in the stage image. Broader dependency-vulnerability-database auditing and full visual browser checks were not performed. No Rails schema-drift check applies to this Alembic project.

All fixes require targeted verification. The routing labels record who would own them under the skill; **report-only mode applied none of them**, including `safe_auto` items. No residual todo handoff was created because the requested deliverable is this report.

---

> **Verdict:** Not ready.
>
> **Reasoning:** The existing suites are green and all Ponytail recommendations are implemented, but the new publish-release path can accept expired results and stall dispatch of other orders. The advertised request cap is bypassable through the real HTTP server.
>
> **Fix order:** CR02 publication certainty/identity and CR03 stale-release guard → CR01 actual-byte body cap → CR04/CR06 idempotent replay → CR05 status-query cost → CR07 schema regression coverage.
