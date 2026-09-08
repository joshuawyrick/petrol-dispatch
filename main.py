"""Petrol Dispatch Optimizer — web app.

Phase 1: master data, settings, mileage cache.   Phase 2: login, fuel surcharge + EIA price, drivers, daily load board.
"""
import os, hashlib
from datetime import date, datetime, timedelta
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from db import SessionLocal, Setting, Location, Lane, Distance, Driver, DriverDay, LoadRequest, Plan, init_db
from seed import seed
import mileage, fsc, optimizer, samsara
import json

app = FastAPI(title="Petrol Dispatch Optimizer")
PASSWORD = os.environ.get("DISPATCH_PASSWORD", "").strip()
SECRET = os.environ.get("SESSION_SECRET") or hashlib.sha256(("petrol-" + PASSWORD).encode()).hexdigest()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.filters["hm"] = optimizer.min_to_hm
templates.env.filters["money"] = lambda v: f"${v:,.0f}"

KINDS = ["pickup", "dropoff", "both", "yard"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PRIORITIES = ["must", "normal", "flexible"]


@app.on_event("startup")
def _startup():
    init_db()
    print("seed:", seed())


# ---------------- login ----------------
@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if PASSWORD and not request.session.get("ok") and not (path.startswith("/login") or path.startswith("/static")):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)

# added after the login check so it wraps it (Starlette runs the last-added middleware first)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=14 * 24 * 3600, same_site="lax")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "msg": request.query_params.get("msg"), "no_pw": not PASSWORD})


@app.post("/login")
async def login_post(request: Request):
    f = await request.form()
    if PASSWORD and f.get("password", "") == PASSWORD:
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?msg=Wrong+password", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------- helpers ----------------
def get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def settings_dict(s: Session):
    return {x.key: x.value for x in s.query(Setting).all()}


def money_ctx(s: Session):
    """Numbers every money display needs: min bbl, FSC %, diesel price."""
    st = settings_dict(s)
    return dict(st=st, min_bbl=st.get("min_bbl") or 150, fsc=fsc.fsc_from_settings(st))


def render(request, name, **ctx):
    ctx.setdefault("request", request)
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("no_pw", not PASSWORD)
    return templates.TemplateResponse(name, ctx)


def fnum(v):
    v = (v or "").strip().replace("$", "").replace(",", "")
    return float(v) if v else None


def fint(v):
    v = (v or "").strip()
    return int(float(v)) if v else None


def lane_money(l: Lane, min_bbl: float, fsc_pct: float, bbl=None):
    bbl = bbl or l.pickup.billable_bbl(min_bbl)
    rev = (l.rate or 0) * bbl
    pay = (l.driver_pay or 0) * bbl
    return dict(bbl=bbl, rev=rev, rev_fsc=rev * (1 + fsc_pct), pay=pay, margin=rev - pay, margin_fsc=rev * (1 + fsc_pct) - pay)


# ---------------- Dashboard ----------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_db)):
    eia_msg = fsc.refresh_diesel_price(s, os.environ.get("EIA_API_KEY"))     # no-op unless >24h since last check
    m = money_ctx(s); st = m["st"]
    cov = mileage.coverage(s)
    todo = []
    if not PASSWORD: todo.append("No password set — add DISPATCH_PASSWORD under Environment in Render so only your dispatchers can open this.")
    if not st.get("diesel_price"): todo.append("Diesel price is blank — click 'Refresh from EIA' on Settings or enter it by hand.")
    if not st.get("min_wage"): todo.append("Enter the California minimum wage on the Settings page.")
    no_rate = s.query(Lane).filter(Lane.active == True, Lane.rate == None).count()
    if no_rate: todo.append(f"{no_rate} active lane(s) have no rate — see Lanes.")
    if cov["missing"]: todo.append(f"{cov['missing']} of {cov['needed']} mileage pairs still need road miles — see Mileage.")
    if not s.query(Driver).filter(Driver.active == True).count(): todo.append("No drivers yet — add them on the Drivers page.")
    counts = {k: s.query(Location).filter(Location.kind == k, Location.active == True).count() for k in KINDS}
    today = date.today().isoformat()
    open_loads = s.query(LoadRequest).filter(LoadRequest.plan_date == today, LoadRequest.status == "open").all()
    return render(request, "dashboard.html", counts=counts, lanes=s.query(Lane).filter(Lane.active == True).count(),
                  drivers=s.query(Driver).filter(Driver.active == True).count(), cov=cov, todo=todo,
                  has_key=bool(mileage.get_api_key()), st=st, fsc_pct=m["fsc"], eia_msg=eia_msg, today=today,
                  open_loads=sum(l.count for l in open_loads))


