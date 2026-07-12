"""StudyPal FastAPI application entrypoint.

Run with: ``uvicorn main:app``
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from routers import assessments, capture, cycles, families, gap_report, grading, health, review


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="StudyPal API", version=settings.app_version)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(assessments.router)
    app.include_router(families.router)
    app.include_router(cycles.router)
    app.include_router(capture.router)
    app.include_router(grading.router)
    app.include_router(review.router)
    app.include_router(gap_report.router)

    return app


app = create_app()
