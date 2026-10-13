from fastapi import FastAPI
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from orchestrator.app.api import pages, router
from orchestrator.app.lifespan import lifespan

MAX_BODY_BYTES = 64 * 1024


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    app = FastAPI(lifespan=lifespan if with_lifespan else None)
    app.include_router(router)
    app.include_router(pages)
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)
    return app


app = create_app()
