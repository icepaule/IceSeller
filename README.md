# IceSeller - eBay Selling Assistant

Lokaler, KI-gestuetzter Verkaufsassistent fuer eBay. Erfasst Produkte per USB-Kamera, identifiziert sie automatisch mit Ollama Vision und erstellt eBay-Listings mit Preisrecherche, Versandetiketten und Bestellverwaltung.

## Features

### Produkterfassung & Kamera
- **Live-Kamerastream** (MJPEG) mit Ausschnitt-Auswahl (Crop)
- **PTZ-Steuerung** (Pan/Tilt/Zoom/Fokus) fuer USB-Kameras mit V4L2
- Unterstuetzung fuer verschiedene Kameras (LifeCam, AKASO, etc.)
- Auto-Detection von Codec/Aufloesung (MJPEG 1080p oder YUYV 720p)
- Mindest-Aufloesung fuer Crops (800px) zur Sicherstellung der OCR-Qualitaet
- Bildverbesserung: Denoise, Schaerfen, CLAHE-Kontrastverstärkung

### KI-Identifikation (Ollama Vision)
- **2-Step OCR Pipeline** zur Vermeidung von Halluzinationen:
  1. **OCR-Schritt**: Vision-Modell liest Label-Texte zeichengenau ab (einfacher Prompt)
  2. **Strukturierung**: Text-Modell (kein Bild!) erstellt JSON aus dem OCR-Text
- **Deterministischer RAM Part-Number-Decoder** fuer 5 Hersteller:
  - SK hynix (HMT/HMA/HMCG), Samsung (M471/M378/M393), Kingston (KVR/KF/99xxxxx),
    Micron (MTA/MTC/MT36HTF), Crucial (CT)
  - Dekodiert: Kapazitaet, DDR-Generation, Speed, Formfaktor, Spannung aus der Teilenummer
- **Mengen-Erkennung**: Identische Komponenten im Bild werden gezaehlt (z.B. 2x 8GB RAM)
- **Enrichment**: Text-Modell ergaenzt/korrigiert Specs anhand der Part Number
- Fallback-Pipeline wenn OCR fehlschlaegt (direkte Vision-Identifikation)

### eBay-Integration
- **OAuth 2.0** Authentifizierung (Sandbox + Production)
- **Listing-Erstellung**: Auktion oder Festpreis (Sofort-Kaufen)
- **Geplante Veroeffentlichung** mit optimaler Endzeit-Berechnung
- **Bildupload** zu eBay EPS (UploadSiteHostedPictures)
- **Kategorie-Suche** mit eBay Taxonomy API
- **Item Specifics** automatisch aus KI-Specs befuellt
- **Preisvorschlag** mit Auto-Accept/Decline Schwellen
- Auktionen: Quantity automatisch auf 1 (eBay-Pflicht)
- Zusaetzliche Versandkosten fuer Mehrfachartikel

### Preisrecherche
- eBay Browse API (aktive Angebote)
- Playwright-basiertes Scraping (verkaufte Artikel)
- Automatische Preisempfehlung (Median + Perzentile)

### Versand (DHL)
- DHL Parcel DE Shipping API v2
- Paketschein-Erstellung mit Tracking
- Versandetiketten, Lieferscheine, Rechnungen (HTML/PDF)
- Automatische Versandkosten-Berechnung nach Gewicht/Groesse

### Dashboard & Verwaltung
- Artikeluebersicht mit Status-Workflow (Entwurf > Identifiziert > Recherchiert > Geplant > Eingestellt > Verkauft > Versendet)
- **Interne Nummern** (Post-it System): Auto-generiert (YYYYMMDDnn), inline-editierbar
- Stueckzahl- und Preis-Anzeige (auch fuer geplante Listings)
- Bestellverwaltung mit eBay-Synchronisation
- E-Mail-Benachrichtigungen (SMTP)

## Architektur

```
Browser  -->  nginx (:8083)  -->  FastAPI (:8080)  -->  Ollama API (Vision + Text)
                                       |
                                  SQLite DB
                                       |
                                  eBay REST APIs
                                  DHL Shipping API
                                  USB Camera (V4L2)
```

## Tech Stack

