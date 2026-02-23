"""Ollama Vision API client for IT hardware identification."""

import base64
import json
import logging
import re
from pathlib import Path

import httpx

from app.config import settings
from app.services.part_decoder import decode_ram_part_number

logger = logging.getLogger(__name__)

# Vision models in preference order (qwen2.5vl has best OCR for labels)
VISION_MODELS = ["qwen2.5vl:7b", "minicpm-v:8b", "llava:13b", "llava:7b"]

# Text models for spec enrichment (preference order)
TEXT_MODELS = ["qwen2.5:14b", "mistral-nemo:12b", "qwen2.5:7b-instruct-q4_1", "llama3.1:8b"]

OCR_PROMPT = """\
Lies ALLE sichtbaren Texte auf diesem Bild Zeichen für Zeichen ab. \
Fokussiere dich auf Labels, Aufkleber, Beschriftungen und aufgedruckte Texte. \
Schreibe JEDE Zeile die du lesen kannst einzeln ab, EXAKT wie sie auf dem Label steht. \
Besonders wichtig: Part Numbers, Seriennummern, Kapazitäten (z.B. 4GB, 8GB), \
Geschwindigkeiten (z.B. PC4-25600, DDR4-3200), Hersteller-Namen. \
Achte auf Verwechslungsgefahr: 8/B, 1/l/I, 0/O, S/5, G/6, C/G. \
Gib NUR den abgelesenen Text zurück, KEINE Interpretation, KEIN JSON, KEINE Analyse. \
Wenn du etwas nicht sicher lesen kannst, schreibe [unleserlich].\
"""

# Text-model prompt for structuring OCR text into JSON (NO image needed)
STRUCTURE_FROM_OCR_PROMPT = """\
SPRACHE: Antworte IMMER und AUSSCHLIESSLICH auf DEUTSCH mit korrekten Umlauten (ä, ö, ü, ß).

Du bist ein IT-Hardware-Experte. Ein OCR-System hat folgenden Text von einem Produkt-Label abgelesen:

---
{ocr_text}
---

Erstelle basierend auf diesem Text ein JSON-Objekt. VERWENDE NUR die Informationen aus dem OCR-Text oben!
Erfinde KEINE Daten die nicht im Text stehen!

Bekannte Part-Number-Schemata (zur Herstelleridentifikation):
- 99xxxxx = Kingston (z.B. 9995417-F12.A000)
- HMT/HMA/HMCG = SK hynix (HMT=DDR3, HMA=DDR4, HMCG=DDR5)
- MT36HTF/MT16HTF/MT9HTF = Micron DDR2 FB-DIMM (Fully Buffered)
- MT36HTS/MT16HTS = Micron DDR2
- MTA = Micron DDR4, MTC = Micron DDR5
- M471/M378/M393 = Samsung
- KVR/KF = Kingston
- CT = Crucial
- ASU = ASUS OEM (Hersteller aus Part Number ableiten, z.B. 9995417=Kingston)

DDR-Generation aus Speed erkennen (WICHTIG!):
- 200/266/333/400 MHz = DDR1, "PC-1600/2100/2700/3200", "PC1"
- 400/533/667/800 MHz = DDR2, "PC2-3200/4200/5300/6400"
- 800/1066/1333/1600/1866 MHz = DDR3, "PC3-6400/8500/10600/12800/14900"
- 1600/2133/2400/2666/3200 MHz = DDR4, "PC4-17000/19200/21300/25600"
- 3200/4800/5600 MHz = DDR5, "PC5-25600/38400/44800"
REGEL: 667 MHz = DDR2! 1333 MHz = DDR3! 3200 MHz = DDR4! NIEMALS verwechseln!

Bei RAM: "PC2" = DDR2, "PC3" = DDR3, "PC3L" = DDR3L (1.35V), "PC4" = DDR4, "PC5" = DDR5.
Die Zahl nach PC2/PC3/PC4 ist der Durchsatz: PC2-5300 = DDR2-667, PC3-12800 = DDR3-1600, PC4-25600 = DDR4-3200.
"F" am Ende (z.B. PC2-5300F) = Fully Buffered DIMM (FB-DIMM, Server).
"S" am Ende (z.B. PC3L-12800S) = SO-DIMM. Ohne "S"/"F" = UDIMM/DIMM.

Antworte NUR mit einem JSON-Objekt (ohne Markdown-Codeblock):

{{
  "manufacturer": "Hersteller (aus Part Number oder Label-Text ableiten)",
  "model": "Haupt-Part-Number vom Label",
  "category": "Produktkategorie (RAM, SSD, Switch, etc.)",
  "condition": "gebraucht - hervorragend",
  "details": "Kurze Zusammenfassung auf Deutsch: Hersteller, Typ, Kapazität, Speed, Part Number",
  "specs": {{
    "Marke": "Hersteller",
    "Modell": "Modellbezeichnung",
    "MPN": "Part Number",
    "Typ": "z.B. DDR3L SO-DIMM",
    "Kapazität": "z.B. 4GB (EXAKT wie im OCR-Text!)",
    "Geschwindigkeit": "z.B. DDR3-1600 (PC3L-12800)",
    "Formfaktor": "z.B. SO-DIMM (204-Pin)"
  }},
  "suggested_title": "eBay-Titel, max 80 Zeichen, NUR Keywords: Hersteller MPN Typ Kapazität Speed Formfaktor",
  "suggested_description": "Ausführliche deutsche eBay-Beschreibung, mindestens 5 Sätze. Gebraucht in hervorragendem Zustand.",
  "quantity": {quantity},
  "what_is_included": "Lieferumfang mit Stückzahl"
}}

WICHTIG:
- Kapazität EXAKT aus dem OCR-Text übernehmen (z.B. wenn "4GB" im Text steht → 4GB, NICHT 2GB!)
- Part Number EXAKT übernehmen, NICHT verändern!
- NUR Fakten aus dem OCR-Text, NICHTS erfinden!
"""

