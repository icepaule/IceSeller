"""Dashboard router -- overview of all items, listings, and orders."""

import logging
import os
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models import Item, Listing, Order

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Main dashboard page with item counts grouped by status and recent items."""

    # Total items
    total_items = db.query(func.count(Item.id)).scalar() or 0

    # Count items by status
    status_counts_raw = (
        db.query(Item.status, func.count(Item.id))
        .group_by(Item.status)
        .all()
    )
    status_counts = {status: count for status, count in status_counts_raw}

    listed_count = status_counts.get("listed", 0)
    sold_count = status_counts.get("sold", 0)
    shipped_count = status_counts.get("shipped", 0)
    completed_count = status_counts.get("completed", 0)

    # Active listings count
    active_listings = (
        db.query(func.count(Listing.id))
        .filter(Listing.status == "active")
        .scalar() or 0
    )

    # Revenue from orders
    revenue = (
        db.query(func.sum(Order.total_price))
        .filter(Order.payment_status == "PAID")
        .scalar() or 0.0
    )

    # Recent items (last 10) with their latest listing
    recent_items_raw = (
        db.query(Item)
        .order_by(Item.id.desc())
        .limit(10)
        .all()
    )

    recent_items = []
    for item in recent_items_raw:
        latest_listing = (
            db.query(Listing)
            .filter(Listing.item_id == item.id)
            .order_by(Listing.id.desc())
            .first()
        )
        recent_items.append({
            "item": item,
            "listing": latest_listing,
        })

    # Pick up msg/error from redirect query params
    msg = request.query_params.get("msg", "")
    error = request.query_params.get("error", "")

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_page": "dashboard",
            "total_items": total_items,
            "listed_count": listed_count,
            "sold_count": sold_count,
            "shipped_count": shipped_count,
            "completed_count": completed_count,
            "active_listings": active_listings,
            "revenue": revenue,
            "recent_items": recent_items,
            "status_counts": status_counts,
            "msg": msg,
            "error": error,
        },
    )


@router.post("/items/{item_id}/internal-number")
async def update_internal_number(item_id: int, request: Request, db: Session = Depends(get_db)):
    """Update the internal (Post-it) number for an item."""
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Artikel nicht gefunden")
    body = await request.json()
    item.internal_number = body.get("internal_number", "").strip()
    db.commit()
    return {"ok": True, "internal_number": item.internal_number}


PROTECTED_STATUSES = {"listed", "sold", "shipped"}
IMAGES_DIR = os.path.join("data", "images")


@router.post("/items/delete")
async def delete_items(request: Request, db: Session = Depends(get_db)):
    """Delete one or more items (with cascade) and their image files."""
    form = await request.form()
    item_ids_raw = form.getlist("item_ids")

    # Parse and validate IDs
    item_ids = []
    for raw in item_ids_raw:
        try:
            item_ids.append(int(raw))
        except (ValueError, TypeError):
            continue

    if not item_ids:
        return RedirectResponse(
            url="/?" + urlencode({"error": "Keine Artikel ausgewaehlt."}),
            status_code=303,
        )

    # Fetch items
    items = db.query(Item).filter(Item.id.in_(item_ids)).all()
    if not items:
        return RedirectResponse(
            url="/?" + urlencode({"error": "Artikel nicht gefunden."}),
            status_code=303,
        )

    # Separate protected from deletable
    protected = [i for i in items if i.status in PROTECTED_STATUSES]
    deletable = [i for i in items if i.status not in PROTECTED_STATUSES]

    # Delete image files for deletable items
    for item in deletable:
        if item.images:
            for img_filename in item.images:
                img_path = os.path.join(IMAGES_DIR, img_filename)
                try:
                    os.remove(img_path)
                except OSError:
                    logger.warning("Could not delete image %s", img_path)

    # Delete items (cascade handles listings, orders, research, emails)
    for item in deletable:
        db.delete(item)
    db.commit()

    # Build redirect message
    deleted_count = len(deletable)
    parts = []
    if deleted_count:
        parts.append(f"{deleted_count} Artikel geloescht.")
    if protected:
        ids_str = ", ".join(f"#{i.id}" for i in protected)
        parts.append(
            f"{len(protected)} Artikel uebersprungen (aktiv/verkauft): {ids_str}"
        )

    param = "msg" if deleted_count and not protected else "error" if not deleted_count else "msg"
    return RedirectResponse(
        url="/?" + urlencode({param: " ".join(parts)}),
        status_code=303,
    )
