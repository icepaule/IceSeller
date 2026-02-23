"""eBay OAuth 2.0 authentication service."""

import base64
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EbayToken

logger = logging.getLogger(__name__)

DEFAULT_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
]


def _basic_auth_header() -> str:
    """Build Base64-encoded Basic auth header from app_id:cert_id."""
    credentials = f"{settings.ebay_app_id}:{settings.ebay_cert_id}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def get_auth_url(scopes: list[str] | None = None) -> str:
    """Build the eBay OAuth consent URL for user authorization.

    Parameters
    ----------
    scopes : list[str], optional
        OAuth scopes to request. Defaults to ``DEFAULT_SCOPES``.

    Returns
    -------
    str
        The full authorization URL the user should be redirected to.
    """
    if scopes is None:
        scopes = DEFAULT_SCOPES

    params = {
        "client_id": settings.ebay_app_id,
        "redirect_uri": settings.ebay_redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
    }
    url = f"{settings.ebay_auth_base}/oauth2/authorize?{urlencode(params)}"
    logger.info("Generated eBay auth URL: %s", url)
    return url


async def exchange_code(code: str) -> dict:
    """Exchange an authorization code for access and refresh tokens.

    Parameters
    ----------
    code : str
        The authorization code received from the eBay consent redirect.

    Returns
    -------
    dict
        Token response containing access_token, refresh_token,
        expires_in, token_type, etc.

    Raises
    ------
    httpx.HTTPStatusError
        If the token exchange request fails.
    """
    url = f"{settings.ebay_api_base}/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _basic_auth_header(),
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.ebay_redirect_uri,
    }

    logger.info("Exchanging auth code for tokens at %s", url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(url, headers=headers, data=data)
        resp.raise_for_status()

    token_data = resp.json()
    logger.info(
        "Token exchange successful, expires_in=%s",
        token_data.get("expires_in"),
    )
    return token_data


async def refresh_access_token(refresh_token: str) -> dict:
    """Refresh an expired access token using the refresh token.

    Parameters
    ----------
    refresh_token : str
        The refresh token obtained during the initial authorization.

    Returns
    -------
    dict
        Token response containing a new access_token and expires_in.

    Raises
    ------
    httpx.HTTPStatusError
        If the refresh request fails.
    """
    url = f"{settings.ebay_api_base}/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _basic_auth_header(),
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(DEFAULT_SCOPES),
    }

    logger.info("Refreshing access token at %s", url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(url, headers=headers, data=data)
        resp.raise_for_status()

    token_data = resp.json()
    logger.info(
        "Token refresh successful, expires_in=%s",
        token_data.get("expires_in"),
    )
    return token_data


def save_tokens(db: Session, token_data: dict) -> EbayToken:
    """Save or update eBay tokens in the database.

    If a token row already exists it is updated in place; otherwise a new
    row is created.  Only one token row is expected (single-seller app).

    Parameters
    ----------
    db : Session
        SQLAlchemy database session.
    token_data : dict
        Token response from eBay (access_token, refresh_token, etc.).

    Returns
    -------
    EbayToken
        The persisted token object.
    """
    token = db.query(EbayToken).first()

    now = datetime.utcnow()
    expires_in = token_data.get("expires_in", 7200)
    refresh_expires_in = token_data.get("refresh_token_expires_in", 47304000)

    if token is None:
        token = EbayToken(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token", ""),
            token_type=token_data.get("token_type", "User Access Token"),
            expires_at=now + timedelta(seconds=expires_in),
            refresh_expires_at=now + timedelta(seconds=refresh_expires_in),
            scope=token_data.get("scope", " ".join(DEFAULT_SCOPES)),
            updated_at=now,
        )
        db.add(token)
    else:
        token.access_token = token_data["access_token"]
        if "refresh_token" in token_data:
            token.refresh_token = token_data["refresh_token"]
            token.refresh_expires_at = now + timedelta(seconds=refresh_expires_in)
        token.token_type = token_data.get("token_type", token.token_type)
        token.expires_at = now + timedelta(seconds=expires_in)
        token.scope = token_data.get("scope", token.scope)
        token.updated_at = now

    db.commit()
    db.refresh(token)
    logger.info("Tokens saved, expires_at=%s", token.expires_at)
    return token


async def get_valid_token(db: Session) -> str:
    """Return a valid access token, refreshing if necessary.

    Looks up the stored token in the database.  If the access token has
    expired (or will expire within 5 minutes) and a valid refresh token
    is available, the access token is automatically refreshed and the
    database row is updated.

    Parameters
    ----------
    db : Session
        SQLAlchemy database session.

    Returns
    -------
    str
        A valid eBay access token.

    Raises
    ------
    RuntimeError
        If no token exists or the refresh token has also expired.
    """
    token = db.query(EbayToken).first()
    if token is None:
        raise RuntimeError(
            "No eBay token found. Please authorize via /auth/ebay first."
        )

    now = datetime.utcnow()
    buffer = timedelta(minutes=5)

    # Access token still valid
    if token.expires_at and token.expires_at > now + buffer:
        return token.access_token

    # Access token expired -- try refresh
    if not token.refresh_token:
        raise RuntimeError(
            "Access token expired and no refresh token available. "
            "Please re-authorize via /auth/ebay."
        )

    if token.refresh_expires_at and token.refresh_expires_at <= now:
        raise RuntimeError(
            "Refresh token has expired. Please re-authorize via /auth/ebay."
        )

    logger.info("Access token expired, refreshing...")
    try:
        new_data = await refresh_access_token(token.refresh_token)
        save_tokens(db, new_data)
        # Re-read after commit
        token = db.query(EbayToken).first()
        return token.access_token
    except httpx.HTTPStatusError as exc:
        logger.error("Token refresh failed: %s", exc.response.text)
        raise RuntimeError(
            f"Token refresh failed ({exc.response.status_code}). "
            "Please re-authorize via /auth/ebay."
        ) from exc
