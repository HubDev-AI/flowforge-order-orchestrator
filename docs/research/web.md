**Research value: high** -- All versions verified live against PyPI JSON, Docker Hub tags API, ghcr manifests on 2026-10-13; client choice is clear-cut.

## 1. RabbitMQ client shortlist

| Option | Last release (PyPI) | Asyncio fit | Orchestration hidden | Verdict |
|---|---|---|---|---|
| **aio-pika 10.1.1** (2026-10-10; deps: aiormq 7.2.2, yarl, pamqp) | very active (10.0.4 -> 10.1.0 -> 10.1.1 in weeks; changelog: reconnect/robust-consumer fixes, publish-failure-across-reconnect tests) | native asyncio, same loop as FastAPI | none: raw channel/exchange/queue API | **Recommend** |
| FastStream 0.7.7 (2026-09-23; fast-depends, anyio) | active | native, FastAPI-integrated router | medium: decorator/DI framework, retry/ack policy is "middleware"; uses aio-pika underneath | viable but a framework layer on top of what we must own; extra concept load for a small deliverable |
| pika 1.4.4 (2026-08-06) | maintained | not asyncio-native (SelectConnection/AsyncioConnection is callback-style, poor ergonomics) | none | reject for FastAPI orchestrator; fine only for sync scripts |
| Dramatiq 2.2.1 (2026-09-02) | active | sync workers, threads | HIGH: owns retries, backoff, attempt counting, DLQ | conflicts with "own state machine and attempt numbering" |
| Kombu 5.6.2 (2025-12-29) | slower | sync (Celery's transport) | high-ish abstraction | reject |
| taskiq-aio-pika 0.6.1 | active | async | task-queue semantics hide state | reject |

aio-pika fit to needs: `connect_robust` (auto reconnect + topology restore), `channel.set_qos(prefetch_count=)`, `queue.consume`/`queue.iterator` with `message.process(requeue=..)` or manual `ack/nack/reject`, publisher confirms on by default (`publisher_confirms=True` channel option; `publish` awaits confirm, raises on nack/unroutable with mandatory), queue args `x-dead-letter-exchange`, `x-message-ttl`, per-message `expiration`. Python >=3.11. Caveat: robust-connection consumer-restore behaviour changed in 10.0.x/10.1.x (releases 10.1.0 "stop automatic channel restoration when connection closes", 10.1.1 "restore robust consumers after broker cancellation"): pin `aio-pika==10.1.1` exactly and keep an integration test that kills/restarts the broker container. Version jumped 9.6.x (Mar 2026) -> 10.x within seven months, so expect API churn; pin.

Decision-relevant note (Q3/Q4): aio-pika gives the primitives only; nothing hides redelivery. Broker redelivery after a stage crash before ack is invisible to the orchestrator's attempt counter unless the command carries (order_id, stage, attempt) and the handler is idempotent per that key (already settled). Publisher confirms are cheap in aio-pika (default on) and close the "publish lost in broker" gap but not the DB-commit-then-crash dual-write gap; that stays the sweep's job.

## 2. Verified versions (October 2026)

| Component | Pin | Released | Notes |
|---|---|---|---|
| Python | **3.14** (`python:3.14.8-slim`, floating `3.14-slim`) | host has 3.14.6 | cp314 wheels exist for asyncpg 0.32.0, psycopg-binary 3.3.6, pydantic-core 2.50.1; 3.13 (`3.13.16-slim`) is the safe fallback, nothing found that blocks 3.14. aiormq/aio-pika are pure Python |
| FastAPI | 0.143.0 | 2026-10-08 | needs >=3.10 |
| Pydantic | 2.14.1 | 2026-10-11 | |
| pydantic-settings | 2.15.0 | 2026-08-07 | |
| uvicorn | 0.54.0 | 2026-09-25 | |
| SQLAlchemy | 2.1.4 | 2026-10-07 | 2.1 line is current (needs >=3.11); async API via `sqlalchemy[asyncio]` |
| asyncpg | 0.32.0 | 2026-10-06 | |
| psycopg | 3.3.6 (+ `psycopg[binary]`) | 2026-09-18 | alternative driver to asyncpg; pick one (asyncpg = fewer moving parts with SA async) |
| Alembic | 1.20.0 | 2026-09-11 | |
| aio-pika | 10.1.1 | 2026-10-10 | |
| pytest | 9.1.1 | 2026-06-19 | |
| pytest-asyncio | 1.4.0 | 2026-05-26 | |
| httpx | 0.28.1 | 2024-12-06 | no release in ~22 months; still the standard test client, stale but not blocking |
| testcontainers | 4.15.0 | 2026-07-24 | |
| anyio | 4.15.1 | 2026-09-05 | |

Images (all confirmed to exist): `postgres:18.6-alpine` (also `17.11-alpine`; 18 is current major, use 17 only if conservative); `rabbitmq:4.3.6-management` (`4.3-management` floating; 4.2.9 and 4.1.8 also current patch lines); Python base `python:3.14.8-slim`; uv `ghcr.io/astral-sh/uv:0.13.0` (latest release 2026-10-09; `0.13` and `latest` also exist). Combined uv+python images exist: `ghcr.io/astral-sh/uv:python3.14-trixie-slim`, `python3.13-trixie-slim`, `python3.14-bookworm-slim`.

uv Dockerfile pattern (from uv docs): `FROM python:3.14.8-slim`; `COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/`; `ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy`; copy `pyproject.toml uv.lock`, `RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project --no-dev`; then copy source and `uv sync --locked --no-dev`; `ENV PATH="/app/.venv/bin:$PATH"`. (Pattern from memory of uv docs, not re-fetched this session; verify flags with `uv sync --help`.) Pin exact versions in `uv.lock`, not just tags; rabbitmq tag digests optional.

## What would change the plan
- Choosing FastStream instead of aio-pika only if the team prefers decorator-style consumers; costs hidden ack/retry middleware semantics.
- aio-pika 10.x reconnect/consumer-restore churn: pin exactly and test broker restart.
- No evidence found that any settled decision (RabbitMQ, per-stage queues, stateless stage workers) cannot work.

## Sources
- https://pypi.org/pypi/{aio-pika,aiormq,pika,dramatiq,kombu,faststream,fastapi,pydantic,sqlalchemy,asyncpg,psycopg,psycopg-binary,alembic,pytest,pytest-asyncio,httpx,testcontainers,uvicorn,pydantic-core}/json -- versions, dates, wheels, deps
- https://hub.docker.com/v2/repositories/library/{python,postgres,rabbitmq}/tags -- tag existence
- https://ghcr.io/v2/astral-sh/uv/manifests/<tag> and https://api.github.com/repos/astral-sh/uv/releases/latest -- uv tags
- https://api.github.com/repos/mosquito/aio-pika/releases -- aio-pika 10.x changelog
