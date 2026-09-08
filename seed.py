"""Load the confirmed master data (data/*.csv) into an empty database."""
import csv
from db import SessionLocal, Setting, Location, Lane, init_db

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
    ("reset_after_hours", 80, "On-duty hours before a 34-hour reset", "hours", ""),
    ("reset_hours", 34, "Full reset length", "hours", ""),
]


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
