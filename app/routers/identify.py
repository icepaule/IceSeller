"""Identify router -- AI-powered product identification."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Item
from app.services.ollama_vision import identify_product
from app.services.price_calculator import get_shipping_options

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ------------------------------------------------------------------
# Request / response schemas
# ------------------------------------------------------------------

class IdentifyResponse(BaseModel):
    ok: bool = True
    manufacturer: str = ""
    model: str = ""
    category: str = ""
    condition: str = ""
    details: str = ""
    suggested_title: str = ""
    suggested_description: str = ""


class ConfirmRequest(BaseModel):
    confirmed_title: str = ""
    confirmed_description: str = ""
    weight_g: int | None = None
    dimension_length: float | None = None
    dimension_width: float | None = None
    dimension_height: float | None = None


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------

def _get_item_or_404(item_id: int, db: Session) -> Item:
    item = db.query(Item).filter(Item.id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Artikel nicht gefunden")
    return item


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/shipping-options")
async def shipping_options(weight_g: int = 0, length: float = 0, width: float = 0, height: float = 0):
    """Return DHL shipping options for given weight/dimensions."""
    if weight_g <= 0:
        return {"options": []}
    dims = None
    if length > 0 and width > 0 and height > 0:
        dims = {"length": length, "width": width, "height": height}
    options = get_shipping_options(weight_g, dims)
    return {"options": options}


@router.get("/{item_id}")
async def identify_page(item_id: int, request: Request, db: Session = Depends(get_db)):
    """Render the identification page for an item."""
    item = _get_item_or_404(item_id, db)
    return templates.TemplateResponse(
        "identify.html",
        {
            "request": request,
            "active_page": "identify",
            "item": item,
        },
    )


@router.post("/{item_id}/run")
async def run_identification(item_id: int, db: Session = Depends(get_db)):
    """Run Ollama vision identification on the item's images.

    Returns JSON with the identification results so the frontend can
    populate form fields via AJAX.
    """
    item = _get_item_or_404(item_id, db)

    if not item.images:
        raise HTTPException(
            status_code=400, detail="Keine Bilder vorhanden -- bitte zuerst Fotos aufnehmen."
        )

    try:
        result = await identify_product(item.images)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Ollama identification failed for item %d", item_id)
        raise HTTPException(
            status_code=502,
            detail=f"KI-Identifikation fehlgeschlagen: {exc}",
        )

    # Persist AI results on the item
    item.ai_manufacturer = result.get("manufacturer", "")
    item.ai_model = result.get("model", "")
    item.ai_category = result.get("category", "")
    item.ai_condition = result.get("condition", "")
    item.ai_details = result.get("details", "")
    item.ai_specs = result.get("specs") or {}
    item.ai_what_is_included = result.get("what_is_included", "")

    # Save AI-detected quantity (e.g. 2 identical RAM modules in one photo)
    ai_qty = result.get("quantity", 1)
    if isinstance(ai_qty, str):
        try:
            ai_qty = int(ai_qty)
        except (ValueError, TypeError):
            ai_qty = 1
    if ai_qty > 1:
        item.quantity = ai_qty
        logger.info("AI detected quantity=%d for item %d", ai_qty, item_id)

    # Always update confirmed fields with new AI suggestions
    # (user can still edit them before confirming)
    suggested_title = result.get("suggested_title", "")
    suggested_desc = result.get("suggested_description", "")
    if suggested_title:
        # Clean up common AI artifacts in titles
        import re
        suggested_title = re.sub(r'^(eBay[- ]?)?Titel:\s*', '', suggested_title, flags=re.IGNORECASE)
        suggested_title = re.sub(r'\s*-\s*Gebraucht\s*(Hervorragend)?', '', suggested_title, flags=re.IGNORECASE)
        suggested_title = suggested_title.strip(' -,')
        item.confirmed_title = suggested_title
    if suggested_desc:
        item.confirmed_description = suggested_desc

    item.status = "identified"
    db.commit()
    db.refresh(item)

    return IdentifyResponse(
        ok=True,
        manufacturer=item.ai_manufacturer,
        model=item.ai_model,
        category=item.ai_category,
        condition=item.ai_condition,
        details=item.ai_details,
        suggested_title=item.confirmed_title,
        suggested_description=item.confirmed_description,
    )


@router.post("/{item_id}/confirm")
async def confirm_identification(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """User confirms (and optionally edits) the identification.

    Accepts both JSON and form-encoded data.  Saves the confirmed title,
    description, weight and dimensions, then redirects to the research page.
    """
    item = _get_item_or_404(item_id, db)

    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    item.confirmed_title = body.get("confirmed_title", item.confirmed_title) or ""
    item.confirmed_description = body.get("confirmed_description", item.confirmed_description) or ""

    # Weight
    weight_raw = body.get("weight_g")
    if weight_raw is not None and str(weight_raw).strip():
        try:
            item.weight_g = int(weight_raw)
        except (ValueError, TypeError):
            pass

    # Dimensions
    dim_l = body.get("dimension_length")
    dim_w = body.get("dimension_width")
    dim_h = body.get("dimension_height")
    if any(v is not None and str(v).strip() for v in (dim_l, dim_w, dim_h)):
        try:
            item.dimensions = {
                "length": float(dim_l) if dim_l and str(dim_l).strip() else 0,
                "width": float(dim_w) if dim_w and str(dim_w).strip() else 0,
                "height": float(dim_h) if dim_h and str(dim_h).strip() else 0,
            }
        except (ValueError, TypeError):
            pass

    # Quantity
    qty_raw = body.get("quantity")
    if qty_raw is not None and str(qty_raw).strip():
        try:
            item.quantity = max(1, int(qty_raw))
        except (ValueError, TypeError):
            pass

    # Also persist any AI field edits the user may have made
    for ai_field in ("ai_manufacturer", "ai_model", "ai_category", "ai_condition", "ai_details"):
        form_key = ai_field.replace("ai_", "")  # e.g. "manufacturer"
        if form_key in body and body[form_key]:
            setattr(item, ai_field, body[form_key])

    if item.status == "draft":
        item.status = "identified"

    db.commit()

    # Return redirect for form submissions, JSON for AJAX
    if "application/json" in content_type:
        return {"ok": True, "redirect": f"/research/{item_id}"}
    return RedirectResponse(url=f"/research/{item_id}", status_code=303)