_IDENTIFY_PROMPT_TEMPLATE = """\
SPRACHE: Antworte IMMER und AUSSCHLIESSLICH auf DEUTSCH mit korrekten Umlauten (ä, ö, ü, ß). NIEMALS auf Englisch antworten!

Du bist ein Experte für IT-Hardware-Identifikation.

%%OCR_CONTEXT%%

WICHTIGSTE REGELN:
1. Verwende die VORAB-OCR-Texte oben als PRIMÄRE Quelle für Part Numbers, Kapazitäten, Speeds und Hersteller! \
Errate NIEMALS Spezifikationen -- übernimm sie aus dem OCR-Text!
2. HERSTELLER: Der Herstellername aus dem OCR-Text ist korrekt. \
Verwechsle NIEMALS Hersteller! Wenn der OCR-Text "hynix" enthält, ist es NICHT Kingston!
3. Wenn der OCR-Text "[unleserlich]" enthält oder etwas fehlt, schreibe "nicht lesbar" statt zu raten.
4. ZÄHLE die Anzahl IDENTISCHER Komponenten im Bild! Wenn z.B. 2 gleiche RAM-Module, 3 gleiche SSDs \
oder 2 gleiche Netzteile sichtbar sind, gib die genaue Anzahl an. NUR identische Teile (gleiche Part Number) zusammenzählen!

Erstelle das JSON-Ergebnis basierend auf den OCR-Daten und dem Bild.

Antworte ausschließlich mit einem JSON-Objekt (ohne Markdown-Codeblock) mit folgenden Feldern:

{
  "manufacturer": "Hersteller laut OCR-Text/Label (z.B. Kingston, Cisco, HP, Dell, MikroTik, ...)",
  "model": "EXAKTE Modell-/Teilenummer aus dem OCR-Text (z.B. KVR13S9S8/4, Catalyst 2960-X, ...)",
  "category": "Kategorie (Switch, Router, Server, Laptop, Desktop, Firewall, Access Point, Storage, RAM, Kabel, Modul, Netzteil, SSD, HDD, Sonstiges)",
  "condition": "Zustand: IMMER 'gebraucht - hervorragend' (NIEMALS 'neu' schreiben!)",
  "details": "Kurze sachliche Zusammenfassung auf Deutsch: Hersteller, Typ, Kapazität, Geschwindigkeit, Part Number. NUR Fakten aus dem OCR-Text und Bild. Erfinde NICHTS!",
  "specs": {
    "Marke": "Hersteller",
    "Modell": "Modellbezeichnung",
    "MPN": "Manufacturer Part Number aus OCR-Text",
    "Typ": "z.B. DDR3 SO-DIMM, Managed Switch, Tower Server, ...",
    "Kapazität": "z.B. 4GB, 500GB, ... (nur wenn zutreffend)",
    "Geschwindigkeit": "z.B. DDR3-1333, 1GbE, ... (nur wenn zutreffend)",
    "Formfaktor": "z.B. SO-DIMM, 1U, SFF, 2.5 Zoll, ... (nur wenn zutreffend)",
    "Schnittstelle": "z.B. SATA III, PCIe, USB 3.0, ... (nur wenn zutreffend)",
    "Anschlüsse": "z.B. 24x RJ45, 4x SFP+, ... (nur wenn zutreffend)"
  },
  "suggested_title": "eBay-Artikeltitel, max 80 Zeichen, NUR Keywords. Format: Hersteller Modellnr Typ Kapazität Speed Formfaktor. VERBOTEN: Zustand, Adjektive, Verben, Sätze. Beispiele: 'Kingston KVR13S9S8/4 DDR3 4GB 1333MHz SO-DIMM PC3-10600', 'Cisco Catalyst WS-C2960X-24TS-L 24-Port Gigabit Managed Switch'",
  "suggested_description": "AUSFÜHRLICHE eBay-Artikelbeschreibung AUF DEUTSCH, mindestens 5 Sätze. Struktur: 1. Was wird verkauft. 2. Technische Details aus OCR-Text. 3. GEBRAUCHT in hervorragendem Zustand. 4. Lieferumfang. 5. Einsatzgebiet.",
  "quantity": "Anzahl IDENTISCHER Teile im Bild (Integer). 1 wenn nur ein Teil, 2 wenn zwei gleiche, etc.",
  "what_is_included": "Lieferumfang mit Stückzahl, z.B. '2x Kingston 4GB SO-DIMM, ohne OVP'"
}

HÄUFIGE FEHLER DIE DU VERMEIDEN MUSST:
- FALSCHER HERSTELLER: Der OCR-Text enthält den korrekten Hersteller! Nicht verwechseln!
- FALSCHE PART NUMBER: Die Part Number aus dem OCR-Text EXAKT übernehmen, NICHT verändern!
- ERFUNDENE DETAILS: Schreibe NICHTS was nicht im OCR-Text oder Bild zu sehen ist.
- FEHLENDE UMLAUTE: Schreibe "Stück" (NICHT "Stueck"), "Kapazität" (NICHT "Kapazitaet"), "für" (NICHT "fuer")!
- STÜCKZAHL: Bei mehreren identischen Teilen: Titel mit "2x" Prefix, Beschreibung mit Gesamtkapazität.

WICHTIG: ALLE Texte müssen auf DEUTSCH mit korrekten Umlauten (ä, ö, ü, ß) sein!
Antworte NUR mit dem JSON-Objekt, kein weiterer Text.\
"""

