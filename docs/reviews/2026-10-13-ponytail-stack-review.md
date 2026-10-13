# Ponytail simplification review of the draft PR stack

Reviewed on 2026-10-13. Repository: [HubDev-AI/flowforge-order-orchestrator](https://github.com/HubDev-AI/flowforge-order-orchestrator).

This is a documentation-only review of the combined implementation at **`fd47c6d48a9644bff4e85969eb595cfd4beff9c3`**, the head of [draft PR #12](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/12). All 12 PRs are open drafts in one linear stack. Each PR's base is an ancestor of its head; each head is the next PR's base. Findings below are attributed to the PR that introduced the code, even when the code is inherited by #12.

**12 findings; an estimated 80 lines of implementation, test plumbing, and workflow duplication can go.** These are estimates for the replacements described below, including small supporting changes. They are not measurements from an applied patch. The strongest cuts remove repeated field mapping and build/workflow setup. No new dependency is needed.

Review scope: handwritten Python, tests and fixtures, HTML/CSS/JavaScript, Dockerfiles, Compose, workspace/dependency configuration, migration, and CI changed across the stack. Generated `uv.lock` was checked for the dependency choices, not treated as handwritten code to shrink. Planning documents, README changes, and conversation exports were checked for the constraints relevant to this review; their size is not runtime complexity. This review concerns simplification, not a correctness/security audit.

## PR coverage and attribution

| PR | Change | Reviewed head | Findings | Estimated net cut |
|---|---|---|---|---:|
| [#1](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/1) | Workspace, containers, configuration | `ca13e9cf7d82` | F01 | 7 lines |
| [#2](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/2) | Shared contracts | `34351fa6735f` | No independent cut | 0 |
| [#3](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/3) | Domain, ports, memory adapters | `3c6f0d7bf9b9` | F02 | 5 lines |
| [#4](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/4) | Postgres store and migration | `f1e2334d34be` | F03–F04 | 14 lines |
| [#5](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/5) | RabbitMQ adapter and contract tests | `d84433906d01` | F05 | 6 lines |
| [#6](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/6) | API, dispatcher, results consumer | `fb0344f60c4a` | F06–F08 | 15 lines |
| [#7](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/7) | Stage kit and handlers; shared consume loop | `1cb016cd044a` | F09–F10 | 7 lines |
| [#8](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/8) | Timeline page | `4939a03094eb` | F11 | 7 lines |
| [#9](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/9) | E2E suite, extension example, logging, design | `ef264642d5d5` | No additional cut | 0 |
| [#10](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/10) | GitHub Actions | `443ddf069c72` | F12 | 19 lines |
| [#11](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/11) | README architecture and run instructions | `d2f9be8c9587` | No implementation change | 0 |
| [#12](https://github.com/HubDev-AI/flowforge-order-orchestrator/pull/12) | Conversation logs and AI attribution | `fd47c6d48a96` | No implementation change; inherits F01–F12 | 0 |

Locations and source links use the combined #12 snapshot, so later line movement cannot make the findings point at unrelated code. Savings are counted once, under the originating PR. A single follow-up on top of #12 can address the entire list without rewriting the stack.

## PR #1 — workspace and containers

### F01 — share the Dockerfile prefix, retain separate service targets

`services/orchestrator/Dockerfile:L1–16` and `services/stage/Dockerfile:L1–16`: **shrink:** two identical interpreter/uv/environment/workspace-manifest prefixes. Replace them with one Dockerfile containing a shared `workspace` base and separate `orchestrator` and `stage` targets. Estimated net: **−7 lines**, after adding the two Compose target selections.

[Orchestrator source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/Dockerfile#L1-L27), [stage source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/stage/Dockerfile#L1-L27).

Move only the shared prefix. Each target retains its package-specific dependency sync, source copies, second sync, working directory, and command. Keep `--locked`, `--no-dev`, the cache mount, and the manifest-before-source layering. `docker-compose.yml` selects the appropriate build target. The orchestrator command was finalized in #6; it still runs Alembic before Uvicorn. All stage containers still share one stage image.

This does not justify an image that installs both services' runtime dependencies, an entrypoint with service-selection branches, or a custom build script. Docker already has named build targets. See [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/).

Validation when implemented: build both targets, run Compose with `--wait`, and run the E2E suite, including the gift-wrap extension. Images were not rebuilt for this documentation-only review.

## PR #2 — contracts

**Lean already.** `AttemptKey`, `Command`, and `Result` already share identity fields; Pydantic handles wire parsing, aware timestamps, enum validation, and unknown fields. `OutcomeScript` uses `RootModel` rather than a custom parser. The execute/undo validator enforces a real message invariant. Removing it or loosening `extra="forbid"` would change behavior.

The later call sites fail to take full advantage of these existing models; see F07 and F09. Do not attribute those call-site findings to #2 or count them twice.

## PR #3 — domain and memory adapters

### F02 — collapse the mirrored success walks into one directional walk

`services/orchestrator/src/orchestrator/domain/transitions.py:L76–79,L110–127`: **shrink:** execute-success and undo-success repeat the same current-attempt guard, successful close, and stage lookup. Replace the two helpers with one success helper that advances by `+1` or `−1`. Estimated net: **−5 lines**.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/src/orchestrator/domain/transitions.py#L110-L127).

Keep the state-machine match. Combine only its two success cases and helpers:

```python
def _succeeded(order: Order, event: Succeeded) -> Decision:
    _require_current(order, event.attempt)
    close = AttemptClose(Outcome.SUCCEEDED, None, event.output, event.at)
    undo = order.state is COMPENSATING
    index = order.stages.index(event.attempt.stage) + (-1 if undo else 1)
    reason = order.terminal_reason if undo else None
    if 0 <= index < len(order.stages):
        return Decision(
            order.state, order.stages[index], UNDO if undo else EXECUTE,
            event.at, reason, close,
        )
    return Decision(
        OrderState.FAILED if undo else OrderState.COMPLETED,
        None, None, None, reason, close,
    )
```

Preserve the execute path's clearing of `terminal_reason`, the undo path's retained reason, both terminal states, and scheduling the next stage at `event.at`. Do not replace the state machine with a generic workflow engine or merge failure rules into this helper.

An in-memory comparison against the existing transition function passed **180 valid cases**: one through five stages, each current stage, both directions, absent/present terminal reason, and three output shapes. Existing tests cover stage snapshots, wrong stage/kind, terminal states, and reverse compensation. The candidate helper was not written into the implementation.

Keep the memory adapters and both ports. Each port has two real implementations and shared contract tests; this is not a one-implementation abstraction. Keep precomputing callback decisions before writing the memory store: `test_claim_that_fails_midway_claims_nothing` checks that behavior.

## PR #4 — Postgres adapter

### F03 — let the dataclass provide insert values

`services/orchestrator/src/orchestrator/adapters/postgres_store.py:L100–114`: **stdlib:** manually copy all 13 `Order` fields into `INSERT`. Replace the value list with `dataclasses.asdict(order)`, retaining the explicit stage tuple-to-list conversion. Estimated net: **−10 lines**, including the import.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/src/orchestrator/adapters/postgres_store.py#L97-L120).

```python
await connection.execute(
    sa.insert(orders).values(**(asdict(order) | {"stages": list(order.stages)}))
)
```

At this snapshot, the dataclass field set exactly matches the table's column names. `UUID`, timestamps, and string enums retain their Python values for SQLAlchemy's existing bind processors. The payload is JSON submitted through the API. `asdict` recursively copies containers; do not generalize this replacement to arbitrary non-JSON objects or to `Decision`, which also contains the non-column `close` field. See [the Context7 evidence](2026-10-13-ponytail-library-evidence.md#python-standard-library).

The field-set check and a nested JSON payload check passed locally. Actual Postgres insertion was not exercised. When implemented, run both adapters' store contract tests and the Postgres-specific tests; retain unique-constraint translation, atomic transactions, and SQL NULL handling for attempt outputs.

### F04 — fetch the order from the join already being performed

`services/orchestrator/src/orchestrator/adapters/postgres_store.py:L160–176`: **shrink:** join orders to lock them, discard their fields, then issue a second select and build a `by_id` map. Select the order fields alongside the attempt fields and resolve the paired rows directly. Estimated net: **−4 lines**.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/src/orchestrator/adapters/postgres_store.py#L160-L176).

The current tables have only one colliding column name: `id`. The attempt's database surrogate ID is unused by `_attempt`. A concrete projection reuses both existing decoders:

```python
sa.select(orders, *[c for c in attempts.c if c is not attempts.c.id])
# Join attempts on orders.c.id == attempts.c.order_id;
# retain the existing filters, ordering, limit, and lock clause.

overdue = [(_order(row), _attempt(row)) for row in rows]
resolved = [(attempt, resolve(order, attempt)) for order, attempt in overdue]
```

Delete the second select and the `by_id` dictionary. Keep `FOR UPDATE OF orders SKIP LOCKED`, the same transaction, and resolving the entire batch before applying closes. If overlapping columns are added later, use SQLAlchemy's labels or column-object `RowMapping` lookup rather than a generic row-mapping framework. See [SQLAlchemy evidence](2026-10-13-ponytail-library-evidence.md#sqlalchemy).

The proposed select compiled against the installed PostgreSQL dialect, with unique output names and the original lock clause. This is not a database concurrency test. When implemented, run `test_claim_overdue_skips_an_order_another_transaction_holds` and the full store contract suite against Postgres.

Keep the migration's explicit schema. Replacing a historical Alembic revision with imports of the current application tables would make old migrations change when the application changes.

## PR #5 — RabbitMQ adapter and tests

### F05 — let the channel context manager own channel cleanup

`packages/bus/src/bus/rabbitmq_bus.py:L52–65,L68–80`: **shrink:** both consumers wrap a channel in manual `try/finally: await channel.close()`. Use aio-pika's `async with self._connection.channel() as channel` and retain each consumer's body. Estimated net: **−6 lines**.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/packages/bus/src/bus/rabbitmq_bus.py#L51-L80).

Keep queue declaration, QoS, queue iterator, parsing, deadline filtering, and acknowledgement positions exactly as they are. Replace only channel acquisition and final cleanup; do not merge command and result semantics into a generic consumer. Installed aio-pika 10.1.1 documents channel context management: entry initializes the channel and exit closes it. The robust channel inherits those methods. See [aio-pika evidence](2026-10-13-ponytail-library-evidence.md#aio-pika).

The library context manager passes the exiting exception to `close`; the previous cleanup calls `close()` without it. No application callbacks depend on that argument here, but preserve exception propagation and verify cancellation/reconnection with the existing RabbitMQ-specific tests. Source/API inspection was performed; no modified consumer was run against a broker.

Keep the explicit host-port binding in the restart tests. Docker can reallocate an automatically assigned port on restart; these tests reconnect the existing bus to one URL and need that URL to stay stable. Testcontainers' automatic port API was considered, but does not by itself prove a safe replacement here. See [the rejected port simplification](2026-10-13-ponytail-library-evidence.md#testcontainers).

No cut is recommended in the acknowledgement logic. `message.process()` does not transparently replace acknowledge-before-yield for commands or acknowledge-after-resumption for results. See the rejected replacement in the library evidence.

## PR #6 — orchestrator application

### F06 — use Pydantic's attribute projection for attempt responses

`services/orchestrator/src/orchestrator/app/schemas.py:L65–77`: **shrink:** copy every `AttemptView` field manually. Replace the constructor list with `AttemptView.model_validate(a, from_attributes=True)`. Estimated net: **−10 lines**.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/src/orchestrator/app/schemas.py#L51-L78).

```python
attempts=[AttemptView.model_validate(a, from_attributes=True) for a in attempts],
```

Keep the explicit `AttemptView` schema: it controls which fields reach the API. Keep the surrounding logic that hides due dates between stages and hides terminal reasons during compensation. Mapping the entire `Order` automatically would bypass those presentation rules. Context7 confirms attribute-based validation; see [Pydantic evidence](2026-10-13-ponytail-library-evidence.md#pydantic).

The proposed projection produced the same JSON as the current constructor for **36 cases** across both kinds, open/closed outcomes, and three output shapes. Extra `Attempt` fields did not appear in the response. When implemented, run `test_api.py`, including retry due-date and terminal-reason visibility.

### F07 — reuse the result as its attempt identity

`services/orchestrator/src/orchestrator/app/results.py:L41–43`: **delete:** construct a new `AttemptKey` from a `Result` that already inherits `AttemptKey`. Use `attempt = result` and remove the unused import. Estimated net: **−2 lines**.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/src/orchestrator/app/results.py#L39-L65).

The transition events, `OrderStore.settle`, the memory adapter's `Attempt.matches`, and Postgres's `_of_attempt` use the four identity attributes. They do not serialize this argument or require its exact concrete model class. The existing `Result` is validated and frozen. Keep the explicit tuple comparison in `Attempt.matches`; replacing it with `self.key == result` would fail because Pydantic equality distinguishes model types.

When implemented, run duplicate/stale/unknown result, wrong-kind identity, late-result, and compensation tests. This review checked subtype compatibility and inspected both adapters; it did not apply this change or run a modified consumer.

### F08 — replace the fixture's generator-cleanup boilerplate

`services/orchestrator/tests/app/conftest.py:L131–138`: **stdlib:** a `try/finally` solely calls the consumer's `aclose`. Replace it with `contextlib.aclosing` beside the existing timeout. Estimated net: **−3 lines**, including the import.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/tests/app/conftest.py#L130-L140).

```python
async with aclosing(runtime.bus.consume_commands(stage)) as consumer, asyncio.timeout(1):
    return await anext(consumer)
```

The bus contract tests already use this standard-library feature. The timeout and early generator closure remain. A local probe confirmed closure on block exit. See [Python evidence](2026-10-13-ponytail-library-evidence.md#python-standard-library). Run the existing application tests after implementation.

The configuration merge already uses dict union and Pydantic validation. Replacing it with unchecked `model_copy(update=...)` would lose validation of override keys and values. Keep the clock injection, wake-up event, background task shutdown, deadline recovery, and delayed pending dispatch: each has exercised behavior.

## PR #7 — stage kit and service runtime

### F09 — project shared identity fields instead of repeating them

`packages/stage_kit/src/stage_kit/runner.py:L57–65`: **shrink:** repeat the four `AttemptKey` fields while constructing a result. Use Pydantic's `model_dump(include=set(AttemptKey.model_fields))` in the existing helper. Estimated net: **−3 lines**, including the import.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/packages/stage_kit/src/stage_kit/runner.py#L57-L65).

```python
def _result(command: Command, outcome: Outcome, **fields) -> Result:
    return Result(
        **command.model_dump(include=set(AttemptKey.model_fields)),
        outcome=outcome, **fields,
    )
```

Use the Python-mode dump and the explicit include set. Dumping the entire command would send `payload`, `script`, and `deadline_at` into a model that forbids extra fields. Do not add a generic message factory or new identity model. The small helper remains useful for its three call sites.

The replacement matched the current helper in **10 cases**, across both command kinds and every outcome, with reason and output retained. The existing result-identity, undo-output, handler-exception, and logging tests should run after implementation. See [Pydantic evidence](2026-10-13-ponytail-library-evidence.md#pydantic).

### F10 — remove the stage runner's forwarding method

`packages/stage_kit/src/stage_kit/runner.py:L37–41`: **delete:** `_handle` only awaits `process`. Pass `self.process` directly to `consume_forever`, and type the ignored handler result as `Awaitable[object]` in `packages/bus/src/bus/consume.py:L11`. Estimated net: **−4 lines**.

[Runner source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/packages/stage_kit/src/stage_kit/runner.py#L37-L41), [consume-loop source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/packages/bus/src/bus/consume.py#L10-L22).

`consume_forever` ignores return values. `StageRunner.process` returns a `Result`, and `ResultsConsumer.handle` already returns a `bool` to this same loop. Broaden the callback annotation to reflect both existing callers; do not create a second consume loop. Logging, result publication, exception handling, stream restart, and cancellation remain in their current owners. Logging was added to `process` in #9 and must remain when removing the older forwarding method.

When implemented, run all consume-loop and stage-runner tests, especially the loop publication and every-command logging cases. No modified runner was executed in this review.

Keep `StageHandler` with its default no-op undo and dynamic module loading. There are several concrete handlers, the extension behavior is tested, and import errors inside a handler must still propagate rather than masquerade as an unknown stage. Keep the seeded simulator and injectable sleep/clock. A library retry decorator here would create a second retry authority and change attempt accounting.

## PR #8 — timeline page

### F11 — reuse one small text-node helper

`services/orchestrator/static/index.html:L233–268`: **shrink:** repeat `createElement`, `className`, and `textContent` for title, stage, timestamps, reason, and output, while `tag` already does the same job for spans. Generalize that helper to accept an element name and reuse it for these nodes. Estimated net: **−7 lines**.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/services/orchestrator/static/index.html#L227-L269).

```javascript
function textElement(name, className, text) {
  const element = document.createElement(name);
  element.className = className;
  element.textContent = text;
  return element;
}
// Examples:
const title = textElement("div", "attempt-title", "");
const stage = textElement("span", "", attempt.stage);
const output = textElement("p", "output", JSON.stringify(attempt.stage_output));
```

Use this helper for the existing span tags and paragraph nodes, replacing the old `tag` helper rather than adding another layer beside it. Keep the same elements, classes, ordering, conditionals, and `textContent`. Do not shorten rendering with unescaped `innerHTML` or introduce a frontend framework.

Validation when implemented: check an open attempt, each closed outcome, undo styling, reason/output rendering, markup-like payload strings, dark mode, and phone width. Existing static-route tests only verify that HTML is served; they do not verify DOM behavior. No candidate DOM implementation was run during this review.

Keep the unchanged-snapshot guard, sequential polling, last-known timeline during disconnection, terminal stop, and validation-error details. A bare `crypto.randomUUID()` replacement for the hex-key helper would impose a secure-context requirement; preserving the existing browser behavior is more important than that small cut.

## PR #9 — E2E suite and documentation

**No additional behavior-preserving cut identified.** Review included the Compose override files, `Stack`/`Orders` fixtures, acceptance and restart tests, gift-wrap handler, and the additions to runner logging and handler tests.

The tiny gift-wrap handler is exercised by the acceptance requirement that a new stage needs only a handler, a Compose service, and a config line. Deleting it would delete the extension example. The logging addition is observable and asserted; F10 must preserve it.

The E2E fixtures' monotonic polling and HTTP-error handling deliberately survive service restarts. Replacing them with a fixed sleep would weaken behavior. The eight-line fixture TOML renderer is specific to the shipped file and has a sanity assertion; adding a TOML-writing dependency solely to replace it would not simplify the system. Test duplication across pure domain, application, adapter, and E2E boundaries exercises different contracts and is not a reason to delete coverage.

## PR #10 — CI

### F12 — replace three copied test jobs with a native matrix

`.github/workflows/ci.yml:L35–76`: **native:** repeat checkout, uv setup, locked sync, draft gate, runner, and test invocation in three jobs. Use one test job with a three-entry `strategy.matrix.include`; keep lint separate. Estimated net: **−19 lines**.

[Source](https://github.com/HubDev-AI/flowforge-order-orchestrator/blob/fd47c6d48a9644bff4e85969eb595cfd4beff9c3/.github/workflows/ci.yml#L35-L76).

```yaml
  tests:
    name: ${{ matrix.name }}
    if: github.event.pull_request.draft != true
    runs-on: ubuntu-latest
    timeout-minutes: ${{ matrix.timeout }}
    strategy:
      fail-fast: false
      matrix:
        include:
          - {name: Unit tests, selector: "not contract and not e2e", timeout: 15}
          - {name: Adapter contract tests, selector: contract, timeout: 20}
          - {name: End-to-end tests, selector: e2e, timeout: 30}
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: astral-sh/setup-uv@v10.3.0
        with:
          version: ${{ env.UV_VERSION }}
          enable-cache: true
      - run: uv sync --locked
      - if: matrix.selector == 'e2e'
        run: docker compose config --quiet
      - run: uv run pytest -m "${{ matrix.selector }}"
```

`fail-fast: false` matters: the current independent jobs do not cancel each other on failure. Preserve the three displayed check names, the 15/20/30-minute limits, the draft gate, the E2E-only Compose validation, and the top-level triggers, concurrency, permissions, and uv pin. Keep lint running on drafts. Do not combine all suites into one sequential job or a shell dispatch script.

GitHub documents matrix include entries and failure behavior in [running variations of jobs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations). When implemented, verify check names against any required checks and inspect a draft and a ready-for-review run. No workflow was changed or dispatched by this review.

## PR #11 — README

Documentation only. No new implementation to simplify. The architecture and trade-offs describe behavior that this review preserves. Findings inherited from earlier PRs are already counted under their origins.

## PR #12 — conversation record

Documentation only. No new implementation to simplify. This head is the cumulative review target; all findings remain applicable here. Exported conversation length is not executable boilerplate and is not counted toward possible code cuts.

## What should stay

| Tempting deletion/replacement | Behavior it would lose or extra complexity it would introduce |
|---|---|
| Remove compensation because stages are simulated | Reverse undo, prior outputs, and `compensation_failed` are explicit, tested behavior. |
| Replace ports with direct database/broker calls | Memory adapters and shared adapter contract tests are real second implementations. |
| Move retry policy into Tenacity/Celery/broker redelivery | Deadline recovery, attempt caps, history, and persisted backoff currently have one authority. |
| Replace broker loops wholesale with `message.process()` | Commands and results deliberately have different acknowledgement timing; stale-channel handling also matters. |
| Remove fixed port bindings from broker restart tests | Automatically assigned host ports can change on restart, while the existing bus retains its connection URL. |
| Replace transaction callbacks with separate store writes | Claim/settle must update order and attempt together; failed callback batches must not partially apply. |
| Import live table metadata into the initial migration | Historical migration behavior would depend on future application code. |
| Replace UUID parsing with a typed FastAPI UUID parameter | Malformed order IDs currently return 404; ordinary request validation would return 422. |
| Replace canonical JSON hashing with an ordinary dump | Idempotency deliberately ignores key order and includes the normalized script. |
| Remove the profile/clock/sleep seams or default undo | Reproducible simulation, fast deterministic tests, and extension behavior are already used. |
| Add template/UI/TOML dependencies for small local helpers | Dependency and build/configuration overhead outweighs the removed code. |

## Validation performed and limits

- Confirmed the live GitHub PR list, draft status, base/head chain, and per-PR changed paths against local Git objects. The checkout matches #12; implementation files had no local edits before review. A pre-existing `.gitignore` edit was left alone.
- Read the combined handwritten implementation and test changes, including tests moved into shared contract suites. Earlier scaffolding later replaced by an adapter or shared consume loop is not reported as surviving duplication.
- Queried Context7 for Pydantic, SQLAlchemy, Testcontainers, Python, and aio-pika; recorded the exact library IDs, lookup subjects, source links, and applicability limits in [library evidence](2026-10-13-ponytail-library-evidence.md).
- Ran the **unchanged** no-Docker suite: **159 passed, 46 deselected**. This is a baseline, not evidence that all suggested refactors pass.
- Executed in-memory equivalence probes: **180** success/undo decisions, **36** attempt-response projections, **10** command/result projections, order/table insert field equality, and deterministic generator closure. Compiled the proposed join with the installed PostgreSQL dialect; no Postgres server was used.
- Did not alter implementation, fixtures, dependencies, Docker configuration, CI, or tests. Did not run the Docker-backed contract/E2E suites, build candidate images, execute candidate browser rendering, dispatch CI, commit, push, or post review comments.

Apply the proven field-mapping cuts and the small forwarding/cleanup cuts first. Follow with the database projection, Docker targets, and matrix after their listed integration checks. Count the complete replacement, including imports and supporting edits; do not claim savings by merely compressing whitespace or removing useful tests.

net: -80 lines possible. (Estimated, not applied.)
