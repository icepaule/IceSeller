"""eBay REST API client for Browse, Inventory, Fulfillment, and Trading APIs."""

import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services import ebay_auth

logger = logging.getLogger(__name__)


class EbayApiError(Exception):
    """Raised when an eBay API call returns an error."""

    def __init__(self, status_code: int, detail: str, error_ids: list[int] | None = None):
        self.status_code = status_code
        self.detail = detail
        self.error_ids = error_ids or []
        super().__init__(f"eBay API error {status_code}: {detail}")


class EbayClient:
    """Async client for eBay REST APIs (Browse, Inventory, Fulfillment).

    All methods require a SQLAlchemy ``Session`` to obtain a valid
    access token via the ``ebay_auth`` module.
    """

    def __init__(self, db: Session):
        self._db = db
        self._base_url = settings.ebay_api_base
        self._marketplace = settings.ebay_marketplace

    async def _get_headers(self) -> dict[str, str]:
        """Build request headers with a valid Bearer token."""
        token = await ebay_auth.get_valid_token(self._db)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": self._marketplace,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict | None:
        """Send an authenticated request to the eBay API.

        Parameters
        ----------
        method : str
            HTTP method (GET, POST, PUT, DELETE).
        path : str
            API path, e.g. ``/buy/browse/v1/item_summary/search``.
        params : dict, optional
            Query string parameters.
        json_data : dict, optional
            JSON request body.
        timeout : float
            Request timeout in seconds.

        Returns
        -------
        dict or None
            Parsed JSON response body, or None for 204 No Content.

        Raises
        ------
        EbayApiError
            If the eBay API returns a non-2xx status.
        """
        url = f"{self._base_url}{path}"
        headers = await self._get_headers()

        logger.info("eBay API %s %s", method, url)
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.request(
                method, url, headers=headers, params=params, json=json_data,
            )

        if resp.status_code == 204:
            return None

        if resp.status_code >= 400:
            error_ids = []
            try:
                error_body = resp.json()
                logger.error("eBay API error body: %s", error_body)
                errors = error_body.get("errors", [])
                for e in errors:
                    eid = e.get("errorId")
                    if eid is not None:
                        try:
                            error_ids.append(int(eid))
                        except (ValueError, TypeError):
                            pass
                detail = "; ".join(
                    e.get("message", e.get("longMessage", str(e)))
                    for e in errors
                ) if errors else resp.text
            except Exception:
                detail = resp.text
            logger.error(
                "eBay API error %s on %s %s (errorIds=%s): %s",
                resp.status_code, method, path, error_ids, detail,
            )
            raise EbayApiError(resp.status_code, detail, error_ids=error_ids)

        return resp.json()

    # ------------------------------------------------------------------
    # Browse API
    # ------------------------------------------------------------------

    async def search_active_listings(
        self, query: str, limit: int = 20
    ) -> list[dict]:
        """Search for active listings on eBay DE.

        Uses the Browse API ``item_summary/search`` endpoint.

        Parameters
        ----------
        query : str
            Search keywords.
        limit : int
            Maximum number of results (1-200).

        Returns
        -------
        list[dict]
            List of item summaries.
        """
        params = {
            "q": query,
            "limit": min(limit, 200),
            "filter": "buyingOptions:{FIXED_PRICE|AUCTION}",
        }
        data = await self._request(
            "GET", "/buy/browse/v1/item_summary/search", params=params,
        )
        if data is None:
            return []
        return data.get("itemSummaries", [])

    async def get_item(self, item_id: str) -> dict:
        """Retrieve details for a single item.

        Parameters
        ----------
        item_id : str
            eBay item ID (e.g. ``v1|123456789|0``).

        Returns
        -------
        dict
            Full item details.
        """
        data = await self._request("GET", f"/buy/browse/v1/item/{item_id}")
        return data or {}

    # ------------------------------------------------------------------
    # Taxonomy API
    # ------------------------------------------------------------------

    async def suggest_categories(self, query: str) -> list[dict]:
        """Get category suggestions for a search query on eBay DE.

        Uses the Taxonomy API with category tree ID 77 (eBay Germany).

        Parameters
        ----------
        query : str
            Product keywords.

        Returns
        -------
        list[dict]
            List of suggested categories with categoryId and categoryName.
        """
        params = {"q": query}
        data = await self._request(
            "GET",
            "/commerce/taxonomy/v1/category_tree/77/get_suggested_categories",
            params=params,
        )
        if data is None:
            return []
        suggestions = data.get("categorySuggestions", [])
        return [
            {
                "categoryId": s.get("category", {}).get("categoryId", ""),
                "categoryName": s.get("category", {}).get("categoryName", ""),
                "categoryTreeNodeAncestors": s.get(
                    "categoryTreeNodeAncestors", []
                ),
            }
            for s in suggestions
        ]

    # ------------------------------------------------------------------
    # Inventory API
    # ------------------------------------------------------------------

    async def create_inventory_item(self, sku: str, data: dict) -> None:
        """Create or replace an inventory item.

        Uses PUT so the same SKU can be updated idempotently.

        Parameters
        ----------
        sku : str
            Seller-defined SKU string.
        data : dict
            Inventory item payload (product, condition, availability, etc.).
        """
        await self._request(
            "PUT",
            f"/sell/inventory/v1/inventory_item/{sku}",
            json_data=data,
        )
        logger.info("Inventory item created/updated: SKU=%s", sku)

    async def create_offer(self, data: dict) -> dict:
        """Create an offer for an inventory item.

        Parameters
        ----------
        data : dict
            Offer payload including sku, marketplaceId, format,
            listingPolicies, pricingSummary, etc.

        Returns
        -------
        dict
            Response containing offerId.
        """
        result = await self._request(
            "POST", "/sell/inventory/v1/offer", json_data=data,
        )
        offer_id = (result or {}).get("offerId", "")
        logger.info("Offer created: offerId=%s", offer_id)
        return result or {}

    async def publish_offer(self, offer_id: str) -> dict:
        """Publish an offer to make the listing live on eBay.

        Parameters
        ----------
        offer_id : str
            The offer ID returned by ``create_offer``.

        Returns
        -------
        dict
            Response containing listingId.
        """
        result = await self._request(
            "POST", f"/sell/inventory/v1/offer/{offer_id}/publish",
        )
        listing_id = (result or {}).get("listingId", "")
        logger.info(
            "Offer %s published, listingId=%s", offer_id, listing_id,
        )
        return result or {}

    # ------------------------------------------------------------------
    # Account API -- Business Policies
    # ------------------------------------------------------------------

    async def get_fulfillment_policies(self) -> list[dict]:
        """Get all fulfillment policies for the marketplace."""
        data = await self._request(
            "GET",
            "/sell/account/v1/fulfillment_policy",
            params={"marketplace_id": self._marketplace},
        )
        return (data or {}).get("fulfillmentPolicies", [])

    async def create_fulfillment_policy(
        self, name: str, shipping_cost: float = 5.0,
    ) -> dict:
        """Create a fulfillment policy with DHL domestic shipping."""
        payload = {
            "name": name,
            "marketplaceId": self._marketplace,
            "handlingTime": {"value": 1, "unit": "BUSINESS_DAY"},
            "shippingOptions": [
                {
                    "optionType": "DOMESTIC",
                    "costType": "FLAT_RATE",
                    "shippingServices": [
                        {
                            "sortOrder": 1,
                            "shippingCarrierCode": "DHL",
                            "shippingServiceCode": "DE_DHLPaket",
                            "shippingCost": {
                                "value": str(shipping_cost),
                                "currency": "EUR",
                            },
                            "freeShipping": False,
                        },
                    ],
                },
            ],
        }
        result = await self._request(
            "POST", "/sell/account/v1/fulfillment_policy", json_data=payload,
        )
        logger.info(
            "Fulfillment policy created: %s",
            (result or {}).get("fulfillmentPolicyId", ""),
        )
        return result or {}

    async def get_payment_policies(self) -> list[dict]:
        """Get all payment policies for the marketplace."""
        data = await self._request(
            "GET",
            "/sell/account/v1/payment_policy",
            params={"marketplace_id": self._marketplace},
        )
        return (data or {}).get("paymentPolicies", [])

    async def create_payment_policy(self, name: str) -> dict:
        """Create a payment policy (managed payments on eBay.de)."""
        payload = {
            "name": name,
            "marketplaceId": self._marketplace,
            "paymentMethods": [
                {"paymentMethodType": "PERSONAL_CHECK"},
            ],
        }
        result = await self._request(
            "POST", "/sell/account/v1/payment_policy", json_data=payload,
        )
        logger.info(
            "Payment policy created: %s",
            (result or {}).get("paymentPolicyId", ""),
        )
        return result or {}

    async def get_return_policies(self) -> list[dict]:
        """Get all return policies for the marketplace."""
        data = await self._request(
            "GET",
            "/sell/account/v1/return_policy",
            params={"marketplace_id": self._marketplace},
        )
        return (data or {}).get("returnPolicies", [])

    async def create_return_policy(self, name: str) -> dict:
        """Create a return policy: 14 days, buyer pays return shipping."""
        payload = {
            "name": name,
            "marketplaceId": self._marketplace,
            "returnsAccepted": True,
            "returnPeriod": {"value": 30, "unit": "DAY"},
            "returnShippingCostPayer": "BUYER",
        }
        result = await self._request(
            "POST", "/sell/account/v1/return_policy", json_data=payload,
        )
        logger.info(
            "Return policy created: %s",
            (result or {}).get("returnPolicyId", ""),
        )
        return result or {}

    async def ensure_policies(self) -> dict[str, str]:
        """Get or create the three required business policies.

        Returns dict with fulfillmentPolicyId, paymentPolicyId,
        returnPolicyId.
        """
        policy_ids: dict[str, str] = {}

        # Fulfillment policy
        policies = await self.get_fulfillment_policies()
        if policies:
            policy_ids["fulfillmentPolicyId"] = policies[0]["fulfillmentPolicyId"]
        else:
            result = await self.create_fulfillment_policy("IceSeller DHL Versand")
            policy_ids["fulfillmentPolicyId"] = result.get("fulfillmentPolicyId", "")

        # Payment policy
        policies = await self.get_payment_policies()
        if policies:
            policy_ids["paymentPolicyId"] = policies[0]["paymentPolicyId"]
        else:
            result = await self.create_payment_policy("IceSeller Zahlung")
            policy_ids["paymentPolicyId"] = result.get("paymentPolicyId", "")

        # Return policy
        policies = await self.get_return_policies()
        if policies:
            policy_ids["returnPolicyId"] = policies[0]["returnPolicyId"]
        else:
            result = await self.create_return_policy("IceSeller 14 Tage Ruecknahme")
            policy_ids["returnPolicyId"] = result.get("returnPolicyId", "")

        logger.info("Business policies resolved: %s", policy_ids)
        return policy_ids

    # ------------------------------------------------------------------
    # Fulfillment API
    # ------------------------------------------------------------------

    async def get_orders(self, limit: int = 20) -> list[dict]:
        """Retrieve recent orders.

        Parameters
        ----------
        limit : int
            Maximum number of orders to return.

        Returns
        -------
        list[dict]
            List of order objects.
        """
        params = {"limit": min(limit, 200)}
        data = await self._request(
            "GET", "/sell/fulfillment/v1/order", params=params,
        )
        if data is None:
            return []
        return data.get("orders", [])

    async def create_shipping_fulfillment(
        self, order_id: str, data: dict
    ) -> dict:
        """Create a shipping fulfillment record for an order.

        Parameters
        ----------
        order_id : str
            eBay order ID.
        data : dict
            Fulfillment payload with lineItems, shippingCarrierCode,
            trackingNumber.

        Returns
        -------
        dict
            Response containing fulfillmentId.
        """
        result = await self._request(
            "POST",
            f"/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment",
            json_data=data,
        )
        fulfillment_id = (result or {}).get("fulfillmentId", "")
        logger.info(
            "Shipping fulfillment created for order %s: %s",
            order_id, fulfillment_id,
        )
        return result or {}

    # ------------------------------------------------------------------
    # Trading API (fallback for sellers without Business Policies)
    # ------------------------------------------------------------------

    _CONDITION_MAP = {
        "NEW": "1000",
        "USED_EXCELLENT": "3000",
        "USED_VERY_GOOD": "4000",
        "USED_GOOD": "5000",
        "USED_ACCEPTABLE": "6000",
        "FOR_PARTS_OR_NOT_WORKING": "7000",
    }

    _DURATION_MAP = {
        "DAYS_3": "Days_3",
        "DAYS_5": "Days_5",
        "DAYS_7": "Days_7",
        "DAYS_10": "Days_10",
        "DAYS_30": "Days_30",
        "GTC": "GTC",
    }

    @staticmethod
    def _ensure_min_resolution(local_path: str, min_px: int = 800) -> bytes:
        """Read an image, upscale if longest side < min_px.

        Returns the (possibly resized) JPEG bytes.
        eBay requires >= 500px; we target 800px for better quality.
        """
        from PIL import Image
        import io

        img = Image.open(local_path)
        w, h = img.size
        longest = max(w, h)
        if longest < min_px:
            scale = min_px / longest
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.info("Image upscaled: %dx%d -> %dx%d", w, h, new_w, new_h)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    async def upload_image_to_ebay(self, local_path: str) -> str:
        """Upload a local image to eBay Picture Services (EPS).

        Uses the Trading API ``UploadSiteHostedPictures`` call with
        multipart/form-data to upload the image binary.
        Images smaller than 800px are automatically upscaled.

        Returns the eBay-hosted URL (FullURL) for use in listings.
        """
        from pathlib import Path

        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {local_path}")

        token = await ebay_auth.get_valid_token(self._db)
        ns = "urn:ebay:apis:eBLBaseComponents"

        # Build XML payload
        root = ET.Element("UploadSiteHostedPicturesRequest", xmlns=ns)
        ET.SubElement(root, "ErrorLanguage").text = "de_DE"
        ET.SubElement(root, "WarningLevel").text = "High"
        ET.SubElement(root, "PictureName").text = path.name
        # Request supersize for best quality
        pic_set = ET.SubElement(root, "PictureSet")
        pic_set.text = "Supersize"

        xml_payload = '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(
            root, encoding="unicode"
        )

        trading_url = "https://api.ebay.com/ws/api.dll"
        headers = {
            "X-EBAY-API-SITEID": "77",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1225",
            "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
            "X-EBAY-API-IAF-TOKEN": token,
        }

        # Ensure minimum resolution (eBay requires >= 500px, we target 800px)
        image_data = self._ensure_min_resolution(str(path))

        # Multipart: XML payload + image binary
        files = {
            "XML Payload": ("request.xml", xml_payload, "text/xml"),
            "file": (path.name, image_data, "image/jpeg"),
        }

        logger.info("Uploading image to eBay EPS: %s (%d bytes)", path.name, len(image_data))

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            resp = await client.post(trading_url, headers=headers, files=files)

        # Parse response
        resp_root = ET.fromstring(resp.text)
        ns_map = {"e": ns}
        ack = (resp_root.findtext("e:Ack", namespaces=ns_map)
               or resp_root.findtext("Ack") or "")

        if ack not in ("Success", "Warning"):
            errors = []
            for err in resp_root.findall("e:Errors", ns_map) or resp_root.findall("Errors"):
                msg = (err.findtext("e:LongMessage", namespaces=ns_map)
                       or err.findtext("LongMessage")
                       or err.findtext("e:ShortMessage", namespaces=ns_map)
                       or err.findtext("ShortMessage") or "")
                if msg:
                    errors.append(msg)
            error_detail = "; ".join(errors) or resp.text[:500]
            logger.error("UploadSiteHostedPictures failed: %s", error_detail)
            raise EbayApiError(resp.status_code, error_detail)

        # Extract the hosted URL
        full_url = (resp_root.findtext(".//e:FullURL", namespaces=ns_map)
                    or resp_root.findtext(".//FullURL") or "")

        if not full_url:
            logger.error("UploadSiteHostedPictures: no FullURL in response: %s", resp.text[:500])
            raise EbayApiError(500, "eBay image upload: keine URL in Antwort")

        logger.info("Image uploaded to eBay EPS: %s -> %s", path.name, full_url)
        return full_url

    async def publish_via_trading_api(
        self,
        *,
        title: str,
        description_html: str,
        category_id: str,
        condition: str,
        listing_type: str,
        start_price: float,
        buy_now_price: float = 0.0,
        shipping_cost: float = 5.49,
        duration: str = "DAYS_7",
        image_urls: list[str] | None = None,
        image_paths: list[str] | None = None,
        aspects: dict[str, list[str]] | None = None,
        sku: str = "",
        quantity: int = 1,
        best_offer: bool = False,
        verify_only: bool = False,
    ) -> dict:
        """Publish a listing via the Trading API (AddItem/AddFixedPriceItem).

        This does NOT require Business Policies -- shipping, payment and
        return details are specified inline.

        Parameters
        ----------
        image_urls : list[str], optional
            Pre-existing eBay-hosted image URLs.
        image_paths : list[str], optional
            Local image file paths to upload to eBay EPS first.
        verify_only : bool
            If True, use VerifyAddItem instead of AddItem (dry run).

        Returns dict with 'listingId', 'fees', 'warnings'.
        """
        token = await ebay_auth.get_valid_token(self._db)

        # Upload local images to eBay EPS if provided
        ebay_image_urls = list(image_urls or [])
        for local_path in (image_paths or []):
            try:
                hosted_url = await self.upload_image_to_ebay(local_path)
                ebay_image_urls.append(hosted_url)
            except Exception as exc:
                logger.warning("Failed to upload image %s: %s", local_path, exc)

        is_auction = listing_type == "AUCTION"
        if verify_only:
            call_name = "VerifyAddItem" if is_auction else "VerifyAddFixedPriceItem"
        else:
            call_name = "AddItem" if is_auction else "AddFixedPriceItem"
        api_listing_type = "Chinese" if is_auction else "FixedPriceItem"
        condition_id = self._CONDITION_MAP.get(condition, "3000")
        # eBay requires GTC for FixedPriceItem listings
        if not is_auction:
            trading_duration = "GTC"
        else:
            trading_duration = self._DURATION_MAP.get(duration, "Days_7")

        # Build XML
        ns = "urn:ebay:apis:eBLBaseComponents"
        root = ET.Element(f"{call_name}Request", xmlns=ns)
        ET.SubElement(root, "ErrorLanguage").text = "de_DE"
        ET.SubElement(root, "WarningLevel").text = "High"

        # Clean common AI artifacts from title
        import re
        clean_title = re.sub(r'^(eBay[- ]?)?Titel:\s*', '', title, flags=re.IGNORECASE)
        clean_title = re.sub(r'\s*-\s*Gebraucht\s*(Hervorragend)?$', '', clean_title, flags=re.IGNORECASE)
        clean_title = clean_title.strip(' -,')

        item_el = ET.SubElement(root, "Item")
        ET.SubElement(item_el, "Title").text = clean_title[:80]
        desc_el = ET.SubElement(item_el, "Description")
        desc_el.text = description_html
        ET.SubElement(item_el, "SKU").text = sku

        cat_el = ET.SubElement(item_el, "PrimaryCategory")
        ET.SubElement(cat_el, "CategoryID").text = category_id

        price_el = ET.SubElement(item_el, "StartPrice", currencyID="EUR")
        price_el.text = f"{start_price:.2f}"

        # eBay rule: BuyItNowPrice must be >= 140% of StartPrice
        if is_auction and buy_now_price > 0:
            import math
            min_bnp = math.ceil(start_price * 1.4)
            effective_bnp = max(buy_now_price, min_bnp)
            # Round up to full EUR
            effective_bnp = float(math.ceil(effective_bnp))
            if effective_bnp != buy_now_price:
                logger.info(
                    "BuyItNowPrice adjusted: %.2f -> %.2f (min 140%% of %.2f = %.2f)",
                    buy_now_price, effective_bnp, start_price, start_price * 1.4,
                )
            bnp_el = ET.SubElement(item_el, "BuyItNowPrice", currencyID="EUR")
            bnp_el.text = f"{effective_bnp:.2f}"

        if not is_auction and best_offer:
            bo_el = ET.SubElement(item_el, "BestOfferDetails")
            ET.SubElement(bo_el, "BestOfferEnabled").text = "true"

        ET.SubElement(item_el, "ConditionID").text = condition_id
        ET.SubElement(item_el, "Country").text = "DE"
        ET.SubElement(item_el, "Currency").text = "EUR"
        ET.SubElement(item_el, "DispatchTimeMax").text = "1"
        ET.SubElement(item_el, "ListingDuration").text = trading_duration
        ET.SubElement(item_el, "ListingType").text = api_listing_type
        # Location from sender config
        ET.SubElement(item_el, "Location").text = (
            f"{settings.sender_postal_code} {settings.sender_city}"
            if settings.sender_city else "Deutschland"
        )
        ET.SubElement(item_el, "PostalCode").text = settings.sender_postal_code or ""
        ET.SubElement(item_el, "Quantity").text = str(quantity)
        ET.SubElement(item_el, "Site").text = "Germany"

        # Shipping
        ship_el = ET.SubElement(item_el, "ShippingDetails")
        ET.SubElement(ship_el, "ShippingType").text = "Flat"
        svc_el = ET.SubElement(ship_el, "ShippingServiceOptions")
        ET.SubElement(svc_el, "ShippingServicePriority").text = "1"
        ET.SubElement(svc_el, "ShippingService").text = "DE_DHLPaket"
        cost_el = ET.SubElement(svc_el, "ShippingServiceCost", currencyID="EUR")
        cost_el.text = f"{shipping_cost:.2f}"
        # Additional shipping cost per extra item (same as base for small items)
        add_cost_el = ET.SubElement(svc_el, "ShippingServiceAdditionalCost", currencyID="EUR")
        add_cost_el.text = f"{shipping_cost:.2f}"
        ET.SubElement(svc_el, "FreeShipping").text = "false"

        # Return policy (no RefundOption — eBay ignores it for managed payments)
        ret_el = ET.SubElement(item_el, "ReturnPolicy")
        ET.SubElement(ret_el, "ReturnsAcceptedOption").text = "ReturnsAccepted"
        ET.SubElement(ret_el, "ReturnsWithinOption").text = "Days_30"
        ET.SubElement(ret_el, "ShippingCostPaidByOption").text = "Buyer"

        # Images (eBay-hosted URLs)
        if ebay_image_urls:
            pic_el = ET.SubElement(item_el, "PictureDetails")
            for url in ebay_image_urls[:12]:
                ET.SubElement(pic_el, "PictureURL").text = url

        # Item Specifics
        if aspects:
            specifics_el = ET.SubElement(item_el, "ItemSpecifics")
            for name, values in aspects.items():
                for val in values:
                    nvl = ET.SubElement(specifics_el, "NameValueList")
                    ET.SubElement(nvl, "Name").text = name
                    ET.SubElement(nvl, "Value").text = val

        xml_body = '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(
            root, encoding="unicode"
        )

        # Trading API endpoint
        trading_url = "https://api.ebay.com/ws/api.dll"
        headers = {
            "X-EBAY-API-SITEID": "77",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1225",
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }

        logger.info("Trading API %s: title='%s', price=%.2f, cat=%s, images=%d, loc=%s",
                     call_name, title[:50], start_price, category_id,
                     len(ebay_image_urls), settings.sender_city)

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            resp = await client.post(trading_url, headers=headers, content=xml_body)

        # Parse XML response
        resp_root = ET.fromstring(resp.text)
        ns_map = {"e": ns}
        ack = (resp_root.findtext("e:Ack", default="", namespaces=ns_map)
               or resp_root.findtext("Ack", default=""))

        # Collect warnings and errors separately
        warnings = []
        hard_errors = []
        for err in resp_root.findall("e:Errors", ns_map) or resp_root.findall("Errors"):
            severity = (err.findtext("e:SeverityCode", namespaces=ns_map)
                        or err.findtext("SeverityCode") or "")
            msg = (err.findtext("e:LongMessage", namespaces=ns_map)
                   or err.findtext("LongMessage")
                   or err.findtext("e:ShortMessage", namespaces=ns_map)
                   or err.findtext("ShortMessage")
                   or "Unbekannter Fehler")
            if severity == "Warning":
                warnings.append(msg)
            else:
                hard_errors.append(msg)

        if warnings:
            logger.info("Trading API %s warnings: %s", call_name, "; ".join(warnings))

        if ack not in ("Success", "Warning"):
            error_detail = "; ".join(hard_errors) or "; ".join(warnings) or resp.text[:500]
            logger.error("Trading API %s failed (Ack=%s): %s", call_name, ack, error_detail)
            raise EbayApiError(resp.status_code, error_detail)

        # Extract listing ID
        listing_id = (resp_root.findtext("e:ItemID", namespaces=ns_map)
                      or resp_root.findtext("ItemID") or "")

        # Extract fees
        fees = {}
        for fee in (resp_root.findall(".//e:Fee", ns_map)
                    or resp_root.findall(".//Fee")):
            fname = (fee.findtext("e:Name", namespaces=ns_map)
                     or fee.findtext("Name") or "")
            famount = (fee.findtext("e:Fee", namespaces=ns_map)
                       or fee.findtext("Fee") or "0")
            if fname:
                fees[fname] = famount

        logger.info("Trading API %s success: listingId=%s, warnings=%d",
                     call_name, listing_id, len(warnings))
        return {"listingId": listing_id, "fees": fees, "warnings": warnings}
