"""Listing router -- create, publish and manage eBay listings."""

import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Item, Listing, PriceResearch, EbayToken
from app.services import ebay_auth
from app.services.ebay_api import EbayClient, EbayApiError
from app.services.price_calculator import (
    calculate_suggestions,
    calculate_shipping_total,
    calculate_optimal_publish_time,
)
from app.services.listing_helpers import generate_html_description, build_aspects

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


def _get_ebay_token(db: Session) -> EbayToken | None:
    """Return the stored eBay token row, or None if not authenticated."""
    return db.query(EbayToken).first()


def _is_token_valid(token: EbayToken | None) -> bool:
    """Check whether the stored token exists and is not expired."""
    if token is None:
        return False
    if token.expires_at is None:
        return False
    return token.expires_at > datetime.utcnow()


CONDITION_LABELS = {
    "NEW": "Neu",
    "USED_EXCELLENT": "Gebraucht - Hervorragend",
    "USED_VERY_GOOD": "Gebraucht - Sehr gut",
    "USED_GOOD": "Gebraucht - Gut",
    "USED_ACCEPTABLE": "Gebraucht - Akzeptabel",
    "FOR_PARTS_OR_NOT_WORKING": "Fuer Teile / Defekt",
}

CONDITION_ENUM_MAP = {
    "NEW": "NEW",
    "USED_EXCELLENT": "USED_EXCELLENT",
    "USED_VERY_GOOD": "USED_VERY_GOOD",
    "USED_GOOD": "USED_GOOD",
    "USED_ACCEPTABLE": "USED_ACCEPTABLE",
    "FOR_PARTS_OR_NOT_WORKING": "FOR_PARTS_OR_NOT_WORKING",
}

DURATION_OPTIONS = [
    ("DAYS_3", "3 Tage"),
    ("DAYS_5", "5 Tage"),
    ("DAYS_7", "7 Tage"),
    ("DAYS_10", "10 Tage"),
    ("DAYS_30", "30 Tage"),
    ("GTC", "Gueltig bis auf Widerruf"),
]

# Fallback mapping: AI category -> eBay category ID + name (ebay.de)
_AI_CATEGORY_TO_EBAY = {
    "RAM":          {"categoryId": "170083", "categoryName": "Arbeitsspeicher (RAM)"},
    "SSD":          {"categoryId": "175669", "categoryName": "Solid State Drives (SSD)"},
    "HDD":          {"categoryId": "56083",  "categoryName": "Festplatten (HDD, SAS & SCSI)"},
    "Switch":       {"categoryId": "182094", "categoryName": "Enterprise Switches"},
    "Router":       {"categoryId": "44995",  "categoryName": "Enterprise Router"},
    "Firewall":     {"categoryId": "175700", "categoryName": "Enterprise Firewalls"},
    "Access Point": {"categoryId": "175709", "categoryName": "Enterprise Access Points"},
    "Server":       {"categoryId": "11211",  "categoryName": "Server"},
    "Laptop":       {"categoryId": "177",    "categoryName": "Notebooks & Netbooks"},
    "Desktop":      {"categoryId": "179",    "categoryName": "Desktops & All-in-One-PCs"},
    "Netzteil":     {"categoryId": "42017",  "categoryName": "Netzteile"},
    "Modul":        {"categoryId": "182093", "categoryName": "Enterprise Netzwerk-Module"},
    "Kabel":        {"categoryId": "64035",  "categoryName": "Kabel & Adapter"},
    "Storage":      {"categoryId": "56083",  "categoryName": "Festplatten (HDD, SAS & SCSI)"},
}


# ------------------------------------------------------------------
# eBay Auth endpoints
# ------------------------------------------------------------------

