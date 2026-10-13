import asyncio

import httpx
import pytest
from contracts import CommandKind, Outcome, Result
from orchestrator.app.lifespan import lifespan
from orchestrator.app.main import create_app
from pydantic import ValidationError

PIPELINE = """
stages = [{stages}]

[defaults]
max_attempts = 3
backoff_base_seconds = 1.0
backoff_cap_seconds = 30.0
dispatch_deadline_seconds = 10.0

[sweep]
interval_seconds = 0.05
pending_threshold_seconds = 0.0

[adapters]
bus = "memory"
store = "memory"
"""


def write_pipeline(tmp_path, monkeypatch, stages: str):
    path = tmp_path / "pipeline.toml"
    path.write_text(PIPELINE.format(stages=stages))
    monkeypatch.setenv("PIPELINE_CONFIG", str(path))


async def wait_for(condition, within=3.0):
    async with asyncio.timeout(within):
        while not await condition():
            await asyncio.sleep(0.02)


async def test_startup_fails_when_the_pipeline_names_a_stage_twice(tmp_path, monkeypatch):
    write_pipeline(tmp_path, monkeypatch, '"inventory", "fraud", "fraud"')

    with pytest.raises(ValidationError, match="duplicate stage: fraud"):
        async with lifespan(create_app(with_lifespan=False)):
            pass


async def test_the_running_service_dispatches_and_advances_an_order_on_its_own(
    tmp_path, monkeypatch
):
    write_pipeline(tmp_path, monkeypatch, '"inventory", "fraud"')
    app = create_app(with_lifespan=False)

    async with (
        lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        created = await client.post(
            "/api/orders", json={"payload": {"sku": "A-1"}}, headers={"Idempotency-Key": "k1"}
        )
        order_id = created.json()["id"]

        async def status():
            return (await client.get(f"/api/orders/{order_id}")).json()

        async def dispatched():
            return (await status())["state"] == "running"

        await wait_for(dispatched)
        consumer = app.state.runtime.bus.consume_commands("inventory")
        command = await anext(consumer)
        await consumer.aclose()
        await app.state.runtime.bus.publish_result(
            Result(
                order_id=command.order_id,
                stage="inventory",
                attempt=command.attempt,
                kind=CommandKind.EXECUTE,
                outcome=Outcome.SUCCEEDED,
            )
        )

        async def advanced():
            return (await status())["current_stage"] == "fraud"

        await wait_for(advanced)
