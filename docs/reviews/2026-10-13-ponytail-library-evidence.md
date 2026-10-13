# Library evidence for the Ponytail stack review

Collected on 2026-10-13 for the [stack review](2026-10-13-ponytail-stack-review.md), which reviews cumulative draft PR #12 at `fd47c6d48a9644bff4e85969eb595cfd4beff9c3`.

Context7 was accessed directly through its remote MCP endpoint, `https://mcp.context7.com/mcp`, using JSON-RPC `tools/list`, `resolve-library-id`, then `query-docs`. These were fresh lookups for this review, not a claim based on the repository's earlier research notes. Queries concerned general library APIs and did not send repository source or credentials.

The resolved documentation IDs below are mostly unversioned documentation indexes. They are not exact-version guarantees. Installed versions were checked locally: Python **3.14.6**, Pydantic **2.14.1**, SQLAlchemy **2.1.4**, aio-pika **10.1.1**, Testcontainers **4.15.0**, and FastAPI **0.143.0**. Dockerfiles pin Python **3.14.8**, so local probes do not validate the image runtime itself.

## Pydantic

**Resolved ID:** `/pydantic/pydantic`.

**Queries:** attribute-based validation from arbitrary objects; projecting shared identity fields through model serialization. Separate query responses covered `model_validate(..., from_attributes=True)`, handling of extra fields, and `model_dump(include=...)` using sets of field names in Python mode. Both replacements were also exercised directly with the installed version.

**Primary sources returned by Context7:**

