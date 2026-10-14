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
from contracts import RECORD_ACTOR, RECORDS_QUEUE, CommandKind, Outcome, stage_message
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.brokers.stub import StubBroker
from psycopg.types.json import Jsonb

_url = os.environ.get("RABBITMQ_URL")
broker = RabbitmqBroker(url=_url, confirm_delivery=True) if _url else StubBroker()
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
                " next_due_at = NULL, updated_at = %s WHERE id = %s",
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
            " next_due_at = NULL, updated_at = %s WHERE id = %s",
            (state, current, None if current is None else kind, at, order_id),
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
            "UPDATE orders SET next_due_at = %s, updated_at = %s WHERE id = %s",
            (due_at, at, order_id),
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
                " current_kind = NULL, next_due_at = NULL,"
                " terminal_reason = concat_ws('; ', terminal_reason, %s::text), updated_at = %s"
                " WHERE id = %s",
                (f"undo of {summary}", at, order_id),
            )
            return
        connection.execute(
            "UPDATE orders SET state = 'compensating', current_stage = %s,"
            " current_kind = 'undo', next_due_at = NULL, terminal_reason = %s,"
            " updated_at = %s WHERE id = %s",
            (stage, summary, at, order_id),
        )
        outputs = {
            p: execute_output(connection, order_id, plan[p]["name"]) for p in range(position + 1)
        }
    compensate(order_id, plan, position, submission, outputs)


def compensate(
    order_id: str,
    plan: list[dict[str, Any]],
    position: int,
    submission: dict[str, Any],
    outputs: dict[int, Any],
) -> None:
    """Undo every dispatched stage in reverse, the failing one first, each with its own output."""
    undo = [
        stage_message(
            order_id=order_id,
            position=p,
            kind=CommandKind.UNDO,
            plan=plan,
            submission=submission,
            prior_output=outputs.get(p),
        )
        for p in range(position, -1, -1)
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