# Fallback prompt without OCR context (if OCR step fails)
_IDENTIFY_PROMPT_NO_OCR = """\
SPRACHE: Antworte IMMER und AUSSCHLIESSLICH auf DEUTSCH mit korrekten Umlauten (ä, ö, ü, ß). NIEMALS auf Englisch antworten!

Du bist ein Experte für IT-Hardware-Identifikation mit exzellenten OCR-Fähigkeiten.

WICHTIGSTE REGELN:
1. Lies ZUERST alle sichtbaren Labels, Aufkleber und Beschriftungen auf dem Produkt WORT FÜR WORT ab. \
Errate NIEMALS Spezifikationen -- lies sie vom Label ab!
2. HERSTELLER: Lies den Markennamen EXAKT vom Label ab. Verwechsle NIEMALS Hersteller!
3. Wenn du etwas NICHT lesen kannst, schreibe "nicht lesbar" statt zu raten.
4. ZÄHLE die Anzahl IDENTISCHER Komponenten im Bild!

Schritt 1: Transkribiere ALLE sichtbaren Texte auf Labels BUCHSTABE FÜR BUCHSTABE.
Schritt 2: Lies die Kapazität und Speed DIREKT vom Label ab.
Schritt 3: Erstelle das JSON-Ergebnis basierend NUR auf den abgelesenen Daten.

Antworte ausschließlich mit einem JSON-Objekt (ohne Markdown-Codeblock) mit folgenden Feldern:

{
  "manufacturer": "Hersteller laut Label",
  "model": "EXAKTE Modell-/Teilenummer vom Label",
  "category": "Kategorie (Switch, Router, Server, RAM, SSD, HDD, Netzteil, Sonstiges, ...)",
  "condition": "gebraucht - hervorragend",
  "details": "Kurze Zusammenfassung: Hersteller, Typ, Kapazität, Speed, Part Number",
  "specs": {
    "Marke": "Hersteller",
    "Modell": "Modellbezeichnung",
    "MPN": "Part Number vom Label",
    "Typ": "z.B. DDR3 SO-DIMM",
    "Kapazität": "z.B. 4GB",
    "Geschwindigkeit": "z.B. DDR3-1333",
    "Formfaktor": "z.B. SO-DIMM",
    "Schnittstelle": "z.B. SATA III",
    "Anschlüsse": "z.B. 24x RJ45"
  },
  "suggested_title": "eBay-Titel, max 80 Zeichen, NUR Keywords",
  "suggested_description": "Ausführliche deutsche eBay-Beschreibung, mindestens 5 Sätze",
  "quantity": "Anzahl identischer Teile (Integer)",
  "what_is_included": "Lieferumfang mit Stückzahl"
}

WICHTIG: ALLE Texte auf DEUTSCH mit Umlauten! NUR JSON, kein weiterer Text.\
"""


def _build_identify_prompt(ocr_text: str | None = None) -> str:
    """Build the identification prompt, injecting OCR context if available."""
    if not ocr_text or not ocr_text.strip():
        return _IDENTIFY_PROMPT_NO_OCR

    ocr_section = (
        "VORAB-OCR (maschinell abgelesen, VERTRAUENSWÜRDIG):\n"
        "---\n"
        f"{ocr_text.strip()}\n"
        "---\n"
        "Die obigen Texte wurden in einem separaten OCR-Schritt Zeichen für Zeichen vom Label abgelesen. "
        "Sie sind ZUVERLÄSSIGER als deine eigene Ablesung! "
        "Verwende sie als PRIMÄRE Quelle für Part Numbers, Kapazitäten, Speeds und Hersteller!"
    )
    return _IDENTIFY_PROMPT_TEMPLATE.replace("%%OCR_CONTEXT%%", ocr_section)


