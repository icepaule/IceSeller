"""Scraper for eBay.de completed/sold listings using httpx + HTML parsing."""

import logging
import re
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

# Headers to mimic a real browser
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _parse_price(price_str: str) -> float | None:
    """Parse a German-format eBay price string to a float.

    Examples
    --------
    >>> _parse_price("EUR 45,99")
    45.99
    >>> _parse_price("EUR 1.234,50")
    1234.5
    """
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.,]", "", price_str.strip())
    if not cleaned:
        return None
    # German format: 1.234,56 -> remove dots (thousands sep), replace comma
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


async def scrape_completed_listings(
    query: str, max_results: int = 20
) -> list[dict]:
    """Scrape sold/completed listings from ebay.de.

    Uses plain HTTP requests (httpx) + regex parsing instead of
    a browser, which is much more resource-efficient.

    Parameters
    ----------
    query : str
        Search keywords.
    max_results : int
        Maximum number of results to return.

    Returns
    -------
    list[dict]
        List of dicts with keys: title, price, price_type, sold, url.
    """
    encoded_query = quote_plus(query)
    url = (
        f"https://www.ebay.de/sch/i.html"
        f"?_nkw={encoded_query}"
        f"&LH_Complete=1&LH_Sold=1"
    )

    logger.info("Scraping completed listings for '%s' (max %d)", query, max_results)

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:
        logger.error("HTTP request to eBay failed: %s", exc)
        return []

    html = resp.text
    logger.info("Fetched %d bytes from eBay", len(html))

    # Try new structure first (.s-card), then legacy (.s-item)
    results = _parse_card_listings(html, max_results)
    if not results:
        results = _parse_legacy_listings(html, max_results)

    logger.info("Scraped %d completed listings for '%s'", len(results), query)
    return results


def _parse_card_listings(html: str, max_results: int) -> list[dict]:
    """Parse eBay's current .s-card listing structure (2025+)."""
    results: list[dict] = []

    # Split by data-listingid (works with both quoted and unquoted attrs)
    parts = re.split(r"data-listingid=[\"\']?(\d+)[\"\']?", html)
    # parts = [pre, id1, block1, id2, block2, ...]
    # iterate in pairs: (id, block)
    for i in range(1, len(parts) - 1, 2):
        if len(results) >= max_results:
            break
        listing_id = parts[i]
        block = parts[i + 1]
        result = _extract_card_data(block, listing_id)
        if result:
            results.append(result)

    return results


def _extract_card_data(block: str, listing_id: str = "") -> dict | None:
    """Extract title, price, type, and URL from a .s-card HTML block."""
    # Title: s-card__title (handles both quoted and unquoted class attr)
    title_match = re.search(
        r'class=["\']?s-card__title["\']?[^>]*>(.*?)</(?:span|h3|div)',
        block,
        re.DOTALL,
    )
    if not title_match:
        return None

    title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
    if not title or "shop on ebay" in title.lower():
        return None

    # Price: look for EUR pattern
    price_match = re.search(r"EUR\s*([\d.,]+)", block)
    if not price_match:
        return None
    price = _parse_price(f"EUR {price_match.group(1)}")
    if price is None or price <= 0:
        return None

    # Listing type: check for "Gebot" (auction bid)
    is_auction = bool(re.search(r"[Gg]ebot", block))
    price_type = "auction" if is_auction else "fixed_price"

    # URL (handle both quoted and unquoted href)
    url = ""
    url_match = re.search(
        r'href=["\']?(https://www\.ebay\.de/itm/[^"\'>\s]+)',
        block,
    )
    if url_match:
        url = url_match.group(1).replace("&amp;", "&")

    return {
        "title": title,
        "price": price,
        "price_type": price_type,
        "sold": True,
        "url": url,
    }


def _parse_legacy_listings(html: str, max_results: int) -> list[dict]:
    """Parse eBay's legacy .s-item listing structure (pre-2025)."""
    results: list[dict] = []

    parts = re.split(r'<li[^>]*class="s-item\s', html)

    for part in parts[1:]:
        if len(results) >= max_results:
            break

        # Title
        title_match = re.search(
            r'class="s-item__title"[^>]*>(.*?)</(?:span|h3|div)',
            part,
            re.DOTALL,
        )
        if not title_match:
            continue
        title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
        if not title or title.lower().startswith("ergebnisse"):
            continue

        # Price
        price_match = re.search(r"EUR\s*([\d.,]+)", part)
        if not price_match:
            continue
        price = _parse_price(f"EUR {price_match.group(1)}")
        if price is None or price <= 0:
            continue

        # Type
        is_auction = bool(re.search(r"[Gg]ebot", part))

        # URL
        url = ""
        url_match = re.search(
            r'href="(https://www\.ebay\.de/itm/[^"]+)"', part,
        )
        if url_match:
            url = url_match.group(1).replace("&amp;", "&")

        results.append({
            "title": title,
            "price": price,
            "price_type": "auction" if is_auction else "fixed_price",
            "sold": True,
            "url": url,
        })

    return results