# ---------------- Settings ----------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_db)):
    rows = [r for r in s.query(Setting).order_by(Setting.sort).all() if not r.key.startswith("eia_")]
    info = {r.key: r.note for r in s.query(Setting).filter(Setting.key.like("eia_%")).all()}
    m = money_ctx(s)
    return render(request, "settings.html", rows=rows, info=info, fsc_pct=m["fsc"], has_eia_key=bool(os.environ.get("EIA_API_KEY")))


@app.post("/settings")
async def settings_save(request: Request, s: Session = Depends(get_db)):
    form = await request.form()
    for row in s.query(Setting).all():
        if row.key in form:
            row.value = fnum(form[row.key])
    s.commit()
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


@app.post("/settings/eia")
def settings_eia(s: Session = Depends(get_db)):
    msg = fsc.refresh_diesel_price(s, os.environ.get("EIA_API_KEY"), force=True) or "Already up to date."
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


# ---------------- Locations ----------------
@app.get("/locations", response_class=HTMLResponse)
def locations(request: Request, kind: str = "", q: str = "", show_inactive: int = 0, s: Session = Depends(get_db)):
    m = money_ctx(s)
    qry = s.query(Location)
    if kind: qry = qry.filter(Location.kind == kind)
    if q: qry = qry.filter(Location.name.ilike(f"%{q}%"))
    if not show_inactive: qry = qry.filter(Location.active == True)
    rows = qry.order_by(Location.kind, Location.name).all()
    return render(request, "locations.html", rows=rows, kind=kind, q=q, show_inactive=show_inactive, min_bbl=m["min_bbl"], kinds=KINDS)


@app.get("/locations/new", response_class=HTMLResponse)
def location_new(request: Request, s: Session = Depends(get_db)):
    return render(request, "location_form.html", loc=None, kinds=KINDS)


@app.get("/locations/{loc_id}", response_class=HTMLResponse)
def location_edit(request: Request, loc_id: int, s: Session = Depends(get_db)):
    loc = s.get(Location, loc_id)
    lanes = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(
        or_(Lane.pickup_id == loc_id, Lane.dropoff_id == loc_id)).all()
    return render(request, "location_form.html", loc=loc, kinds=KINDS, lanes=lanes)


@app.post("/locations/save")
async def location_save(request: Request, s: Session = Depends(get_db)):
    f = await request.form()
    loc = s.get(Location, int(f["id"])) if f.get("id") else Location()
    loc.name = f["name"].strip().upper()
    loc.kind = f["kind"]
    loc.lat = float(f["lat"]); loc.lon = float(f["lon"])
    loc.active = bool(f.get("active"))
    loc.avg_bbl_override = fnum(f.get("avg_bbl_override"))
    loc.load_minutes = fint(f.get("load_minutes"))
    loc.open_time = f.get("open_time") or None
    loc.close_time = f.get("close_time") or None
    loc.max_trucks_at_once = fint(f.get("max_trucks_at_once"))
    loc.notes = f.get("notes") or None
    s.add(loc); s.commit()
    return RedirectResponse(f"/locations/{loc.id}?msg=Saved", status_code=303)


# ---------------- Lanes ----------------
@app.get("/lanes", response_class=HTMLResponse)
def lanes(request: Request, q: str = "", show_inactive: int = 0, s: Session = Depends(get_db)):
    m = money_ctx(s)
    qry = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff))
    if not show_inactive: qry = qry.filter(Lane.active == True)
    rows = qry.all()
    if q:
        ql = q.lower()
        rows = [l for l in rows if ql in (l.pickup.name + " " + l.dropoff.name + " " + (l.account or "")).lower()]
    rows.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    view = [dict(l=l, **lane_money(l, m["min_bbl"], m["fsc"])) for l in rows]
    return render(request, "lanes.html", rows=view, q=q, show_inactive=show_inactive, fsc_pct=m["fsc"])


def _lane_form(request, s, lane):
    pickups = s.query(Location).filter(Location.kind.in_(["pickup", "both"]), Location.active == True).order_by(Location.name).all()
    drops = s.query(Location).filter(Location.kind.in_(["dropoff", "both"]), Location.active == True).order_by(Location.name).all()
    return render(request, "lane_form.html", lane=lane, pickups=pickups, drops=drops)


