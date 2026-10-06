from fastapi import FastAPI

from app.features.health.router import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Atlas AI",
        version="0.1.0",
        description="Ask questions about your documents and get answers with citations.",
    )
    app.include_router(health_router)
    return app


app = create_app()
