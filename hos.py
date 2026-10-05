"""Hours-of-service projections.

Today we show and plan with the driver's LIVE Samsara clocks.
For any later day we plan on a worst-case estimate: assume every driver who is working uses ALL of their
available hours on the days before, then work out what is left.

    cycle at the start of tomorrow = Samsara's "cycle tomorrow" figure  -  hours used today
    hours used today               = the shift hours still available today (limited by the cycle left)

A driver who is not working a day (unticked) uses nothing that day. Days further out chain the same way
(minus a full shift each working day). Drive and shift clocks for a later day are the full 10 / 16 after the
10-hour break. The earliest legal start tomorrow is the worst-case end of today's shift (live clock for a driver on shift now, planned
start + 16 h for one who hasn't started) + the 10-hour break.
The real Samsara numbers replace the estimate the moment that day arrives and hours are pulled again.
"""
from datetime import datetime, date, timedelta, timezone
from db import DriverDay

ON_SHIFT = {"driving", "onduty", "yardmove"}
OFF_GROUP = {"offduty", "sleeperberth", "sleeperbed", "sleeper", "personalconveyance"}


def norm_status(v):
    return (v or "").lower().replace("_", "").replace(" ", "")


def to_local(dt_utc):
    """Naive UTC datetime -> naive local datetime (the app sets its timezone at startup)."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def snapshots(s):
    """Newest live Samsara snapshot per driver (a DriverDay row), from the last 3 days."""
    cutoff = datetime.utcnow() - timedelta(days=3)
    rows = s.query(DriverDay).filter(DriverDay.hos_synced_at != None, DriverDay.hos_synced_at >= cutoff) \
        .order_by(DriverDay.hos_synced_at).all()
    out = {}
    for x in rows: out[x.driver_id] = x                   # newest wins
    return out


def fresh(snap):
    """A snapshot is only good for projecting if it was pulled today (local time)."""
    return bool(snap and snap.hos_synced_at and to_local(snap.hos_synced_at).date() == date.today())


def status_elapsed_h(snap):
    """Hours the driver had been in their current status (off-duty/sleeper count as one stretch) at the time of the pull."""
    if not snap or not snap.hos_status_since or not snap.hos_synced_at: return None
    return max(0.0, (snap.hos_synced_at - snap.hos_status_since).total_seconds() / 3600)


def fmt_hm(hours):
    if hours is None: return ""
    m = int(round(hours * 60))
    return f"{m // 60}h {m % 60:02d}m"


def fmt_clock(minutes):
    """Minutes after midnight -> '7:00 PM' (past 24h shows the next-day clock)."""
    m = int(minutes) % 1440
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d} {'AM' if h < 12 else 'PM'}"


def _default_start_min(st, shift):
    return int((st.get("am_start_hour") if shift == "AM" else st.get("pm_start_hour")) or (5 if shift == "AM" else 17)) * 60


def project(s, plan_date, drivers, st, snaps=None):
    """-> {driver_id: dict} describing the hours to show/plan with for `plan_date`.

    kind: 'live' (today, Samsara clocks) | 'est' (later day, worst-case estimate) | None (no usable Samsara data:
    the typed hours on the day's row are used instead).
    Keys: cycle, drive, shift, cycle_after (estimate for the day after plan_date), basis, legal_start (minutes after
    midnight of plan_date, only for tomorrow), used_before (hours assumed used on earlier days).
    """
    snaps = snaps if snaps is not None else snapshots(s)
    today = date.today()
    target = datetime.strptime(plan_date, "%Y-%m-%d").date()
    n = (target - today).days
    days = {}
    if n >= 0:
        for x in s.query(DriverDay).filter(DriverDay.plan_date >= today.isoformat(), DriverDay.plan_date <= plan_date).all():
            days[(x.plan_date, x.driver_id)] = x
    max_duty_default = float(st.get("max_duty_hours") or 16)
    max_drive_default = float(st.get("max_drive_hours") or 10)
    min_off = float(st.get("min_off_hours") or 10)
    now = datetime.now()
    out = {}

    def working(d, day):
        x = days.get((day.isoformat(), d.id))
        if x is not None and x.available is not None: return bool(x.available)
        wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][day.weekday()]
        return wd not in (d.days_off or "").split(",")

    for d in drivers:
        snap = snaps.get(d.id)
        if n < 0 or not d.samsara_id or not fresh(snap):
            out[d.id] = dict(kind=None); continue
        duty_max = float(d.max_duty_hours or max_duty_default)
        drive_max = float(d.max_drive_hours or max_drive_default)
        cyc_now = snap.hos_cycle_left
        cyc_tm = snap.hos_cycle_tomorrow if snap.hos_cycle_tomorrow is not None else cyc_now
        shift_now = snap.hos_shift_left if snap.hos_shift_left is not None else duty_max
        if cyc_now is None or cyc_tm is None:
            out[d.id] = dict(kind=None); continue
        # ---- today: what they can still use, and what that leaves for tomorrow
        in_today = working(d, today)
        used_today = min(shift_now, max(cyc_now, 0)) if in_today else 0.0
        cyc_tomorrow = max(0.0, cyc_tm - used_today)
        if n == 0:
            out[d.id] = dict(kind="live", cycle=cyc_now, drive=snap.hos_drive_left, shift=snap.hos_shift_left,
                             cycle_after=cyc_tomorrow, used_before=0.0,
                             basis=(f"{cyc_tm:.2f} (Samsara, tomorrow) − {used_today:.2f} (rest of today's shift)" if in_today
                                    else f"{cyc_tm:.2f} (Samsara, tomorrow) — not working today, nothing deducted"))
            continue
        # ---- later days: chain forward, each working day uses a full shift (or whatever cycle is left)
        cyc = cyc_tomorrow
        used_total = used_today
        basis = (f"{cyc_tm:.2f} (Samsara, tomorrow) − {used_today:.2f} (rest of today's shift)" if in_today
                 else f"{cyc_tm:.2f} (Samsara, tomorrow); not working today")
        for k in range(1, n):
            day = today + timedelta(days=k)
            if working(d, day):
                u = min(duty_max, cyc); cyc -= u; used_total += u
                basis += f" − {u:.2f} ({day.strftime('%a')})"
        cycle_after = max(0.0, cyc - (min(duty_max, cyc) if working(d, target) else 0.0))
        # ---- earliest legal start tomorrow: worst-case end of today's shift + the 10-hour break
        legal = None
        if n == 1 and in_today:
            status = norm_status(snap.hos_status)
            if status in ON_SHIFT:
                end = now + timedelta(hours=shift_now)
            else:
                tx = days.get((today.isoformat(), d.id))
                shift_code = (tx.shift if tx and tx.shift else d.usual_shift) or "AM"
                tstart = datetime.combine(today, datetime.min.time()) + timedelta(
                    minutes=(int(tx.start_time[:2]) * 60 + int(tx.start_time[3:5])) if tx and tx.start_time else _default_start_min(st, shift_code))
                end = tstart + timedelta(hours=duty_max)           # their planned shift today, run to the full 16 hours
            legal_dt = end + timedelta(hours=min_off)
            legal = (legal_dt - datetime.combine(target, datetime.min.time())).total_seconds() / 60
            legal = max(0, legal)
        out[d.id] = dict(kind="est", cycle=max(0.0, cyc), drive=drive_max, shift=duty_max, cycle_after=cycle_after,
                         used_before=used_total, basis=basis, legal_start=legal)
    return out