def _parse_json_response(text: str) -> dict:
    """Parse JSON from the Ollama response, handling markdown code blocks."""
    logger.info("Raw Ollama response (first 800 chars): %s", text[:800])
    text = text.strip()

    # Remove markdown code blocks if present
    md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1).strip()

    # Find JSON object in text
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        text = text[brace_start : brace_end + 1]

    # First attempt: parse as-is
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Second attempt: fix common issues (unescaped newlines in string values)
    try:
        fixed = _fix_json_string(text)
        return json.loads(fixed)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from Ollama response: %s", exc)
        logger.error("Cleaned text (first 1000 chars): %s", text[:1000])
        return {
            "manufacturer": "",
            "model": "",
            "category": "",
            "condition": "",
            "details": "",
            "suggested_title": "",
            "suggested_description": "",
            "_raw_response": text[:2000],
            "_parse_error": str(exc),
        }


def _fix_json_string(text: str) -> str:
    """Fix common JSON issues from LLM output: unescaped newlines, trailing commas."""
    # Replace literal newlines inside string values with \\n
    # Strategy: process character by character, track if we're inside a string
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch == '\n':
            result.append('\\n')
            continue
        if in_string and ch == '\r':
            continue
        if in_string and ch == '\t':
            result.append('\\t')
            continue
        result.append(ch)
    fixed = ''.join(result)
    # Remove trailing commas before } or ]
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    return fixed


async def _get_available_models() -> list[str]:
    """Fetch list of installed model names from Ollama."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(f"{settings.ollama_host}/api/tags")
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
    except Exception as exc:
        logger.warning("Could not fetch Ollama model list: %s", exc)
        return []


async def _pick_vision_model() -> str:
    """Pick the best available vision model.

    If OLLAMA_MODEL is set and installed, use it.
    Otherwise auto-detect from VISION_MODELS preference list.
    """
    available = await _get_available_models()
    logger.info("Ollama models available: %s", available)

    # User-configured model takes priority
    configured = settings.ollama_model
    if configured:
        # Check exact match or prefix match (e.g. "minicpm-v" matches "minicpm-v:8b")
        for name in available:
            if name == configured or name.startswith(configured.split(":")[0]):
                logger.info("Using configured model: %s", name)
                return name

    # Auto-detect best vision model
    for preferred in VISION_MODELS:
        base = preferred.split(":")[0]
        for name in available:
            if name == preferred or name.startswith(base):
                logger.info("Auto-detected vision model: %s", name)
                return name

    raise RuntimeError(
        f"Kein Vision-Modell gefunden auf {settings.ollama_host}. "
        f"Bitte installieren: ollama pull minicpm-v:8b\n"
        f"Verfuegbare Modelle: {', '.join(available) or 'keine'}"
    )


async def _pick_text_model() -> str | None:
    """Pick the best available text model for spec enrichment.

    Excludes vision models (containing 'vl' or 'llava' or 'minicpm-v').
    """
    available = await _get_available_models()
    # Filter out vision models
    vision_keywords = ("vl:", "vl:", "llava", "minicpm-v")
    text_only = [m for m in available if not any(vk in m for vk in vision_keywords)]
    for preferred in TEXT_MODELS:
        # Exact match first, then prefix match
        if preferred in text_only:
            logger.info("Picked text model for enrichment: %s", preferred)
            return preferred
    # Fallback: prefix match
    for preferred in TEXT_MODELS:
        base = preferred.split(":")[0]
        for name in text_only:
            if name.startswith(base + ":"):
                logger.info("Picked text model for enrichment (prefix): %s", name)
                return name
    return None


ENRICH_PROMPT_TEMPLATE = """\
Du bist ein IT-Hardware-Experte. Ein Vision-Modell hat folgende Daten von einem Produkt-Label gelesen:

Hersteller (Vision): {manufacturer}
Modell/Part Number (Vision): {model}
MPN (Vision): {mpn}
Kategorie: {category}
Kapazität (Vision): {capacity}
Geschwindigkeit (Vision): {speed}

WICHTIGSTE REGEL: Die Part Number / MPN ist der ZUVERLÄSSIGSTE Wert aus der Bilderkennung! \
Kapazität, Geschwindigkeit und Typ können vom Vision-Modell FALSCH erkannt sein. \
PRÜFE die Specs anhand der Part Number und KORRIGIERE sie wenn nötig!

