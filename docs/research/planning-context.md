# Planning context summary

Greenfield interview deliverable. Repository contains only a README, `docs/brief.md` (the task statement), and the requirements-only plan at `docs/plans/2026-10-13-1425-feat-async-order-orchestrator-plan.md`. No code exists yet. Toolchain on the machine: Python 3.14.6, uv 0.12.18, Docker 29.6.1.

## Problem

Accept orders over a REST API and run each through a configurable, linear sequence of simulated stages (inventory check, fraud analysis, shipping label) that are slow and fail intermittently. The evaluated part is orchestration, state management, and observability of the workflow, not the stage logic. Must be Python, services communicating asynchronously, Docker Compose, small and well-tested over sprawling.

## Settled decisions (constraints, not open questions)

- Linear pipeline, ordered list of stage names in config at orchestrator startup. Rejected: per-order-type pipelines, conditional skip, DAG.
- Orchestrator service owns all order state in Postgres and holds the explicit state machine. Rejected: Postgres-as-queue (stages polling a job table), Temporal.
- RabbitMQ as broker, one command queue per stage, results come back to the orchestrator. Rejected: Redis Streams.
- One container per stage, built from one base stage-service template (Python package). Stage services are stateless: never touch the DB. Rejected: one API container plus one worker pool.
- Compensation (saga undo) built now: every stage has an undo handler, template default is a no-op success; orchestrator runs undo of completed stages in reverse on permanent failure. Rejected: no compensation.
- Order row with current state plus append-only attempts table. Rejected: event-sourced log with projection.
- Client idempotency key on POST; same key + different payload is a 409 conflict.
- Observability in this version: per-attempt history in GET status plus a small polling timeline page. Structured logs and metrics deferred.
- Retry: bounded attempts with backoff per stage, deadline per dispatched command; a periodic sweep in the orchestrator re-dispatches overdue work (dispatched command past deadline, retry whose due time passed, order stuck in pending). Backoff wait persisted as next-attempt due time.
- Command carries its deadline; stage template discards expired commands without running the handler. Handlers idempotent per (order id, stage). Pipeline stage list snapshotted onto the order at submission.
- Failure injection: per-stage config profile (latency range, transient failure probability, permanent rejection probability) plus a per-order outcome script for deterministic tests.
- Testing: state machine unit-testable with no broker or DB; end-to-end suite drives the Compose stack through the API only.
- Exactly one orchestrator instance in this version.

If you find evidence a settled decision cannot work, report it. Do not suppress it.

## Open technical questions planning must settle

1. Python RabbitMQ client for the orchestrator and the stage template: aio-pika vs pika vs Dramatiq vs Kombu vs FastStream. Needs: per-stage queues, a reply/results queue, manual ack/nack, prefetch, dead-letter exchange, publisher confirms, reconnect handling, asyncio fit with FastAPI.
2. Current stable versions (October 2026) and the Docker image tags to pin: Python, FastAPI, Pydantic, SQLAlchemy (async) or psycopg3, asyncpg, Alembic or plain SQL migrations, aio-pika (or chosen client), pytest, pytest-asyncio, httpx, testcontainers-python, Postgres image, RabbitMQ management image, uv base image pattern.
3. Where the stage service acks: before or after the handler, given orchestrator-owned attempt numbering already provides retry; interaction of broker redelivery with the orchestrator's attempt cap.
4. Dual-write between saving state and publishing a command: sweep-based recovery versus transactional outbox; whether publisher confirms are needed.
5. Sweep implementation in a single-instance asyncio service: periodic task with `SELECT ... FOR UPDATE SKIP LOCKED` or plain polling.
6. Serving a static timeline page from FastAPI versus a separate container.
