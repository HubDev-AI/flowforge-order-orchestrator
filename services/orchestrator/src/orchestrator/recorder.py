"""The single writer of order state and attempt history.

Stage workers send an event when an attempt starts and when it ends. One recorder consumes them
from one queue, so events arrive in the order they were sent. A recorder retry can still deliver
an event late, so writes are guarded: a late start never reopens a finished attempt or moves the
order back, and a finish recorded before its start creates the row.
"""

import os
from datetime import datetime
from typing import Any

import dramatiq
import psycopg
from contracts import (
    RECORD_ACTOR,
    RECORDS_QUEUE,
    CommandKind,
    HandOffStamp,
    Outcome,
    stage_message,
)
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.brokers.stub import StubBroker
from psycopg.types.json import Jsonb

ACTIVE = {"pending", "running", "compensating"}

_url = os.environ.get("RABBITMQ_URL")
broker = RabbitmqBroker(url=_url, confirm_delivery=True) if _url else StubBroker()
broker.add_middleware(HandOffStamp())
dramatiq.set_broker(broker)


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def start(
    order_id: str,
    stage: str,
    kind: str,
    attempt: int,
    started_at: datetime,
    deadline_at: datetime,
) -> None:
    state = "running" if kind == CommandKind.EXECUTE else "compensating"
    with _connect() as connection:
        opened = connection.execute(
            "INSERT INTO attempts (order_id, stage, kind, attempt, deadline_at, started_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (order_id, stage, kind, attempt) DO UPDATE SET"
            " started_at = EXCLUDED.started_at, deadline_at = EXCLUDED.deadline_at"
            " WHERE attempts.finished_at IS NULL",
            (order_id, stage, kind, attempt, deadline_at, started_at),
        ).rowcount
        if opened:
            connection.execute(
                "UPDATE orders SET state = %s, current_stage = %s, current_kind = %s,"
                " next_due_at = NULL, waiting_since = NULL, updated_at = %s"
                " WHERE id = %s AND state IN ('pending', 'running', 'compensating')",
                (state, stage, kind, started_at, order_id),
            )


def execute_output(connection: psycopg.Connection, order_id: str, stage: str) -> Any:
    row = connection.execute(
        "SELECT stage_output FROM attempts WHERE order_id = %s AND stage = %s"
        " AND kind = 'execute' AND outcome = 'succeeded'",
        (order_id, stage),
    ).fetchone()
    return None if row is None else row[0]


def _close(
    connection: psycopg.Connection,
    order_id: str,
    stage: str,
    kind: str,
    attempt: int,
    outcome: Outcome,
    reason: str | None,
    output: dict[str, Any] | None,
    at: datetime,
) -> None:
    connection.execute(
        "INSERT INTO attempts (order_id, stage, kind, attempt, outcome, reason, stage_output,"
        " deadline_at, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (order_id, stage, kind, attempt) DO UPDATE SET outcome = EXCLUDED.outcome,"
        " reason = EXCLUDED.reason, stage_output = EXCLUDED.stage_output,"
        " finished_at = EXCLUDED.finished_at",
        (
            order_id,
            stage,
            kind,
            attempt,
            outcome.value,
            reason,
            None if output is None else Jsonb(output),
            at,
            at,
            at,
        ),
    )


def succeed(
    order_id: str,
    plan: list[dict[str, Any]],
    position: int,
    kind: str,
    attempt: int,
    output: dict[str, Any] | None,
    at: datetime,
) -> None:
    stage = plan[position]["name"]
    if kind == CommandKind.EXECUTE:
        last = position == len(plan) - 1
        state = "completed" if last else "running"
        current = None if last else plan[position + 1]["name"]
    else:
        first = position == 0
        state = "failed" if first else "compensating"
        current = None if first else plan[position - 1]["name"]
    with _connect() as connection:
        _close(connection, order_id, stage, kind, attempt, Outcome.SUCCEEDED, None, output, at)
        connection.execute(
            "UPDATE orders SET state = %s, current_stage = %s, current_kind = %s,"
            " next_due_at = NULL, waiting_since = %s, updated_at = %s WHERE id = %s",
            (state, current, None if current is None else kind, current and at, at, order_id),
        )


def retry(
    order_id: str,
    stage: str,
    kind: str,
    attempt: int,
    outcome: Outcome,
    reason: str | None,
    at: datetime,
    due_at: datetime,
) -> None:
    with _connect() as connection:
        _close(connection, order_id, stage, kind, attempt, outcome, reason, None, at)
        connection.execute(
            "UPDATE orders SET next_due_at = %s, waiting_since = %s, updated_at = %s WHERE id = %s",
            (due_at, due_at, at, order_id),
        )


