"""Email notification service for IceSeller.

Sends HTML email notifications for key selling events (identification,
listing, sale, shipment) via async SMTP.  Logs each sent email to the
EmailLog table when a database session and item_id are provided.
"""

import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EmailLog

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# HTML helpers
# ------------------------------------------------------------------

_STYLE = """
<style>
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f6f8; margin: 0; padding: 0; }
  .container { max-width: 600px; margin: 20px auto; background: #ffffff; border-radius: 8px;
               box-shadow: 0 2px 8px rgba(0,0,0,0.08); overflow: hidden; }
  .header { background: linear-gradient(135deg, #1a73e8, #0d47a1); color: #ffffff;
            padding: 24px 32px; }
  .header h1 { margin: 0; font-size: 22px; letter-spacing: 0.5px; }
  .header .subtitle { margin: 4px 0 0; font-size: 13px; opacity: 0.85; }
  .body { padding: 24px 32px; color: #333333; line-height: 1.6; }
  .body h2 { margin: 0 0 16px; font-size: 18px; color: #1a73e8; }
  .detail-table { width: 100%; border-collapse: collapse; margin: 12px 0; }
  .detail-table td { padding: 8px 0; vertical-align: top; }
  .detail-table .label { color: #666666; width: 140px; font-weight: 600; }
  .detail-table .value { color: #222222; }
  .footer { padding: 16px 32px; background: #f9fafb; color: #999999;
            font-size: 12px; text-align: center; border-top: 1px solid #eeeeee; }
  a { color: #1a73e8; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
"""


def _wrap_html(title: str, body_html: str) -> str:
    """Wrap body content in the IceSeller email template."""
    return f"""\
<!DOCTYPE html>
<html lang="de">
<head><meta charset="utf-8">{_STYLE}</head>
<body>
  <div class="container">
    <div class="header">
      <h1>IceSeller</h1>
      <div class="subtitle">{title}</div>
    </div>
    <div class="body">
      {body_html}
    </div>
    <div class="footer">
      IceSeller &ndash; eBay Selling Assistant &bull; automatische Benachrichtigung
    </div>
  </div>
</body>
</html>"""


def _detail_row(label: str, value: str) -> str:
    """Build a single table row for the detail table."""
    return (
        f'<tr><td class="label">{label}</td>'
        f'<td class="value">{value}</td></tr>'
    )


# ------------------------------------------------------------------
# Core send function
# ------------------------------------------------------------------

async def send_notification(
    subject: str,
    html_body: str,
    item_id: int | None = None,
    event_type: str = "",
    db: Session | None = None,
) -> bool:
    """Send an HTML email notification via SMTP.

    Parameters
    ----------
    subject : str
        Email subject line.
    html_body : str
        Full HTML content of the email.
    item_id : int, optional
        Associated item ID for logging.
    event_type : str
        Event type string stored in EmailLog (e.g. "identified", "sold").
    db : Session, optional
        Database session -- if provided together with ``item_id``, a log
        entry is written to the ``email_log`` table.

    Returns
    -------
    bool
        True if the email was sent successfully, False otherwise.
    """
    if not settings.smtp_user or not settings.smtp_password:
        logger.warning(
            "SMTP credentials not configured -- skipping email notification "
            "(subject: %s)",
            subject,
        )
        return False

    recipient = settings.notify_email or settings.smtp_user

    msg = MIMEMultipart("alternative")
    msg["From"] = f"IceSeller <{settings.smtp_user}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            start_tls=True,
            username=settings.smtp_user,
            password=settings.smtp_password,
        )
        logger.info("Email sent: %s -> %s", subject, recipient)
    except Exception:
        logger.exception("Failed to send email: %s", subject)
        return False

    # Log to database
    if db is not None and item_id is not None:
        try:
            log_entry = EmailLog(
                item_id=item_id,
                event_type=event_type,
                subject=subject,
                sent_at=datetime.utcnow(),
            )
            db.add(log_entry)
            db.commit()
        except Exception:
            logger.exception("Failed to log email to database")
            db.rollback()

    return True


# ------------------------------------------------------------------
# Convenience notification methods
# ------------------------------------------------------------------

async def notify_identified(item, db: Session) -> bool:
    """Notify that an item has been identified by the AI.

    Parameters
    ----------
    item : Item
        The identified item model instance.
    db : Session
        Database session for email logging.

    Returns
    -------
    bool
        True if the email was sent successfully.
    """
    title = item.confirmed_title or f"{item.ai_manufacturer} {item.ai_model}".strip()
    subject = f"Artikel identifiziert: {title}"

    rows = ""
    if item.ai_manufacturer:
        rows += _detail_row("Hersteller", item.ai_manufacturer)
    if item.ai_model:
        rows += _detail_row("Modell", item.ai_model)
    if item.ai_category:
        rows += _detail_row("Kategorie", item.ai_category)
    if item.ai_condition:
        rows += _detail_row("Zustand", item.ai_condition)
    if item.ai_details:
        rows += _detail_row("Details", item.ai_details)

    body = f"""\
<h2>Artikel identifiziert</h2>
<p>Ein neuer Artikel wurde erfolgreich per KI erkannt:</p>
<table class="detail-table">
  {rows}
</table>
<p>Der Artikel kann jetzt in der <a href="/research/{item.id}">Preisrecherche</a>
   weiter bearbeitet werden.</p>"""

    html = _wrap_html("Neuer Artikel identifiziert", body)
    return await send_notification(
        subject=subject,
        html_body=html,
        item_id=item.id,
        event_type="identified",
        db=db,
    )