Bekannte Part-Number-Schemata:
- SK hynix: HMT=DDR3, HMA=DDR4, HMCG=DDR5. Position 4=Density(8=8Gbit→8GB@1Rx8), Pos5=Ranks(1=1R,2=2R), Pos6=Form(G=SODIMM,U=UDIMM,R=RDIMM). Suffix: XN=3200,JJ/VK=2666,DY/AF=2400,TF=2133,PB=1600,MR=1333
- Samsung: M471=DDR4 SODIMM, M378=DDR4 UDIMM, M393=DDR4 RDIMM. Suffix: CWE=3200,CTD=2666,CRC/CRF=2400
- Kingston: KVR/KF + Speed(32=3200,26=2666,24=2400,16=1600) + Form(S=SODIMM,N=UDIMM) + /Kapazität
- Micron: MTA=DDR4, MTC=DDR5. HZ=SODIMM,AZ=UDIMM,PZ=RDIMM. Suffix: 3G2=3200,2G6=2666
- Crucial: CT+Kapazität+G+Gen(4=DDR4)+Form(S=SODIMM,D=UDIMM)+Speed(832=3200)

Aufgabe:
1. DEKODIERE die Part Number und bestimme die KORREKTEN Specs (DDR-Generation, Kapazität, Speed, Formfaktor).
2. Wenn Part-Number-Specs von den Vision-Specs abweichen, VERWENDE die Part-Number-Specs!
3. ERGÄNZE fehlende Details (Voltage, CAS Latency, Pin-Anzahl).
4. Erstelle einen guten eBay-Titel und eine ausführliche deutsche Beschreibung.
5. Wenn du die Part Number GAR NICHT kennst, antworte mit: UNKNOWN

WICHTIG: Antworte AUF DEUTSCH mit Umlauten (ä, ö, ü, ß). NUR Fakten, NICHTS erfinden!

Antworte NUR mit einem JSON-Objekt (ohne Markdown-Codeblock):
{{
  "manufacturer": "Hersteller",
  "model": "{mpn}",
  "category": "{category}",
  "condition": "gebraucht - hervorragend",
  "details": "Kurze sachliche Zusammenfassung: Hersteller, Typ, Kapazität, Speed, Part Number",
  "specs": {{
    "Marke": "Hersteller",
    "Modell": "Modellbezeichnung",
    "MPN": "{mpn}",
    "Typ": "z.B. DDR4 SO-DIMM",
    "Kapazität": "z.B. 8GB (aus Part Number dekodiert)",
    "Geschwindigkeit": "z.B. DDR4-3200 (aus Part Number dekodiert)",
    "Formfaktor": "z.B. SO-DIMM (260-Pin)"
  }},
  "suggested_title": "NUR Keywords, max 80 Zeichen, z.B. 'SK hynix HMA81GS6CJR8N-XN DDR4 8GB 3200MHz SO-DIMM PC4-25600'. KEIN Prefix wie 'eBay-Titel:' davor!",
  "suggested_description": "Ausführliche deutsche eBay-Beschreibung, mindestens 5 Sätze. Gebraucht in hervorragendem Zustand.",
  "what_is_included": "Lieferumfang, z.B. '1x SK hynix 8GB DDR4 SO-DIMM, ohne OVP'"
}}
"""

ENRICH_DECODED_PROMPT_TEMPLATE = """\
Du bist ein IT-Hardware-Experte. Die folgenden Specs wurden DETERMINISTISCH aus der Part Number dekodiert und sind KORREKT:

Hersteller: {manufacturer}
Part Number: {mpn}
Kategorie: {category}
Typ: {type}
Kapazität (pro Modul): {capacity}
Geschwindigkeit: {speed}
Formfaktor: {form_factor}
Spannung: {voltage}
Stückzahl: {quantity}

Diese Werte sind 100% korrekt und dürfen NICHT geändert werden!

Aufgabe:
1. BEHALTE alle oben genannten Specs EXAKT bei.
2. Erstelle einen eBay-Titel (max 80 Zeichen, NUR Keywords). {qty_title_hint}
3. Erstelle eine ausführliche deutsche eBay-Beschreibung (mindestens 5 Sätze). {qty_desc_hint}
4. ERGÄNZE nur CAS Latency oder Pin-Anzahl falls du sie sicher weißt.

WICHTIG: Antworte AUF DEUTSCH mit Umlauten (ä, ö, ü, ß). NUR Fakten, NICHTS erfinden!

