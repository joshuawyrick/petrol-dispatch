"""Database models for the Petrol Dispatch Optimizer.

Uses PostgreSQL when DATABASE_URL is set (Replit's built-in database), otherwise a local SQLite file.
"""
import os
from datetime import datetime
from sqlalchemy import (create_engine, Column, Integer, Float, String, Boolean, DateTime, ForeignKey,
                        UniqueConstraint, Text)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///data/dispatch.db")
# Render gives postgres://... ; SQLAlchemy wants postgresql+psycopg://... (psycopg v3 driver)
for prefix in ("postgres://", "postgresql://"):
    if DB_URL.startswith(prefix):
        DB_URL = "postgresql+psycopg://" + DB_URL[len(prefix):]
        break

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
    approved = Column(Boolean, default=False)          # a dispatcher reviewed this leg and approved a route
    approved_at = Column(DateTime)
    route_kind = Column(String(20))                    # google-default | custom (dragged) | manual-miles
    via_json = Column(Text)                            # JSON list of [lat, lng] via points from dragging
    polyline = Column(Text)                            # encoded overview polyline of the approved route
    origin = relationship("Location", foreign_keys=[origin_id])
    dest = relationship("Location", foreign_keys=[dest_id])

    @property
    def miles(self):
        return self.override_miles if self.override_miles else self.google_miles


class Driver(Base):
    """A driver: home yard, personal hour limits, regular days off, truck sharing."""
    __tablename__ = "drivers"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    yard_id = Column(Integer, ForeignKey("locations.id"))
    active = Column(Boolean, default=True)
    truck = Column(String(40))                         # truck number; two drivers with the same truck share it
    max_drive_hours = Column(Float)                    # None = Settings default (10)
    max_duty_hours = Column(Float)                     # None = Settings default (16)
    days_off = Column(String(60))                      # "Sat,Sun" — regular days off, blank = none
    usual_shift = Column(String(2))                    # "AM" | "PM" | blank
    samsara_id = Column(String(40))                    # Samsara driver id when synced
    samsara_vehicle = Column(String(80))               # vehicle name Samsara reports for this driver
    notes = Column(Text)
    yard = relationship("Location", foreign_keys=[yard_id])


class DriverDay(Base):
    """A driver's availability for one plan date (entered by the dispatcher)."""
    __tablename__ = "driver_days"
    __table_args__ = (UniqueConstraint("plan_date", "driver_id", name="uq_driver_day"),)
    id = Column(Integer, primary_key=True)
    plan_date = Column(String(10), nullable=False)     # YYYY-MM-DD
    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=False)
    available = Column(Boolean, default=True)
    shift = Column(String(2), default="AM")            # AM | PM
    start_time = Column(String(5))                     # "05:00" earliest departure from yard
    drive_hours_left = Column(Float)                   # None = driver default; lets dispatcher cap a tired driver
    duty_hours_left = Column(Float)
    cycle_hours_left = Column(Float)                   # hours left before the 80-hr / 34-hr reset kicks in
    hos_status = Column(String(30))                    # Samsara duty status at last sync
    hos_synced_at = Column(DateTime)
    notes = Column(String(200))
    driver = relationship("Driver")


class LoadRequest(Base):
    """A called-in load (or several on the same lane) that needs hauling."""
    __tablename__ = "load_requests"
    id = Column(Integer, primary_key=True)
    plan_date = Column(String(10), nullable=False)     # the day it is planned for
    lane_id = Column(Integer, ForeignKey("lanes.id"), nullable=False)
    count = Column(Integer, default=1)                 # how many loads on this lane
    must_go_by = Column(String(10))                    # YYYY-MM-DD deadline; blank = flexible
    priority = Column(String(10), default="normal")    # must | normal | flexible
    earliest_pickup = Column(String(5))                # optional time window for this load only
    latest_pickup = Column(String(5))
    bbl_override = Column(Float)                       # known barrels for this specific load
    status = Column(String(12), default="open")        # open | planned | hauled | cancelled
    standing_order_id = Column(Integer, ForeignKey("standing_orders.id"))   # set when auto-created from a standing order
    notes = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)
    lane = relationship("Lane")


class StandingOrder(Base):
    """A recurring load: e.g. Lost Hills -> AFS N Midway, 12 loads every day; Mt Poso -> Olympus 4/day Mon-Sat."""
    __tablename__ = "standing_orders"
    id = Column(Integer, primary_key=True)
    lane_id = Column(Integer, ForeignKey("lanes.id"), nullable=False)
    count = Column(Integer, default=1)                 # loads per day
    days = Column(String(30), default="Mon,Tue,Wed,Thu,Fri,Sat,Sun")
    priority = Column(String(10), default="normal")
    earliest_pickup = Column(String(5))
    latest_pickup = Column(String(5))
    start_date = Column(String(10))                    # blank = already running
    end_date = Column(String(10))                      # blank = until switched off
    active = Column(Boolean, default=True)
    notes = Column(String(200))
    lane = relationship("Lane")


class Plan(Base):
    """A saved optimizer result for one plan date (the latest one is shown; older ones are kept)."""
    __tablename__ = "plans"
    id = Column(Integer, primary_key=True)
    plan_date = Column(String(10), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="draft")        # draft | final
    result_json = Column(Text)                           # full result (shifts, stops, totals, unassigned)
    summary = Column(String(300))


def init_db():
    """Create tables, then add any columns that newer versions of the app introduced (simple forward migration)."""
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(engine)
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'
                    conn.execute(text(ddl))