@router.get("/ebay-auth")
async def ebay_auth_page(request: Request, db: Session = Depends(get_db)):
    """Render the eBay authentication status page."""
    token = _get_ebay_token(db)
    is_valid = _is_token_valid(token)

    return templates.TemplateResponse(
        "ebay_auth.html",
        {
            "request": request,
            "active_page": "ebay_auth",
            "token": token,
            "is_authenticated": is_valid,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/ebay-auth/start")
async def ebay_auth_start():
    """Redirect to the eBay OAuth consent URL."""
    if not settings.ebay_app_id or not settings.ebay_cert_id:
        raise HTTPException(
            status_code=400,
            detail="eBay API-Zugangsdaten nicht konfiguriert. "
                   "Bitte EBAY_APP_ID und EBAY_CERT_ID in der .env setzen.",
        )

    auth_url = ebay_auth.get_auth_url()
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/ebay-auth/callback")
async def ebay_auth_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """Handle the eBay OAuth callback, exchange code for tokens."""
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        logger.warning("eBay OAuth error: %s", error)
        return RedirectResponse(
            url="/listing/ebay-auth?error=eBay-Autorisierung fehlgeschlagen: " + error,
            status_code=303,
        )

    if not code:
        return RedirectResponse(
            url="/listing/ebay-auth?error=Kein Autorisierungscode erhalten",
            status_code=303,
        )

    try:
        token_data = await ebay_auth.exchange_code(code)
        ebay_auth.save_tokens(db, token_data)
        logger.info("eBay OAuth tokens saved successfully")
        return RedirectResponse(
            url="/listing/ebay-auth?success=Erfolgreich mit eBay verbunden!",
            status_code=303,
        )
    except Exception as exc:
        logger.exception("eBay token exchange failed")
        return RedirectResponse(
            url="/listing/ebay-auth?error=Token-Austausch fehlgeschlagen: " + str(exc),
            status_code=303,
        )


# ------------------------------------------------------------------
# Listing CRUD endpoints
# ------------------------------------------------------------------

@router.get("/")
async def list_listings(request: Request, db: Session = Depends(get_db)):
    """List all listings."""
    import json
    from pathlib import Path

    listings = (
        db.query(Listing)
        .order_by(Listing.id.desc())
        .all()
    )

    scheduled_dir = Path(settings.data_dir) / "scheduled"

    # Preload associated items and fees
    listing_data = []
    for listing in listings:
        item = db.query(Item).filter(Item.id == listing.item_id).first()
        fees = None
        fees_label = None
        job_file = scheduled_dir / f"listing_{listing.id}.json"
        if job_file.exists():
            try:
                jd = json.loads(job_file.read_text())
                if jd.get("actual_fees"):
                    fees = jd["actual_fees"]
                    fees_label = "real"
                elif jd.get("dry_run", {}).get("fees"):
                    fees = jd["dry_run"]["fees"]
                    fees_label = "erwartet"
            except Exception:
                pass
        listing_data.append({
            "listing": listing,
            "item": item,
            "fees": fees,
            "fees_label": fees_label,
        })

    return templates.TemplateResponse(
        "listing_list.html",
        {
            "request": request,
            "active_page": "listings",
            "listing_data": listing_data,
        },
    )


@router.get("/{item_id}/create")
async def create_listing_form(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render the listing creation form pre-filled with item and research data."""
    item = _get_item_or_404(item_id, db)

    # Check eBay auth status
    token = _get_ebay_token(db)
    ebay_authenticated = _is_token_valid(token)

    # Get category suggestions from eBay API, fallback to AI category mapping
    categories = []
    if ebay_authenticated and item.confirmed_title:
        try:
            client = EbayClient(db)
            categories = await client.suggest_categories(item.confirmed_title)
        except (EbayApiError, RuntimeError) as exc:
            logger.warning("Category suggestion failed: %s", exc)
    if not categories and item.ai_category:
        fallback = _AI_CATEGORY_TO_EBAY.get(item.ai_category)
        if fallback:
            categories = [fallback]
            logger.info("Using fallback category for '%s': %s", item.ai_category, fallback)

    # Get pricing suggestions from existing research
    research_results = (
        db.query(PriceResearch)
        .filter(PriceResearch.item_id == item_id)
        .all()
    )

    suggestions = None
    if research_results:
        results_dicts = [
            {
                "price": r.price,
                "price_type": r.price_type,
                "sold": r.sold,
                "title": r.title,
            }
            for r in research_results
        ]
        suggestions = calculate_suggestions(results_dicts, item.weight_g)

    # Shipping cost with packaging
    shipping_total = None
    if item.weight_g and item.weight_g > 0:
        shipping_total = calculate_shipping_total(item.weight_g, item.dimensions)

    # Optimal auction timing (Sunday 19:00 CET)
    optimal_timing = calculate_optimal_publish_time("DAYS_7")

    return templates.TemplateResponse(
        "listing_form.html",
        {
            "request": request,
            "active_page": "listing",
            "item": item,
            "categories": categories,
            "suggestions": suggestions,
            "shipping_total": shipping_total,
            "optimal_timing": optimal_timing,
            "ebay_authenticated": ebay_authenticated,
            "condition_labels": CONDITION_LABELS,
            "duration_options": DURATION_OPTIONS,
        },
    )


@router.post("/{item_id}/publish")
async def publish_listing(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: str = Form(...),
    category_id: str = Form(...),
    condition: str = Form(...),
    format: str = Form(...),
    start_price: float = Form(0.0),
    buy_now_price: float = Form(0.0),
    shipping_cost: float = Form(0.0),
    duration: str = Form("DAYS_7"),
    schedule_mode: str = Form("now"),
    schedule_datetime: str = Form(""),
    best_offer: str = Form(""),
):
    """Create and publish an eBay listing (now or scheduled)."""
    item = _get_item_or_404(item_id, db)

    # Validate condition
    if condition not in CONDITION_ENUM_MAP:
        raise HTTPException(status_code=400, detail="Ungueltiger Zustand")

    # Validate format
    if format not in ("FIXED_PRICE", "AUCTION"):
        raise HTTPException(status_code=400, detail="Ungueltiges Format")

    # Validate prices
    if format == "AUCTION" and start_price <= 0:
        raise HTTPException(status_code=400, detail="Startpreis muss groesser als 0 sein")
    if format == "FIXED_PRICE" and buy_now_price <= 0:
        raise HTTPException(status_code=400, detail="Sofortkauf-Preis muss groesser als 0 sein")

    # eBay rule: Auctions only allow quantity=1
    effective_quantity = item.quantity or 1
    if format == "AUCTION" and effective_quantity > 1:
        logger.info(
            "Auction format: forcing quantity=1 (item has %d). "
            "Listing as 1 lot of %d pieces.",
            effective_quantity, effective_quantity,
        )

    # Generate unique SKU
    timestamp = int(time.time())
    sku_prefix = item.internal_number if item.internal_number else f"IS-{item_id}"
    sku = f"{sku_prefix}-{timestamp}"

    # Check eBay auth
    token = _get_ebay_token(db)
    if not _is_token_valid(token):
        raise HTTPException(
            status_code=400,
            detail="Nicht mit eBay verbunden. Bitte zuerst authentifizieren.",
        )

    # Scheduled publish: save data and defer via APScheduler
    if schedule_mode in ("timed", "custom"):
        if schedule_mode == "custom" and schedule_datetime:
            # Parse user-provided datetime (comes as "YYYY-MM-DDTHH:MM" from input)
            from zoneinfo import ZoneInfo
            try:
                naive_dt = datetime.strptime(schedule_datetime, "%Y-%m-%dT%H:%M")
                publish_at = naive_dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Ungültiges Datum/Uhrzeit Format",
                )
            if publish_at <= datetime.now(ZoneInfo("Europe/Berlin")):
                raise HTTPException(
                    status_code=400,
                    detail="Veröffentlichungszeitpunkt muss in der Zukunft liegen",
                )
        else:
            timing = calculate_optimal_publish_time(duration)
            publish_at = timing["publish_at"]

        # Save as draft listing
        listing = Listing(
            item_id=item_id,
            ebay_sku=sku,
            format=format,
            start_price=start_price,
            buy_now_price=buy_now_price if buy_now_price > 0 else None,
            category_id=category_id,
            status="scheduled",
            listed_at=None,
        )
        db.add(listing)
        item.status = "scheduled"
        db.commit()
        db.refresh(listing)

        # Save publish data as JSON for the scheduler
        import json
        from pathlib import Path
        scheduled_dir = Path(settings.data_dir) / "scheduled"
        scheduled_dir.mkdir(parents=True, exist_ok=True)
        # Calculate end time for display
        from zoneinfo import ZoneInfo
        DURATION_DAYS_MAP = {"DAYS_3": 3, "DAYS_5": 5, "DAYS_7": 7, "DAYS_10": 10, "DAYS_30": 30, "GTC": 30}
        duration_days = DURATION_DAYS_MAP.get(duration, 7)
        from datetime import timedelta
        end_at = publish_at + timedelta(days=duration_days)

        job_data = {
            "item_id": item_id,
            "listing_id": listing.id,
            "sku": sku,
            "title": title,
            "description": description,
            "category_id": category_id,
            "condition": condition,
            "format": format,
            "start_price": start_price,
            "buy_now_price": buy_now_price,
            "shipping_cost": shipping_cost,
            "duration": duration,
            "publish_at": publish_at.isoformat(),
            "end_at": end_at.isoformat(),
        }
        job_file = scheduled_dir / f"listing_{listing.id}.json"
        job_file.write_text(json.dumps(job_data, ensure_ascii=False))

        # Schedule the publish job
        from app.services.scheduler import schedule_listing_publish
        schedule_listing_publish(listing.id, publish_at)

        logger.info(
            "Listing scheduled: item=%d, listing=%d, publish_at=%s",
            item_id, listing.id, publish_at.isoformat(),
        )

        # Run dry run (VerifyAddItem) to catch errors early
        from pathlib import Path
        dry_run_result = {"status": "pending"}
        try:
            client = EbayClient(db)
            aspects = build_aspects(item.ai_specs, item.ai_manufacturer, item.ai_model)
            html_description = generate_html_description(
                title=title, description=description,
                specs=item.ai_specs, condition=condition,
                what_is_included=item.ai_what_is_included or "",
            )
            image_local_paths = [
                str(Path(settings.data_dir) / "images" / img)
                for img in (item.images or [])
            ] if item.images else []

            price_value = buy_now_price if format == "FIXED_PRICE" else start_price
            verify_result = await client.publish_via_trading_api(
                title=title,
                description_html=html_description,
                category_id=category_id,
                condition=condition,
                listing_type=format,
                start_price=price_value,
                buy_now_price=buy_now_price if format == "AUCTION" and buy_now_price > 0 else 0.0,
                shipping_cost=shipping_cost,
                duration=duration,
                image_paths=image_local_paths,
                aspects=aspects,
                sku=sku,
                quantity=1 if format == "AUCTION" else (item.quantity or 1),
                best_offer=format == "FIXED_PRICE" and best_offer == "on",
                verify_only=True,
            )
            dry_run_result = {
                "status": "ok",
                "warnings": verify_result.get("warnings", []),
                "fees": verify_result.get("fees", {}),
            }
            logger.info("Dry run passed for listing %d", listing.id)
        except EbayApiError as dry_exc:
            dry_run_result = {"status": "error", "detail": dry_exc.detail}
            logger.warning("Dry run failed for listing %d: %s", listing.id, dry_exc.detail)
        except Exception as dry_exc:
            dry_run_result = {"status": "error", "detail": str(dry_exc)}
            logger.warning("Dry run error for listing %d: %s", listing.id, dry_exc)

        # Save dry run result to job file
        job_data["dry_run"] = dry_run_result
        job_file.write_text(json.dumps(job_data, ensure_ascii=False))

        return RedirectResponse(
            url=f"/listing/{item_id}/detail",
            status_code=303,
        )

    try:
        client = EbayClient(db)

        # Build Item Specifics (aspects) from AI specs
        aspects = build_aspects(
            item.ai_specs, item.ai_manufacturer, item.ai_model,
        )

        # Generate structured HTML description (includes disclaimer)
        html_description = generate_html_description(
            title=title,
            description=description,
            specs=item.ai_specs,
            condition=condition,
            what_is_included=item.ai_what_is_included or "",
        )

        # Build local image paths for Trading API upload / URLs for Inventory API
        from pathlib import Path
        image_local_paths = [
            str(Path(settings.data_dir) / "images" / img)
            for img in (item.images or [])
        ] if item.images else []

        price_value = buy_now_price if format == "FIXED_PRICE" else start_price

        # Try Inventory API first (requires Business Policies)
        use_trading_api = False
        try:
            policy_ids = await client.ensure_policies()
        except EbayApiError as policy_exc:
            # errorId 20403 = "User is not eligible for Business Policy"
            # German message is just "Ungültig: ." — match on errorId + text patterns
            is_policy_error = (
                20403 in policy_exc.error_ids
                or "not eligible" in str(policy_exc.detail).lower()
                or "business polic" in str(policy_exc.detail).lower()
                or "ungültig" in str(policy_exc.detail).lower()
            )
            if is_policy_error:
                logger.warning(
                    "Business Policies unavailable (errorIds=%s, detail=%s), "
                    "using Trading API fallback",
                    policy_exc.error_ids, policy_exc.detail,
                )
                use_trading_api = True
            else:
                raise

        if use_trading_api:
            # --- Trading API fallback (no Business Policies needed) ---
            # Images are uploaded to eBay EPS via UploadSiteHostedPictures
            result = await client.publish_via_trading_api(
                title=title,
                description_html=html_description,
                category_id=category_id,
                condition=condition,
                listing_type=format,
                start_price=price_value,
                buy_now_price=buy_now_price if format == "AUCTION" and buy_now_price > 0 else 0.0,
                shipping_cost=shipping_cost,
                duration=duration,
                image_paths=image_local_paths,
                aspects=aspects,
                sku=sku,
                quantity=1 if format == "AUCTION" else (item.quantity or 1),
                best_offer=format == "FIXED_PRICE" and best_offer == "on",
            )
            listing_id = result.get("listingId", "")
            offer_id = ""

            logger.info(
                "Listing published via Trading API: item=%d, sku=%s, listing_id=%s, "
                "shipping=%.2f, fees=%s",
                item_id, sku, listing_id, shipping_cost, result.get("fees", {}),
            )

        else:
            # --- Inventory API flow (with Business Policies) ---
            # Upload images to eBay EPS for Inventory API too
            ebay_image_urls = []
            for local_path in image_local_paths:
                try:
                    hosted_url = await client.upload_image_to_ebay(local_path)
                    ebay_image_urls.append(hosted_url)
                except Exception as img_exc:
                    logger.warning("Failed to upload image %s: %s", local_path, img_exc)

            inventory_data = {
                "product": {
                    "title": title,
                    "description": html_description,
                    "aspects": aspects,
                    "imageUrls": ebay_image_urls,
                },
                "condition": condition,
                "availability": {
                    "shipToLocationAvailability": {
                        "quantity": 1 if format == "AUCTION" else (item.quantity or 1),
                    },
                },
            }
            await client.create_inventory_item(sku, inventory_data)

            listing_policies = {
                "fulfillmentPolicyId": policy_ids.get("fulfillmentPolicyId", ""),
                "paymentPolicyId": policy_ids.get("paymentPolicyId", ""),
                "returnPolicyId": policy_ids.get("returnPolicyId", ""),
            }

            if shipping_cost > 0:
                listing_policies["shippingCostOverrides"] = [
                    {
                        "priority": 1,
                        "shippingCost": {
                            "value": str(shipping_cost),
                            "currency": "EUR",
                        },
                        "shippingServiceType": "DOMESTIC",
                    },
                ]

            offer_data = {
                "sku": sku,
                "marketplaceId": settings.ebay_marketplace,
                "format": format,
                "categoryId": category_id,
                "listingDescription": html_description,
                "listingPolicies": listing_policies,
                "pricingSummary": {
                    "price": {
                        "value": str(price_value),
                        "currency": "EUR",
                    },
                },
                "listingDuration": duration,
            }

            if format == "AUCTION" and buy_now_price > 0:
                offer_data["pricingSummary"]["auctionReservePrice"] = {
                    "value": str(buy_now_price),
                    "currency": "EUR",
                }

            if format == "FIXED_PRICE" and best_offer == "on":
                auto_accept = round(buy_now_price * 0.9, 2)
                auto_decline = round(buy_now_price * 0.7, 2)
                offer_data["bestOfferTerms"] = {
                    "bestOfferEnabled": True,
                    "autoAcceptPrice": {
                        "value": str(auto_accept),
                        "currency": "EUR",
                    },
                    "autoDeclinePrice": {
                        "value": str(auto_decline),
                        "currency": "EUR",
                    },
                }
                logger.info(
                    "Best Offer enabled: accept >= %.2f, decline < %.2f",
                    auto_accept, auto_decline,
                )

            offer_result = await client.create_offer(offer_data)
            offer_id = offer_result.get("offerId", "")

            publish_result = await client.publish_offer(offer_id)
            listing_id = publish_result.get("listingId", "")

            logger.info(
                "Listing published via Inventory API: item=%d, sku=%s, "
                "listing_id=%s, offer_id=%s, shipping=%.2f",
                item_id, sku, listing_id, offer_id, shipping_cost,
            )

        # Save listing to database (both paths)
        listing = Listing(
            item_id=item_id,
            ebay_listing_id=listing_id,
            ebay_offer_id=offer_id,
            ebay_sku=sku,
            format=format,
            start_price=start_price if format == "AUCTION" else None,
            buy_now_price=buy_now_price if format == "FIXED_PRICE" else buy_now_price if buy_now_price > 0 else None,
            category_id=category_id,
            status="active",
            current_price=price_value,
            listed_at=datetime.utcnow(),
        )
        db.add(listing)
        item.status = "listed"
        db.commit()
        db.refresh(listing)

        # Save fees to job file for the listing list overview
        import json
        from pathlib import Path
        actual_fees = result.get("fees", {}) if use_trading_api else {}
        if actual_fees:
            scheduled_dir = Path(settings.data_dir) / "scheduled"
            scheduled_dir.mkdir(parents=True, exist_ok=True)
            fee_file = scheduled_dir / f"listing_{listing.id}.json"
            fee_file.write_text(json.dumps({
                "item_id": item_id,
                "listing_id": listing.id,
                "actual_fees": actual_fees,
                "published": True,
            }, ensure_ascii=False))

        return RedirectResponse(
            url=f"/listing/{item_id}/detail",
            status_code=303,
        )

    except EbayApiError as exc:
        logger.error("eBay API error during publish: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"eBay API Fehler: {exc.detail}",
        )
    except RuntimeError as exc:
        logger.error("Runtime error during publish: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )


@router.get("/{item_id}/detail")
async def listing_detail(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Render the listing detail page with status and stats."""
    item = _get_item_or_404(item_id, db)

    listing = (
        db.query(Listing)
        .filter(Listing.item_id == item_id)
        .order_by(Listing.id.desc())
        .first()
    )

    if listing is None:
        raise HTTPException(status_code=404, detail="Kein Listing fuer diesen Artikel gefunden")

    # Build eBay URL
    ebay_url = None
    if listing.ebay_listing_id:
        if settings.ebay_environment == "PRODUCTION":
            ebay_url = f"https://www.ebay.de/itm/{listing.ebay_listing_id}"
        else:
            ebay_url = f"https://sandbox.ebay.de/itm/{listing.ebay_listing_id}"

    # Build timeline events
    timeline = []
    if item.created_at:
        timeline.append({
            "date": item.created_at,
            "icon": "bi-plus-circle",
            "color": "secondary",
            "text": "Artikel erstellt",
        })
    # Load job data for scheduled listings (timing + dry run)
    dry_run = None
    publish_error = None
    if listing.status == "scheduled":
        import json
        from pathlib import Path
        job_file = Path(settings.data_dir) / "scheduled" / f"listing_{listing.id}.json"
        if job_file.exists():
            job_data = json.loads(job_file.read_text())

            # Use stored publish_at/end_at times
            publish_at_str = job_data.get("publish_at")
            end_at_str = job_data.get("end_at")
            if publish_at_str:
                pub_dt = datetime.fromisoformat(publish_at_str)
                end_dt = datetime.fromisoformat(end_at_str) if end_at_str else None
                end_text = f" → Ende {end_dt.strftime('%a %d.%m. %H:%M')}" if end_dt else ""
                timeline.append({
                    "date": datetime.utcnow(),
                    "icon": "bi-calendar-check",
                    "color": "info",
                    "text": f"Geplant: Start {pub_dt.strftime('%a %d.%m. %H:%M')}{end_text}",
                })

            dry_run = job_data.get("dry_run")
            if dry_run:
                if dry_run.get("status") == "ok":
                    timeline.append({
                        "date": datetime.utcnow(),
                        "icon": "bi-check-circle-fill",
                        "color": "success",
                        "text": "Dry Run bestanden (VerifyAddItem)",
                    })
                else:
                    timeline.append({
                        "date": datetime.utcnow(),
                        "icon": "bi-x-circle-fill",
                        "color": "danger",
                        "text": f"Dry Run fehlgeschlagen: {dry_run.get('detail', 'Unbekannter Fehler')}",
                    })

            publish_error = job_data.get("publish_error")
            if publish_error:
                timeline.append({
                    "date": datetime.utcnow(),
                    "icon": "bi-exclamation-octagon-fill",
                    "color": "danger",
                    "text": f"Veröffentlichung fehlgeschlagen: {publish_error.get('detail', 'Unbekannter Fehler')}",
                })

    # Extra context for scheduled listing editing
    schedule_publish_at = None
    schedule_duration = None
    if listing.status == "scheduled":
        import json
        from pathlib import Path
        job_file = Path(settings.data_dir) / "scheduled" / f"listing_{listing.id}.json"
        if job_file.exists():
            _job = json.loads(job_file.read_text())
            pub_iso = _job.get("publish_at")
            if pub_iso:
                from zoneinfo import ZoneInfo
                _pub_dt = datetime.fromisoformat(pub_iso)
                if _pub_dt.tzinfo is None:
                    _pub_dt = _pub_dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
                schedule_publish_at = _pub_dt.strftime("%Y-%m-%dT%H:%M")
            schedule_duration = _job.get("duration", "DAYS_7")

    if listing.listed_at:
        timeline.append({
            "date": listing.listed_at,
            "icon": "bi-tags",
            "color": "success",
            "text": "Auf eBay eingestellt",
        })
    if listing.ended_at:
        timeline.append({
            "date": listing.ended_at,
            "icon": "bi-clock-history",
            "color": "warning",
            "text": "Listing beendet",
        })

    # Check for orders and shipping info
    has_orders = len(listing.orders) > 0 if listing.orders else False

    # Get shipping info from the most recent order (if any)
    shipping_info = None
    if listing.orders:
        order = listing.orders[-1]
        if order.dhl_tracking:
            shipping_info = {
                "tracking_number": order.dhl_tracking,
                "shipped_at": order.shipped_at,
                "carrier": "DHL",
            }

    today = datetime.utcnow().strftime("%Y-%m-%d")

    return templates.TemplateResponse(
        "listing_detail.html",
        {
            "request": request,
            "active_page": "listing",
            "item": item,
            "listing": listing,
            "ebay_url": ebay_url,
            "timeline": timeline,
            "has_orders": has_orders,
            "shipping_info": shipping_info,
            "today": today,
            "dry_run": dry_run,
            "publish_error": publish_error,
            "schedule_publish_at": schedule_publish_at,
            "schedule_duration": schedule_duration,
        },
    )


@router.post("/{item_id}/update-scheduled")
async def update_scheduled_listing(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    title: str = Form(...),
    description: str = Form(...),
    schedule_datetime: str = Form(...),
):
    """Update title, description and schedule time for a scheduled (not yet published) listing."""
    import json
    from pathlib import Path
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    item = _get_item_or_404(item_id, db)

    listing = (
        db.query(Listing)
        .filter(Listing.item_id == item_id)
        .order_by(Listing.id.desc())
        .first()
    )
    if listing is None:
        raise HTTPException(status_code=404, detail="Kein Listing gefunden")
    if listing.status != "scheduled":
        raise HTTPException(status_code=400, detail="Nur geplante Listings koennen bearbeitet werden")

    # Parse new publish time
    try:
        naive_dt = datetime.strptime(schedule_datetime, "%Y-%m-%dT%H:%M")
        publish_at = naive_dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungueltiges Datum/Uhrzeit Format")

    if publish_at <= datetime.now(ZoneInfo("Europe/Berlin")):
        raise HTTPException(status_code=400, detail="Zeitpunkt muss in der Zukunft liegen")

    # Update item fields in DB
    item.confirmed_title = title.strip()
    item.confirmed_description = description.strip()

    # Load and update job JSON
    job_file = Path(settings.data_dir) / "scheduled" / f"listing_{listing.id}.json"
    if not job_file.exists():
        raise HTTPException(status_code=404, detail="Job-Datei nicht gefunden")

    job_data = json.loads(job_file.read_text())

    # Calculate new end_at based on duration from job
    DURATION_DAYS_MAP = {"DAYS_3": 3, "DAYS_5": 5, "DAYS_7": 7, "DAYS_10": 10, "DAYS_30": 30, "GTC": 30}
    duration = job_data.get("duration", "DAYS_7")
    duration_days = DURATION_DAYS_MAP.get(duration, 7)
    end_at = publish_at + timedelta(days=duration_days)

    job_data["title"] = title.strip()
    job_data["description"] = description.strip()
    job_data["publish_at"] = publish_at.isoformat()
    job_data["end_at"] = end_at.isoformat()

    # Remove old dry run (invalidated by changes)
    job_data.pop("dry_run", None)

    # Run new dry run
    dry_run_result = {"status": "pending"}
    try:
        client = EbayClient(db)
        aspects = build_aspects(item.ai_specs, item.ai_manufacturer, item.ai_model)
        html_description = generate_html_description(
            title=title.strip(),
            description=description.strip(),
            specs=item.ai_specs,
            condition=job_data.get("condition", "USED_GOOD"),
            what_is_included=item.ai_what_is_included or "",
        )
        image_local_paths = [
            str(Path(settings.data_dir) / "images" / img)
            for img in (item.images or [])
        ] if item.images else []

        price_value = (
            job_data.get("buy_now_price", 0)
            if job_data.get("format") == "FIXED_PRICE"
            else job_data.get("start_price", 0)
        )
        verify_result = await client.publish_via_trading_api(
            title=title.strip(),
            description_html=html_description,
            category_id=job_data.get("category_id", ""),
            condition=job_data.get("condition", "USED_GOOD"),
            listing_type=job_data.get("format", "AUCTION"),
            start_price=price_value,
            buy_now_price=(
                job_data.get("buy_now_price", 0)
                if job_data.get("format") == "AUCTION" and job_data.get("buy_now_price", 0) > 0
                else 0.0
            ),
            shipping_cost=job_data.get("shipping_cost", 0),
            duration=duration,
            image_paths=image_local_paths,
            aspects=aspects,
            sku=job_data.get("sku", listing.ebay_sku or ""),
            quantity=1 if format == "AUCTION" else (item.quantity or 1),
            best_offer=False,
            verify_only=True,
        )
        dry_run_result = {
            "status": "ok",
            "warnings": verify_result.get("warnings", []),
            "fees": verify_result.get("fees", {}),
        }
        logger.info("Dry run passed for updated listing %d", listing.id)
    except EbayApiError as dry_exc:
        dry_run_result = {"status": "error", "detail": dry_exc.detail}
        logger.warning("Dry run failed for updated listing %d: %s", listing.id, dry_exc.detail)
    except Exception as dry_exc:
        dry_run_result = {"status": "error", "detail": str(dry_exc)}
        logger.warning("Dry run error for updated listing %d: %s", listing.id, dry_exc)

    job_data["dry_run"] = dry_run_result
    job_file.write_text(json.dumps(job_data, ensure_ascii=False))

    # Reschedule the APScheduler job
    from app.services.scheduler import schedule_listing_publish
    schedule_listing_publish(listing.id, publish_at)

    db.commit()

    logger.info(
        "Scheduled listing updated: item=%d, listing=%d, publish_at=%s",
        item_id, listing.id, publish_at.isoformat(),
    )

    return RedirectResponse(
        url=f"/listing/{item_id}/detail",
        status_code=303,
    )


@router.post("/{item_id}/ship")
async def submit_shipping(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    shipped_at: str = Form(""),
    tracking_number: str = Form(""),
    carrier: str = Form("DHL"),
):
    """Record shipping info and report tracking to eBay."""
    from app.models import Order

    item = _get_item_or_404(item_id, db)

    listing = (
        db.query(Listing)
        .filter(Listing.item_id == item_id)
        .order_by(Listing.id.desc())
        .first()
    )

    if listing is None:
        raise HTTPException(status_code=404, detail="Kein Listing gefunden")

    # Parse shipping date
    ship_date = None
    if shipped_at:
        try:
            ship_date = datetime.strptime(shipped_at, "%Y-%m-%d")
        except ValueError:
            ship_date = datetime.utcnow()
    else:
        ship_date = datetime.utcnow()

    # Find or create order record for this listing
    order = None
    if listing.orders:
        order = listing.orders[-1]
    else:
        order = Order(
            listing_id=listing.id,
            fulfillment_status="pending",
        )
        db.add(order)
        db.flush()

    # Save tracking info
    order.dhl_tracking = tracking_number.strip()
    order.shipped_at = ship_date
    order.fulfillment_status = "shipped"

    # Report tracking to eBay if we have an order ID and tracking number
    if listing.ebay_listing_id and tracking_number.strip():
        try:
            # Map carrier name to eBay carrier code
            carrier_codes = {
                "DHL": "DHL",
                "DPD": "DPD",
                "Hermes": "HERMES",
                "GLS": "GLS",
                "Deutsche Post": "DEUTSCHE_POST",
            }
            ebay_carrier = carrier_codes.get(carrier, "DHL")

            # If we have an eBay order, report fulfillment
            if order.ebay_order_id:
                client = EbayClient(db)
                await client.create_shipping_fulfillment(
                    order.ebay_order_id,
                    {
                        "trackingNumber": tracking_number.strip(),
                        "shippingCarrierCode": ebay_carrier,
                    },
                )
                logger.info(
                    "Tracking reported to eBay: order=%s, tracking=%s, carrier=%s",
                    order.ebay_order_id, tracking_number.strip(), ebay_carrier,
                )
            else:
                logger.info(
                    "Tracking saved locally (no eBay order ID yet): tracking=%s",
                    tracking_number.strip(),
                )
        except EbayApiError as exc:
            logger.warning("Failed to report tracking to eBay: %s", exc)

    # Update item status
    item.status = "shipped"
    db.commit()

    logger.info(
        "Shipping recorded: item=%d, tracking=%s, carrier=%s, date=%s",
        item_id, tracking_number.strip(), carrier, shipped_at,
    )

    return RedirectResponse(
        url=f"/listing/{item_id}/detail",
        status_code=303,
    )
