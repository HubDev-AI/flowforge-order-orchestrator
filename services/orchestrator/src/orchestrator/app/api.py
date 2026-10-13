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
TIMELINE_PAGE = Path(__file__).resolve().parents[3] / "static" / "index.html"

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
    stages = tuple(runtime.config.stages)
    try:
        request.script.validate_stages(stages)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    submission = request.model_dump(mode="json")
    digest = payload_hash(submission)
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
        existing = await runtime.store.get_order_by_key(idempotency_key)
        if existing is None or existing.payload_hash != digest:
            raise HTTPException(
                status_code=409, detail="idempotency key already used with a different payload"
            ) from None
        response.status_code = 200
        return SubmissionResponse(id=existing.id, state=existing.state)
    runtime.nudge.set()
    return SubmissionResponse(id=order.id, state=order.state)


@router.get("/{order_id}")
async def get_order_status(order_id: str, runtime: RuntimeDep) -> OrderStatus:
    try:
        parsed = UUID(order_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="order not found") from None
    order = await runtime.store.get_order(parsed)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return to_status(order, await runtime.store.list_attempts(parsed))


@pages.get("/", include_in_schema=False)
@pages.get("/orders/{order_id}", include_in_schema=False)
async def timeline_page() -> FileResponse:
    return FileResponse(TIMELINE_PAGE)
