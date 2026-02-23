import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import engine, Base, migrate_db
from app.routers import camera, identify, research, listing, orders, shipping, dashboard, ebay_auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    migrate_db()
    # Start scheduler
    from app.services.scheduler import start_scheduler
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="IceSeller - eBay Selling Assistant", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/data", StaticFiles(directory=settings.data_dir), name="data")

templates = Jinja2Templates(directory="app/templates")

# Include routers
app.include_router(dashboard.router)
app.include_router(camera.router, prefix="/camera", tags=["camera"])
app.include_router(identify.router, prefix="/identify", tags=["identify"])
app.include_router(research.router, prefix="/research", tags=["research"])
app.include_router(listing.router, prefix="/listing", tags=["listing"])
app.include_router(orders.router, prefix="/orders", tags=["orders"])
app.include_router(shipping.router, prefix="/shipping", tags=["shipping"])
app.include_router(ebay_auth_router.router, prefix="/auth", tags=["auth"])


@app.get("/health")
async def health():
    return {"status": "ok"}
