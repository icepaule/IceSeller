"""Research router -- price research via eBay Browse API and completed listings scraper."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Item, PriceResearch
from app.services.ebay_api import EbayClient, EbayApiError
from app.services.ebay_scraper import scrape_completed_listings
from app.services.price_calculator import calculate_suggestions, get_shipping_cost

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ------------------------------------------------------------------
# Helper
# ------------------------------------------------------------------

def _get_item_or_404(item_id: int, db: Session) -> Item:
    item = db.query(Item).filter(Item.id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Artikel nicht gefunden")
    return item


def _build_search_query(item: Item) -> str:
    """Build a search query string from item data.

    Prefers confirmed_title; falls back to ai_manufacturer + ai_model.
    """
    if item.confirmed_title and item.confirmed_title.strip():
        return item.confirmed_title.strip()

    parts = []
    if item.ai_manufacturer:
        parts.append(item.ai_manufacturer.strip())
    if item.ai_model:
        parts.append(item.ai_model.strip())

    if parts:
        return " ".join(parts)

    raise HTTPException(
        status_code=400,
        detail="Kein Titel oder Hersteller/Modell vorhanden -- "
               "bitte zuerst den Artikel identifizieren.",
    )


def _ebay_auth_configured() -> bool:
    """Return True if eBay API credentials are configured."""
    return bool(settings.ebay_app_id and settings.ebay_cert_id)


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/{item_id}")
async def research_page(item_id: int, request: Request, db: Session = Depends(get_db)):
    """Render the price research page for an item."""
    item = _get_item_or_404(item_id, db)

    # Load existing research results
    existing_results = (
        db.query(PriceResearch)
        .filter(PriceResearch.item_id == item_id)
        .all()
    )

    # Calculate suggestions from existing results if available
    suggestions = None
    shipping = None
    if existing_results:
        results_dicts = [
            {
                "price": r.price,
                "price_type": r.price_type,
                "sold": r.sold,
                "title": r.title,
            }
            for r in existing_results
        ]
        suggestions = calculate_suggestions(results_dicts, item.weight_g)

        if item.weight_g and item.weight_g > 0:
            try:
                service_name, cost = get_shipping_cost(item.weight_g)
                shipping = {"service": service_name, "cost": cost, "weight_g": item.weight_g}
            except ValueError:
                shipping = None

    return templates.TemplateResponse(
        "research.html",
        {
            "request": request,
            "active_page": "research",
            "item": item,
            "research_results": existing_results,
            "suggestions": suggestions,
            "shipping": shipping,
            "ebay_auth_configured": _ebay_auth_configured(),
        },
    )


@router.post("/{item_id}/run")
async def run_research(item_id: int, db: Session = Depends(get_db)):
    """Run price research: fetch active listings via Browse API and scrape sold listings.

    Returns JSON with the combined research results and pricing suggestions.
    """
    item = _get_item_or_404(item_id, db)
    query = _build_search_query(item)

    logger.info("Running price research for item %d, query='%s'", item_id, query)

    # Delete previous research results for this item
    db.query(PriceResearch).filter(PriceResearch.item_id == item_id).delete()
    db.flush()

    all_results: list[dict] = []
    api_error_msg: str | None = None

    # 1. Browse API -- active listings (skip if auth not configured)
    if _ebay_auth_configured():
        try:
            client = EbayClient(db)
            api_listings = await client.search_active_listings(query)
            for listing in api_listings:
                price_val = None
                price_obj = listing.get("price", {})
                if isinstance(price_obj, dict):
                    try:
                        price_val = float(price_obj.get("value", 0))
                    except (ValueError, TypeError):
                        pass
                elif isinstance(price_obj, (int, float)):
                    price_val = float(price_obj)

                buying_options = listing.get("buyingOptions", [])
                if "AUCTION" in buying_options:
                    price_type = "auction"
                else:
                    price_type = "fixed_price"

                result_dict = {
                    "source": "browse_api",
                    "title": listing.get("title", ""),
                    "price": price_val,
                    "price_type": price_type,
                    "sold": False,
                    "url": listing.get("itemWebUrl", ""),
                }
                all_results.append(result_dict)

            logger.info("Browse API returned %d active listings", len(api_listings))

        except (EbayApiError, RuntimeError) as exc:
            api_error_msg = str(exc)
            logger.warning("Browse API unavailable: %s", api_error_msg)
    else:
        api_error_msg = "eBay API-Zugangsdaten nicht konfiguriert -- nur Scraper-Ergebnisse."
        logger.info("eBay auth not configured, skipping Browse API")

    # 2. Scrape completed/sold listings
    try:
        scraped = await scrape_completed_listings(query)
        for s in scraped:
            result_dict = {
                "source": "completed_scrape",
                "title": s.get("title", ""),
                "price": s.get("price"),
                "price_type": s.get("price_type", ""),
                "sold": s.get("sold", True),
                "url": s.get("url", ""),
            }
            all_results.append(result_dict)

        logger.info("Scraper returned %d completed listings", len(scraped))

    except Exception as exc:
        logger.error("Completed listings scraper failed: %s", exc)
        # Continue with whatever we have from the API

    # 3. Save results to PriceResearch table
    for r in all_results:
        pr = PriceResearch(
            item_id=item_id,
            source=r["source"],
            title=r["title"],
            price=r["price"],
            price_type=r["price_type"],
            sold=r["sold"],
            url=r["url"],
        )
        db.add(pr)

    # 4. Calculate pricing suggestions
    suggestions = calculate_suggestions(all_results, item.weight_g)

    # 5. Update item status
    item.status = "researched"
    db.commit()

    # Build shipping info for response
    shipping = None
    if item.weight_g and item.weight_g > 0:
        try:
            service_name, cost = get_shipping_cost(item.weight_g)
            shipping = {"service": service_name, "cost": cost, "weight_g": item.weight_g}
        except ValueError as exc:
            shipping = {"service": "unknown", "cost": 0.0, "weight_g": item.weight_g, "error": str(exc)}

    return {
        "ok": True,
        "query": query,
        "results": all_results,
        "suggestions": suggestions,
        "shipping": shipping,
        "api_error": api_error_msg,
        "total_results": len(all_results),
    }
