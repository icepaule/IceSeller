"""DHL Parcel Germany API client for creating shipments and labels."""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class DhlApiError(Exception):
    """Raised when a DHL API call returns an error."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"DHL API error {status_code}: {detail}")


class DhlClient:
    """Async client for the DHL Parcel DE Shipping API (v2).

    Uses Basic Auth (username:password) + API key header.
    """

    def __init__(self):
        self._base_url = settings.dhl_api_base
        self._api_key = settings.dhl_api_key

    def _get_headers(self) -> dict[str, str]:
        return {
            "dhl-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get_auth(self) -> tuple[str, str]:
        return (settings.dhl_username, settings.dhl_password)

    async def create_shipment(
        self,
        recipient_address: dict[str, Any],
        weight_g: int,
    ) -> dict[str, str]:
        """Create a DHL shipment and return tracking number + label URL.

        Parameters
        ----------
        recipient_address : dict
            Recipient address with keys: name, street, street2, city,
            postal_code, country (ISO 2-letter code).
        weight_g : int
            Package weight in grams.

        Returns
        -------
        dict
            A dict with ``tracking_number`` and ``label_url``.

        Raises
        ------
        DhlApiError
            If the DHL API returns an error.
        """
        # Determine product based on destination
        country = recipient_address.get("country", "DE")
        product = "V01PAK" if country == "DE" else "V53WPAK"

        # Weight in kg for the API
        weight_kg = max(weight_g / 1000, 0.1)

        shipment_data = {
            "profile": "STANDARD_GRUPPENPROFIL",
            "shipments": [
                {
                    "product": product,
                    "billingNumber": settings.dhl_billing_number,
                    "shipper": {
                        "name1": settings.sender_name,
                        "addressStreet": settings.sender_street,
                        "postalCode": settings.sender_postal_code,
                        "city": settings.sender_city,
                        "country": settings.sender_country,
                    },
                    "consignee": {
                        "name1": recipient_address.get("name", ""),
                        "addressStreet": recipient_address.get("street", ""),
                        "additionalAddressInformation1": recipient_address.get("street2", ""),
                        "postalCode": recipient_address.get("postal_code", ""),
                        "city": recipient_address.get("city", ""),
                        "country": recipient_address.get("country", "DE"),
                    },
                    "details": {
                        "weight": {
                            "uom": "kg",
                            "value": round(weight_kg, 2),
                        },
                    },
                },
            ],
        }

        headers = self._get_headers()
        auth = self._get_auth()
        url = f"{self._base_url}/parcel/de/shipping/v2/orders"

        logger.info("DHL API POST %s (weight=%dg, product=%s)", url, weight_g, product)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                url,
                headers=headers,
                auth=auth,
                json=shipment_data,
            )

        if resp.status_code >= 400:
            try:
                error_body = resp.json()
                detail = error_body.get("detail", resp.text)
            except Exception:
                detail = resp.text
            logger.error("DHL API error %s: %s", resp.status_code, detail)
            raise DhlApiError(resp.status_code, detail)

        data = resp.json()

        # Extract tracking number and label URL from response
        items = data.get("items", [])
        if not items:
            raise DhlApiError(0, "DHL API returned no shipment items")

        shipment_item = items[0]
        tracking_number = shipment_item.get("shipmentNo", "")
        label = shipment_item.get("label", {})
        label_url = label.get("url", "") or label.get("b64", "")

        logger.info("DHL shipment created: tracking=%s", tracking_number)

        return {
            "tracking_number": tracking_number,
            "label_url": label_url,
        }
