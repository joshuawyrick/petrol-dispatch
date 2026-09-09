"""Database models for the Petrol Dispatch Optimizer.

Uses PostgreSQL when DATABASE_URL is set (Replit's built-in database), otherwise a local SQLite file.
"""
import os
from datetime import datetime
from sqlalchemy import (create_engine, Column, Integer, Float, String, Boolean, DateTime, ForeignKey,
                        UniqueConstraint, Text, LargeBinary)
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
    requires_gauging = Column(Boolean, default=False)  # first load from each tank each day must be gauged (API gravity, BS&W)
    notes = Column(Text)

    def billable_bbl(self, min_bbl: float) -> float:
        base = self.avg_bbl_override if self.avg_bbl_override else self.avg_bbl_history
        return max(min_bbl, base) if base else min_bbl


class Tank(Base):
    """A tank at a location (pickup sites with several tanks; drop-offs can have them too). Loads can name the tank."""
    __tablename__ = "tanks"
    __table_args__ = (UniqueConstraint("location_id", "name", name="uq_tank"),)
    id = Column(Integer, primary_key=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    name = Column(String(60), nullable=False)          # "Tank 1", "T-204"
    active = Column(Boolean, default=True)
    notes = Column(String(200))
    location = relationship("Location")


class LocationRestriction(Base):
    """A driver or a truck that cannot go to this location (too tall/long for the site, no site training, etc.)."""
    __tablename__ = "location_restrictions"
    id = Column(Integer, primary_key=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    driver_id = Column(Integer, ForeignKey("drivers.id"))      # one of driver_id / truck is set
    truck = Column(String(40))                                 # truck number, matched case-insensitively
    reason = Column(String(200))
    location = relationship("Location")
    driver = relationship("Driver")


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
    product = Column(String(60))                       # e.g. crude oil, naphtha (shown on the JMP)
    jmp_hazards = Column(Text)                         # JSON list of {hazard, location, control}
    jmp_rest_stop = Column(String(200))
    jmp_notes = Column(Text)
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
    steps_json = Column(Text)                          # JSON list of {instruction, miles} turn-by-turn along the approved route
    origin = relationship("Location", foreign_keys=[origin_id])
    dest = relationship("Location", foreign_keys=[dest_id])

    @property
    def miles(self):
        return self.override_miles if self.override_miles else self.google_miles


class Company(Base):
    """A hauling company: Petrol Transport itself (is_petrol) or a sub-hauler that runs loads for us.

    Sub-haulers get `share_pct` of the load pay (e.g. 75 or 90) plus the entire fuel surcharge; they cover their own
    drivers' pay and fuel. Petrol keeps the rest of the load pay as its margin.
    """
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    short_name = Column(String(30))                    # badge label on the plan: "King D", "MKB"
    is_petrol = Column(Boolean, default=False)         # exactly one row: our own drivers (per-lane driver pay)
    share_pct = Column(Float)                          # sub-hauler's share of load pay, in percent (75 = sub keeps 75%)
    dispatch_phone = Column(String(60))
    has_samsara = Column(Boolean, default=False)       # if false, dispatch calls the sub for hours; Samsara pull skips them
    priority = Column(Integer, default=2)              # 1 = fill first (Petrol + Petrol-owned subs), 2 = next, 3 = last
    petrol_owned = Column(Boolean, default=False)      # sub Petrol owns: engine treats it like our own truck (fuel + lane driver pay)
    active = Column(Boolean, default=True)
    notes = Column(Text)

    @property
    def petrol_pct(self):
        return None if self.is_petrol or self.share_pct is None else round(100 - self.share_pct, 2)


class CompanyLaneRate(Base):
    """A special deal for one sub-hauler on one lane: either a different share % or a flat $/bbl the sub gets."""
    __tablename__ = "company_lane_rates"
    __table_args__ = (UniqueConstraint("company_id", "lane_id", name="uq_company_lane"),)
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    lane_id = Column(Integer, ForeignKey("lanes.id"), nullable=False)
    share_pct = Column(Float)                          # overrides the company default share for this lane
    flat_bbl = Column(Float)                           # or: the sub gets this many $/bbl on this lane (wins over share_pct)
    notes = Column(String(200))
    company = relationship("Company")
    lane = relationship("Lane")


class Driver(Base):
    """A driver: home yard, personal hour limits, regular days off, truck sharing."""
    __tablename__ = "drivers"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    yard_id = Column(Integer, ForeignKey("locations.id"))
    company_id = Column(Integer, ForeignKey("companies.id"))   # blank = Petrol Transport
    active = Column(Boolean, default=True)
    truck = Column(String(40))                         # truck number; two drivers with the same truck share it
    max_drive_hours = Column(Float)                    # None = Settings default (10)
    max_duty_hours = Column(Float)                     # None = Settings default (16)
    days_off = Column(String(60))                      # "Sat,Sun" — regular days off, blank = none
    usual_shift = Column(String(2))                    # "AM" | "PM" | blank
    samsara_id = Column(String(40))                    # Samsara driver id when synced
    samsara_vehicle = Column(String(80))               # vehicle name Samsara reports for this driver
    can_gauge = Column(Boolean, default=False)         # trained/equipped to gauge a tank (sample, API gravity, BS&W)
    notes = Column(Text)
    yard = relationship("Location", foreign_keys=[yard_id])
    company = relationship("Company")

    @property
    def is_sub(self):
        return bool(self.company) and not self.company.is_petrol

    @property
    def company_short(self):
        """Short label for badges: 'King D', 'MKB', ...; blank for Petrol."""
        return (self.company.short_name or self.company.name) if self.is_sub else ""


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
    tank_id = Column(Integer, ForeignKey("tanks.id"))  # which tank at the pickup (optional; matters for gauging)
    gauge = Column(String(12))                         # none | haul (gauger hauls the first load) | only (gauger just gauges first)
    status = Column(String(12), default="open")        # open | planned | hauled | cancelled
    standing_order_id = Column(Integer, ForeignKey("standing_orders.id"))   # set when auto-created from a standing order
    notes = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)
    lane = relationship("Lane")
    tank = relationship("Tank")


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
    tank_id = Column(Integer, ForeignKey("tanks.id"))
    notes = Column(String(200))
    lane = relationship("Lane")
    tank = relationship("Tank")


class CompanyInfo(Base):
    """One row: the fixed company details printed on every Journey Management Plan."""
    __tablename__ = "company_info"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), default="Petrol Transport, Inc.")
    address = Column(String(200))
    dispatch_phone = Column(String(60))
    emergency_phone = Column(String(60))
    safety_contact = Column(String(120))
    safety_phone = Column(String(60))
    spill_response = Column(String(200))
    checkin_rule = Column(String(300))
    overdue_rule = Column(String(300))
    prepared_by_title = Column(String(80), default="Dispatcher")
    approved_by_title = Column(String(80), default="Operations Manager")
    require_manager_signature = Column(Boolean, default=True)


class JmpDoc(Base):
    """A generated Journey Management Plan PDF for a lane (one row per version)."""
    __tablename__ = "jmp_docs"
    id = Column(Integer, primary_key=True)
    lane_id = Column(Integer, ForeignKey("lanes.id"), nullable=False)
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    include_yard_id = Column(Integer, ForeignKey("locations.id"))
    summary = Column(String(200))
    pdf = Column(LargeBinary)
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
