"""Background scheduler for periodic eBay sync tasks.

Uses APScheduler ``BackgroundScheduler`` to run two recurring jobs:

1. **update_listing_stats** (every 15 min) -- refreshes views, watchers,
   bids, and current price for all active listings.
2. **check_new_orders** (every 5 min) -- fetches recent eBay orders,
   creates local ``Order`` records for new sales, and sends email
   notifications.
3. **publish_scheduled_listing** (one-shot date trigger) -- publishes
   listings that were scheduled for a future date/time.

Both jobs create their own database session and handle errors gracefully
so that one failure does not affect subsequent runs.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.database import SessionLocal
from app.models import Item, Listing, Order, EbayToken

logger = logging.getLogger(__name__)

# Module-level scheduler reference (set in start_scheduler)
_scheduler: BackgroundScheduler | None = None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _run_async(coro):
    """Run an async coroutine from a synchronous APScheduler job."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _is_ebay_authenticated(db) -> bool:
    """Check whether a valid eBay token exists in the database."""
    token = db.query(EbayToken).first()
    if token is None:
        return False
    if token.refresh_token and token.refresh_expires_at:
        if token.refresh_expires_at <= datetime.utcnow():
            return False
    return bool(token.access_token)


# ------------------------------------------------------------------
# Job 1: Update listing statistics
# ------------------------------------------------------------------

async def _update_listing_stats_async():
    """Fetch current stats for all active listings from eBay."""
    db = SessionLocal()
    try:
        if not _is_ebay_authenticated(db):
            logger.debug("Listing stats update skipped -- eBay not authenticated")
            return

        active_listings = (
            db.query(Listing).filter(Listing.status == "active").all()
        )
        if not active_listings:
            logger.debug("No active listings to update")
            return

        logger.info("Updating stats for %d active listing(s)...", len(active_listings))
        from app.services.ebay_api import EbayClient, EbayApiError

        client = EbayClient(db)
        for listing in active_listings:
            if not listing.ebay_listing_id:
                continue
            try:
                browse_item_id = f"v1|{listing.ebay_listing_id}|0"
                item_data = await client.get_item(browse_item_id)
                if not item_data:
                    continue

                price_info = item_data.get("price", {})
                current_price = price_info.get("value")
                if current_price is not None:
                    try:
                        listing.current_price = float(current_price)
                    except (ValueError, TypeError):
                        pass

                bid_count = item_data.get("bidCount")
                if bid_count is not None:
                    listing.bids = int(bid_count)

                item_end_date = item_data.get("itemEndDate")
                if item_end_date:
                    try:
                        end_dt = datetime.fromisoformat(
                            item_end_date.replace("Z", "+00:00")
                        )
                        if end_dt <= datetime.utcnow().astimezone(end_dt.tzinfo):
                            listing.status = "ended"
                            listing.ended_at = end_dt
                    except (ValueError, TypeError):
                        pass

            except EbayApiError as exc:
                if exc.status_code == 404:
                    logger.warning("Listing %s not found on eBay", listing.id)
                else:
                    logger.error("Failed to fetch stats for listing %s: %s", listing.id, exc)
            except Exception:
                logger.exception("Unexpected error updating listing %s", listing.id)

        db.commit()
        logger.info("Listing stats update complete")
    except Exception:
        logger.exception("Listing stats update job failed")
        db.rollback()
    finally:
        db.close()


def update_listing_stats():
    """Synchronous wrapper for the listing stats update job."""
    _run_async(_update_listing_stats_async())


# ------------------------------------------------------------------
# Job 2: Check for new orders
# ------------------------------------------------------------------

