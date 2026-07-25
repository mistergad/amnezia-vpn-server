from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.templating import Jinja2Templates

from app.config import get_settings
from app.database import Base, SessionLocal, engine, migrate_schema
from app.services.lifecycle import (
    BusinessRuleError,
    reconcile_expired,
    refresh_peer_stats,
    seed_data,
)
from app.services.payments import PaymentProviderError, build_payment_provider
from app.services.provisioning import ProvisioningError, build_provisioner
from app.web import router


ROOT = Path(__file__).resolve().parent


async def _reconciler(app: FastAPI) -> None:
    interval = app.state.settings.subscription_reconcile_seconds
    while True:
        await asyncio.sleep(interval)
        with SessionLocal() as db:
            try:
                await asyncio.to_thread(refresh_peer_stats, db, app.state.provisioner)
                await asyncio.to_thread(reconcile_expired, db, app.state.provisioner)
            except Exception:
                db.rollback()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_schema(engine)
    with SessionLocal() as db:
        seed_data(db, app.state.settings)
    task = asyncio.create_task(_reconciler(app))
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.provisioner = build_provisioner(settings)
    app.state.payment_provider = build_payment_provider(settings)
    app.state.templates = Jinja2Templates(
        env=Environment(
            loader=FileSystemLoader(ROOT / "templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.session_https_only,
        max_age=60 * 60 * 24 * 14,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(BusinessRuleError)
    @app.exception_handler(PaymentProviderError)
    @app.exception_handler(ProvisioningError)
    async def service_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app


app = create_app()