- [Models: arbitrary class instances and extra data](https://github.com/pydantic/pydantic/blob/main/docs/concepts/models.md).
- [Validation errors: attribute extraction](https://github.com/pydantic/pydantic/blob/main/docs/errors/validation_errors.md).
- [Serialization: field inclusion and Python-mode dumping](https://github.com/pydantic/pydantic/blob/main/docs/concepts/serialization.md).

**Additional API reference:** [Pydantic serialization: including and excluding fields](https://docs.pydantic.dev/latest/concepts/serialization/#field-inclusion-and-exclusion).

**What it supports:**

- F06: a declared response model can read a dataclass's matching attributes without eight handwritten field assignments. The local 36-case comparison checked identical serialized values and retained field filtering.
- F09: keep only the four base identity fields while dumping a command, then construct the existing result model with its outcome/reason/output. Ten local cases matched the existing helper.

**Limits:** attribute projection does not implement the application's retry-date and terminal-reason visibility rules. Keep those explicit. `Command.model_dump()` without an include set contains extra fields forbidden by `Result`. `model_copy(update=...)` is not a replacement for validating configuration overrides. Pydantic model equality is not a structural comparison across arbitrary subclasses, so F07 must retain the memory adapter's tuple-based identity matching.

## SQLAlchemy

**Resolved ID:** `/websites/sqlalchemy_en_20_core`.

**Query:** selecting both sides of a Core join, disambiguating matching column names, and accessing row data by column objects.

**Primary sources returned by Context7:**

- [Core selectable objects and label styles](https://docs.sqlalchemy.org/en/20/core/selectable.html).
- [Connections: RowMapping](https://docs.sqlalchemy.org/en/20/core/connections.html#sqlalchemy.engine.RowMapping).
- [SQL elements and labels](https://docs.sqlalchemy.org/en/20/core/sqlelement.html).

**What it supports:** F04 can select needed order and attempt values from the existing joined query. SQLAlchemy can label overlapping names or expose column-object mapping keys; a second database query is not required just to recover the already joined order.

**Concrete adaptation for this snapshot:** select every order column and every attempt column except the unused attempt surrogate `id`. All resulting names are unique, allowing the existing `_order` and `_attempt` decoders to remain unchanged. The proposed select compiled under installed SQLAlchemy 2.1.4 and retained `FOR UPDATE OF orders SKIP LOCKED`.

**Limits:** Context7 returned the 2.0 Core documentation, not a versioned 2.1.4 index. Local compilation checks API compatibility and generated SQL, not live locks, rollback, ordering, or isolation. Those require the existing Postgres contract and locking tests.

## Testcontainers

**Resolved ID:** `/testcontainers/testcontainers-python`.

**Query:** exposed ports, automatic host-port allocation, and retrieving actual connection information for a container.

**Primary sources returned by Context7:**

- [Networking](https://github.com/testcontainers/testcontainers-python/blob/main/docs/features/networking.md).
- [Creating a container](https://github.com/testcontainers/testcontainers-python/blob/main/docs/features/creating_container.md).

**What it supports:** `with_exposed_ports(5672)` delegates host-port allocation; `get_exposed_port(5672)` returns the selected port after startup. The repository already uses both methods and `get_container_host_ip()` for ordinary tests.

**Replacement rejected:** deleting the fixed-port setup in the two broker-restart tests. Although both tests restart the same underlying container, that alone does not guarantee a stable automatically allocated host port. Docker's [Engine API documentation](https://github.com/moby/moby/blob/master/api/docs/v1.24.md) explicitly describes ports being deallocated on stop and potentially changing when restarted. These tests keep the existing bus and its URL, so the fixed binding has a purpose. No savings are counted for removing it.

**Limits:** no candidate restart was performed. Context7 did not return an aio-pika-compatible RabbitMQ URL helper, so no replacement is claimed for the small existing `amqp_url` function. The Compose-backed E2E allocator is separate and no replacement is proposed for it either.

## Python standard library

**Resolved ID:** `/python/cpython`.

**Query:** deterministic async-generator cleanup with `contextlib.aclosing`, and dataclass conversion with `dataclasses.asdict`.

**Primary sources returned by Context7:**

- [contextlib documentation](https://github.com/python/cpython/blob/main/Doc/library/contextlib.rst).
- [dataclasses documentation](https://github.com/python/cpython/blob/main/Doc/library/dataclasses.rst).
- [asyncio development notes](https://github.com/python/cpython/blob/main/Doc/library/asyncio-dev.rst).

**What it supports:**

- F08: `aclosing` calls the generator's `aclose()` when leaving the async context, including an early return. A local probe confirmed cleanup without a manual `finally`.
- F03: `asdict(order)` provides a dictionary of dataclass fields. At this snapshot those fields exactly match the `orders` insert columns. The proposed values retain the explicit stage-list conversion.

**Limits:** `asdict` traverses nested containers/dataclasses and deep-copies other values. It is appropriate here at the JSON persistence boundary; it is not a free shallow mapping for arbitrary objects. Do not blindly unpack `Decision` into SQL because its `close` field is not an order column. Do not erase explicit enum conversion from database rows by assuming dataclass constructors validate types.

## aio-pika

**Resolved ID:** `/mosquito/aio-pika`.

**Queries:** `IncomingMessage.process`, acknowledgement on context exit, exception handling, stale channel behavior, and channel context management.

**Primary sources returned by Context7:**

- [Work queues tutorial](https://github.com/mosquito/aio-pika/blob/master/docs/source/rabbitmq-tutorial/2-work-queues.md).
- [Message implementation](https://github.com/mosquito/aio-pika/blob/master/aio_pika/message.py).
- [RPC example](https://github.com/mosquito/aio-pika/blob/master/docs/source/rabbitmq-tutorial/6-rpc.md).

**Additional exact-version check for F05:** the channel-context query mainly returned connection, pool, and transaction examples. Rather than infer a channel API from them, inspected the installed 10.1.1 `Connection.channel`, `Channel.__aenter__`, and `Channel.__aexit__` definitions. `Connection.channel` explicitly documents `async with connection.channel()`. Entry initializes an unopened channel; exit calls `close(exc_val)`. `RobustChannel` inherits both context methods and the abstract channel interface declares them. Official source references: [connection API implementation](https://docs.aio-pika.com/_modules/aio_pika/connection.html), [channel API implementation](https://docs.aio-pika.com/_modules/aio_pika/channel.html).

**Supported cut:** F05 replaces only manual channel acquisition/final cleanup. The context exit supplies the exiting exception to `close`; the current manual call does not. There are no application callbacks attached to these consumer channels, but live cancellation, redelivery, and restart behavior still need the existing broker tests after implementation.

**Finding:** `message.process()` is a useful library context manager, but it does not erase the repository's acknowledgement policy. The tutorial's broad exception description should not be read as an unconditional requeue guarantee; the processor's requeue option and implementation determine rejection behavior.

The current command stream acknowledges before parsing/yielding and drops expired commands. The result stream yields first and acknowledges only when resumed; closing the generator while suspended leaves the last result available for redelivery. `_ack` also handles a stale underlying channel. Replacing both with a uniform context around `yield` would change these behaviors or require enough special handling to cancel the supposed simplification.

**Decision:** retain the adapter's explicit acknowledgement handling, publisher confirmation, unroutable translation, invalid-message handling, and restart behavior. The only aio-pika savings counted are F05's channel cleanup wrappers, not message-processing policy.

## Native platform references

- F01: [Docker multi-stage builds and named targets](https://docs.docker.com/build/building/multi-stage/). Use named service targets, retaining the separate dependency sets and source-copy layers.
- F12: [GitHub matrix jobs and failure behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations). Use include entries and `fail-fast: false` to preserve the independent suites.

These platform references were verified through official documentation in addition to the Context7 library lookups. No image build or GitHub workflow run was performed.
