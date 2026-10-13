import asyncio
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from bus import MemoryBus
from contracts import Command, CommandKind, Outcome, Result
from orchestrator.adapters.memory_store import MemoryStore
from orchestrator.app.config import PipelineConfig
from orchestrator.app.dispatcher import Dispatcher
from orchestrator.app.lifespan import Runtime
from orchestrator.app.main import create_app
from orchestrator.app.results import ResultsConsumer

STAGES = ["inventory", "fraud", "shipping"]


def pipeline_config(stages=STAGES, pending_threshold_seconds=0.0) -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "stages": stages,
            "defaults": {
                "max_attempts": 3,
                "backoff_base_seconds": 1.0,
                "backoff_cap_seconds": 30.0,
                "dispatch_deadline_seconds": 10.0,
            },
            "sweep": {
                "interval_seconds": 1.0,
                "pending_threshold_seconds": pending_threshold_seconds,
            },
            "adapters": {"bus": "memory", "store": "memory"},
        }
    )


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(now):
    return FakeClock(now)


@pytest.fixture
async def runtime(clock):
    bus = MemoryBus(clock=clock)
    config = pipeline_config()
    await bus.declare_topology(config.stages)
    return Runtime(config, MemoryStore(), bus, clock, asyncio.Event())


@pytest.fixture
def dispatcher(runtime):
    return Dispatcher(runtime.store, runtime.bus, runtime.config, runtime.clock, runtime.nudge)


@pytest.fixture
def results(runtime):
    return ResultsConsumer(runtime.store, runtime.bus, runtime.config, runtime.clock, runtime.nudge)


@pytest.fixture
async def client(runtime):
    app = create_app(with_lifespan=False)
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def client_for(clock):
    clients = []

    async def build(bus):
        config = pipeline_config()
        await bus.declare_topology(config.stages)
        runtime = Runtime(config, MemoryStore(), bus, clock, asyncio.Event())
        app = create_app(with_lifespan=False)
        app.state.runtime = runtime
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)

        async def submit():
            return await client.post(
                "/api/orders",
                json={"payload": {"sku": "A-1"}},
                headers={"Idempotency-Key": "k1"},
            )

        return runtime, submit

    yield build
    for client in clients:
        await client.aclose()


@pytest.fixture
def submit(client):
    async def post(payload=None, script=None, key="k1") -> httpx.Response:
        body = {"payload": payload if payload is not None else {"sku": "A-1"}}
        if script is not None:
            body["script"] = script
        return await client.post("/api/orders", json=body, headers={"Idempotency-Key": key})

    return post


@pytest.fixture
def status(client):
    async def get(order_id) -> dict:
        response = await client.get(f"/api/orders/{order_id}")
        assert response.status_code == 200, response.text
        return response.json()

    return get


@pytest.fixture
def next_command(runtime):
    async def pull(stage: str) -> Command:
        consumer = runtime.bus.consume_commands(stage)
        try:
            async with asyncio.timeout(1):
                return await anext(consumer)
        finally:
            await consumer.aclose()

    return pull


@pytest.fixture
def reply():
    def build(command: Command, outcome=Outcome.SUCCEEDED, **fields) -> Result:
        return Result(
            order_id=command.order_id,
            stage=command.stage,
            attempt=command.attempt,
            kind=command.kind,
            outcome=outcome,
            **fields,
        )

    return build


@pytest.fixture
def history(status):
    async def summarize(order_id: UUID | str) -> list[tuple]:
        body = await status(order_id)
        return [(a["stage"], a["kind"], a["attempt"], a["outcome"]) for a in body["attempts"]]

    return summarize


EXECUTE, UNDO = CommandKind.EXECUTE.value, CommandKind.UNDO.value
