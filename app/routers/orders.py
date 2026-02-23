"""Orders router -- list, detail, and sync eBay orders."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Order, Listing, Item
from app.services.ebay_api import EbayClient, EbayApiError

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

@router.get("/")
async def list_orders(request: Request, db: Session = Depends(get_db)):
    """List all orders with associated item and listing info."""
    orders = (
        db.query(Order)
        .order_by(Order.id.desc())
        .all()
    )

    order_data = []
    for order in orders:
        listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
        item = None
        if listing:
            item = db.query(Item).filter(Item.id == listing.item_id).first()
        order_data.append({
            "order": order,
            "listing": listing,
            "item": item,
        })

    return templates.TemplateResponse(
        "order_list.html",
        {
            "request": request,
            "active_page": "orders",
            "order_data": order_data,
        },
    )


@router.get("/{order_id}")
async def order_detail(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render the order detail page."""
    order = _get_order_or_404(order_id, db)

    listing = db.query(Listing).filter(Listing.id == order.listing_id).first()
    item = None
    if listing:
        item = db.query(Item).filter(Item.id == listing.item_id).first()

    return templates.TemplateResponse(
        "order_detail.html",
        {
            "request": request,
            "active_page": "orders",
            "order": order,
            "listing": listing,
            "item": item,
        },
    )


@router.post("/sync")
async def sync_orders(request: Request, db: Session = Depends(get_db)):
    """Sync orders from eBay, matching them to local listings by ebay_listing_id."""
    try:
        client = EbayClient(db)
        ebay_orders = await client.get_orders(limit=50)
    except EbayApiError as exc:
        logger.error("Failed to fetch eBay orders: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"eBay API Fehler beim Abrufen der Bestellungen: {exc.detail}",
        )
    except RuntimeError as exc:
        logger.error("Runtime error fetching orders: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    created = 0
    updated = 0

    for ebay_order in ebay_orders:
        ebay_order_id = ebay_order.get("orderId", "")
        if not ebay_order_id:
            continue

        # Extract buyer info
        buyer = ebay_order.get("buyer", {})
        buyer_username = buyer.get("username", "")

        # Extract fulfillment info for shipping address
        fulfillment_start = ebay_order.get("fulfillmentStartInstructions", [])
        buyer_name = ""
        buyer_address = None
        if fulfillment_start:
            ship_to = fulfillment_start[0].get("shippingStep", {}).get("shipTo", {})
            contact = ship_to.get("fullName", "")
            buyer_name = contact
            address = ship_to.get("contactAddress", {})
            if address:
                buyer_address = {
                    "name": contact,
                    "street": address.get("addressLine1", ""),
                    "street2": address.get("addressLine2", ""),
                    "city": address.get("city", ""),
                    "postal_code": address.get("postalCode", ""),
                    "state": address.get("stateOrProvince", ""),
                    "country": address.get("countryCode", ""),
                }

        # Extract pricing
        payment_summary = ebay_order.get("paymentSummary", {})
        total_amount = ebay_order.get("pricingSummary", {}).get("total", {})
        total_price = float(total_amount.get("value", 0))
        delivery_cost = ebay_order.get("pricingSummary", {}).get("deliveryCost", {})
        shipping_cost = float(delivery_cost.get("value", 0))

        # Payment and fulfillment status
        payment_status = ebay_order.get("orderPaymentStatus", "")
        fulfillment_status_raw = ebay_order.get("orderFulfillmentStatus", "")

        # Map eBay fulfillment status to local
        fulfillment_map = {
            "NOT_STARTED": "pending",
            "IN_PROGRESS": "pending",
            "FULFILLED": "shipped",
        }
        fulfillment_status = fulfillment_map.get(fulfillment_status_raw, "pending")

        # Extract sold date
        sold_at = None
        creation_date = ebay_order.get("creationDate", "")
        if creation_date:
            try:
                sold_at = datetime.fromisoformat(
                    creation_date.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Find matching local listing via line items
        line_items = ebay_order.get("lineItems", [])
        listing = None
        for li in line_items:
            legacy_item_id = li.get("legacyItemId", "")
            if legacy_item_id:
                listing = (
                    db.query(Listing)
                    .filter(Listing.ebay_listing_id == legacy_item_id)
                    .first()
                )
                if listing:
                    break

        if not listing:
            # Try matching by SKU
            for li in line_items:
                sku = li.get("sku", "")
                if sku:
                    listing = (
                        db.query(Listing)
                        .filter(Listing.ebay_sku == sku)
                        .first()
                    )
                    if listing:
                        break

        if not listing:
            logger.warning(
                "No local listing found for eBay order %s, skipping",
                ebay_order_id,
            )
            continue

        # Check if order already exists
        existing = (
            db.query(Order)
            .filter(Order.ebay_order_id == ebay_order_id)
            .first()
        )

        if existing:
            # Update existing order
            existing.buyer_username = buyer_username
            existing.buyer_name = buyer_name
            existing.buyer_address = buyer_address
            existing.total_price = total_price
            existing.shipping_cost = shipping_cost
            existing.payment_status = payment_status
            if existing.fulfillment_status == "pending":
                existing.fulfillment_status = fulfillment_status
            updated += 1
        else:
            # Create new order
            new_order = Order(
                listing_id=listing.id,
                ebay_order_id=ebay_order_id,
                buyer_username=buyer_username,
                buyer_name=buyer_name,
                buyer_address=buyer_address,
                total_price=total_price,
                shipping_cost=shipping_cost,
                payment_status=payment_status,
                fulfillment_status=fulfillment_status,
                sold_at=sold_at,
            )
            db.add(new_order)

            # Update listing and item status
            listing.status = "sold"
            item = db.query(Item).filter(Item.id == listing.item_id).first()
            if item and item.status == "listed":
                item.status = "sold"

            created += 1

    db.commit()
    logger.info("Order sync complete: %d created, %d updated", created, updated)

    return RedirectResponse(url="/orders/", status_code=303)
