# IceSeller - eBay Selling Assistant

## Project conventions
- Python 3.12, FastAPI, async where possible
- Frontend: Vanilla HTML/JS + Bootstrap 5 dark theme, Jinja2 templates
- Database: SQLite via SQLAlchemy (models in app/models.py)
- All config via .env / Pydantic BaseSettings (app/config.py)
- German UI, English code/comments
- No hardcoded credentials - everything from .env

## Code patterns
- Routers in app/routers/, services in app/services/
- Each router uses Jinja2Templates("app/templates"), Depends(get_db)
- Helper pattern: _get_item_or_404(item_id, db) for 404 handling
- Service classes: EbayClient(db), DhlClient, CameraService singleton
- Async httpx for external APIs, sync SQLAlchemy for DB

## Key dependencies
- Ollama Vision API at OLLAMA_HOST for product identification
- eBay REST APIs (Browse, Inventory, Fulfillment, Trading, Taxonomy)
- DHL Parcel DE Shipping API v2
- Playwright headless Chromium for eBay scraping
- OpenCV for USB camera, gphoto2 for Canon cameras

## AI identification pipeline
- 2-step OCR approach: Vision model reads text (OCR_PROMPT), text model structures JSON (STRUCTURE_FROM_OCR_PROMPT)
- Fallback: Vision model does direct JSON identification if OCR fails
- Deterministic RAM part number decoder (app/services/part_decoder.py) for SK hynix, Samsung, Kingston, Micron, Crucial
- Text model enrichment with protected decoded values
- Quantity detection for multiple identical components

## File structure
- app/main.py - FastAPI app with lifespan (DB init, scheduler)
- app/models.py - SQLAlchemy models (Item, Listing, Order, PriceResearch, EmailLog, EbayToken)
- app/config.py - Pydantic BaseSettings
- app/database.py - Engine, SessionLocal, get_db dependency
- app/services/ollama_vision.py - 2-step OCR pipeline, enrichment, JSON parsing
- app/services/part_decoder.py - Deterministic RAM MPN decoder
- app/services/camera_service.py - USB camera with auto codec/resolution, PTZ, crop
- app/services/listing_helpers.py - HTML description generator, aspects builder
- Templates extend base.html, use Bootstrap 5 dark theme

## Important notes
- Auctions: quantity always forced to 1 (eBay rule)
- Internal numbers: format YYYYMMDDnn, auto-incremented from highest existing
- Camera: auto-detects MJPEG 1080p, falls back to YUYV 720p
- Crops: minimum 800px enforced, upscaled with INTER_LANCZOS4
- OCR timeout: 600s (vision model on NUC hardware is slow)
