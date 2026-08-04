import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text, update

from .config import Settings, get_settings
from .db import Base, create_engine_and_session_factory
from .generation import GenerationRegistry, SlidingWindowRateLimiter
from .llm import create_llm_adapter
from .models import Message, utc_now
from .routes import router as api_router
from .telegram_bot import configure_telegram
from .telegram_bot import router as telegram_router

logger = logging.getLogger("octa_oracle")


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine, session_factory = create_engine_and_session_factory(app_settings.database_url)
        app.state.settings = app_settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.llm_adapter = create_llm_adapter(app_settings)
        app.state.generation_registry = GenerationRegistry()
        app.state.rate_limiter = SlidingWindowRateLimiter(
            app_settings.rate_limit_requests_per_minute
        )

        if app_settings.db_auto_create:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            await session.execute(
                update(Message)
                .where(Message.status == "streaming")
                .values(
                    status="failed",
                    error_code="server_restarted",
                    finish_reason="error",
                    completed_at=utc_now(),
                )
            )
            await session.commit()

        if app_settings.telegram_auto_setup:
            try:
                await configure_telegram(app_settings)
            except Exception:
                logger.exception("Telegram auto setup failed")

        yield

        await app.state.generation_registry.cancel_all()
        await app.state.llm_adapter.close()
        await engine.dispose()

    app = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if app_settings.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if app_settings.app_env != "production" else None,
    )
    app.state.settings = app_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            payload = detail
        else:
            payload = {"code": "http_error", "message": str(detail)}
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=exc.headers,
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request):
        try:
            async with request.app.state.session_factory() as session:
                await session.execute(text("SELECT 1"))
            llm_ready = await request.app.state.llm_adapter.ready()
        except Exception:
            llm_ready = False
        if not llm_ready:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ok"}

    app.include_router(api_router)
    app.include_router(telegram_router)
    return app


app = create_app()
