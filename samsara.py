"""Samsara integration: pull the active driver list and each driver's hours-of-service clocks.

Needs SAMSARA_API_TOKEN (Samsara dashboard -> Settings -> API Tokens) with permissions
"Read Drivers" and "Read ELD Compliance Settings (US)".
"""
import os
from datetime import datetime, date, timedelta, timezone
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


OFF_TYPES = {"offduty", "sleeperberth", "sleeperbed", "sleeper", "personalconveyance"}


def _norm(v):
    return (v or "").lower().replace("_", "").replace(" ", "")


def _parse_ts(v):
    """Samsara ISO timestamp -> naive UTC datetime."""
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        return None


def status_since_from_logs(logs):
    """From a driver's HOS log entries, when did the current status begin?
    Off-duty, sleeper and personal-conveyance stretches count as one continuous 'off' period."""
    ents = []
    for l in logs or []:
        st = _norm(l.get("hosStatusType"))
        t0 = _parse_ts(l.get("logStartTime") or l.get("startTime"))
        if st and t0: ents.append((t0, st))
    if not ents: return None
    ents.sort()
    cur = ents[-1][1]
    group = OFF_TYPES if cur in OFF_TYPES else {cur}
    since = ents[-1][0]
    for t0, st in reversed(ents[:-1]):
        if st in group: since = t0
        else: break
    return since


def fetch_status_since(sids):
    """{samsara driver id: datetime} from /fleet/hos/logs for the last 3 days. Raises on API errors."""
    out = {}
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sids = list(sids)
    for i in range(0, len(sids), 50):
        rows = _paged("/fleet/hos/logs", {"driverIds": ",".join(sids[i:i + 50]), "startTime": start, "endTime": end})
        for r in rows:
            sid = str((r.get("driver") or {}).get("id"))
            since = status_since_from_logs(r.get("hosLogs"))
            if since: out[sid] = since
    return out


def pull_hos(s: Session, plan_date: str) -> str:
    """Fill each driver's hours from Samsara's live HOS clocks.

    The live clocks (drive / shift / cycle / break / cycle-tomorrow / status and how long in it) are always stored
    on TODAY's row and on the viewed day's row. For today the planner uses them as they are. For a later day the
    planner works from an estimate (see hos.py): every working driver is assumed to use all of today's remaining
    hours. As a fallback the viewed day's cycle is also stored from Samsara's 'cycle tomorrow' figure.
    """
    if not token(): return "SAMSARA_API_TOKEN is not set (add it under Environment in Render)."
    try:
        rows = _paged("/fleet/hos/clocks", {"limit": 512})
    except Exception as e:
        return f"Samsara error: {type(e).__name__} {str(e)[:150]}"
    drivers = {d.samsara_id: d for d in s.query(Driver).filter(Driver.active == True).all() if d.samsara_id}
    today = date.today().isoformat()
    dates = sorted({plan_date, today})
    existing = {(x.plan_date, x.driver_id): x for x in s.query(DriverDay).filter(DriverDay.plan_date.in_(dates)).all()}
    is_today = plan_date == today
    since_note = ""
    try:
        since = fetch_status_since([sid for sid in drivers])
    except Exception as e:
        since = {}
        since_note = f" Time-in-status not available ({type(e).__name__}; the Samsara token may need the 'Read ELD Hours of Service (US)' permission)."
    now = datetime.utcnow()
    n = 0
    for r in rows:
        sid = str((r.get("driver") or {}).get("id"))
        d = drivers.get(sid)
        if not d: continue
        clocks = r.get("clocks") or {}
        cyc = clocks.get("cycle") or {}
        for dt in dates:
            x = existing.get((dt, d.id))
            if x is None:
                wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][datetime.strptime(dt, "%Y-%m-%d").weekday()]
                x = DriverDay(plan_date=dt, driver_id=d.id, available=wd not in (d.days_off or "").split(","), shift=d.usual_shift or "AM")
                existing[(dt, d.id)] = x
            # the live clocks, always kept as-is for the dispatcher to see
            x.hos_drive_left = _h((clocks.get("drive") or {}).get("driveRemainingDurationMs"))
            x.hos_shift_left = _h((clocks.get("shift") or {}).get("shiftRemainingDurationMs"))
            x.hos_cycle_left = _h(cyc.get("cycleRemainingDurationMs"))
            x.hos_break_left = _h((clocks.get("break") or {}).get("timeUntilBreakDurationMs"))
            x.hos_cycle_tomorrow = _h(cyc.get("cycleTomorrowDurationMs"))
            x.hos_status = (r.get("currentDutyStatus") or {}).get("hosStatusType")
            x.hos_status_since = since.get(sid)
            x.hos_synced_at = now
            if dt == today:
                x.drive_hours_left = _h((clocks.get("drive") or {}).get("driveRemainingDurationMs"))
                x.duty_hours_left = _h((clocks.get("shift") or {}).get("shiftRemainingDurationMs"))
                x.cycle_hours_left = _h(cyc.get("cycleRemainingDurationMs"))
            else:
                x.drive_hours_left = None; x.duty_hours_left = None
                x.cycle_hours_left = _h(cyc.get("cycleTomorrowDurationMs", cyc.get("cycleRemainingDurationMs")))
            s.add(x)
        veh = (r.get("currentVehicle") or {}).get("name")
        if veh: d.samsara_vehicle = veh
        n += 1
    s.commit()
    active = s.query(Driver).filter(Driver.active == True).all()
    missing = [d.name for d in active if not d.samsara_id and not (d.company and not d.company.is_petrol and not d.company.has_samsara)]
    by_phone = sorted({d.company.short_name or d.company.name for d in active if d.company and not d.company.is_petrol and not d.company.has_samsara})
    msg = f"Samsara hours pulled for {n} driver(s) ({'live clocks' if is_today else 'live clocks, with an estimate for this day'})."
    msg += since_note
    if missing: msg += f" Not linked to Samsara: {', '.join(missing[:6])}{'…' if len(missing) > 6 else ''} — click 'Sync drivers from Samsara' on the Drivers page."
    if by_phone: msg += f" Hours for {', '.join(by_phone)} drivers are entered by hand (call their dispatch)."
    return msg