async def notify_listed(item, listing, db: Session) -> bool:
    """Notify that an item has been listed on eBay.

    Parameters
    ----------
    item : Item
        The item model instance.
    listing : Listing
        The listing model instance with eBay details.
    db : Session
        Database session for email logging.

    Returns
    -------
    bool
        True if the email was sent successfully.
    """
    title = item.confirmed_title or f"{item.ai_manufacturer} {item.ai_model}".strip()
    subject = f"Auf eBay eingestellt: {title}"

    price_str = ""
    if listing.start_price:
        price_str = f"{listing.start_price:.2f} EUR"
    if listing.buy_now_price:
        price_str = f"{listing.buy_now_price:.2f} EUR"

    ebay_url = ""
    if listing.ebay_listing_id:
        ebay_url = f"https://www.ebay.de/itm/{listing.ebay_listing_id}"

    rows = _detail_row("Titel", title)
    rows += _detail_row("Format", listing.format or "FIXED_PRICE")
    if price_str:
        rows += _detail_row("Preis", price_str)
    if listing.category_id:
        rows += _detail_row("Kategorie-ID", listing.category_id)
    if listing.ebay_listing_id:
        rows += _detail_row("eBay-ID", listing.ebay_listing_id)

    link_html = ""
    if ebay_url:
        link_html = f'<p><a href="{ebay_url}">Listing auf eBay ansehen</a></p>'

    body = f"""\
<h2>Listing erstellt</h2>
<p>Dein Artikel wurde erfolgreich auf eBay eingestellt:</p>
<table class="detail-table">
  {rows}
</table>
{link_html}"""

    html = _wrap_html("Neues eBay-Listing", body)
    return await send_notification(
        subject=subject,
        html_body=html,
        item_id=item.id,
        event_type="listed",
        db=db,
    )


async def notify_sold(item, order, db: Session) -> bool:
    """Notify that an item has been sold.

    Parameters
    ----------
    item : Item
        The item model instance.
    order : Order
        The order model instance with buyer and price details.
    db : Session
        Database session for email logging.

    Returns
    -------
    bool
        True if the email was sent successfully.
    """
    title = item.confirmed_title or f"{item.ai_manufacturer} {item.ai_model}".strip()
    buyer = order.buyer_name or order.buyer_username or "Unbekannt"
    subject = f"Artikel verkauft: {title} an {buyer}"

    rows = _detail_row("Artikel", title)
    rows += _detail_row("Käufer", buyer)
    if order.buyer_username:
        rows += _detail_row("eBay-User", order.buyer_username)
    if order.total_price is not None:
        rows += _detail_row("Gesamtpreis", f"{order.total_price:.2f} EUR")
    if order.shipping_cost is not None:
        rows += _detail_row("Versandkosten", f"{order.shipping_cost:.2f} EUR")
    if order.payment_status:
        rows += _detail_row("Zahlung", order.payment_status)
    if order.ebay_order_id:
        rows += _detail_row("Bestell-Nr.", order.ebay_order_id)

    # Build address snippet
    addr_html = ""
    if order.buyer_address and isinstance(order.buyer_address, dict):
        addr = order.buyer_address
        parts = [
            addr.get("name", ""),
            addr.get("street", ""),
            addr.get("street2", ""),
            f"{addr.get('postal_code', '')} {addr.get('city', '')}".strip(),
            addr.get("country", ""),
        ]
        addr_lines = "<br>".join(p for p in parts if p)
        addr_html = f'<p><strong>Lieferadresse:</strong><br>{addr_lines}</p>'

    body = f"""\
<h2>Verkauft!</h2>
<p>Herzlichen Glückwunsch, dein Artikel wurde verkauft:</p>
<table class="detail-table">
  {rows}
</table>
{addr_html}
<p>Bitte den Artikel zeitnah versenden. Zum Versand geht es
   <a href="/shipping/{order.id}">hier</a>.</p>"""

    html = _wrap_html("Artikel verkauft!", body)
    return await send_notification(
        subject=subject,
        html_body=html,
        item_id=item.id,
        event_type="sold",
        db=db,
    )


async def notify_shipped(item, order, db: Session) -> bool:
    """Notify that an item has been shipped.

    Parameters
    ----------
    item : Item
        The item model instance.
    order : Order
        The order model instance with tracking details.
    db : Session
        Database session for email logging.

    Returns
    -------
    bool
        True if the email was sent successfully.
    """
    title = item.confirmed_title or f"{item.ai_manufacturer} {item.ai_model}".strip()
    tracking = order.dhl_tracking or "nicht verfügbar"
    subject = f"Versendet: {title}, Tracking: {tracking}"

    buyer = order.buyer_name or order.buyer_username or "Unbekannt"

    rows = _detail_row("Artikel", title)
    rows += _detail_row("Käufer", buyer)
    rows += _detail_row("Sendungsnummer", tracking)
    if order.shipped_at:
        rows += _detail_row(
            "Versendet am",
            order.shipped_at.strftime("%d.%m.%Y %H:%M"),
        )
    if order.ebay_order_id:
        rows += _detail_row("Bestell-Nr.", order.ebay_order_id)

    tracking_link = ""
    if order.dhl_tracking:
        dhl_url = f"https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?piececode={order.dhl_tracking}"
        tracking_link = f'<p><a href="{dhl_url}">Sendung bei DHL verfolgen</a></p>'

    label_link = ""
    if order.dhl_label_url:
        label_link = f'<p><a href="{order.dhl_label_url}">Versandlabel herunterladen</a></p>'

    body = f"""\
<h2>Versendet!</h2>
<p>Dein Artikel wurde erfolgreich versendet:</p>
<table class="detail-table">
  {rows}
</table>
{tracking_link}
{label_link}"""

    html = _wrap_html("Artikel versendet", body)
    return await send_notification(
        subject=subject,
        html_body=html,
        item_id=item.id,
        event_type="shipped",
        db=db,
    )
