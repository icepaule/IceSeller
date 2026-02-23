"""eBay OAuth 2.0 authentication router + Marketplace Account Deletion endpoint."""

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.ebay_auth import (
    get_auth_url,
    exchange_code,
    save_tokens,
    get_valid_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ------------------------------------------------------------------
# Marketplace Account Deletion / Closure Notification
# (Required by eBay to keep your keyset enabled)
# ------------------------------------------------------------------

@router.get("/ebay/deletion")
async def deletion_notification_get(request: Request):
    """Respond to eBay's account deletion verification (GET challenge)."""
    challenge_code = request.query_params.get("challenge_code", "")
    verification_token = settings.ebay_verification_token

    # Reconstruct the public endpoint URL as eBay sees it.
    # Use X-Forwarded-Proto/Host headers (set by nginx/ngrok) to get
    # the correct external scheme and host, not the internal ones.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    endpoint_url = f"{scheme}://{host}/auth/ebay/deletion"

    logger.info(
        "Deletion challenge: code=%s, token=%s..., endpoint=%s",
        challenge_code[:10], verification_token[:10], endpoint_url,
    )

    # eBay expects: SHA256(challengeCode + verificationToken + endpointURL)
    hash_input = f"{challenge_code}{verification_token}{endpoint_url}"
    response_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    return {"challengeResponse": response_hash}


@router.post("/ebay/deletion")
async def deletion_notification_post(request: Request):
    """Handle eBay account deletion notification (POST).

    eBay sends this when a user requests account closure.
    Since this is a single-seller app, we just acknowledge it.
    """
    try:
        body = await request.json()
        logger.info(
            "eBay account deletion notification received: %s",
            json.dumps(body)[:500],
        )
    except Exception:
        logger.info("eBay account deletion notification received (no body)")

    # Acknowledge receipt
    return {"status": "ok"}


# ------------------------------------------------------------------
# OAuth 2.0 Flow
# ------------------------------------------------------------------

@router.get("/ebay")
async def auth_ebay_page(request: Request, db: Session = Depends(get_db)):
    """Show eBay auth status page with authorize button."""
    token_status = None
    try:
        token = await get_valid_token(db)
        token_status = "valid"
    except RuntimeError as exc:
        token_status = str(exc)

    configured = bool(settings.ebay_app_id and settings.ebay_cert_id)

    return templates.TemplateResponse(
        "ebay_auth.html",
        {
            "request": request,
            "active_page": "settings",
            "configured": configured,
            "token_status": token_status,
            "app_id": settings.ebay_app_id[:20] + "..." if len(settings.ebay_app_id) > 20 else settings.ebay_app_id,
            "environment": settings.ebay_environment,
        },
    )


@router.get("/ebay/authorize")
async def authorize_ebay():
    """Redirect user to eBay OAuth consent page."""
    if not settings.ebay_app_id or not settings.ebay_cert_id:
        return RedirectResponse(url="/auth/ebay?error=no_credentials")

    auth_url = get_auth_url()
    return RedirectResponse(url=auth_url)


@router.get("/ebay/callback")
async def ebay_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """Handle eBay OAuth callback with authorization code."""
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        logger.error("eBay OAuth error: %s", error)
        return RedirectResponse(url=f"/auth/ebay?error={error}")

    if not code:
        return RedirectResponse(url="/auth/ebay?error=no_code")

    try:
        token_data = await exchange_code(code)
        save_tokens(db, token_data)
        logger.info("eBay tokens saved successfully")
        return RedirectResponse(url="/auth/ebay?success=1")
    except Exception as exc:
        logger.error("eBay token exchange failed: %s", exc)
        return RedirectResponse(url=f"/auth/ebay?error=exchange_failed")
