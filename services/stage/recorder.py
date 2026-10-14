"""Order state and attempt history, written by the stage worker as each attempt starts and ends.

Every function is one transaction. Starting an attempt is an upsert, so a redelivered message
restarts the same attempt row instead of failing on the unique key.
"""

import os
from datetime import datetime
from typing import Any

import psycopg
from contracts import CommandKind, Outcome
from psycopg.types.json import Jsonb


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
        connection.execute(
            "INSERT INTO attempts (order_id, stage, kind, attempt, deadline_at, started_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (order_id, stage, kind, attempt) DO UPDATE SET"
            " started_at = EXCLUDED.started_at, deadline_at = EXCLUDED.deadline_at,"
            " outcome = NULL, reason = NULL, stage_output = NULL, finished_at = NULL",
            (order_id, stage, kind, attempt, deadline_at, started_at),
        )
        connection.execute(
            "UPDATE orders SET state = %s, current_stage = %s, current_kind = %s,"
            " next_due_at = NULL, updated_at = %s WHERE id = %s",
            (state, stage, kind, started_at, order_id),
        )


def execute_output(order_id: str, stage: str) -> dict[str, Any] | None:
    with _connect() as connection:
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
        "UPDATE attempts SET outcome = %s, reason = %s, stage_output = %s, finished_at = %s"
        " WHERE order_id = %s AND stage = %s AND kind = %s AND attempt = %s",
        (
            outcome.value,
            reason,
            None if output is None else Jsonb(output),
            at,
            order_id,
            stage,
            kind,
            attempt,
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
    stage: str,
    kind: str,
    attempt: int,
    outcome: Outcome,
    reason: str | None,
    at: datetime,
) -> None:
    """A permanent failure: an execute starts compensation, an undo ends the order."""
    summary = f"{stage} {outcome.value}: {reason}"
    with _connect() as connection:
        _close(connection, order_id, stage, kind, attempt, outcome, reason, None, at)
        if kind == CommandKind.EXECUTE:
            connection.execute(
                "UPDATE orders SET state = 'compensating', current_stage = %s,"
                " current_kind = 'undo', next_due_at = NULL, terminal_reason = %s,"
                " updated_at = %s WHERE id = %s",
                (stage, summary, at, order_id),
            )
        else:
            connection.execute(
                "UPDATE orders SET state = 'compensation_failed', current_stage = NULL,"
                " current_kind = NULL, next_due_at = NULL,"
                " terminal_reason = concat_ws('; ', terminal_reason, %s::text), updated_at = %s"
                " WHERE id = %s",
                (f"undo of {summary}", at, order_id),
            )
