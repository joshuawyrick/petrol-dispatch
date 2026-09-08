"""Road-mileage cache: which location pairs the planner needs, and how to fill them from Google's Routes API.

Every pair is looked up once and stored in the `distances` table. A dispatcher can override any pair with
approved-route miles (hazmat / heavy-truck routes) and the override always wins.
"""
import math, os
from datetime import datetime
import httpx
from sqlalchemy.orm import Session
from db import Location, Lane, Distance

ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
MAX_ELEMENTS = 625          # Google's limit per request (origins x destinations)
STRAIGHT_LINE_FACTOR = 1.30 # road miles are typically ~30% longer than straight-line in this territory


def is_pickup(loc: Location):  return loc.kind in ("pickup", "both")
def is_dropoff(loc: Location): return loc.kind in ("dropoff", "both")
def is_yard(loc: Location):    return loc.kind == "yard"


def needed_pairs(s: Session):
    """All (origin, dest) pairs a shift can contain: yard->pickup, pickup->dropoff (lanes), dropoff->pickup, dropoff->yard."""
    locs = [l for l in s.query(Location).filter(Location.active == True).all()]
    yards = [l for l in locs if is_yard(l)]
    pickups = [l for l in locs if is_pickup(l)]
    dropoffs = [l for l in locs if is_dropoff(l)]
    pairs = set()
    for y in yards:
        for p in pickups: pairs.add((y.id, p.id))
    for lane in s.query(Lane).filter(Lane.active == True).all():
        pairs.add((lane.pickup_id, lane.dropoff_id))
    for d in dropoffs:
        for p in pickups:
            if p.id != d.id: pairs.add((d.id, p.id))
        for y in yards: pairs.add((d.id, y.id))
    return pairs


def coverage(s: Session):
    pairs = needed_pairs(s)
    have = {(d.origin_id, d.dest_id) for d in s.query(Distance).all()
            if d.override_miles or (d.google_miles and d.source == "google")}
    missing = [p for p in pairs if p not in have]
    return {"needed": len(pairs), "have": len(pairs) - len(missing), "missing": len(missing), "missing_pairs": missing}


def haversine_miles(a: Location, b: Location):
    R = 3958.8
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = math.radians(b.lat - a.lat), math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def fill_straight_line(s: Session):
    """Fallback used before Google has been run: estimate every missing pair as straight-line x factor."""
    cov = coverage(s)
    locs = {l.id: l for l in s.query(Location).all()}
    existing = {(d.origin_id, d.dest_id): d for d in s.query(Distance).all()}
    n = 0
    for o, d in cov["missing_pairs"]:
        row = existing.get((o, d))
        if row and row.google_miles: continue
        miles = round(haversine_miles(locs[o], locs[d]) * STRAIGHT_LINE_FACTOR, 1)
        if row:
            row.google_miles, row.source = miles, "straight-line"
        else:
            s.add(Distance(origin_id=o, dest_id=d, google_miles=miles, source="straight-line"))
        n += 1
    s.commit()
    return n


def _waypoint(loc: Location):
    return {"waypoint": {"location": {"latLng": {"latitude": loc.lat, "longitude": loc.lon}}}}


def fetch_from_google(s: Session, api_key: str, max_pairs: int | None = None):
    """Look up every missing pair with Google's Routes API (compute route matrix) and store the results."""
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY is not set (add it under Secrets in Replit)."}
    cov = coverage(s)
    missing = cov["missing_pairs"]
    if max_pairs: missing = missing[:max_pairs]
    if not missing:
        return {"fetched": 0, "requests": 0, "errors": []}
    locs = {l.id: l for l in s.query(Location).all()}
    existing = {(d.origin_id, d.dest_id): d for d in s.query(Distance).all()}
    by_origin = {}
    for o, d in missing: by_origin.setdefault(o, []).append(d)

    fetched, requests_made, errors = 0, 0, []
    headers = {"X-Goog-Api-Key": api_key,
               "X-Goog-FieldMask": "originIndex,destinationIndex,distanceMeters,duration,condition"}
    with httpx.Client(timeout=60) as client:
        for o, dests in by_origin.items():
            for i in range(0, len(dests), MAX_ELEMENTS):
                chunk = dests[i:i + MAX_ELEMENTS]
                body = {"origins": [_waypoint(locs[o])], "destinations": [_waypoint(locs[d]) for d in chunk],
                        "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE"}
                try:
                    r = client.post(ROUTES_URL, json=body, headers=headers)
                    requests_made += 1
                    if r.status_code != 200:
                        errors.append(f"{locs[o].name}: HTTP {r.status_code} {r.text[:200]}")
                        if r.status_code in (401, 403): return {"fetched": fetched, "requests": requests_made, "errors": errors}
                        continue
                    for el in r.json():
                        if el.get("condition") != "ROUTE_EXISTS": continue
                        d = chunk[el["destinationIndex"]]
                        miles = round(el["distanceMeters"] / 1609.344, 1)
                        minutes = round(float(el["duration"].rstrip("s")) / 60, 1)
                        row = existing.get((o, d))
                        if row:
                            row.google_miles, row.google_minutes, row.source, row.fetched_at = miles, minutes, "google", datetime.utcnow()
                        else:
                            row = Distance(origin_id=o, dest_id=d, google_miles=miles, google_minutes=minutes,
                                           source="google", fetched_at=datetime.utcnow())
                            s.add(row); existing[(o, d)] = row
                        fetched += 1
                    s.commit()
                except Exception as e:  # network hiccup: record and keep going
                    errors.append(f"{locs[o].name}: {e}")
    return {"fetched": fetched, "requests": requests_made, "errors": errors}


def get_api_key():
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
