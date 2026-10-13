from fastapi import FastAPI

from orchestrator.app.api import pages, router
from orchestrator.app.lifespan import lifespan


def create_app(*, with_lifespan: bool = True) -> FastAPI:
    app = FastAPI(lifespan=lifespan if with_lifespan else None)
    app.include_router(router)
    app.include_router(pages)
    return app


app = create_app()
