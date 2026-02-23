from datetime import datetime
from sqlalchemy import Column, Integer, Text, DateTime, Float, Boolean, ForeignKey, JSON
from sqlalchemy.orm import relationship

from app.database import Base


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(Text, default="draft")  # draft/identified/researched/listed/sold/shipped/completed
    images = Column(JSON, default=list)
    internal_number = Column(Text, default="", index=True)

    # AI identification
    ai_manufacturer = Column(Text, default="")
    ai_model = Column(Text, default="")
    ai_category = Column(Text, default="")
    ai_condition = Column(Text, default="")
    ai_details = Column(Text, default="")
    ai_specs = Column(JSON, nullable=True)  # structured specs from Ollama {Marke, Modell, MPN, ...}
    ai_what_is_included = Column(Text, default="")  # Lieferumfang

    # User confirmed
    confirmed_title = Column(Text, default="")
    confirmed_description = Column(Text, default="")

    # Physical
    quantity = Column(Integer, default=1)
    weight_g = Column(Integer, nullable=True)
    dimensions = Column(JSON, nullable=True)  # {length, width, height} cm

    # Relationships
    price_research = relationship("PriceResearch", back_populates="item", cascade="all, delete-orphan")
    listings = relationship("Listing", back_populates="item", cascade="all, delete-orphan")
    email_logs = relationship("EmailLog", back_populates="item", cascade="all, delete-orphan")


class PriceResearch(Base):
    __tablename__ = "price_research"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    source = Column(Text, default="")  # browse_api / completed_scrape
    title = Column(Text, default="")
    price = Column(Float, nullable=True)
    price_type = Column(Text, default="")  # auction / fixed_price
    sold = Column(Boolean, default=False)
    end_date = Column(DateTime, nullable=True)
    url = Column(Text, default="")

    item = relationship("Item", back_populates="price_research")


class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    ebay_listing_id = Column(Text, default="")
    ebay_offer_id = Column(Text, default="")
    ebay_sku = Column(Text, default="")
    format = Column(Text, default="FIXED_PRICE")  # AUCTION / FIXED_PRICE
    start_price = Column(Float, nullable=True)
    buy_now_price = Column(Float, nullable=True)
    category_id = Column(Text, default="")
    status = Column(Text, default="draft")  # draft/active/ended/sold/unsold
    views = Column(Integer, default=0)
    watchers = Column(Integer, default=0)
    bids = Column(Integer, default=0)
    current_price = Column(Float, nullable=True)
    listed_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)

    item = relationship("Item", back_populates="listings")
    orders = relationship("Order", back_populates="listing", cascade="all, delete-orphan")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False)
    ebay_order_id = Column(Text, default="")
    buyer_username = Column(Text, default="")
    buyer_name = Column(Text, default="")
    buyer_address = Column(JSON, nullable=True)
    total_price = Column(Float, nullable=True)
    shipping_cost = Column(Float, nullable=True)
    payment_status = Column(Text, default="")
    fulfillment_status = Column(Text, default="pending")  # pending/shipped/delivered
    dhl_tracking = Column(Text, default="")
    dhl_label_url = Column(Text, default="")
    sold_at = Column(DateTime, nullable=True)
    shipped_at = Column(DateTime, nullable=True)

    listing = relationship("Listing", back_populates="orders")


class EmailLog(Base):
    __tablename__ = "email_log"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    event_type = Column(Text, default="")  # identified/listed/bid/sold/shipped/delivered
    subject = Column(Text, default="")
    sent_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", back_populates="email_logs")


class EbayToken(Base):
    __tablename__ = "ebay_tokens"

    id = Column(Integer, primary_key=True, index=True)
    access_token = Column(Text, default="")
    refresh_token = Column(Text, default="")
    token_type = Column(Text, default="")
    expires_at = Column(DateTime, nullable=True)
    refresh_expires_at = Column(DateTime, nullable=True)
    scope = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow)
