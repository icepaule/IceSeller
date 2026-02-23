"""Price suggestion calculator based on market research and DHL shipping costs."""

import logging
import math
import statistics
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# DHL domestic shipping options (Germany, 2025/2026 rates)
# Format: (max_weight_g, max_l_cm, max_w_cm, max_h_cm, service_name, price_eur, dhl_product)
DHL_SHIPPING_OPTIONS: list[tuple[int, int, int, int, str, float, str]] = [
    (500,   35, 25, 3,   "DHL Warenpost bis 500g (Briefkasten)",  1.99, "V62WP"),
    (1000,  35, 25, 10,  "DHL Warenpost bis 1kg (Briefkasten)",   2.49, "V62WP"),
    (1000,  25, 18, 10,  "DHL Paeckchen S",                       3.99, "V62WP"),
    (2000,  60, 30, 15,  "DHL Paeckchen M",                       4.79, "V62WP"),
    (2000,  120, 60, 60, "DHL Paket 2 kg",                        5.49, "V01PAK"),
    (5000,  120, 60, 60, "DHL Paket 5 kg",                        6.49, "V01PAK"),
    (10000, 120, 60, 60, "DHL Paket 10 kg",                       9.49, "V01PAK"),
    (31500, 120, 60, 60, "DHL Paket 31.5 kg",                    16.49, "V01PAK"),
]


def get_shipping_options(weight_g: int, dimensions: dict = None) -> list[dict]:
    """Return all suitable DHL shipping options sorted by price.

    Parameters
    ----------
    weight_g : int
        Package weight in grams.
    dimensions : dict, optional
        {length, width, height} in cm.

    Returns
    -------
    list[dict]
        List of {service, price, product, fits} dicts, cheapest first.
    """
    if weight_g <= 0:
        return []

    l = dimensions.get("length", 0) if dimensions else 0
    w = dimensions.get("width", 0) if dimensions else 0
    h = dimensions.get("height", 0) if dimensions else 0
    has_dims = l > 0 and w > 0 and h > 0
    # Sort actual dimensions ascending for fitting check
    actual = sorted([l, w, h])

    options = []
    for max_wg, max_l, max_w, max_h, name, price, product in DHL_SHIPPING_OPTIONS:
        if weight_g > max_wg:
            continue
        if has_dims:
            limits = sorted([max_l, max_w, max_h])
            fits = actual[0] <= limits[0] and actual[1] <= limits[1] and actual[2] <= limits[2]
        else:
            fits = True  # assume it fits if no dimensions given
        options.append({
            "service": name,
            "price": price,
            "product": product,
            "fits": fits,
        })

    return options


def get_shipping_cost(weight_g: int) -> tuple[str, float]:
    """Return cheapest fitting DHL option (legacy interface)."""
    if weight_g <= 0:
        raise ValueError(f"Weight must be positive, got {weight_g}g")
    options = get_shipping_options(weight_g)
    for opt in options:
        if opt["fits"]:
            return opt["service"], opt["price"]
    if options:
        return options[0]["service"], options[0]["price"]
    raise ValueError(
        f"Gewicht {weight_g}g uebersteigt DHL-Maximum von 31.5 kg."
    )


# Packaging surcharge (EUR)
PACKAGING_COST = 3.0


def calculate_shipping_total(
    weight_g: int, dimensions: dict = None,
) -> dict:
    """Calculate total shipping cost: DHL base + packaging, rounded up.

    The total is DHL shipping cost + 3 EUR packaging, rounded up to
    the next full euro.  E.g. DHL 1.99 + 3.00 = 4.99 -> 5.00 EUR.

    Parameters
    ----------
    weight_g : int
        Package weight in grams.
    dimensions : dict, optional
        {length, width, height} in cm.

    Returns
    -------
    dict
        {service, product, dhl_cost, packaging, total}
    """
    options = get_shipping_options(weight_g, dimensions)
    cheapest = None
    for opt in options:
        if opt["fits"]:
            cheapest = opt
            break
    if cheapest is None and options:
        cheapest = options[0]
    if cheapest is None:
        return {
            "service": "Kein passender Versand",
            "product": "",
            "dhl_cost": 0.0,
            "packaging": PACKAGING_COST,
            "total": math.ceil(PACKAGING_COST),
        }

    dhl_cost = cheapest["price"]
    raw_total = dhl_cost + PACKAGING_COST
    total = float(math.ceil(raw_total))

    return {
        "service": cheapest["service"],
        "product": cheapest["product"],
        "dhl_cost": dhl_cost,
        "packaging": PACKAGING_COST,
        "total": total,
    }


