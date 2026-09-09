"""Load the confirmed master data (data/*.csv) into an empty database."""
import csv
from db import SessionLocal, Setting, Location, Lane, Company, Driver, init_db

SETTINGS = [
    # key, value, label, unit, note
    ("avg_speed_mph", 41, "Average truck speed", "mph", "From Josh's actual data"),
    ("load_minutes", 60, "Pickup (loading) time", "minutes", "Average; can be overridden per location"),
    ("unload_minutes", 60, "Drop-off (offloading) time", "minutes", "Average; can be overridden per location"),
    ("min_bbl", 150, "Minimum billable barrels per load", "bbl", "Minimum load fee; applies to driver pay too"),
    ("target_per_hour", 135, "Target gross earnings per shift hour", "$/hour", "Yard-to-yard"),
    ("mpg", 5.5, "Truck fuel economy", "mpg", "Fleet average"),
    ("diesel_price", None, "California diesel price (EIA weekly avg)", "$/gallon", "Auto-updated from EIA every week; used for fuel cost AND fuel surcharge"),
    ("fsc_base_price", 5.50, "Fuel surcharge base diesel price", "$/gallon", "No surcharge at or below this price"),
    ("fsc_step_price", 0.10, "Fuel surcharge price step", "$/gallon", "Each step above base adds one increment"),
    ("fsc_step_pct", 0.006, "Fuel surcharge per step", "fraction (0.006 = 0.6%)", "Applied to freight charge; NOT paid to drivers"),
    ("inspection_minutes", 45, "Pre/post-trip inspection pay", "minutes/day", "Paid at California minimum wage"),
    ("min_wage", None, "California minimum wage", "$/hour", "Changes each January"),
    ("max_drive_hours", 10, "Max driving hours per shift", "hours", "Default; can be set per driver"),
    ("max_duty_hours", 16, "Max on-duty hours per shift (yard to yard)", "hours", "Hard wall"),
    ("drive_buffer_hours", 0.5, "Drive-hours safety buffer", "hours", "Plan stays this far under the limit"),
    ("duty_buffer_hours", 0.5, "On-duty safety buffer", "hours", "Plan stays this far under the limit"),
    ("min_off_hours", 10, "Minimum off-duty between shifts", "hours", ""),
    ("am_start_hour", 5, "Default AM shift earliest start", "hour (0-23)", "Used when a driver has no start time entered for the day"),
    ("pm_start_hour", 17, "Default PM shift earliest start", "hour (0-23)", ""),
    ("truck_handoff_minutes", 30, "Truck handoff time between drivers sharing a truck", "minutes", "Post-trip + pre-trip when a truck changes hands"),
    ("solver_seconds", 20, "Optimizer thinking time", "seconds", "Longer = slightly better plans; 10-60"),
    ("reset_after_hours", 80, "On-duty hours before a 34-hour reset", "hours", ""),
    ("reset_hours", 34, "Full reset length", "hours", ""),
    ("gauge_minutes", 15, "Time to gauge a tank", "minutes", "Sample + API gravity / BS&W; done while loading when the gauger hauls"),
    ("priority_step", 30, "Priority strength between company tiers", "% of load profit", "A load on a tier-2 company counts as this much worse than on tier 1 (tier 3 = double). 30 = fill tier 1 first unless it's a very bad fit; 90 = always"),
]


# name, short name, sub-hauler's share of load pay (%) — None = Petrol Transport itself, Petrol-owned?
# Priority: Petrol and Petrol-owned subs = 1 (fill first), 25% subs = 2, 10% subs = 3
COMPANIES = [
    ("Petrol Transport Inc.", "Petrol", None, False),
    ("MKB Transportation, Inc.", "MKB", 90, False),
    ("King D Trucking, Inc.", "King D", 90, False),
    ("Copperhead Oil Field Services", "Copperhead", 90, True),
    ("Quail Canyon Transport, Inc.", "Quail Canyon", 90, True),
    ("Flying B Transport, Inc.", "Flying B", 90, True),
    ("J&V Transport, LLC", "J&V", 75, False),
    ("Transportillo, LLC", "Transportillo", 75, False),
    ("California Coast Services LLC", "Cal Coast", 75, False),
    ("J Oregon Trucking LLC", "J Oregon", 75, False),
    ("Maye Trucking", "Maye", 75, False),
    ("Lucas Trucking, LLC", "Lucas", 75, False),
]


def default_priority(share, owned):
    if share is None or owned: return 1
    return 3 if share >= 85 else 2


