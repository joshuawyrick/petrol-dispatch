"""Load urgency: 'today' (must move on the plan day), 'tomorrow' (by the next day) or 'flex' (within N days, N from Settings).

The deadline (LoadRequest.must_go_by) is what's stored; the word shown is worked out from how many days are left
between the plan day and that deadline, so a load that slides a day automatically becomes more urgent.
"""
from datetime import datetime, timedelta

PRIORITIES = ["today", "tomorrow", "flex"]
LEGACY = {"must": "today", "normal": "tomorrow", "flexible": "flex"}      # values used before this version
DEFAULT_FLEX_DAYS = 5


def norm(p: str | None) -> str:
    p = (p or "").strip().lower()
    return LEGACY.get(p, p) if (LEGACY.get(p, p) in PRIORITIES) else "tomorrow"


def _d(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def deadline_for(priority: str, plan_date: str, flex_days: int) -> str:
    """The must-go-by date implied by picking a priority word on a given plan day."""
    p = norm(priority)
    days = 0 if p == "today" else (1 if p == "tomorrow" else max(2, int(flex_days or DEFAULT_FLEX_DAYS)))
    return (_d(plan_date) + timedelta(days=days)).isoformat()


def urgency(must_go_by: str | None, priority: str | None, plan_date: str, flex_days: int) -> dict:
    """code: today | tomorrow | flex; days_left: whole days from plan day to the deadline (0 = today, <0 = overdue)."""
    if not must_go_by:
        must_go_by = deadline_for(priority, plan_date, flex_days)
    days_left = (_d(must_go_by) - _d(plan_date)).days
    code = "today" if days_left <= 0 else ("tomorrow" if days_left == 1 else "flex")
    label = {"today": "today", "tomorrow": "tomorrow"}.get(code) or f"flex · by {_d(must_go_by).strftime('%a %b %-d')}"
    if days_left < 0: label = f"OVERDUE ({-days_left}d)"
    return dict(code=code, days_left=days_left, deadline=must_go_by, label=label, overdue=days_left < 0)
