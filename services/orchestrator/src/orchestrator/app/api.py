from datetime import timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from orchestrator.app.lifespan import Runtime
from orchestrator.app.schemas import (
    OrderRequest,
    OrderStatus,
    SubmissionResponse,
    payload_hash,
    to_status,
)
from orchestrator.domain.errors import DuplicateIdempotencyKey
from orchestrator.domain.models import Order
from orchestrator.domain.transitions import Submitted, transition

MAX_IDEMPOTENCY_KEY_LENGTH = 255
TIMELINE_PAGE = Path(__file__).resolve().parents[1] / "static" / "index.html"

router = APIRouter(prefix="/api/orders")
pages = APIRouter()


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


@router.post("", status_code=201)
async def submit_order(
    request: OrderRequest,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LENGTH)],
    runtime: RuntimeDep,
    response: Response,
) -> SubmissionResponse:
    submission = request.model_dump(mode="json")
    digest = payload_hash(submission)
    existing = await runtime.store.get_order_by_key(idempotency_key)
    if existing is not None:
        return _replay(existing, digest, response)
    stages = tuple(runtime.config.stages)
    try:
        request.script.validate_stages(stages)
        request.script.validate_lengths(runtime.config.max_attempts_for)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    now = runtime.clock()
    due_at = now + timedelta(seconds=runtime.config.sweep.pending_threshold_seconds)
    decision = transition(None, Submitted(at=due_at))
    order = Order(
        id=uuid4(),
        idempotency_key=idempotency_key,
        payload=submission,
        payload_hash=digest,
        stages=stages,
        state=decision.state,
        current_stage=decision.current_stage,
        current_kind=decision.current_kind,
        next_due_at=decision.next_due_at,
        terminal_reason=decision.terminal_reason,
        created_at=now,
        updated_at=now,
    )
    try:
        await runtime.store.create_order(order)
    except DuplicateIdempotencyKey:
        raced = await runtime.store.get_order_by_key(idempotency_key)
        if raced is None:
            raise
        return _replay(raced, digest, response)
    runtime.nudge.set()
    return SubmissionResponse(id=order.id, state=order.state)


def _replay(existing: Order, digest: str, response: Response) -> SubmissionResponse:
    if existing.payload_hash != digest:
        raise HTTPException(
            status_code=409, detail="idempotency key already used with a different payload"
        )
    response.status_code = 200
    return SubmissionResponse(id=existing.id, state=existing.state)


@router.get("/{order_id}")
async def get_order_status(order_id: str, runtime: RuntimeDep) -> OrderStatus:
    try:
        parsed = UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="order not found") from None
    found = await runtime.store.get_order_with_attempts(parsed)
    if found is None:
        raise HTTPException(status_code=404, detail="order not found")
    return to_status(*found)


@pages.get("/", include_in_schema=False)
@pages.get("/orders/{order_id}", include_in_schema=False)
async def timeline_page() -> FileResponse:
    return FileResponse(TIMELINE_PAGE)