Antworte NUR mit einem JSON-Objekt (ohne Markdown-Codeblock):
{{
  "manufacturer": "{manufacturer}",
  "model": "{mpn}",
  "category": "{category}",
  "condition": "gebraucht - hervorragend",
  "details": "Kurze sachliche Zusammenfassung",
  "specs": {{
    "Marke": "{manufacturer}",
    "Modell": "{mpn}",
    "MPN": "{mpn}",
    "Typ": "{type}",
    "Kapazität": "{capacity}",
    "Geschwindigkeit": "{speed}",
    "Formfaktor": "{form_factor}",
    "Spannung": "{voltage}"
  }},
  "suggested_title": "NUR Keywords, max 80 Zeichen",
  "suggested_description": "Ausführliche deutsche eBay-Beschreibung, mindestens 5 Sätze. Gebraucht in hervorragendem Zustand.",
  "what_is_included": "Lieferumfang mit Stückzahl"
}}
"""


def _apply_decoded_specs(vision_result: dict, decoded: dict) -> dict:
    """Override vision-detected specs with deterministically decoded values."""
    result = dict(vision_result)
    specs = dict(result.get("specs", {})) if isinstance(result.get("specs"), dict) else {}
    quantity = _get_quantity(result)

    # Override manufacturer
    if decoded.get("manufacturer"):
        result["manufacturer"] = decoded["manufacturer"]
        specs["Marke"] = decoded["manufacturer"]

    # Override specs fields
    if decoded.get("type"):
        specs["Typ"] = decoded["type"]
    if decoded.get("capacity"):
        specs["Kapazität"] = decoded["capacity"]
    if decoded.get("speed"):
        specs["Geschwindigkeit"] = decoded["speed"]
    if decoded.get("form_factor"):
        specs["Formfaktor"] = decoded["form_factor"]
    if decoded.get("voltage"):
        specs["Spannung"] = decoded["voltage"]
    if decoded.get("ranks"):
        specs["Konfiguration"] = decoded["ranks"]

    result["specs"] = specs

    # Regenerate suggested_title with correct values
    mpn = specs.get("MPN", result.get("model", ""))
    qty_prefix = f"{quantity}x " if quantity > 1 else ""
    title_parts = [
        qty_prefix + decoded.get("manufacturer", ""),
        mpn,
        decoded.get("type", ""),
        decoded.get("capacity", ""),
    ]
    # Extract MHz from speed (e.g. "DDR4-3200 (PC4-25600)" → "3200MHz")
    speed = decoded.get("speed", "")
    if speed:
        mhz_match = re.search(r"-(\d+)", speed)
        if mhz_match:
            title_parts.append(f"{mhz_match.group(1)}MHz")
        pc_match = re.search(r"\((PC\S+)\)", speed)
        if pc_match:
            title_parts.append(pc_match.group(1))
    # Add total capacity for multi-module sets
    if quantity > 1 and decoded.get("capacity"):
        cap_match = re.match(r"(\d+)", decoded["capacity"])
        if cap_match:
            total_gb = int(cap_match.group(1)) * quantity
            title_parts.append(f"({total_gb}GB gesamt)")
    result["suggested_title"] = " ".join(p for p in title_parts if p)[:80]

    result["_decoded"] = True
    return result


def _get_quantity(result: dict) -> int:
    """Extract detected quantity from vision result."""
    qty = result.get("quantity", 1)
    if isinstance(qty, str):
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 1
    return max(1, qty) if isinstance(qty, int) else 1


async def _enrich_with_text_model(vision_result: dict) -> dict:
    """Use a text model to verify/correct specs based on the detected part number.

    Two modes:
    - Decoded mode (_decoded=True): Specs are authoritative, only generate title/description.
    - Standard mode: Ask text model to VERIFY and CORRECT specs using part number knowledge.
    """
    model = await _pick_text_model()
    if not model:
        logger.info("No text model available for enrichment, skipping")
        return vision_result

    manufacturer = vision_result.get("manufacturer", "")
    model_name = vision_result.get("model", "")
    specs = vision_result.get("specs", {})
    if not isinstance(specs, dict):
        specs = {}
    mpn = specs.get("MPN", "")
    category = vision_result.get("category", "")
    is_decoded = vision_result.get("_decoded", False)
    quantity = _get_quantity(vision_result)

    # Only enrich if we have something to look up
    if not model_name and not mpn:
        return vision_result

    # Build quantity hints for enrichment prompts
    qty_title_hint = ""
    qty_desc_hint = ""
    if quantity > 1:
        cap = specs.get("Kapazität", "")
        cap_match = re.match(r"(\d+)", cap) if cap else None
        total_gb = int(cap_match.group(1)) * quantity if cap_match else None
        qty_title_hint = f"Titel MUSS mit '{quantity}x' beginnen!"
        if total_gb:
            qty_desc_hint = (
                f"Es sind {quantity} identische Module enthalten (je {cap}). "
                f"Erwähne die Gesamtkapazität von {total_gb}GB in der Beschreibung!"
            )
        else:
            qty_desc_hint = f"Es sind {quantity} identische Teile enthalten. Erwähne die Stückzahl in der Beschreibung!"

    # Choose prompt based on whether decoder already ran
    if is_decoded:
        prompt = ENRICH_DECODED_PROMPT_TEMPLATE.format(
            manufacturer=manufacturer,
            mpn=mpn or model_name,
            category=category,
            type=specs.get("Typ", ""),
            capacity=specs.get("Kapazität", ""),
            speed=specs.get("Geschwindigkeit", ""),
            form_factor=specs.get("Formfaktor", ""),
            voltage=specs.get("Spannung", ""),
            quantity=quantity,
            qty_title_hint=qty_title_hint,
            qty_desc_hint=qty_desc_hint,
        )
        logger.info("Enriching (decoded mode, qty=%d) with %s for: %s / %s", quantity, model, manufacturer, mpn or model_name)
    else:
        capacity = specs.get("Kapazität", specs.get("Kapazitaet", ""))
        speed = specs.get("Geschwindigkeit", "")
        prompt = ENRICH_PROMPT_TEMPLATE.format(
            manufacturer=manufacturer,
            model=model_name,
            mpn=mpn or model_name,
            category=category,
            capacity=capacity or "nicht lesbar",
            speed=speed or "nicht lesbar",
        )
        logger.info("Enriching (verify mode, qty=%d) with %s for: %s / %s", quantity, model, manufacturer, mpn or model_name)

    try:
        url = f"{settings.ollama_host}/api/chat"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        raw_text = resp.json().get("message", {}).get("content", "")
    except Exception as exc:
        logger.warning("Text model enrichment failed: %r", exc, exc_info=True)
        return vision_result

    if not raw_text or "UNKNOWN" in raw_text.upper()[:50]:
        logger.info("Text model does not know this part number, keeping vision result")
        return vision_result

    enriched = _parse_json_response(raw_text)
    if enriched.get("_parse_error"):
        logger.warning("Could not parse enrichment response, keeping vision result")
        return vision_result

    # Merge enriched data into vision result
    merged = dict(vision_result)
    for key in ("manufacturer", "model", "category", "condition", "details",
                "suggested_title", "suggested_description", "what_is_included"):
        val = enriched.get(key, "")
        if val and str(val).strip():
            merged[key] = val
    # Merge specs
    enriched_specs = enriched.get("specs", {})
    if isinstance(enriched_specs, dict) and enriched_specs:
        merged_specs = dict(specs) if isinstance(specs, dict) else {}
        for k, v in enriched_specs.items():
            if v and str(v).strip():
                merged_specs[k] = v
        merged["specs"] = merged_specs

    # PROTECT decoded values: if decoder ran, restore authoritative specs
    if is_decoded:
        protected_keys = ("Typ", "Kapazität", "Geschwindigkeit", "Formfaktor",
                          "Spannung", "Konfiguration", "Marke")
        merged_specs = merged.get("specs", {})
        for key in protected_keys:
            if key in specs and specs[key]:
                merged_specs[key] = specs[key]
        merged["specs"] = merged_specs
        merged["manufacturer"] = vision_result["manufacturer"]
        logger.info("Protected decoded specs from enrichment override")

    merged["_enriched_by"] = model
    logger.info("Enriched result: %s %s (%s)",
                merged.get("manufacturer", "?"),
                merged.get("model", "?"),
                merged.get("specs", {}).get("Kapazität", "?"))
    return merged


async def _structure_with_text_model(ocr_text: str, quantity: int = 1) -> dict | None:
    """Use the text model to structure OCR text into JSON (no image needed).

    This avoids hallucination because the text model only sees the OCR text,
    not the image directly. It cannot "re-read" and hallucinate different values.
    """
    text_model = await _pick_text_model()
    if not text_model:
        logger.warning("No text model available for OCR structuring")
        return None

    prompt = STRUCTURE_FROM_OCR_PROMPT.format(
        ocr_text=ocr_text.strip(),
        quantity=quantity,
    )

    logger.info("Structuring OCR text with %s (qty=%d)", text_model, quantity)
    try:
        url = f"{settings.ollama_host}/api/chat"
        payload = {
            "model": text_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        raw_text = resp.json().get("message", {}).get("content", "")
    except Exception as exc:
        logger.warning("Text model structuring failed: %s", exc)
        return None

    if not raw_text:
        return None

    result = _parse_json_response(raw_text)
    if result.get("_parse_error"):
        logger.warning("Could not parse text model structuring response")
        return None

    result["_structured_by"] = text_model
    logger.info(
        "Text model structured: %s %s (%s)",
        result.get("manufacturer", "?"),
        result.get("model", "?"),
        result.get("specs", {}).get("Kapazität", "?"),
    )
    return result


async def identify_product(image_paths: list[str]) -> dict:
    """Identify a product from one or more images using Ollama vision.

    Pipeline to prevent hallucination:
    Step 0: Pure OCR via vision model (simple prompt, just read text).
    Step 1: If OCR succeeded → text model structures the data (no image, no hallucination).
            If OCR failed → vision model does direct JSON identification (fallback).
    Step 1.5: Deterministic part number decode.
    Step 2: Text model enriches/corrects specs (only if not already structured by text model).
    """
    images_b64: list[str] = []
    for img_path in image_paths:
        full_path = Path(settings.images_dir) / img_path
        if not full_path.exists():
            logger.warning("Image not found: %s", full_path)
            continue
        raw = full_path.read_bytes()
        images_b64.append(base64.b64encode(raw).decode("utf-8"))

    if not images_b64:
        raise FileNotFoundError(
            f"Keine Bilder gefunden: {image_paths}"
        )

    vision_model = await _pick_vision_model()

    # Step 0: Pure OCR pass (vision model reads text, no JSON)
    ocr_text = await _ocr_labels(vision_model, images_b64)

    if ocr_text and len(ocr_text.strip()) > 10:
        # Step 1a: OCR succeeded → text model structures (NO image = NO hallucination)
        # Default quantity=1 (text model can't count items from OCR alone)
        result = await _structure_with_text_model(ocr_text, 1)
        if result:
            result["_model_used"] = vision_model
            result["_ocr_text"] = ocr_text
            result["_pipeline"] = "ocr+text"
        else:
            # Text model structuring failed, fall back to vision
            logger.warning("Text model structuring failed, falling back to vision model")
            result = None

    if not ocr_text or len(ocr_text.strip()) <= 10 or result is None:
        # Step 1b: Fallback → vision model does direct JSON identification
        identify_prompt = _build_identify_prompt(ocr_text)
        raw_text = await _try_chat_api(vision_model, images_b64, identify_prompt)
        if raw_text is None:
            raw_text = await _try_generate_api(vision_model, images_b64, identify_prompt)
        result = _parse_json_response(raw_text)
        result["_model_used"] = vision_model
        result["_ocr_text"] = ocr_text or ""
        result["_pipeline"] = "vision-fallback"

    quantity = _get_quantity(result)
    logger.info(
        "Identified: %s %s (%s) qty=%d [pipeline=%s, ocr=%s]",
        result.get("manufacturer", "?"),
        result.get("model", "?"),
        result.get("category", "?"),
        quantity,
        result.get("_pipeline", "?"),
        "yes" if ocr_text else "no",
    )

    # Step 1.5: Deterministic part number decode
    mpn = result.get("model", "") or (result.get("specs") or {}).get("MPN", "")
    decoded = decode_ram_part_number(mpn)
    if not decoded and ocr_text:
        decoded, mpn = _find_decodable_mpn(ocr_text)
        if decoded:
            result["model"] = mpn
            if isinstance(result.get("specs"), dict):
                result["specs"]["MPN"] = mpn
    if decoded:
        result = _apply_decoded_specs(result, decoded)
        logger.info("Part decoder override: %s → %s (qty=%d)", mpn, decoded.get("capacity"), quantity)

    # Step 2: Enrich with text model (skip if already structured by text model)
    if result.get("_pipeline") != "ocr+text":
        result = await _enrich_with_text_model(result)

    # Ensure quantity is preserved in final result
    result["quantity"] = quantity
    return result


def _find_decodable_mpn(ocr_text: str) -> tuple[dict | None, str]:
    """Scan OCR text for part numbers the decoder can handle.

    Returns (decoded_dict, mpn_string) or (None, "") if nothing found.
    """
    # Split OCR text into words/tokens and try each
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9/_-]{5,}", ocr_text)
    for token in tokens:
        decoded = decode_ram_part_number(token)
        if decoded:
            logger.info("Found decodable MPN in OCR text: %s", token)
            return decoded, token
    return None, ""


async def _ocr_labels(model: str, images_b64: list[str]) -> str | None:
    """Step 0: Pure OCR pass -- read all text from labels without JSON structuring.

    Uses a simple prompt that avoids hallucination by not asking for structured output.
    Returns the raw OCR text, or None if the call fails.
    """
    url = f"{settings.ollama_host}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": OCR_PROMPT,
                "images": images_b64,
            }
        ],
        "stream": False,
    }

    logger.info("OCR step: reading labels with %s (%d images)", model, len(images_b64))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        data = resp.json()
        ocr_text = data.get("message", {}).get("content", "")
        if ocr_text and ocr_text.strip():
            logger.info("OCR result (first 500 chars): %s", ocr_text.strip()[:500])
            return ocr_text.strip()
        logger.warning("OCR step returned empty text")
        return None
    except Exception as exc:
        logger.warning("OCR step failed (%s): %s", type(exc).__name__, exc)
        return None


async def _try_chat_api(model: str, images_b64: list[str], prompt: str) -> str | None:
    """Try the /api/chat endpoint (required by most vision models)."""
    url = f"{settings.ollama_host}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": images_b64,
            }
        ],
        "stream": False,
    }

    logger.info("Trying /api/chat with model=%s (%d images)", model, len(images_b64))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        data = resp.json()
        raw_text = data.get("message", {}).get("content", "")
        if raw_text:
            logger.debug("Chat API response: %s", raw_text[:300])
            return raw_text
        logger.warning("Chat API returned empty content, trying generate API")
        return None
    except httpx.HTTPStatusError as exc:
        logger.info("Chat API returned %s, trying generate API", exc.response.status_code)
        return None
    except Exception as exc:
        logger.info("Chat API failed (%s), trying generate API", exc)
        return None


async def _try_generate_api(model: str, images_b64: list[str], prompt: str) -> str:
    """Use the /api/generate endpoint (legacy, works with llava)."""
    url = f"{settings.ollama_host}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "images": images_b64,
        "stream": False,
    }

    logger.info("Using /api/generate with model=%s (%d images)", model, len(images_b64))
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()

    data = resp.json()
    raw_text = data.get("response", "")
    logger.debug("Generate API response: %s", raw_text[:300])
    return raw_text
