from datetime import timedelta

import httpx
import pytest
from orchestrator.adapters.memory_store import MemoryStore
from orchestrator.app.config import PipelineConfig
from orchestrator.app.lifespan import Runtime
from orchestrator.app.main import create_app

STAGES = ["inventory", "fraud", "shipping"]


def pipeline_config(stages=STAGES) -> PipelineConfig:
    return PipelineConfig.model_validate(
        {
            "stages": stages,
            "defaults": {
                "max_attempts": 3,
                "backoff_base_seconds": 1.0,
                "backoff_cap_seconds": 30.0,
                "deadline_seconds": 10.0,
                "max_wait_seconds": 60.0,
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
def launched():
    return []


@pytest.fixture
def runtime(clock, launched):
    async def launch(order):
        launched.append(order)

    return Runtime(pipeline_config(), MemoryStore(), launch, clock)


@pytest.fixture
async def client(runtime):
    app = create_app(with_lifespan=False)
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


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
