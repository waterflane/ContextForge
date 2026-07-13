"""FastAPI application factory."""

from fastapi import FastAPI

from contextforge._metadata import APP_NAME, APP_TAGLINE, __version__
from contextforge.api.routes import router


def create_app() -> FastAPI:
    """Create the local ContextForge API application."""

    app = FastAPI(
        title=APP_NAME,
        description=APP_TAGLINE,
        version=__version__,
    )
    app.include_router(router)
    return app


app = create_app()