def ensure_companies(s) -> int:
    """Make sure Petrol + the known sub-haulers exist (never changes a share % the dispatcher already edited).
    Also fills in priority / Petrol-owned for rows created before those fields existed."""
    have = {c.name.strip().lower(): c for c in s.query(Company).all()}
    n = 0
    for name, short, share, owned in COMPANIES:
        c = have.get(name.lower())
        if c:
            if c.priority is None:
                c.petrol_owned = owned if c.petrol_owned is None else c.petrol_owned
                c.priority = default_priority(c.share_pct, c.petrol_owned)
            continue
        s.add(Company(name=name, short_name=short, is_petrol=share is None, share_pct=share, has_samsara=share is None, active=True,
                      petrol_owned=owned, priority=default_priority(share, owned)))
        n += 1
    for c in s.query(Company).all():                       # subs added by hand before this version
        if c.priority is None: c.priority = default_priority(c.share_pct, bool(c.petrol_owned))
    s.commit()
    return n


def petrol_company(s):
    return s.query(Company).filter(Company.is_petrol == True).first()


def _norm_name(n: str) -> str:
    """'GUERRA , EDDIE' / 'Eddie Guerra' / 'guerra, eddie' -> 'eddie guerra' so spreadsheet and Samsara names match."""
    n = n.replace("\xa0", " ").strip().lower()
    if "," in n:
        last, first = [x.strip() for x in n.split(",", 1)]
        n = f"{first} {last}"
    parts = n.split()
    return f"{parts[0]} {parts[-1]}" if len(parts) > 1 else n       # first + last name only (middle names differ between systems)


def import_drivers(s) -> str:
    """Add drivers from data/drivers.csv (name, company, truck, active). Existing drivers (same name, either name order)
    are updated with company and truck only; nothing else about them changes."""
    ensure_companies(s)
    cos = {c.name.strip().lower(): c for c in s.query(Company).all()}
    petrol = petrol_company(s)
    existing = {_norm_name(d.name): d for d in s.query(Driver).all()}
    added = updated = 0
    with open("data/drivers.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            name = r["name"].strip()
            co = cos.get((r.get("company") or "").strip().lower()) or petrol
            truck = (r.get("truck") or "").strip() or None
            active = (r.get("active") or "1").strip() not in ("0", "no", "false")
            d = existing.get(_norm_name(name))
            if d:
                d.company_id = co.id
                if truck and not d.truck: d.truck = truck
                updated += 1
            else:
                d = Driver(name=name, company_id=co.id, truck=truck, active=active)
                s.add(d); existing[_norm_name(name)] = d; added += 1
    s.commit()
    return f"Drivers imported: {added} added, {updated} already existed (company and truck filled in)."


def seed(force: bool = False):
    init_db()
    s = SessionLocal()
    try:
        for i, (k, v, label, unit, note) in enumerate(SETTINGS):      # always make sure every setting exists
            row = s.get(Setting, k)
            if not row:
                s.add(Setting(key=k, value=v, label=label, unit=unit, note=note, sort=i))
            else:
                row.label, row.unit, row.note, row.sort = label, unit, note, i
        s.commit()
        ensure_companies(s)
        # bring in the driver list (with companies and trucks) automatically, once; the Drivers page button re-runs it
        if not s.get(Setting, "drivers_imported"):
            print(import_drivers(s))
            s.add(Setting(key="drivers_imported", value=1, label="Driver spreadsheet imported", unit="", note="internal flag", sort=999))
            s.commit()
        if s.query(Location).count() and not force:
            return "already seeded"
        by_name = {}
        with open("data/locations.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                loc = Location(name=r["name"].strip(), kind=r["kind"], lat=float(r["lat"]), lon=float(r["lon"]),
                               loads_history=int(r["loads_history"] or 0),
                               avg_bbl_history=float(r["avg_bbl_history"]) if r["avg_bbl_history"] else None,
                               notes=r["notes"] or None)
                s.add(loc); by_name[loc.name] = loc
        s.flush()
        with open("data/lanes.csv", newline="") as fh:
            for r in csv.DictReader(fh):
                s.add(Lane(account=r["account"] or None, operator=r["operator"] or None,
                           pickup_id=by_name[r["pickup"].strip()].id, dropoff_id=by_name[r["dropoff"].strip()].id,
                           api_gravity=float(r["api_gravity"]) if r["api_gravity"] else None,
                           rate=float(r["rate"]) if r["rate"] else None,
                           driver_pay=float(r["driver_pay"]) if r["driver_pay"] else None,
                           notes=r["notes"] or None))
        s.commit()
        return f"seeded {len(by_name)} locations"
    finally:
        s.close()


if __name__ == "__main__":
    print(seed())
