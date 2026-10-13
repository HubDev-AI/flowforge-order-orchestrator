import random
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from contracts import Command, CommandKind, OutcomeScript, ScriptToken
from stage_kit.simulator import Profile, Simulator

QUIET = Profile(latency_min_s=0, latency_max_s=0, transient_probability=0, reject_probability=0)


def command(stage="fraud", attempt=1, kind=CommandKind.EXECUTE, script=None, deadline_in=60):
    return Command(
        order_id=uuid4(),
        stage=stage,
        attempt=attempt,
        kind=kind,
        deadline_at=datetime.now(UTC) + timedelta(seconds=deadline_in),
        payload={},
        script=OutcomeScript.model_validate(script or {}),
    )


def simulator(profile=QUIET, seed=1, sleeps=None):
    async def record(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return Simulator(profile, random.Random(seed), sleep=record)


async def test_a_script_decides_each_attempt_and_later_attempts_succeed():
    script = {"fraud": {"execute": ["retry", "retry", "succeed"]}}
    sim = simulator()

    decided = [await sim.decide(command(attempt=n, script=script)) for n in (1, 2, 3, 4)]

    assert decided == [
        ScriptToken.RETRY,
        ScriptToken.RETRY,
        ScriptToken.SUCCEED,
        ScriptToken.SUCCEED,
    ]


async def test_a_script_for_one_stage_leaves_the_others_on_their_profile():
    always_reject = Profile(
        latency_min_s=0, latency_max_s=0, transient_probability=0, reject_probability=1
    )
    script = {"fraud": {"execute": ["succeed"]}}

    decided = await simulator(always_reject).decide(command(stage="inventory", script=script))

    assert decided is ScriptToken.REJECT


async def test_script_tokens_are_keyed_by_kind():
    script = {"fraud": {"undo": ["retry"]}}
    sim = simulator()

    execute = await sim.decide(command(script=script))
    undo = await sim.decide(command(script=script, kind=CommandKind.UNDO))

    assert (execute, undo) == (ScriptToken.SUCCEED, ScriptToken.RETRY)


@pytest.mark.parametrize(
    ("transient", "reject", "expected"),
    [(0, 0, ScriptToken.SUCCEED), (1, 0, ScriptToken.RETRY), (0, 1, ScriptToken.REJECT)],
)
async def test_the_profile_probabilities_decide_an_unscripted_stage(transient, reject, expected):
    profile = Profile(
        latency_min_s=0,
        latency_max_s=0,
        transient_probability=transient,
        reject_probability=reject,
    )

    assert await simulator(profile).decide(command()) is expected


async def test_a_seed_makes_the_unscripted_decisions_reproducible():
    coin_flip = Profile(
        latency_min_s=0, latency_max_s=0, transient_probability=0.5, reject_probability=0.25
    )

    runs = []
    for _ in range(2):
        sim = simulator(coin_flip, seed=7)
        runs.append([await sim.decide(command()) for _ in range(30)])

    assert runs[0] == runs[1]
    assert len(set(runs[0])) > 1


async def test_the_profile_latency_is_drawn_from_its_range_for_unscripted_stages():
    profile = Profile(
        latency_min_s=0.2, latency_max_s=0.4, transient_probability=0, reject_probability=0
    )
    sleeps = []
    sim = simulator(profile, sleeps=sleeps)

    for _ in range(20):
        await sim.decide(command())

    assert len(sleeps) == 20
    assert all(0.2 <= seconds <= 0.4 for seconds in sleeps)


async def test_scripted_attempts_do_not_wait():
    sleeps = []
    profile = Profile(
        latency_min_s=5, latency_max_s=5, transient_probability=0, reject_probability=0
    )

    await simulator(profile, sleeps=sleeps).decide(
        command(script={"fraud": {"execute": ["retry"]}})
    )

    assert sleeps == []


async def test_a_timeout_token_returns_only_after_the_deadline_has_passed():
    deadline_in = 0.05
    script = {"fraud": {"execute": ["timeout"]}}
    sim = Simulator(QUIET, random.Random(1))
    scripted = command(script=script, deadline_in=deadline_in)

    decided = await sim.decide(scripted)

    assert datetime.now(UTC) >= scripted.deadline_at
    assert decided is ScriptToken.SUCCEED