- **Backend**: Python 3.12, FastAPI, SQLAlchemy, Pydantic
- **Frontend**: Vanilla JS, Bootstrap 5 Dark Theme, Jinja2 Templates
- **KI**: Ollama (qwen2.5vl:7b Vision, qwen2.5:14b Text)
- **APIs**: eBay REST (Browse, Inventory, Fulfillment, Trading, Taxonomy), DHL Parcel DE v2
- **Kamera**: OpenCV (USB), gphoto2 (DSLR)
- **Scraping**: Playwright (headless Chromium)
- **Deployment**: Docker Compose + nginx Reverse Proxy

## Setup

### Voraussetzungen
- Docker + Docker Compose
- Ollama Server mit Vision-Modell (z.B. `ollama pull qwen2.5vl:7b && ollama pull qwen2.5:14b`)
- USB-Kamera (optional, fuer Produkterfassung)

### Installation

```bash
git clone https://github.com/icepaule/IceSeller.git
cd IceSeller

# Konfiguration
cp .env.example .env
# .env anpassen (eBay API Keys, Ollama Host, DHL, SMTP, etc.)

# Starten
docker compose up -d
```

Die App ist dann erreichbar unter `http://localhost:8083` (via nginx) oder `http://localhost:8082` (direkt).

### Konfiguration (.env)

| Variable | Beschreibung |
|----------|-------------|
| `EBAY_APP_ID` / `CERT_ID` / `DEV_ID` | eBay Developer Credentials |
| `EBAY_ENVIRONMENT` | `SANDBOX` oder `PRODUCTION` |
| `OLLAMA_HOST` | URL des Ollama Servers |
| `OLLAMA_MODEL` | Vision-Modell (leer = Auto-Erkennung) |
| `DHL_API_KEY` / `API_SECRET` | DHL Geschaeftskunden API |
| `CAMERA_DEVICE` | USB-Kamera Device (z.B. `/dev/video0`) |
| `CAMERA_TYPE` | `usb` oder `gphoto2` |

## Projektstruktur

```
app/
  main.py                    # FastAPI App mit Lifespan
  config.py                  # Pydantic BaseSettings
  models.py                  # SQLAlchemy Models (Item, Listing, Order, ...)
  database.py                # DB Engine, Session, Migrations
  routers/
    camera.py                # Kamera-Erfassung, Stream, PTZ
    dashboard.py             # Dashboard, Artikelverwaltung
    identify.py              # KI-Identifikation
    research.py              # Preisrecherche
    listing.py               # eBay Listing-Erstellung
    orders.py                # Bestellverwaltung
    shipping.py              # DHL Versand
  services/
    ollama_vision.py          # 2-Step OCR Pipeline + Enrichment
    part_decoder.py           # Deterministischer RAM Part-Number-Decoder
    camera_service.py         # USB-Kamera (OpenCV, V4L2)
    ebay_api.py               # eBay REST API Client
    ebay_auth.py              # OAuth 2.0 Flow
    ebay_scraper.py           # Playwright eBay Scraper
    dhl_api.py                # DHL Shipping API
    price_calculator.py       # Versandkosten-Rechner
    email_service.py          # SMTP Benachrichtigungen
    scheduler.py              # APScheduler (geplante Listings)
  templates/                  # Jinja2 Templates (Bootstrap 5 Dark)
  static/                     # CSS, JS
```

## KI-Pipeline im Detail

```
Bild aufnehmen
      |
      v
[OCR-Schritt] Vision-Modell liest alle Texte vom Label
      |         (einfacher Prompt, keine JSON-Struktur)
      v
[Strukturierung] Text-Modell erstellt JSON aus OCR-Text
      |            (kein Bild = keine Halluzination)
      v
[Part-Number-Decoder] Deterministisch: MPN -> Specs
      |                 (SK hynix, Samsung, Kingston, Micron, Crucial)
      v
[Enrichment] Text-Modell ergaenzt fehlende Specs
      |        (CAS Latency, Pin-Anzahl, Beschreibung)
      v
Ergebnis: Hersteller, Modell, Specs, eBay-Titel, Beschreibung
```

## Lizenz

Private Nutzung.