@app.get("/lanes/new", response_class=HTMLResponse)
def lane_new(request: Request, s: Session = Depends(get_db)):
    return _lane_form(request, s, None)


@app.get("/lanes/{lane_id}", response_class=HTMLResponse)
def lane_edit(request: Request, lane_id: int, s: Session = Depends(get_db)):
    return _lane_form(request, s, s.get(Lane, lane_id))


@app.post("/lanes/save")
async def lane_save(request: Request, s: Session = Depends(get_db)):
    f = await request.form()
    lane = s.get(Lane, int(f["id"])) if f.get("id") else Lane()
    lane.account = f.get("account") or None
    lane.operator = f.get("operator") or None
    lane.pickup_id = int(f["pickup_id"]); lane.dropoff_id = int(f["dropoff_id"])
    lane.api_gravity = fnum(f.get("api_gravity"))
    lane.rate = fnum(f.get("rate")); lane.driver_pay = fnum(f.get("driver_pay"))
    lane.active = bool(f.get("active"))
    lane.notes = f.get("notes") or None
    s.add(lane); s.commit()
    return RedirectResponse(f"/lanes/{lane.id}?msg=Saved", status_code=303)


# ---------------- Mileage ----------------
@app.get("/mileage", response_class=HTMLResponse)
def mileage_page(request: Request, q: str = "", only: str = "", s: Session = Depends(get_db)):
    st = settings_dict(s)
    cov = mileage.coverage(s)
    rows = []
    if q or only:
        qry = s.query(Distance).options(joinedload(Distance.origin), joinedload(Distance.dest))
        if only == "override": qry = qry.filter(Distance.override_miles != None)
        if only == "straight": qry = qry.filter(Distance.source == "straight-line")
        rows = qry.all()
        if q:
            ql = q.lower()
            rows = [d for d in rows if ql in d.origin.name.lower() or ql in d.dest.name.lower()]
        rows.sort(key=lambda d: (d.origin.name, d.dest.name))
        rows = rows[:400]
    return render(request, "mileage.html", cov=cov, rows=rows, q=q, only=only, speed=st.get("avg_speed_mph") or 41,
                  has_key=bool(mileage.get_api_key()))


@app.post("/mileage/fetch")
def mileage_fetch(s: Session = Depends(get_db)):
    res = mileage.fetch_from_google(s, mileage.get_api_key())
    if "error" in res:
        return RedirectResponse(f"/mileage?msg={res['error']}", status_code=303)
    msg = f"Google returned {res['fetched']} pairs in {res['requests']} requests. {res['remaining']} still missing"
    msg += " — click Fetch again to continue." if res["remaining"] else "."
    if res["errors"]: msg += f" {len(res['errors'])} note(s): " + " | ".join(res["errors"][:3])
    return RedirectResponse(f"/mileage?msg={msg}", status_code=303)


@app.post("/mileage/straight")
def mileage_straight(s: Session = Depends(get_db)):
    n = mileage.fill_straight_line(s)
    return RedirectResponse(f"/mileage?msg=Estimated+{n}+pairs+as+straight-line+x+{mileage.STRAIGHT_LINE_FACTOR}", status_code=303)


@app.post("/mileage/override")
async def mileage_override(request: Request, s: Session = Depends(get_db)):
    f = await request.form()
    d = s.get(Distance, int(f["id"]))
    d.override_miles = fnum(f.get("override_miles"))
    d.override_minutes = fnum(f.get("override_minutes"))
    d.override_note = f.get("override_note") or None
    s.commit()
    return RedirectResponse(f"/mileage?q={f.get('q','')}&msg=Override+saved", status_code=303)


# ---------------- Drivers ----------------
@app.get("/drivers", response_class=HTMLResponse)
def drivers(request: Request, show_inactive: int = 0, s: Session = Depends(get_db)):
    qry = s.query(Driver).options(joinedload(Driver.yard))
    if not show_inactive: qry = qry.filter(Driver.active == True)
    rows = sorted(qry.all(), key=lambda d: ((d.yard.name if d.yard else ""), d.name))
    st = settings_dict(s)
    return render(request, "drivers.html", rows=rows, show_inactive=show_inactive, st=st, has_samsara=bool(samsara.token()))


