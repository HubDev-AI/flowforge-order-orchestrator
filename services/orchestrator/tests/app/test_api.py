from uuid import UUID, uuid4

from orchestrator.app.main import MAX_BODY_BYTES

from .conftest import pipeline_config


async def test_submission_returns_the_order_id_and_the_initial_state(submit):
    response = await submit()

    assert response.status_code == 201
    assert response.json()["state"] == "pending"
    UUID(response.json()["id"])


async def test_the_payload_comparison_ignores_key_order(submit):
    first = await submit(payload={"sku": "A-1", "qty": 2}, key="k1")
    replay = await submit(payload={"qty": 2, "sku": "A-1"}, key="k1")

    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


async def test_reusing_a_key_with_another_payload_is_a_conflict_and_changes_nothing(submit, status):
    first = (await submit(payload={"sku": "A-1"}, key="k1")).json()

    conflict = await submit(payload={"sku": "B-2"}, key="k1")

    assert conflict.status_code == 409
    assert (await status(first["id"]))["state"] == "pending"


async def test_a_script_changes_the_payload_for_the_idempotency_comparison(submit):
    await submit(key="k1")

    conflict = await submit(key="k1", script={"fraud": {"execute": ["reject"]}})

    assert conflict.status_code == 409


async def test_a_missing_idempotency_key_is_rejected(client):
    response = await client.post("/api/orders", json={"payload": {"sku": "A-1"}})

    assert response.status_code == 422


async def test_a_script_naming_an_unknown_stage_is_rejected_with_that_stage(submit):
    response = await submit(script={"gift_wrap": {"execute": ["succeed"]}})

    assert response.status_code == 422
    assert "gift_wrap" in response.json()["detail"]


async def test_a_script_with_an_unknown_token_is_rejected(submit):
    response = await submit(script={"fraud": {"execute": ["explode"]}})

    assert response.status_code == 422


async def test_an_unknown_order_is_not_found(client):
    assert (await client.get(f"/api/orders/{uuid4()}")).status_code == 404


async def test_a_malformed_order_id_is_not_found(client):
    assert (await client.get("/api/orders/not-a-uuid")).status_code == 404


async def test_a_pending_order_hides_stage_and_due_time(submit, status):
    order = (await submit()).json()

    body = await status(order["id"])

    assert body["state"] == "pending"
    assert (body["current_stage"], body["next_attempt_due_at"], body["terminal_reason"]) == (
        None,
        None,
        None,
    )
    assert body["attempts"] == []


async def test_an_oversized_idempotency_key_is_rejected(submit):
    response = await submit(key="k" * 256)

    assert response.status_code == 422


def body_of(size: int) -> bytes:
    prefix, suffix = b'{"payload": {"blob": "', b'"}}'
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


async def test_a_body_at_the_cap_is_accepted_and_one_byte_more_is_rejected(client):
    headers = {"Idempotency-Key": "k1", "Content-Type": "application/json"}

    at_cap = await client.post("/api/orders", content=body_of(MAX_BODY_BYTES), headers=headers)
    over = await client.post(
        "/api/orders",
        content=body_of(MAX_BODY_BYTES + 1),
        headers=headers | {"Idempotency-Key": "k2"},
    )

    assert at_cap.status_code == 201
    assert over.status_code == 413


async def test_a_streamed_body_is_counted_as_it_arrives(client):
    def stream(size):
        async def chunks():
            body = body_of(size)
            for start in range(0, len(body), 4096):
                yield body[start : start + 4096]

        return chunks()

    headers = {"Content-Type": "application/json"}
    small = await client.post(
        "/api/orders", content=stream(1024), headers=headers | {"Idempotency-Key": "k1"}
    )
    large = await client.post(
        "/api/orders",
        content=stream(MAX_BODY_BYTES + 1),
        headers=headers | {"Idempotency-Key": "k2"},
    )

    assert small.status_code == 201
    assert large.status_code == 413


async def test_a_replay_is_answered_from_the_stored_order_after_the_config_tightens(
    submit, runtime
):
    script = {"fraud": {"execute": ["retry", "retry", "succeed"]}}
    first = await submit(script=script)
    runtime.config = pipeline_config(["inventory", "shipping"])

    replay = await submit(script=script)
    changed = await submit(script={"fraud": {"execute": ["reject"]}})

    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.json()["id"] == first.json()["id"]
    assert changed.status_code == 409


async def test_a_replay_survives_a_lower_attempt_cap(submit, runtime):
    script = {"fraud": {"execute": ["retry", "retry", "succeed"]}}
    first = await submit(script=script)
    tightened = runtime.config.model_dump()
    tightened["defaults"]["max_attempts"] = 1
    runtime.config = type(runtime.config).model_validate(tightened)

    replay = await submit(script=script)
    fresh = await submit(script=script, key="k2")

    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert fresh.status_code == 422


async def test_a_script_longer_than_the_attempt_cap_is_rejected(submit):
    response = await submit(script={"fraud": {"execute": ["retry"] * 4}})

    assert response.status_code == 422
    assert "4 execute tokens for fraud" in response.json()["detail"]
    assert "3 attempts" in response.json()["detail"]


async def test_a_script_as_long_as_the_attempt_cap_is_accepted(submit):
    response = await submit(script={"fraud": {"execute": ["retry"] * 3}})

    assert response.status_code == 201


async def test_a_new_order_starts_its_workflow_once_and_a_replay_starts_nothing(submit, launched):
    first = (await submit(key="k1")).json()

    replay = await submit(key="k1")
    conflict = await submit(payload={"sku": "B-2"}, key="k1")

    assert (replay.status_code, conflict.status_code) == (200, 409)
    assert replay.json()["id"] == first["id"]
    assert [str(order.id) for order in launched] == [first["id"]]
    assert launched[0].stages == ("inventory", "fraud", "shipping")


async def test_a_rejected_submission_starts_nothing(submit, launched):
    await submit(script={"gift_wrap": {"execute": ["succeed"]}})

    assert launched == []
