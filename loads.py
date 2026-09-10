"""Per-load tracking: keep one Load row per physical load behind every LoadRequest line, and record what happened to it.

Rules
  * a line's `count` = how many of its loads are still OPEN (that is what the planner plans)
  * marking N hauled / cancelled / rejected resolves N open loads and lowers the line count; at 0 the line is "done"
  * moving N loads to another day creates (or reuses) a line on that day and re-homes those loads
  * nothing is ever deleted: removing a line marks its open loads "removed" and the line "removed"
"""
from datetime import datetime, date
from sqlalchemy.orm import Session
from db import Load, LoadEvent, LoadRequest

LINE_OPEN = ("open", "planned")           # line statuses the planner and the day page treat as live
OUTCOMES = {"hauled": "hauled", "cancelled": "cancelled", "rejected": "rejected"}


def log(s: Session, load: Load, event: str, detail: str = "", plan_date: str | None = None):
    s.add(LoadEvent(load_id=load.id, event=event, detail=detail[:300] if detail else None, plan_date=plan_date))


def open_loads(s: Session, line: LoadRequest):
    return s.query(Load).filter(Load.line_id == line.id, Load.status == "open").order_by(Load.id).all()


def sync_line(s: Session, line: LoadRequest, plan_date: str | None = None, note: str = ""):
    """Make the number of OPEN loads behind a line equal its count (create or 'remove' the difference)."""
    if line.status not in LINE_OPEN: return
    s.flush()
    opens = open_loads(s, line)
    want = max(0, int(line.count or 0))
    today = date.today().isoformat()
    for _ in range(want - len(opens)):
        ld = Load(line_id=line.id, lane_id=line.lane_id, tank_id=line.tank_id, created_date=today, plan_date=line.plan_date, status="open")
        s.add(ld); s.flush()
        log(s, ld, "created", note or f"entered for {line.plan_date}", plan_date or line.plan_date)
    for ld in opens[want:] if want < len(opens) else []:
        ld.status = "removed"; ld.outcome_date = today
        log(s, ld, "removed", "count lowered on the day page", plan_date or line.plan_date)
    for ld in opens[:want]:
        if ld.plan_date != line.plan_date: ld.plan_date = line.plan_date
        if ld.tank_id != line.tank_id: ld.tank_id = line.tank_id
        if ld.lane_id != line.lane_id: ld.lane_id = line.lane_id


def sync_all(s: Session) -> int:
    """One-time catch-up for lines that existed before per-load tracking."""
    n = 0
    for line in s.query(LoadRequest).filter(LoadRequest.status.in_(LINE_OPEN)).all():
        if not s.query(Load).filter(Load.line_id == line.id).count():
            sync_line(s, line, note=f"entered for {line.plan_date} (before load tracking)"); n += 1
    if n: s.commit()
    return n


def remove_line(s: Session, line: LoadRequest, plan_date: str):
    today = date.today().isoformat()
    for ld in open_loads(s, line):
        ld.status = "removed"; ld.outcome_date = today
        log(s, ld, "removed", "line deleted from the day page", plan_date)
    line.count = 0
    line.status = "removed"


def resolve(s: Session, line: LoadRequest, n: int, outcome: str, when: str, note: str, plan_date: str, driver_id: int | None = None) -> int:
    """Mark n open loads of this line hauled / cancelled / rejected. Returns how many were marked."""
    outcome = OUTCOMES.get(outcome)
    if not outcome: return 0
    opens = open_loads(s, line)
    picked = opens[:max(0, int(n))]
    for ld in picked:
        ld.status = outcome; ld.outcome_date = when or plan_date; ld.outcome_note = note or None
        if outcome == "hauled": ld.hauled_by_id = driver_id or ld.planned_driver_id
        log(s, ld, outcome, (f"on {ld.outcome_date}" + (f" — {note}" if note else "")), plan_date)
    line.count = len(opens) - len(picked)
    if line.count == 0: line.status = "done"
    return len(picked)


def reopen(s: Session, ld: Load, plan_date: str):
    """Undo an outcome (wrong click). The load goes back to open on its line."""
    line = ld.line
    ld.status = "open"; ld.outcome_date = None; ld.outcome_note = None; ld.hauled_by_id = None
    log(s, ld, "reopened", "", plan_date)
    if line.status in ("done", "removed"): line.status = "open"
    s.flush()
    line.count = len(open_loads(s, line))


def move(s: Session, line: LoadRequest, n: int, new_date: str, plan_date: str) -> int:
    """Move n open loads of this line to another day (all of them = the whole line moves)."""
    opens = open_loads(s, line)
    n = max(0, min(int(n), len(opens)))
    if not n or new_date == line.plan_date: return 0
    old = line.plan_date
    if n == len(opens):
        line.plan_date = new_date
        for ld in opens:
            ld.plan_date = new_date; log(s, ld, "moved", f"{old} → {new_date}", plan_date)
        return n
    # partial: find or create a matching line on the target day
    target = s.query(LoadRequest).filter(LoadRequest.plan_date == new_date, LoadRequest.lane_id == line.lane_id,
                                         LoadRequest.status.in_(LINE_OPEN), LoadRequest.tank_id == line.tank_id).first()
    if not target:
        target = LoadRequest(plan_date=new_date, lane_id=line.lane_id, count=0, priority=line.priority, must_go_by=line.must_go_by,
                             earliest_pickup=line.earliest_pickup, latest_pickup=line.latest_pickup, bbl_override=line.bbl_override,
                             tank_id=line.tank_id, gauge=line.gauge, notes=line.notes, status="open")
        s.add(target); s.flush()
    for ld in opens[:n]:
        ld.line_id = target.id; ld.plan_date = new_date
        log(s, ld, "moved", f"{old} → {new_date}", plan_date)
    target.count = (target.count or 0) + n
    line.count = len(opens) - n
    if line.count == 0: line.status = "done"
    return n


def record_plan(s: Session, plan: dict) -> int:
    """After a plan build: remember which driver each load was planned to (logged only when it changes)."""
    changed = 0
    for sh in plan.get("shifts", []):
        for st in sh.get("stops", []):
            if st.get("kind") != "pickup" or not st.get("load_row_id"): continue
            ld = s.get(Load, st["load_row_id"])
            if not ld or ld.status != "open": continue
            if ld.planned_driver_id != sh["driver_id"]:
                ld.planned_driver_id = sh["driver_id"]
                log(s, ld, "planned", f"to {sh['driver']} ({sh['shift']} shift)", plan["plan_date"]); changed += 1
    return changed


def summary(s: Session, line: LoadRequest) -> dict:
    """Counts by status for the little 'hauled 3 · rejected 1' note on the day page."""
    out = {}
    for ld in s.query(Load).filter(Load.line_id == line.id).all():
        out[ld.status] = out.get(ld.status, 0) + 1
    return out
