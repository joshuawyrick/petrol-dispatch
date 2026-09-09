"""Samsara integration: pull the active driver list and each driver's hours-of-service clocks.

Needs SAMSARA_API_TOKEN (Samsara dashboard -> Settings -> API Tokens) with permissions
"Read Drivers" and "Read ELD Compliance Settings (US)".
"""
import os
from datetime import datetime, date
import httpx
from sqlalchemy.orm import Session
from db import Driver, DriverDay

BASE = "https://api.samsara.com"
MS_PER_H = 3_600_000


def token():
    return os.environ.get("SAMSARA_API_TOKEN", "").strip()


def _get(path, params=None):
    r = httpx.get(BASE + path, params=params or {}, headers={"Authorization": f"Bearer {token()}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _paged(path, params=None):
    params = dict(params or {}); out = []
    while True:
        j = _get(path, params)
        out.extend(j.get("data", []))
        pg = j.get("pagination", {})
        if not pg.get("hasNextPage"): return out
        params["after"] = pg.get("endCursor")


def sync_drivers(s: Session) -> str:
    """Create/refresh Driver rows from Samsara's active driver list. Matches by Samsara id, then by name."""
    if not token(): return "SAMSARA_API_TOKEN is not set (add it under Environment in Render)."
    try:
        rows = _paged("/fleet/drivers", {"driverActivationStatus": "active", "limit": 512})
    except Exception as e:
        return f"Samsara error: {type(e).__name__} {str(e)[:150]}"
    by_sid = {d.samsara_id: d for d in s.query(Driver).all() if d.samsara_id}
    by_name = {d.name.strip().lower(): d for d in s.query(Driver).all()}
    added = updated = 0
    seen = set()
    for r in rows:
        sid, name = str(r.get("id")), (r.get("name") or "").strip()
        if not name: continue
        seen.add(sid)
        veh = (r.get("staticAssignedVehicle") or {}).get("name")
        d = by_sid.get(sid) or by_name.get(name.lower())
        if d:
            d.samsara_id = sid; d.samsara_vehicle = veh
            if veh and not d.truck: d.truck = veh
            updated += 1                      # active/inactive is left exactly as the dispatcher set it
        else:
            s.add(Driver(name=name, samsara_id=sid, samsara_vehicle=veh, truck=veh, active=True)); added += 1
    gone = [d.name for sid, d in by_sid.items() if sid not in seen and d.active]
    s.commit()
    msg = f"Samsara: {added} driver(s) added, {updated} matched"
    if gone: msg += f". No longer active in Samsara (left as-is here, untick Active if they're gone): {', '.join(gone[:5])}"
    if added: msg += ". New drivers need a home yard — open each one on the Drivers page."
    return msg if msg.endswith(".") else msg + "."


def _h(ms):
    return round(ms / MS_PER_H * 4) / 4 if ms is not None else None      # to the nearest quarter hour


def pull_hos(s: Session, plan_date: str) -> str:
    """Fill each driver's remaining hours for `plan_date` from Samsara's live HOS clocks.

    For today: drive / shift / cycle remaining as of now.
    For a future date: drive and shift are left blank (they reset after the 10-hour break) and only the
    cycle hours are filled, using Samsara's 'cycle tomorrow' figure.
    """
    if not token(): return "SAMSARA_API_TOKEN is not set (add it under Environment in Render)."
    try:
        rows = _paged("/fleet/hos/clocks", {"limit": 512})
    except Exception as e:
        return f"Samsara error: {type(e).__name__} {str(e)[:150]}"
    drivers = {d.samsara_id: d for d in s.query(Driver).filter(Driver.active == True).all() if d.samsara_id}
    existing = {x.driver_id: x for x in s.query(DriverDay).filter(DriverDay.plan_date == plan_date).all()}
    is_today = plan_date == date.today().isoformat()
    n = 0
    for r in rows:
        sid = str((r.get("driver") or {}).get("id"))
        d = drivers.get(sid)
        if not d: continue
        clocks = r.get("clocks") or {}
        x = existing.get(d.id) or DriverDay(plan_date=plan_date, driver_id=d.id, available=True, shift=d.usual_shift or "AM")
        cyc = clocks.get("cycle") or {}
        if is_today:
            x.drive_hours_left = _h((clocks.get("drive") or {}).get("driveRemainingDurationMs"))
            x.duty_hours_left = _h((clocks.get("shift") or {}).get("shiftRemainingDurationMs"))
            x.cycle_hours_left = _h(cyc.get("cycleRemainingDurationMs"))
        else:
            x.drive_hours_left = None; x.duty_hours_left = None
            x.cycle_hours_left = _h(cyc.get("cycleTomorrowDurationMs", cyc.get("cycleRemainingDurationMs")))
        x.hos_status = (r.get("currentDutyStatus") or {}).get("hosStatusType")
        x.hos_synced_at = datetime.utcnow()
        veh = (r.get("currentVehicle") or {}).get("name")
        if veh: d.samsara_vehicle = veh
        s.add(x); n += 1
    s.commit()
    active = s.query(Driver).filter(Driver.active == True).all()
    missing = [d.name for d in active if not d.samsara_id and not (d.company and not d.company.is_petrol and not d.company.has_samsara)]
    by_phone = sorted({d.company.short_name or d.company.name for d in active if d.company and not d.company.is_petrol and not d.company.has_samsara})
    msg = f"Samsara hours pulled for {n} driver(s) ({'today: drive/shift/cycle' if is_today else 'future date: cycle hours only'})."
    if missing: msg += f" Not linked to Samsara: {', '.join(missing[:6])}{'…' if len(missing) > 6 else ''} — click 'Sync drivers from Samsara' on the Drivers page."
    if by_phone: msg += f" Hours for {', '.join(by_phone)} drivers are entered by hand (call their dispatch)."
    return msg
