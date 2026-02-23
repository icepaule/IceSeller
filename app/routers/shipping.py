"""Shipping router -- create DHL labels, shipping documents, and report tracking to eBay."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Order, Listing, Item
from app.services.dhl_api import DhlClient, DhlApiError
from app.services.ebay_api import EbayClient, EbayApiError
from app.services.price_calculator import get_shipping_cost

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_order_or_404(order_id: int, db: Session) -> Order:
    order = db.query(Order).filter(Order.id == order_id).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")
    return order


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/{order_id}")
async def shipping_page(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render the shipping page for an order."""
    order = _get_order_or_404(order_id, db)

    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    item = None
    if listing:
        item = db.query(Item).filter(Item.id == listing.item_id).first()

    # Calculate shipping info from item weight
    shipping_info = None
    if item and item.weight_g and item.weight_g > 0:
        try:
            service_name, cost = get_shipping_cost(item.weight_g)
            shipping_info = {
                "service": service_name,
                "cost": cost,
                "weight_g": item.weight_g,
            }
        except ValueError as exc:
            shipping_info = {
                "service": "Nicht berechenbar",
                "cost": 0.0,
                "weight_g": item.weight_g if item else 0,
                "error": str(exc),
            }

    return templates.TemplateResponse(
        "shipping.html",
        {
            "request": request,
            "active_page": "orders",
            "order": order,
            "listing": listing,
            "item": item,
            "shipping_info": shipping_info,
        },
    )


@router.post("/{order_id}/create-label")
async def create_label(
    order_id: int,
    db: Session = Depends(get_db),
):
    """Create a DHL shipping label and report tracking to eBay.

    Returns JSON with tracking number and label URL.
    """
    order = _get_order_or_404(order_id, db)

    if order.fulfillment_status == "shipped":
        raise HTTPException(
            status_code=400,
            detail="Bestellung wurde bereits versendet",
        )

    if not order.buyer_address:
        raise HTTPException(
            status_code=400,
            detail="Keine Kaeuferadresse vorhanden",
        )

    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing nicht gefunden")

    item = db.query(Item).filter(Item.id == listing.item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Artikel nicht gefunden")

    weight_g = item.weight_g or 1000  # default 1kg if not set

    # 1. Create DHL shipment
    try:
        dhl_client = DhlClient()
        shipment_result = await dhl_client.create_shipment(
            recipient_address=order.buyer_address,
            weight_g=weight_g,
        )
        tracking_number = shipment_result.get("tracking_number", "")
        label_url = shipment_result.get("label_url", "")
    except DhlApiError as exc:
        logger.error("DHL API error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"DHL API Fehler: {exc.detail}",
        )

    # 2. Save tracking info to order
    order.dhl_tracking = tracking_number
    order.dhl_label_url = label_url
    order.fulfillment_status = "shipped"
    order.shipped_at = datetime.utcnow()

    # 3. Report tracking to eBay
    if order.ebay_order_id:
        try:
            ebay_client = EbayClient(db)

            # Build line items from order's listing
            line_items = []
            if listing.ebay_listing_id:
                line_items.append({
                    "lineItemId": listing.ebay_listing_id,
                    "quantity": 1,
                })

            fulfillment_data = {
                "lineItems": line_items,
                "shippingCarrierCode": "DHL",
                "trackingNumber": tracking_number,
            }
            await ebay_client.create_shipping_fulfillment(
                order.ebay_order_id, fulfillment_data,
            )
            logger.info(
                "eBay fulfillment reported for order %s, tracking %s",
                order.ebay_order_id, tracking_number,
            )
        except (EbayApiError, RuntimeError) as exc:
            logger.warning(
                "Failed to report tracking to eBay for order %s: %s",
                order.ebay_order_id, exc,
            )
            # Don't fail the whole operation; label was already created

    # 4. Update item status
    if item.status == "sold":
        item.status = "shipped"

    db.commit()
    logger.info(
        "Shipping label created: order=%d, tracking=%s",
        order_id, tracking_number,
    )

    return JSONResponse({
        "tracking_number": tracking_number,
        "label_url": label_url,
        "status": "shipped",
    })


# ------------------------------------------------------------------
# Shipping documents (print-optimized HTML)
# ------------------------------------------------------------------

def _build_document_context(order_id: int, db: Session) -> dict:
    """Load order, listing, item and sender address for shipping documents."""
    order = _get_order_or_404(order_id, db)

    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    item = None
    if listing:
        item = db.query(Item).filter(Item.id == listing.item_id).first()

    sender = {
        "name": settings.sender_name,
        "street": settings.sender_street,
        "postal_code": settings.sender_postal_code,
        "city": settings.sender_city,
        "country": settings.sender_country,
    }

    return {
        "order": order,
        "listing": listing,
        "item": item,
        "sender": sender,
    }


@router.get("/{order_id}/packing-slip")
async def packing_slip(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render a printable packing slip (Lieferschein)."""
    ctx = _build_document_context(order_id, db)
    ctx["request"] = request
    ctx["today"] = datetime.now().strftime("%d.%m.%Y")
    return templates.TemplateResponse("shipping_packing_slip.html", ctx)


@router.get("/{order_id}/address-label")
async def address_label(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render a printable address label (Adresslabel)."""
    ctx = _build_document_context(order_id, db)
    ctx["request"] = request
    return templates.TemplateResponse("shipping_address_label.html", ctx)


@router.get("/{order_id}/invoice")
async def invoice(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render a printable invoice (Rechnung)."""
    ctx = _build_document_context(order_id, db)
    ctx["request"] = request
    ctx["today"] = datetime.now().strftime("%d.%m.%Y")
    # Invoice number: RE-{order_id}-{year}
    ctx["invoice_number"] = f"RE-{order_id:04d}-{datetime.now().year}"
    return templates.TemplateResponse("shipping_invoice.html", ctx)