@app.post("/drivers/samsara")
def drivers_samsara(s: Session = Depends(get_db)):
    return RedirectResponse(f"/drivers?msg={samsara.sync_drivers(s)}", status_code=303)


@app.post("/day/{plan_date}/samsara")
def day_samsara(plan_date: str, s: Session = Depends(get_db)):
    return RedirectResponse(f"/day/{plan_date}?msg={samsara.pull_hos(s, plan_date)}", status_code=303)


def _driver_form(request, s, drv):
    yards = s.query(Location).filter(Location.kind == "yard", Location.active == True).order_by(Location.name).all()
    st = settings_dict(s)
    return render(request, "driver_form.html", drv=drv, yards=yards, days=DAYS, st=st,
                  days_off=set((drv.days_off or "").split(",")) if drv else set())


@app.get("/drivers/new", response_class=HTMLResponse)
def driver_new(request: Request, s: Session = Depends(get_db)):
    return _driver_form(request, s, None)


@app.get("/drivers/{drv_id}", response_class=HTMLResponse)
def driver_edit(request: Request, drv_id: int, s: Session = Depends(get_db)):
    return _driver_form(request, s, s.get(Driver, drv_id))


@app.post("/drivers/save")
async def driver_save(request: Request, s: Session = Depends(get_db)):
    f = await request.form()
    d = s.get(Driver, int(f["id"])) if f.get("id") else Driver()
    d.name = f["name"].strip()
    d.yard_id = fint(f.get("yard_id"))
    d.active = bool(f.get("active"))
    d.truck = (f.get("truck") or "").strip() or None
    d.usual_shift = f.get("usual_shift") or None
    d.max_drive_hours = fnum(f.get("max_drive_hours"))
    d.max_duty_hours = fnum(f.get("max_duty_hours"))
    d.days_off = ",".join(x for x in DAYS if f.get("off_" + x)) or None
    d.notes = f.get("notes") or None
    s.add(d); s.commit()
    return RedirectResponse("/drivers?msg=Saved", status_code=303)


# ---------------- Daily plan: drivers available + loads called in ----------------
def _day_ctx(s: Session, plan_date: str):
    m = money_ctx(s)
    drivers = s.query(Driver).options(joinedload(Driver.yard)).filter(Driver.active == True).all()
    drivers.sort(key=lambda d: ((d.yard.name if d.yard else ""), d.name))
    dd = {x.driver_id: x for x in s.query(DriverDay).filter(DriverDay.plan_date == plan_date).all()}
    weekday = DAYS[datetime.strptime(plan_date, "%Y-%m-%d").weekday()]
    drv_rows = []
    for d in drivers:
        x = dd.get(d.id)
        off_today = weekday in (d.days_off or "").split(",")
        drv_rows.append(dict(d=d, x=x, available=(x.available if x else not off_today), shift=(x.shift if x else (d.usual_shift or "AM")),
                             off_today=off_today))
    loads = s.query(LoadRequest).options(joinedload(LoadRequest.lane).joinedload(Lane.pickup),
                                         joinedload(LoadRequest.lane).joinedload(Lane.dropoff)) \
        .filter(LoadRequest.plan_date == plan_date, LoadRequest.status != "cancelled").all()
    loads.sort(key=lambda l: (PRIORITIES.index(l.priority or "normal"), l.lane.pickup.name))
    load_rows = []
    tot = dict(count=0, rev=0.0, rev_fsc=0.0, pay=0.0)
    for l in loads:
        mm = lane_money(l.lane, m["min_bbl"], m["fsc"], bbl=l.bbl_override)
        load_rows.append(dict(l=l, **mm))
        tot["count"] += l.count; tot["rev"] += mm["rev"] * l.count; tot["rev_fsc"] += mm["rev_fsc"] * l.count; tot["pay"] += mm["pay"] * l.count
    lanes_all = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.active == True).all()
    lanes_all.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    d0 = datetime.strptime(plan_date, "%Y-%m-%d").date()
    latest = s.query(Plan).filter(Plan.plan_date == plan_date).order_by(Plan.id.desc()).first()
    plan = json.loads(latest.result_json) if latest else None
    return dict(plan=plan, plan_row=latest, plan_date=plan_date, weekday=weekday, drv_rows=drv_rows, load_rows=load_rows, tot=tot, lanes_all=lanes_all,
                prev=(d0 - timedelta(days=1)).isoformat(), next=(d0 + timedelta(days=1)).isoformat(), fsc_pct=m["fsc"],
                avail=sum(1 for r in drv_rows if r["available"]), priorities=PRIORITIES, st=m["st"],
                has_samsara=bool(samsara.token()))