async def _check_new_orders_async():
    """Fetch recent orders from eBay and create local records for new ones."""
    db = SessionLocal()
    try:
        if not _is_ebay_authenticated(db):
            logger.debug("Order check skipped -- eBay not authenticated")
            return

        from app.services.ebay_api import EbayClient, EbayApiError
        from app.services import email_service

        client = EbayClient(db)
        try:
            ebay_orders = await client.get_orders(limit=50)
        except EbayApiError as exc:
            logger.error("Failed to fetch eBay orders: %s", exc)
            return
        except RuntimeError as exc:
            logger.warning("Cannot fetch orders: %s", exc)
            return

        if not ebay_orders:
            logger.debug("No orders returned from eBay")
            return

        new_count = 0
        for ebay_order in ebay_orders:
            ebay_order_id = ebay_order.get("orderId", "")
            if not ebay_order_id:
                continue

            existing = db.query(Order).filter(Order.ebay_order_id == ebay_order_id).first()
            if existing:
                continue

            buyer = ebay_order.get("buyer", {})
            buyer_username = buyer.get("username", "")

            fulfillment_start = ebay_order.get("fulfillmentStartInstructions", [])
            buyer_name = ""
            buyer_address = None
            if fulfillment_start:
                ship_to = fulfillment_start[0].get("shippingStep", {}).get("shipTo", {})
                buyer_name = ship_to.get("fullName", "")
                address = ship_to.get("contactAddress", {})
                if address:
                    buyer_address = {
                        "name": buyer_name,
                        "street": address.get("addressLine1", ""),
                        "street2": address.get("addressLine2", ""),
                        "city": address.get("city", ""),
                        "postal_code": address.get("postalCode", ""),
                        "state": address.get("stateOrProvince", ""),
                        "country": address.get("countryCode", ""),
                    }

            total_amount = ebay_order.get("pricingSummary", {}).get("total", {})
            total_price = float(total_amount.get("value", 0))
            delivery_cost = ebay_order.get("pricingSummary", {}).get("deliveryCost", {})
            shipping_cost = float(delivery_cost.get("value", 0))

            payment_status = ebay_order.get("orderPaymentStatus", "")
            fulfillment_status_raw = ebay_order.get("orderFulfillmentStatus", "")
            fulfillment_map = {
                "NOT_STARTED": "pending",
                "IN_PROGRESS": "pending",
                "FULFILLED": "shipped",
            }
            fulfillment_status = fulfillment_map.get(fulfillment_status_raw, "pending")

            sold_at = None
            creation_date = ebay_order.get("creationDate", "")
            if creation_date:
                try:
                    sold_at = datetime.fromisoformat(creation_date.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            line_items = ebay_order.get("lineItems", [])
            listing = None
            for li in line_items:
                legacy_item_id = li.get("legacyItemId", "")
                if legacy_item_id:
                    listing = db.query(Listing).filter(
                        Listing.ebay_listing_id == legacy_item_id
                    ).first()
                    if listing:
                        break
            if not listing:
                for li in line_items:
                    sku = li.get("sku", "")
                    if sku:
                        listing = db.query(Listing).filter(Listing.ebay_sku == sku).first()
                        if listing:
                            break

            if not listing:
                logger.info("Order %s has no matching local listing -- skipping", ebay_order_id)
                continue

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

            listing.status = "sold"
            item = db.query(Item).filter(Item.id == listing.item_id).first()
            if item and item.status == "listed":
                item.status = "sold"

            db.commit()
            db.refresh(new_order)
            new_count += 1

            if item:
                try:
                    await email_service.notify_sold(item, new_order, db)
                except Exception:
                    logger.exception(
                        "Failed to send sale notification for order %s",
                        ebay_order_id,
                    )

        if new_count:
            logger.info("Order check complete: %d new order(s) created", new_count)
        else:
            logger.debug("Order check complete: no new orders")

    except Exception:
        logger.exception("Order check job failed")
        db.rollback()
    finally:
        db.close()


def check_new_orders():
    """Synchronous wrapper for the order check job."""
    _run_async(_check_new_orders_async())


# ------------------------------------------------------------------
# Job 3: Scheduled listing publish
# ------------------------------------------------------------------

async def _publish_scheduled_listing_async(listing_id: int):
    """Publish a previously scheduled listing on eBay."""
    db = SessionLocal()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).first()
        if not listing or listing.status != "scheduled":
            logger.warning("Scheduled publish: listing %d not found or not scheduled", listing_id)
            return

        job_file = Path(settings.data_dir) / "scheduled" / f"listing_{listing_id}.json"
        if not job_file.exists():
            logger.error("Scheduled publish: data file not found for listing %d", listing_id)
            return

        data = json.loads(job_file.read_text())

        from app.services.ebay_api import EbayClient, EbayApiError

        if not _is_ebay_authenticated(db):
            logger.error("Scheduled publish: eBay not authenticated")
            return

        client = EbayClient(db)

        item = db.query(Item).filter(Item.id == data["item_id"]).first()

        # Build aspects and HTML description
        from app.services.listing_helpers import generate_html_description, build_aspects
        aspects = build_aspects(
            item.ai_specs if item else None,
            item.ai_manufacturer if item else "",
            item.ai_model if item else "",
        )
        html_description = generate_html_description(
            title=data["title"],
            description=data["description"],
            specs=item.ai_specs if item else None,
            condition=data["condition"],
            what_is_included=item.ai_what_is_included if item else "",
        )

        price_value = (
            data["buy_now_price"]
            if data["format"] == "FIXED_PRICE"
            else data["start_price"]
        )

        # Build local image paths for EPS upload
        image_local_paths = []
        if item and item.images:
            for img in item.images:
                img_path = str(Path(settings.data_dir) / "images" / img)
                if Path(img_path).exists():
                    image_local_paths.append(img_path)
                else:
                    logger.warning("Scheduled publish: image not found: %s", img_path)

        # Try Inventory API first, fall back to Trading API
        use_trading_api = False
        try:
            policy_ids = await client.ensure_policies()
        except EbayApiError as policy_exc:
            is_policy_error = (
                20403 in policy_exc.error_ids
                or "not eligible" in str(policy_exc.detail).lower()
                or "business polic" in str(policy_exc.detail).lower()
                or "ungültig" in str(policy_exc.detail).lower()
            )
            if is_policy_error:
                logger.warning(
                    "Scheduled publish: Business Policies unavailable (errorIds=%s), "
                    "using Trading API",
                    policy_exc.error_ids,
                )
                use_trading_api = True
            else:
                raise

        if use_trading_api:
            result = await client.publish_via_trading_api(
                title=data["title"],
                description_html=html_description,
                category_id=data["category_id"],
                condition=data["condition"],
                listing_type=data["format"],
                start_price=price_value,
                buy_now_price=data["buy_now_price"] if data["format"] == "AUCTION" and data["buy_now_price"] > 0 else 0.0,
                shipping_cost=data["shipping_cost"],
                duration=data["duration"],
                image_paths=image_local_paths,
                aspects=aspects,
                sku=data["sku"],
                quantity=item.quantity if item else 1,
            )
            ebay_listing_id = result.get("listingId", "")
            offer_id = ""
        else:
            # Upload images to eBay EPS for Inventory API
            ebay_image_urls = []
            for local_path in image_local_paths:
                try:
                    hosted_url = await client.upload_image_to_ebay(local_path)
                    ebay_image_urls.append(hosted_url)
                except Exception as img_exc:
                    logger.warning("Failed to upload image %s: %s", local_path, img_exc)

            inventory_data = {
                "product": {
                    "title": data["title"],
                    "description": html_description,
                    "aspects": aspects,
                    "imageUrls": ebay_image_urls,
                },
                "condition": data["condition"],
                "availability": {
                    "shipToLocationAvailability": {
                        "quantity": item.quantity if item else 1,
                    },
                },
            }
            await client.create_inventory_item(data["sku"], inventory_data)

            listing_policies = {
                "fulfillmentPolicyId": policy_ids.get("fulfillmentPolicyId", ""),
                "paymentPolicyId": policy_ids.get("paymentPolicyId", ""),
                "returnPolicyId": policy_ids.get("returnPolicyId", ""),
            }
            if data["shipping_cost"] > 0:
                listing_policies["shippingCostOverrides"] = [
                    {
                        "priority": 1,
                        "shippingCost": {
                            "value": str(data["shipping_cost"]),
                            "currency": "EUR",
                        },
                        "shippingServiceType": "DOMESTIC",
                    },
                ]

            offer_data = {
                "sku": data["sku"],
                "marketplaceId": settings.ebay_marketplace,
                "format": data["format"],
                "categoryId": data["category_id"],
                "listingDescription": html_description,
                "listingPolicies": listing_policies,
                "pricingSummary": {
                    "price": {
                        "value": str(price_value),
                        "currency": "EUR",
                    },
                },
                "listingDuration": data["duration"],
            }

            if data["format"] == "AUCTION" and data["buy_now_price"] > 0:
                offer_data["pricingSummary"]["auctionReservePrice"] = {
                    "value": str(data["buy_now_price"]),
                    "currency": "EUR",
                }

            offer_result = await client.create_offer(offer_data)
            offer_id = offer_result.get("offerId", "")

            publish_result = await client.publish_offer(offer_id)
            ebay_listing_id = publish_result.get("listingId", "")

        listing.ebay_listing_id = ebay_listing_id
        listing.ebay_offer_id = offer_id
        listing.status = "active"
        listing.current_price = price_value
        listing.listed_at = datetime.utcnow()

        if item:
            item.status = "listed"

        db.commit()

        # Save actual fees to job file (keep file as record)
        try:
            if job_file.exists():
                final_data = json.loads(job_file.read_text())
            else:
                final_data = data
            actual_fees = result.get("fees", {}) if use_trading_api else {}
            final_data["actual_fees"] = actual_fees
            final_data["published"] = True
            job_file.write_text(json.dumps(final_data, ensure_ascii=False))
        except Exception:
            logger.warning("Failed to save actual fees for listing %d", listing_id)

        logger.info(
            "Scheduled listing published: listing=%d, ebay_id=%s",
            listing_id, ebay_listing_id,
        )

    except Exception as exc:
        logger.exception("Scheduled listing publish failed for listing %d", listing_id)
        db.rollback()

        # Persist error to job file so the detail page can show it
        try:
            job_file = Path(settings.data_dir) / "scheduled" / f"listing_{listing_id}.json"
            if job_file.exists():
                err_data = json.loads(job_file.read_text())
                err_data["publish_error"] = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "detail": str(exc),
                }
                job_file.write_text(json.dumps(err_data, ensure_ascii=False))
        except Exception:
            logger.exception("Failed to persist publish error for listing %d", listing_id)
    finally:
        db.close()