def calculate_suggestions(
    research_results: list[dict],
    weight_g: Optional[int] = None,
) -> dict:
    """Calculate price suggestions from completed listings research data.

    Analyses the prices from scraped or API-fetched research results and
    produces pricing recommendations for both auction and fixed-price
    listings.

    Parameters
    ----------
    research_results : list[dict]
        Each dict must contain at least a ``price`` key (float).
        Optional keys: ``price_type``, ``sold``, ``title``.
    weight_g : int, optional
        Item weight in grams for shipping cost estimation.

    Returns
    -------
    dict
        A dict with the following structure::

            {
                "sample_size": int,
                "median_price": float,
                "avg_price": float,
                "min_price": float,
                "max_price": float,
                "suggested_auction_start": float,
                "suggested_fixed_price": float,
                "shipping": {
                    "service": str,
                    "cost": float,
                    "weight_g": int | None,
                } | None,
                "suggested_total_auction": float | None,
                "suggested_total_fixed": float | None,
            }

        Prices are rounded to two decimal places.  If ``weight_g`` is
        provided, shipping info and totals (item + shipping) are included.
    """
    # Separate sold (completed) from active listings
    sold_prices = [
        r["price"]
        for r in research_results
        if isinstance(r.get("price"), (int, float)) and r["price"] > 0
        and r.get("sold")
    ]
    active_prices = [
        r["price"]
        for r in research_results
        if isinstance(r.get("price"), (int, float)) and r["price"] > 0
        and not r.get("sold")
    ]
    all_prices = sold_prices + active_prices

    if not all_prices:
        logger.warning("No valid prices in research results")
        result: dict = {
            "sample_size": 0,
            "sold_count": 0,
            "active_count": 0,
            "sold_avg": 0.0,
            "sold_median": 0.0,
            "active_avg": 0.0,
            "median_price": 0.0,
            "avg_price": 0.0,
            "min_price": 0.0,
            "max_price": 0.0,
            "suggested_auction_start": 0.0,
            "suggested_fixed_price": 0.0,
            "shipping": None,
            "suggested_total_auction": None,
            "suggested_total_fixed": None,
        }
        return result

    # Sold listings are the primary price indicator (actual market value)
    if sold_prices:
        sold_avg = statistics.mean(sold_prices)
        sold_median = statistics.median(sold_prices)
    else:
        sold_avg = 0.0
        sold_median = 0.0

    if active_prices:
        active_avg = statistics.mean(active_prices)
    else:
        active_avg = 0.0

    # Use sold prices as the basis for suggestions; fall back to
    # active prices only if no sold data is available
    if sold_prices:
        basis_avg = sold_avg
        basis_median = sold_median
    else:
        basis_avg = statistics.mean(active_prices)
        basis_median = statistics.median(active_prices)

    median_price = basis_median
    avg_price = basis_avg
    min_price = min(all_prices)
    max_price = max(all_prices)

    # Auction start: 90% of average sold price -- slightly below to
    # attract bidders while staying close to realistic market value.
    # Round up to next full euro for cleaner pricing.
    suggested_auction_start = float(math.ceil(basis_avg * 0.9))

    # Fixed price: 125% of average sold price -- premium for
    # immediate purchase convenience.
    # Round up to next full euro for cleaner pricing.
    suggested_fixed_price = float(math.ceil(basis_avg * 1.25))

    # Shipping (DHL + 3 EUR packaging, rounded up to full euro)
    shipping_info: dict | None = None
    suggested_total_auction: float | None = None
    suggested_total_fixed: float | None = None

    if weight_g is not None and weight_g > 0:
        try:
            shipping_total = calculate_shipping_total(weight_g)
            shipping_info = {
                "service": shipping_total["service"],
                "dhl_cost": shipping_total["dhl_cost"],
                "packaging": shipping_total["packaging"],
                "cost": shipping_total["total"],
                "weight_g": weight_g,
            }
            suggested_total_auction = round(
                suggested_auction_start + shipping_total["total"], 2,
            )
            suggested_total_fixed = round(
                suggested_fixed_price + shipping_total["total"], 2,
            )
        except ValueError as exc:
            logger.warning("Shipping cost calculation failed: %s", exc)
            shipping_info = {
                "service": "unknown",
                "cost": 0.0,
                "weight_g": weight_g,
                "error": str(exc),
            }

    result = {
        "sample_size": len(all_prices),
        "sold_count": len(sold_prices),
        "active_count": len(active_prices),
        "sold_avg": round(sold_avg, 2),
        "sold_median": round(sold_median, 2),
        "active_avg": round(active_avg, 2),
        "median_price": round(median_price, 2),
        "avg_price": round(avg_price, 2),
        "min_price": round(min_price, 2),
        "max_price": round(max_price, 2),
        "suggested_auction_start": suggested_auction_start,
        "suggested_fixed_price": suggested_fixed_price,
        "shipping": shipping_info,
        "suggested_total_auction": suggested_total_auction,
        "suggested_total_fixed": suggested_total_fixed,
    }

    logger.info(
        "Price suggestions: sold_avg=%.2f (n=%d), active_avg=%.2f (n=%d), "
        "auction_start=%.2f, fixed=%.2f",
        sold_avg, len(sold_prices),
        active_avg, len(active_prices),
        suggested_auction_start,
        suggested_fixed_price,
    )
    return result


