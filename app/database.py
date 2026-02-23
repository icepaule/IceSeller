import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def migrate_db():
    """Add any missing columns to existing tables (simple SQLite migration)."""
    inspector = inspect(engine)
    migrations = {
        "items": {
            "ai_specs": "TEXT",
            "ai_what_is_included": "TEXT DEFAULT ''",
            "internal_number": "TEXT DEFAULT ''",
        },
    }
    with engine.connect() as conn:
        for table, columns in migrations.items():
            if not inspector.has_table(table):
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_type in columns.items():
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
                    logger.info("Added column %s.%s", table, col_name)
        conn.commit()

    # Backfill internal_number for existing items
    _backfill_internal_numbers()


def _backfill_internal_numbers():
    """Assign internal numbers to items that don't have one yet."""
    from collections import Counter
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, created_at FROM items "
            "WHERE internal_number IS NULL OR internal_number = '' "
            "ORDER BY created_at, id"
        )).fetchall()
        if not rows:
            return
        day_counts: Counter = Counter()
        # Also count existing items that already have internal numbers
        existing = conn.execute(text(
            "SELECT internal_number FROM items "
            "WHERE internal_number IS NOT NULL AND internal_number != ''"
        )).fetchall()
        for (num,) in existing:
            if len(num) == 10:
                prefix = num[:8]
                day_counts[prefix] += 1
        for item_id, created_at in rows:
            if created_at:
                prefix = created_at[:10].replace("-", "")  # "2026-02-22" -> "20260222"
            else:
                prefix = "19700101"
            seq = day_counts[prefix]
            day_counts[prefix] += 1
            internal_number = f"{prefix}{seq:02d}"
            conn.execute(text(
                "UPDATE items SET internal_number = :num WHERE id = :id"
            ), {"num": internal_number, "id": item_id})
        conn.commit()
        logger.info("Backfilled internal_number for %d items", len(rows))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
