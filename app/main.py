"""Application entry point: starts the poller and the REST API."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from .api import router
from .web import router as web_router
from .config import Settings, load_settings
from .db import init_db, make_engine
from .poller import Poller, sync_devices
from .rollup import RollupJob
from .shelly import ShellyCloud

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO level - they contain the Shelly auth_key.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("shelly-tracker")


def create_app(settings: Settings | None = None, cloud: ShellyCloud | None = None,
               start_poller: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.shelly_host or not settings.shelly_auth_key:
            log.warning("SHELLY_HOST / SHELLY_AUTH_KEY not set - polling will fail")
        engine = make_engine(settings.database_url)
        session_factory = init_db(engine)
        sync_devices(session_factory, settings.devices)
        client = cloud or ShellyCloud(
            settings.base_url,
            settings.shelly_auth_key,
            settings.shelly_api_version,
            settings.request_timeout,
        )
        poller = Poller(settings, session_factory, client)
        rollup = RollupJob(session_factory, settings.raw_retention_months)

        app.state.settings = settings
        app.state.tz = ZoneInfo(settings.timezone)
        app.state.session_factory = session_factory
        app.state.poller = poller
        app.state.rollup = rollup
        log.info(
            "Tracking %d device(s): %s",
            len(settings.devices),
            ", ".join(f"{d.code}={d.id}" for d in settings.devices) or "-",
        )
        if start_poller:
            poller.start()
            rollup.start()
        try:
            yield
        finally:
            rollup.stop()
            poller.stop()
            client.close()
            engine.dispose()

    app = FastAPI(
        title="Shelly Tracker",
        description="Stores Shelly Cloud device data and exports it.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(web_router)
    return app


app = create_app() if os.environ.get("SHELLY_TRACKER_NO_AUTOAPP") != "1" else None
