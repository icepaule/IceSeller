"""Camera router -- capture, stream and PTZ control."""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Item
from app.services.camera_service import camera_service

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ------------------------------------------------------------------
# Request / response schemas
# ------------------------------------------------------------------

class CropRect(BaseModel):
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

class CaptureRequest(BaseModel):
    item_id: Optional[int] = None
    crop: Optional[CropRect] = None


class CaptureResponse(BaseModel):
    item_id: int
    image_path: str
    internal_number: str = ""


class PTZRequest(BaseModel):
    direction: str


class PTZResponse(BaseModel):
    ok: bool = True


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _generate_internal_number(db: Session) -> str:
    """Generate the next internal number in the format YYYYMMDDnn.

    Finds the highest existing sequence number across ALL items
    (not just today's prefix) to handle manual renumbering gracefully.
    Falls back to today's prefix with sequence 00 if no numbers exist.
    """
    today = date.today()
    prefix = today.strftime("%Y%m%d")

    # Find all numeric internal_numbers and get the highest one
    rows = (
        db.query(Item.internal_number)
        .filter(
            Item.internal_number.isnot(None),
            Item.internal_number != "",
        )
        .all()
    )

    max_seq = -1
    max_prefix = prefix
    for (num,) in rows:
        # Parse numbers in format YYYYMMDDnn (10 digits)
        stripped = num.strip()
        if stripped.isdigit() and len(stripped) >= 10:
            item_prefix = stripped[:8]
            try:
                seq = int(stripped[8:])
            except ValueError:
                continue
            # Only consider entries with today's prefix or higher
            if item_prefix == prefix and seq > max_seq:
                max_seq = seq
            # Also check if someone manually set a future date prefix
            if item_prefix > max_prefix:
                max_prefix = item_prefix

    next_seq = max_seq + 1
    return f"{prefix}{next_seq:02d}"


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/capture")
async def capture_page(request: Request):
    """Render the camera capture page."""
    return templates.TemplateResponse(
        "capture.html",
        {"request": request, "active_page": "capture"},
    )


@router.get("/stream")
async def stream():
    """MJPEG video stream from the camera."""
    return StreamingResponse(
        camera_service.mjpeg_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.post("/capture-photo", response_model=CaptureResponse)
async def capture_photo(body: CaptureRequest, db: Session = Depends(get_db)):
    """Capture a photo and associate it with an item.

    If *item_id* is ``None`` a new draft item is created automatically.
    The captured image filename is appended to the item's ``images`` list.
    """
    if body.crop and not (body.crop.x == 0 and body.crop.y == 0 and body.crop.w == 1 and body.crop.h == 1):
        filename = camera_service.capture_usb_cropped(body.crop.model_dump())
    else:
        filename = camera_service.capture()

    # Resolve or create the item
    item: Optional[Item] = None
    if body.item_id is not None:
        item = db.query(Item).filter(Item.id == body.item_id).first()

    if item is None:
        internal_number = _generate_internal_number(db)
        item = Item(status="draft", images=[], internal_number=internal_number)
        db.add(item)
        db.flush()  # assign id

    # Append image -- SQLAlchemy needs a new list instance to detect change
    images = list(item.images or [])
    images.append(filename)
    item.images = images

    db.commit()
    db.refresh(item)

    logger.info("Photo captured for item %d: %s", item.id, filename)
    return CaptureResponse(
        item_id=item.id,
        image_path=filename,
        internal_number=item.internal_number or "",
    )


@router.post("/ptz", response_model=PTZResponse)
async def ptz_control(body: PTZRequest):
    """Send a PTZ command to the camera."""
    camera_service.ptz(body.direction)
    return PTZResponse(ok=True)