@app.get("/day", response_class=HTMLResponse)
def day_redirect():
    return RedirectResponse(f"/day/{date.today().isoformat()}", status_code=303)


@app.get("/day/{plan_date}", response_class=HTMLResponse)
def day_page(request: Request, plan_date: str, s: Session = Depends(get_db)):
    return render(request, "day.html", **_day_ctx(s, plan_date))


@app.post("/day/{plan_date}/drivers")
async def day_drivers_save(request: Request, plan_date: str, s: Session = Depends(get_db)):
    f = await request.form()
    existing = {x.driver_id: x for x in s.query(DriverDay).filter(DriverDay.plan_date == plan_date).all()}
    for d in s.query(Driver).filter(Driver.active == True).all():
        x = existing.get(d.id) or DriverDay(plan_date=plan_date, driver_id=d.id)
        x.available = bool(f.get(f"avail_{d.id}"))
        x.shift = f.get(f"shift_{d.id}") or "AM"
        x.start_time = f.get(f"start_{d.id}") or None
        x.drive_hours_left = fnum(f.get(f"drive_{d.id}"))
        x.duty_hours_left = fnum(f.get(f"duty_{d.id}"))
        x.cycle_hours_left = fnum(f.get(f"cycle_{d.id}"))
        x.notes = f.get(f"dnote_{d.id}") or None
        s.add(x)
    s.commit()
    return RedirectResponse(f"/day/{plan_date}?msg=Driver+availability+saved", status_code=303)


@app.post("/day/{plan_date}/loads/add")
async def day_load_add(request: Request, plan_date: str, s: Session = Depends(get_db)):
    f = await request.form()
    l = LoadRequest(plan_date=plan_date, lane_id=int(f["lane_id"]), count=fint(f.get("count")) or 1,
                    priority=f.get("priority") or "normal", must_go_by=f.get("must_go_by") or None,
                    earliest_pickup=f.get("earliest_pickup") or None, latest_pickup=f.get("latest_pickup") or None,
                    bbl_override=fnum(f.get("bbl_override")), notes=f.get("notes") or None)
    s.add(l); s.commit()
    return RedirectResponse(f"/day/{plan_date}?msg=Load+added", status_code=303)


@app.post("/day/{plan_date}/loads/{load_id}")
async def day_load_update(request: Request, plan_date: str, load_id: int, s: Session = Depends(get_db)):
    f = await request.form()
    l = s.get(LoadRequest, load_id)
    action = f.get("action")
    if action == "delete":
        s.delete(l)
    elif action == "move":
        l.plan_date = f.get("new_date") or l.plan_date
    else:
        l.count = fint(f.get("count")) or 1
        l.priority = f.get("priority") or "normal"
        l.must_go_by = f.get("must_go_by") or None
        l.earliest_pickup = f.get("earliest_pickup") or None
        l.latest_pickup = f.get("latest_pickup") or None
        l.bbl_override = fnum(f.get("bbl_override"))
        l.status = f.get("status") or l.status
        l.notes = f.get("notes") or None
    s.commit()
    return RedirectResponse(f"/day/{plan_date}?msg=Updated", status_code=303)


@app.post("/day/{plan_date}/plan")
def day_plan(plan_date: str, s: Session = Depends(get_db)):
    res = optimizer.solve(s, plan_date)
    if not res.get("ok"):
        return RedirectResponse(f"/day/{plan_date}?msg={res['error']}", status_code=303)
    optimizer.save_plan(s, res)
    t = res["totals"]
    msg = f"Plan built: {t['loads']} loads on {t['drivers_used']} drivers, ${t['revenue']:,.0f} base revenue, ${t['per_hour']:,.0f}/hr"
    if t["unassigned"]: msg += f" — {t['unassigned']} load(s) could not be fitted" + (f" ({t['unassigned_must']} MUST)" if t["unassigned_must"] else "")
    return RedirectResponse(f"/day/{plan_date}?msg={msg}#plan", status_code=303)


@app.get("/plan/{plan_id}/print", response_class=HTMLResponse)
def plan_print(request: Request, plan_id: int, s: Session = Depends(get_db)):
    row = s.get(Plan, plan_id)
    return render(request, "plan_print.html", plan=json.loads(row.result_json), plan_row=row)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