def publish_scheduled_listing(listing_id: int):
    """Synchronous wrapper for scheduled listing publish."""
    _run_async(_publish_scheduled_listing_async(listing_id))


def schedule_listing_publish(listing_id: int, publish_at: datetime):
    """Schedule a listing to be published at a specific time."""
    global _scheduler
    if _scheduler is None:
        logger.error("Scheduler not started -- cannot schedule listing")
        return

    job_id = f"publish_listing_{listing_id}"
    _scheduler.add_job(
        publish_scheduled_listing,
        "date",
        run_date=publish_at,
        args=[listing_id],
        id=job_id,
        name=f"Publish listing #{listing_id}",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled listing %d for publish at %s", listing_id, publish_at.isoformat())


# ------------------------------------------------------------------
# Scheduler setup
# ------------------------------------------------------------------

def start_scheduler() -> BackgroundScheduler:
    """Create and start the background scheduler with all recurring jobs."""
    global _scheduler

    scheduler = BackgroundScheduler(
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 120,
        },
    )

    scheduler.add_job(
        update_listing_stats,
        "interval",
        minutes=15,
        id="update_listing_stats",
        name="Update eBay listing statistics",
    )
    logger.info("Scheduled job: update_listing_stats (every 15 min)")

    scheduler.add_job(
        check_new_orders,
        "interval",
        minutes=5,
        id="check_new_orders",
        name="Check for new eBay orders",
    )
    logger.info("Scheduled job: check_new_orders (every 5 min)")

    # Restore any pending scheduled listings from disk
    scheduled_dir = Path(settings.data_dir) / "scheduled"
    if scheduled_dir.exists():
        for job_file in scheduled_dir.glob("listing_*.json"):
            try:
                data = json.loads(job_file.read_text())
                listing_id = int(job_file.stem.split("_")[1])

                # Use the stored publish_at time, not a recalculated one
                publish_at_str = data.get("publish_at")
                if not publish_at_str:
                    logger.warning("No publish_at in %s -- skipping", job_file)
                    continue

                publish_at = datetime.fromisoformat(publish_at_str)

                # If publish_at is already in the past, run immediately
                now = datetime.now(publish_at.tzinfo) if publish_at.tzinfo else datetime.utcnow()
                if publish_at <= now:
                    logger.warning(
                        "Scheduled listing %d was due at %s (past) -- publishing now",
                        listing_id, publish_at.isoformat(),
                    )
                    publish_at = None  # APScheduler runs immediately when run_date=None

                scheduler.add_job(
                    publish_scheduled_listing,
                    "date",
                    run_date=publish_at,
                    args=[listing_id],
                    id=f"publish_listing_{listing_id}",
                    name=f"Publish listing #{listing_id}",
                    replace_existing=True,
                    misfire_grace_time=3600,
                )
                logger.info(
                    "Restored scheduled listing %d for %s",
                    listing_id,
                    publish_at.isoformat() if publish_at else "NOW (overdue)",
                )
            except Exception:
                logger.exception("Failed to restore scheduled listing from %s", job_file)

    scheduler.start()
    _scheduler = scheduler
    logger.info("Background scheduler started with %d jobs", len(scheduler.get_jobs()))

    return scheduler
