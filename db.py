"""Database models for the Petrol Dispatch Optimizer.

Uses PostgreSQL when DATABASE_URL is set (Replit's built-in database), otherwise a local SQLite file.
"""
import os
from datetime import datetime
from sqlalchemy import (create_engine, Column, Integer, Float, String, Boolean, DateTime, ForeignKey,
                        UniqueConstraint, Text)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///data/dispatch.db")
if DB_URL.startswith("postgres://"):            # SQLAlchemy needs the postgresql:// scheme
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
                       pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


class Setting(Base):
    """One row per program setting (speed, load minutes, 150 bbl minimum, etc.)."""
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    value = Column(Float)
    label = Column(String(120))
    unit = Column(String(30))
    note = Column(String(200))
    sort = Column(Integer, default=0)


class Location(Base):
    """A physical place: pickup lease, drop-off, both, or one of our yards."""
    __tablename__ = "locations"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    kind = Column(String(10), nullable=False)          # pickup | dropoff | both | yard
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    active = Column(Boolean, default=True)
    loads_history = Column(Integer, default=0)
    avg_bbl_history = Column(Float)                    # loads-weighted true average from history; None = no history
    avg_bbl_override = Column(Float)                   # dispatcher can pin a value
    load_minutes = Column(Integer)                     # None = use the default from Settings
    open_time = Column(String(5))                      # "06:00"  earliest a truck can start loading/offloading here
    close_time = Column(String(5))                     # "18:00"  latest a truck can start loading/offloading here
    max_trucks_at_once = Column(Integer)               # None = no limit
    notes = Column(Text)

    def billable_bbl(self, min_bbl: float) -> float:
        base = self.avg_bbl_override if self.avg_bbl_override else self.avg_bbl_history
        return max(min_bbl, base) if base else min_bbl


class Lane(Base):
    """A scenario: pickup -> drop-off for a given account, with company rate and driver pay per barrel."""
    __tablename__ = "lanes"
    __table_args__ = (UniqueConstraint("account", "pickup_id", "dropoff_id", name="uq_lane"),)
    id = Column(Integer, primary_key=True)
    account = Column(String(120))
    operator = Column(String(120))
    pickup_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    dropoff_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    api_gravity = Column(Float)
    rate = Column(Float)                               # company $/bbl
    driver_pay = Column(Float)                         # driver $/bbl
    active = Column(Boolean, default=True)
    notes = Column(Text)
    pickup = relationship("Location", foreign_keys=[pickup_id])
    dropoff = relationship("Location", foreign_keys=[dropoff_id])


class Distance(Base):
    """Cached road miles between two locations (one direction). Override wins over Google when set."""
    __tablename__ = "distances"
    __table_args__ = (UniqueConstraint("origin_id", "dest_id", name="uq_dist"),)
    id = Column(Integer, primary_key=True)
    origin_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    dest_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    google_miles = Column(Float)
    google_minutes = Column(Float)
    override_miles = Column(Float)
    override_minutes = Column(Float)
    override_note = Column(String(200))
    source = Column(String(30))                        # google | manual | straight-line
    fetched_at = Column(DateTime)
    origin = relationship("Location", foreign_keys=[origin_id])
    dest = relationship("Location", foreign_keys=[dest_id])

    @property
    def miles(self):
        return self.override_miles if self.override_miles else self.google_miles


class Driver(Base):
    """Placeholder for step 3 (drivers & hours). Created now so the table exists."""
    __tablename__ = "drivers"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    yard_id = Column(Integer, ForeignKey("locations.id"))
    active = Column(Boolean, default=True)
    max_drive_hours = Column(Float, default=10)
    max_duty_hours = Column(Float, default=16)
    days_off = Column(String(60))                      # e.g. "Sat,Sun"
    notes = Column(Text)
    yard = relationship("Location", foreign_keys=[yard_id])


def init_db():
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(engine)
