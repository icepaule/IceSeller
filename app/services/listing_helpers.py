"""Helper functions for eBay listing creation."""

from html import escape


def build_aspects(ai_specs: dict | None, ai_manufacturer: str = "", ai_model: str = "") -> dict:
    """Build eBay Item Specifics (aspects) from AI-extracted specs.

    Maps German spec keys from Ollama to eBay aspect names.
    Only includes non-empty values.
    """
    aspects: dict[str, list[str]] = {}

    if not ai_specs:
        ai_specs = {}

    # Direct mappings from Ollama specs to eBay aspects
    # Support both old ASCII keys (Kapazitaet) and new umlaut keys (Kapazität)
    mapping = {
        "Marke": "Marke",
        "Modell": "Modell",
        "MPN": "MPN",
        "Typ": "Produktart",
        "Kapazität": "Speicherkapazität",
        "Kapazitaet": "Speicherkapazität",
        "Geschwindigkeit": "Busgeschwindigkeit",
        "Formfaktor": "Formfaktor",
        "Schnittstelle": "Schnittstelle",
        "Anschlüsse": "Anschlüsse",
        "Anschluesse": "Anschlüsse",
    }

    for ollama_key, ebay_key in mapping.items():
        val = ai_specs.get(ollama_key, "")
        if val and str(val).strip():
            aspects[ebay_key] = [str(val).strip()]

    # Fallbacks from top-level AI fields
    if "Marke" not in aspects and ai_manufacturer:
        aspects["Marke"] = [ai_manufacturer]
    if "Modell" not in aspects and ai_model:
        aspects["Modell"] = [ai_model]

    return aspects


def generate_html_description(
    title: str,
    description: str,
    specs: dict | None = None,
    condition: str = "",
    what_is_included: str = "",
) -> str:
    """Generate a structured HTML description for eBay listings.

    Produces mobile-friendly HTML with sections for description,
    specs table, condition, and included items.
    """
    parts = []

    # CSS for clean, mobile-friendly layout
    parts.append(
        '<div style="font-family: Arial, Helvetica, sans-serif; '
        'max-width: 800px; margin: 0 auto; color: #333; line-height: 1.6;">'
    )

    # Product description
    parts.append('<h2 style="color: #0654ba; border-bottom: 2px solid #0654ba; '
                 'padding-bottom: 8px;">Produktbeschreibung</h2>')
    # Convert newlines to <br> for the description text
    desc_html = escape(description).replace("\n", "<br>")
    parts.append(f'<p>{desc_html}</p>')

    # Technical specs table
    if specs:
        non_empty = {k: v for k, v in specs.items() if v and str(v).strip()}
        if non_empty:
            parts.append(
                '<h2 style="color: #0654ba; border-bottom: 2px solid #0654ba; '
                'padding-bottom: 8px; margin-top: 24px;">Technische Daten</h2>'
            )
            parts.append(
                '<table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">'
            )
            for key, val in non_empty.items():
                parts.append(
                    f'<tr>'
                    f'<td style="padding: 6px 12px; border: 1px solid #ddd; '
                    f'background: #f5f5f5; font-weight: bold; width: 35%;">'
                    f'{escape(str(key))}</td>'
                    f'<td style="padding: 6px 12px; border: 1px solid #ddd;">'
                    f'{escape(str(val))}</td>'
                    f'</tr>'
                )
            parts.append('</table>')

    # What is included
    if what_is_included:
        parts.append(
            '<h2 style="color: #0654ba; border-bottom: 2px solid #0654ba; '
            'padding-bottom: 8px; margin-top: 24px;">Lieferumfang</h2>'
        )
        parts.append(f'<p>{escape(what_is_included)}</p>')

    # Condition note
    condition_labels = {
        "NEW": "Neu / Originalverpackt",
        "USED_EXCELLENT": "Gebraucht - Hervorragender Zustand",
        "USED_VERY_GOOD": "Gebraucht - Sehr guter Zustand",
        "USED_GOOD": "Gebraucht - Guter Zustand",
        "USED_ACCEPTABLE": "Gebraucht - Akzeptabler Zustand",
        "FOR_PARTS_OR_NOT_WORKING": "Für Teile / Defekt",
    }
    cond_text = condition_labels.get(condition, "")
    if cond_text:
        parts.append(
            '<h2 style="color: #0654ba; border-bottom: 2px solid #0654ba; '
            'padding-bottom: 8px; margin-top: 24px;">Zustand</h2>'
        )
        parts.append(f'<p>{escape(cond_text)}</p>')

    # Shipping & legal
    parts.append(
        '<h2 style="color: #0654ba; border-bottom: 2px solid #0654ba; '
        'padding-bottom: 8px; margin-top: 24px;">Versand &amp; Hinweise</h2>'
    )
    parts.append(
        '<ul style="padding-left: 20px;">'
        '<li>Versand mit DHL innerhalb Deutschlands</li>'
        '<li>30 Tage Rücknahme (Käufer zahlt Rücksendung)</li>'
        '</ul>'
    )
    parts.append(
        '<p style="color: #888; font-size: 0.9em; margin-top: 16px;">'
        'Irrtümer und Zwischenverkauf vorbehalten.</p>'
    )

    parts.append('</div>')

    return "\n".join(parts)
