from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from orchestrator.app.api import pages, router
from orchestrator.app.lifespan import lifespan

MAX_BODY_BYTES = 64 * 1024
METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimit:
    """Reject a request body over the cap before it is read; an order travels in every command."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self._app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in METHODS_WITH_BODY:
            declared = dict(scope["headers"]).get(b"content-length")
            if declared is None:
                response = JSONResponse({"detail": "Content-Length is required"}, status_code=411)
                await response(scope, receive, send)
                return
            if int(declared) > self._limit:
                response = JSONResponse(
                    {"detail": f"request body exceeds {self._limit} bytes"}, status_code=413
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    app = FastAPI(lifespan=lifespan if with_lifespan else None)
    app.include_router(router)
    app.include_router(pages)
    app.add_middleware(BodySizeLimit, limit=MAX_BODY_BYTES)
    return app


app = create_app()