# ------------------------------------------------------------------
# Auction timing -- end on Sunday evening
# ------------------------------------------------------------------

CET = ZoneInfo("Europe/Berlin")

# Duration mapping (eBay key -> days)
DURATION_DAYS = {
    "DAYS_3": 3,
    "DAYS_5": 5,
    "DAYS_7": 7,
    "DAYS_10": 10,
    "DAYS_30": 30,
}


def calculate_optimal_publish_time(
    duration: str = "DAYS_7",
    target_weekday: int = 6,  # 0=Mon, 6=Sun
    target_hour: int = 19,
    target_minute: int = 0,
) -> dict:
    """Calculate when to publish an auction so it ends Sunday evening CET.

    Parameters
    ----------
    duration : str
        eBay auction duration key (DAYS_3, DAYS_5, DAYS_7, DAYS_10).
    target_weekday : int
        Day the auction should end (0=Mon .. 6=Sun). Default Sunday.
    target_hour : int
        Hour (CET) the auction should end. Default 19 (7 PM).
    target_minute : int
        Minute the auction should end.

    Returns
    -------
    dict
        {publish_at: datetime (CET), end_at: datetime (CET),
         wait_hours: float, duration_days: int}
    """
    days = DURATION_DAYS.get(duration, 7)
    now = datetime.now(CET)

    # Find the next target_weekday at target_hour:target_minute
    # that is at least `days` from now
    target_end = now.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0,
    )

    # Advance to the next target_weekday
    days_ahead = (target_weekday - target_end.weekday()) % 7
    if days_ahead == 0 and target_end <= now:
        days_ahead = 7
    target_end += timedelta(days=days_ahead)

    # Ensure at least `days` between publish and end
    publish_at = target_end - timedelta(days=days)
    while publish_at <= now:
        target_end += timedelta(weeks=1)
        publish_at = target_end - timedelta(days=days)

    wait_seconds = (publish_at - now).total_seconds()

    return {
        "publish_at": publish_at,
        "end_at": target_end,
        "wait_hours": round(wait_seconds / 3600, 1),
        "duration_days": days,
    }