def fail(
    order_id: str,
    plan: list[dict[str, Any]],
    position: int,
    kind: str,
    attempt: int,
    outcome: Outcome,
    reason: str | None,
    at: datetime,
    submission: dict[str, Any],
) -> None:
    """A permanent failure: an execute starts compensation, an undo ends the order."""
    stage = plan[position]["name"]
    summary = f"{stage} {outcome.value}: {reason}"
    with _connect() as connection:
        _close(connection, order_id, stage, kind, attempt, outcome, reason, None, at)
        if kind != CommandKind.EXECUTE:
            connection.execute(
                "UPDATE orders SET state = 'compensation_failed', current_stage = NULL,"
                " current_kind = NULL, next_due_at = NULL, waiting_since = NULL,"
                " terminal_reason = concat_ws('; ', terminal_reason, %s::text), updated_at = %s"
                " WHERE id = %s",
                (f"undo of {summary}", at, order_id),
            )
            return
        connection.execute(
            "UPDATE orders SET state = 'compensating', current_stage = %s,"
            " current_kind = 'undo', next_due_at = NULL, terminal_reason = %s,"
            " waiting_since = %s, updated_at = %s WHERE id = %s",
            (stage, summary, at, at, order_id),
        )
        undo = list(range(position, -1, -1))
        outputs = {p: execute_output(connection, order_id, plan[p]["name"]) for p in undo}
    compensate(order_id, plan, undo, submission, outputs)


def give_up(
    order_id: str,
    plan: list[dict[str, Any]],
    position: int,
    kind: str,
    waiting_since: datetime,
    at: datetime,
    submission: dict[str, Any],
) -> None:
    """No worker took the order's current work within the stage's max wait.

    Applies only if the order is still waiting on the same handoff with nothing running. A
    waiting execute is undone only if it ever ran; the stages before it are undone in reverse.
    """
    stage = plan[position]["name"]
    max_wait = plan[position]["policy"]["max_wait_seconds"]
    summary = f"{stage} unavailable: no worker took it within {max_wait:g} s"
    with _connect() as connection:
        row = connection.execute(
            "SELECT state, waiting_since FROM orders WHERE id = %s FOR UPDATE", (order_id,)
        ).fetchone()
        if row is None or row[0] not in ACTIVE or row[1] != waiting_since:
            return
        running = connection.execute(
            "SELECT 1 FROM attempts WHERE order_id = %s AND finished_at IS NULL", (order_id,)
        ).fetchone()
        if running:
            return
        if kind != CommandKind.EXECUTE:
            connection.execute(
                "UPDATE orders SET state = 'compensation_failed', current_stage = NULL,"
                " current_kind = NULL, next_due_at = NULL, waiting_since = NULL,"
                " terminal_reason = concat_ws('; ', terminal_reason, %s::text), updated_at = %s"
                " WHERE id = %s",
                (f"undo of {summary}", at, order_id),
            )
            return
        ran = connection.execute(
            "SELECT 1 FROM attempts WHERE order_id = %s AND stage = %s AND kind = 'execute'",
            (order_id, stage),
        ).fetchone()
        undo = list(range(position if ran else position - 1, -1, -1))
        if not undo:
            connection.execute(
                "UPDATE orders SET state = 'failed', current_stage = NULL, current_kind = NULL,"
                " next_due_at = NULL, waiting_since = NULL, terminal_reason = %s,"
                " updated_at = %s WHERE id = %s",
                (summary, at, order_id),
            )
            return
        connection.execute(
            "UPDATE orders SET state = 'compensating', current_stage = %s, current_kind = 'undo',"
            " next_due_at = NULL, waiting_since = %s, terminal_reason = %s, updated_at = %s"
            " WHERE id = %s",
            (plan[undo[0]]["name"], at, summary, at, order_id),
        )
        outputs = {p: execute_output(connection, order_id, plan[p]["name"]) for p in undo}
    compensate(order_id, plan, undo, submission, outputs)


def compensate(
    order_id: str,
    plan: list[dict[str, Any]],
    positions: list[int],
    submission: dict[str, Any],
    outputs: dict[int, Any],
) -> None:
    """Undo the given stages in the order given, each with the output of its execute."""
    undo = [
        stage_message(
            order_id=order_id,
            position=p,
            kind=CommandKind.UNDO,
            plan=plan,
            submission=submission,
            prior_output=outputs.get(p),
        )
        for p in positions
    ]
    dramatiq.pipeline(undo, broker=broker).run()


@dramatiq.actor(actor_name=RECORD_ACTOR, queue_name=RECORDS_QUEUE, max_retries=10)
def record(event: dict[str, Any]) -> None:
    at = datetime.fromisoformat(event["at"])
    match event["type"]:
        case "started":
            start(
                event["order_id"],
                event["stage"],
                event["kind"],
                event["attempt"],
                at,
                datetime.fromisoformat(event["deadline_at"]),
            )
        case "succeeded":
            succeed(
                event["order_id"],
                event["plan"],
                event["position"],
                event["kind"],
                event["attempt"],
                event["output"],
                at,
            )
        case "retry":
            retry(
                event["order_id"],
                event["stage"],
                event["kind"],
                event["attempt"],
                Outcome(event["outcome"]),
                event["reason"],
                at,
                datetime.fromisoformat(event["due_at"]),
            )
        case "gave_up":
            give_up(
                event["order_id"],
                event["plan"],
                event["position"],
                event["kind"],
                datetime.fromisoformat(event["waiting_since"]),
                at,
                event["submission"],
            )
        case "failed":
            fail(
                event["order_id"],
                event["plan"],
                event["position"],
                event["kind"],
                event["attempt"],
                Outcome(event["outcome"]),
                event["reason"],
                at,
                event["submission"],
            )
