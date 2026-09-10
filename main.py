"""Petrol Dispatch Optimizer — web app.

Phase 1: master data, settings, mileage cache.   Phase 2: login, fuel surcharge + EIA price, drivers, daily load board.
"""
import os, hashlib, re
from datetime import date, datetime, timedelta
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from db import (SessionLocal, Setting, Location, Lane, Distance, Driver, DriverDay, LoadRequest, Plan, StandingOrder, CompanyInfo, JmpDoc,
                Company, CompanyLaneRate, Tank, LocationRestriction, Load, LoadEvent, init_db)
from seed import seed, ensure_companies, import_drivers, petrol_company
import mileage, fsc, optimizer, samsara, jmp
import urgency as urg
import loads as ldm
from fastapi.responses import Response
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
PRIORITIES = urg.PRIORITIES          # today | tomorrow | flex


@app.on_event("startup")
def _startup():
    init_db()
    print("seed:", seed())
    with SessionLocal() as s0:
        n = ldm.sync_all(s0)
        if n: print(f"load tracking: created tracking rows for {n} existing load line(s)")


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
    rows = [r for r in s.query(Setting).order_by(Setting.sort).all() if not r.key.startswith("eia_") and r.key != "drivers_imported"]
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
    return render(request, "locations.html", rows=rows, kind=kind, q=q, show_inactive=show_inactive, min_bbl=m["min_bbl"], kinds=KINDS,
                  inactive_count=s.query(Location).filter(Location.active == False).count())


@app.get("/locations/new", response_class=HTMLResponse)
def location_new(request: Request, s: Session = Depends(get_db)):
    return render(request, "location_form.html", loc=None, kinds=KINDS)


@app.get("/locations/{loc_id}", response_class=HTMLResponse)
def location_edit(request: Request, loc_id: int, s: Session = Depends(get_db)):
    loc = s.get(Location, loc_id)
    lanes = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(
        or_(Lane.pickup_id == loc_id, Lane.dropoff_id == loc_id)).all()
    tanks = s.query(Tank).filter(Tank.location_id == loc_id).order_by(Tank.name).all()
    restr = s.query(LocationRestriction).options(joinedload(LocationRestriction.driver)).filter(LocationRestriction.location_id == loc_id).all()
    drivers = sorted(s.query(Driver).options(joinedload(Driver.company)).filter(Driver.active == True).all(), key=lambda d: d.name)
    trucks = sorted({(d.truck or "").strip().upper() for d in drivers if d.truck})
    return render(request, "location_form.html", loc=loc, kinds=KINDS, lanes=lanes, tanks=tanks, restr=restr, drivers=drivers, trucks=trucks)


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
    loc.requires_gauging = bool(f.get("requires_gauging"))
    loc.notes = f.get("notes") or None
    s.add(loc); s.commit()
    return RedirectResponse(f"/locations/{loc.id}?msg=Saved", status_code=303)


@app.post("/locations/{loc_id}/tank")
async def location_tank(request: Request, loc_id: int, s: Session = Depends(get_db)):
    f = await request.form()
    if f.get("action") == "delete":
        tk = s.get(Tank, int(f["tank_id"]))
        if tk:
            in_use = s.query(LoadRequest).filter(LoadRequest.tank_id == tk.id).count() + s.query(StandingOrder).filter(StandingOrder.tank_id == tk.id).count()
            if in_use: tk.active = False
            else: s.delete(tk)
            s.commit()
        return RedirectResponse(f"/locations/{loc_id}?msg=Tank+removed#tanks", status_code=303)
    name = (f.get("name") or "").strip()
    if not name: return RedirectResponse(f"/locations/{loc_id}?msg=Type+a+tank+name#tanks", status_code=303)
    if not s.query(Tank).filter(Tank.location_id == loc_id, Tank.name == name).first():
        s.add(Tank(location_id=loc_id, name=name, notes=(f.get("notes") or "").strip() or None)); s.commit()
    return RedirectResponse(f"/locations/{loc_id}?msg=Tank+{name}+added#tanks", status_code=303)


@app.post("/locations/{loc_id}/restriction")
async def location_restriction(request: Request, loc_id: int, s: Session = Depends(get_db)):
    f = await request.form()
    if f.get("action") == "delete":
        r = s.get(LocationRestriction, int(f["rid"]))
        if r: s.delete(r); s.commit()
        return RedirectResponse(f"/locations/{loc_id}?msg=Restriction+removed#restrictions", status_code=303)
    drv_id, truck = fint(f.get("driver_id")), (f.get("truck") or "").strip().upper() or None
    if not drv_id and not truck:
        return RedirectResponse(f"/locations/{loc_id}?msg=Pick+a+driver+or+a+truck#restrictions", status_code=303)
    s.add(LocationRestriction(location_id=loc_id, driver_id=drv_id, truck=truck, reason=(f.get("reason") or "").strip() or None)); s.commit()
    return RedirectResponse(f"/locations/{loc_id}?msg=Restriction+added#restrictions", status_code=303)


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
    approved = {(d.origin_id, d.dest_id) for d in s.query(Distance).filter(Distance.approved == True).all()}
    jmp_v = {}
    for doc in s.query(JmpDoc).order_by(JmpDoc.id).all(): jmp_v[doc.lane_id] = doc.version
    view = [dict(l=l, route_ok=(l.pickup_id, l.dropoff_id) in approved, jmp=jmp_v.get(l.id), **lane_money(l, m["min_bbl"], m["fsc"])) for l in rows]
    return render(request, "lanes.html", rows=view, q=q, show_inactive=show_inactive, fsc_pct=m["fsc"],
                  inactive_count=s.query(Lane).filter(Lane.active == False).count())


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
        if only == "approved": qry = qry.filter(Distance.approved == True)
        if only == "straight": qry = qry.filter(Distance.source == "straight-line")
        rows = qry.all()
        if q:
            ql = q.lower()
            rows = [d for d in rows if ql in d.origin.name.lower() or ql in d.dest.name.lower()]
        rows.sort(key=lambda d: (d.origin.name, d.dest.name))
        rows = rows[:400]
    return render(request, "mileage.html", cov=cov, rows=rows, q=q, only=only, speed=st.get("avg_speed_mph") or 41,
                  has_key=bool(mileage.get_api_key()), approved_count=s.query(Distance).filter(Distance.approved == True).count(),
                  has_browser_key=bool(os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "").strip()))


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


# ---------------- Route approval (per leg) ----------------
def _dist_row(s: Session, a: int, b: int, create=True):
    d = s.query(Distance).filter_by(origin_id=a, dest_id=b).first()
    if not d and create:
        d = Distance(origin_id=a, dest_id=b); s.add(d); s.flush()
    return d


@app.get("/route/{a}/{b}", response_class=HTMLResponse)
def route_page(request: Request, a: int, b: int, back: str = "", s: Session = Depends(get_db)):
    o, d = s.get(Location, a), s.get(Location, b)
    if not o or not d:
        return RedirectResponse("/mileage?msg=Unknown+location", status_code=303)
    dist = _dist_row(s, a, b, create=False)
    st = settings_dict(s)
    via = dist.via_json if dist and dist.via_json else "[]"
    back = back.replace("%23", "#")
    return render(request, "route.html", o=o, d=d, dist=dist, via_json=via, back=back, from_plan=_plan_date_from_back(back),
                  solver_seconds=int(st.get("solver_seconds") or 20),
                  browser_key=os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "").strip(), speed=st.get("avg_speed_mph") or 41)


def _back_url(back: str, msg: str) -> str:
    """Append ?msg= to a return URL, keeping any #fragment at the end where browsers expect it."""
    back = back.replace("%23", "#")                       # an over-encoded '#' would otherwise become part of the date
    base, frag = (back.split("#", 1) + [""])[:2]
    url = f"{base}{'&' if '?' in base else '?'}msg={msg}"
    return url + ("#" + frag if frag else "")


def _plan_date_from_back(back: str):
    """'/day/2026-09-10#plan' -> '2026-09-10' when the approval came from a built plan (so we can rebuild it)."""
    m = re.match(r"^/day/(\d{4}-\d{2}-\d{2})", (back or "").replace("%23", "#"))
    return m.group(1) if m else None


def _after_approve(s, back: str, msg: str, rebuild: bool):
    """Return to where the dispatcher came from; if they asked, rebuild that day's plan with the new miles first."""
    pd = _plan_date_from_back(back) if rebuild else None
    if pd:
        res = optimizer.solve(s, pd)
        if res.get("ok"):
            optimizer.save_plan(s, res)
            ldm.record_plan(s, res); s.commit()
            t = res["totals"]
            msg += f". Plan rebuilt: {t['loads']} loads on {t['drivers_used']} drivers"
            if t["unassigned"]: msg += f", {t['unassigned']} not fitted"
        else:
            msg += f". Plan NOT rebuilt: {res.get('error')}"
    return RedirectResponse(_back_url(back or "/mileage", msg), status_code=303)


def _approve(s, a, b, miles, minutes, kind, via, poly, note, steps=None):
    d = _dist_row(s, a, b)
    if steps is not None: d.steps_json = steps or None
    d.override_miles = miles; d.override_minutes = minutes
    d.approved = True; d.approved_at = datetime.utcnow(); d.route_kind = kind
    d.via_json = via or None; d.polyline = poly or None
    if note is not None: d.override_note = note or None
    if d.google_miles is None: d.google_miles, d.source = miles, "google"


@app.post("/route/{a}/{b}/approve")
async def route_approve(request: Request, a: int, b: int, s: Session = Depends(get_db)):
    f = await request.form()
    miles, mins = fnum(f.get("miles")), fnum(f.get("minutes"))
    if not miles:
        return RedirectResponse(f"/route/{a}/{b}?msg=No+route+to+approve+yet", status_code=303)
    _approve(s, a, b, miles, mins, f.get("kind") or "google-default", f.get("via"), f.get("polyline"), f.get("note"), f.get("steps"))
    msg = f"Approved: {miles} mi"
    if f.get("reverse") and fnum(f.get("rev_miles")):
        _approve(s, b, a, fnum(f.get("rev_miles")), fnum(f.get("rev_minutes")), f.get("kind") or "google-default",
                 f.get("rev_via"), f.get("rev_polyline"), f.get("note"), f.get("rev_steps"))
        msg += f" (reverse {fnum(f.get('rev_miles'))} mi)"
    s.commit()
    return _after_approve(s, f.get("back") or f"/route/{a}/{b}", msg, bool(f.get("rebuild")))


@app.post("/route/{a}/{b}/manual")
async def route_manual(request: Request, a: int, b: int, s: Session = Depends(get_db)):
    f = await request.form()
    miles = fnum(f.get("miles"))
    if not miles:
        return RedirectResponse(f"/route/{a}/{b}?msg=Enter+the+miles+first", status_code=303)
    _approve(s, a, b, miles, fnum(f.get("minutes")), "manual-miles", None, None, None)
    if f.get("reverse"): _approve(s, b, a, miles, fnum(f.get("minutes")), "manual-miles", None, None, None)
    s.commit()
    return _after_approve(s, f.get("back") or f"/route/{a}/{b}", f"Approved {miles} mi (typed)", bool(f.get("rebuild")))


@app.post("/route/{a}/{b}/unapprove")
async def route_unapprove(request: Request, a: int, b: int, s: Session = Depends(get_db)):
    f = await request.form()
    d = _dist_row(s, a, b, create=False)
    if d:
        d.approved = False; d.approved_at = None; d.route_kind = None; d.via_json = None; d.polyline = None
        d.override_miles = None; d.override_minutes = None
        s.commit()
    return _after_approve(s, f.get("back") or f"/route/{a}/{b}", "Approval cleared", bool(f.get("rebuild")))


# ---------------- Journey Management Plans ----------------
@app.get("/lanes/{lane_id}/jmp", response_class=HTMLResponse)
def lane_jmp(request: Request, lane_id: int, s: Session = Depends(get_db)):
    lane = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).get(lane_id)
    d = jmp.leg(s, lane.pickup_id, lane.dropoff_id)
    yards = s.query(Location).filter(Location.kind == "yard", Location.active == True).order_by(Location.name).all()
    yard_legs = {y.id: jmp.leg(s, y.id, lane.pickup_id) for y in yards}
    docs = s.query(JmpDoc).filter_by(lane_id=lane_id).order_by(JmpDoc.id.desc()).all()
    hazards = jmp.hazards_of(lane)
    while len(hazards) < 6: hazards.append({})
    return render(request, "lane_jmp.html", lane=lane, d=d, yards=yards, yard_legs=yard_legs, docs=docs, hazards=hazards,
                  company=jmp.company(s))


@app.post("/lanes/{lane_id}/jmp/save")
async def lane_jmp_save(request: Request, lane_id: int, s: Session = Depends(get_db)):
    f = await request.form()
    lane = s.get(Lane, lane_id)
    hz = []
    for i in range(12):
        h = (f.get(f"h_{i}") or "").strip()
        if h: hz.append(dict(hazard=h, location=(f.get(f"l_{i}") or "").strip(), control=(f.get(f"c_{i}") or "").strip()))
    lane.jmp_hazards = json.dumps(hz) if hz else None
    lane.jmp_rest_stop = (f.get("rest_stop") or "").strip() or None
    lane.jmp_notes = (f.get("jmp_notes") or "").strip() or None
    lane.product = (f.get("product") or "").strip() or None
    s.commit()
    return RedirectResponse(f"/lanes/{lane_id}/jmp?msg=Saved", status_code=303)


@app.post("/lanes/{lane_id}/jmp/generate")
async def lane_jmp_generate(request: Request, lane_id: int, s: Session = Depends(get_db)):
    f = await request.form()
    lane = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).get(lane_id)
    yard = s.get(Location, int(f["yard_id"])) if f.get("yard_id") else None
    try:
        pdf, summary = jmp.build_pdf(s, lane, yard, mileage.get_api_key())
    except Exception as e:
        return RedirectResponse(f"/lanes/{lane_id}/jmp?msg=Could+not+build+the+PDF:+{type(e).__name__}+{str(e)[:120]}", status_code=303)
    doc = JmpDoc(lane_id=lane_id, version=s.query(JmpDoc).filter_by(lane_id=lane_id).count() + 1,
                 include_yard_id=yard.id if yard else None, summary=summary, pdf=pdf)
    s.add(doc); s.commit()
    return RedirectResponse(f"/lanes/{lane_id}/jmp?msg=JMP+{summary}+generated", status_code=303)


@app.get("/jmp/{doc_id}.pdf")
def jmp_pdf(doc_id: int, download: int = 0, s: Session = Depends(get_db)):
    doc = s.query(JmpDoc).options(joinedload(JmpDoc.lane).joinedload(Lane.pickup), joinedload(JmpDoc.lane).joinedload(Lane.dropoff)).get(doc_id)
    if not doc: return RedirectResponse("/lanes?msg=JMP+not+found", status_code=303)
    safe = lambda x: "".join(ch if ch.isalnum() else "_" for ch in x)[:40]
    fname = f"JMP_v{doc.version}_{safe(doc.lane.pickup.name)}_to_{safe(doc.lane.dropoff.name)}.pdf"
    disp = ("attachment" if download else "inline") + f'; filename="{fname}"'
    return Response(content=doc.pdf, media_type="application/pdf", headers={"Content-Disposition": disp})


@app.post("/jmp/{doc_id}/delete")
def jmp_delete(doc_id: int, s: Session = Depends(get_db)):
    doc = s.get(JmpDoc, doc_id); lane_id = doc.lane_id if doc else None
    if doc: s.delete(doc); s.commit()
    return RedirectResponse(f"/lanes/{lane_id}/jmp?msg=Deleted" if lane_id else "/lanes", status_code=303)


@app.get("/jmps", response_class=HTMLResponse)
def jmp_list(request: Request, s: Session = Depends(get_db)):
    docs = s.query(JmpDoc).options(joinedload(JmpDoc.lane).joinedload(Lane.pickup), joinedload(JmpDoc.lane).joinedload(Lane.dropoff)).order_by(JmpDoc.id.desc()).all()
    latest = {}
    for d in docs: latest.setdefault(d.lane_id, d)
    lanes_all = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.active == True).all()
    lanes_all.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    return render(request, "jmps.html", latest=latest, lanes_all=lanes_all, company=jmp.company(s))


@app.get("/company", response_class=HTMLResponse)
def company_page(request: Request, s: Session = Depends(get_db)):
    return render(request, "company.html", c=jmp.company(s))


@app.post("/company")
async def company_save(request: Request, s: Session = Depends(get_db)):
    f = await request.form(); c = jmp.company(s)
    for k in ["name", "address", "dispatch_phone", "emergency_phone", "safety_contact", "safety_phone", "spill_response",
              "checkin_rule", "overdue_rule", "prepared_by_title", "approved_by_title"]:
        setattr(c, k, (f.get(k) or "").strip() or None)
    c.require_manager_signature = bool(f.get("require_manager_signature"))
    s.commit()
    return RedirectResponse("/company?msg=Company+details+saved", status_code=303)


# ---------------- Companies (Petrol + sub-haulers) ----------------
def _companies(s):
    ensure_companies(s)
    return sorted(s.query(Company).all(), key=lambda c: (not c.is_petrol, c.name.lower()))


@app.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, s: Session = Depends(get_db)):
    cos = _companies(s)
    counts = {}
    for d in s.query(Driver).filter(Driver.active == True).all():
        counts[d.company_id] = counts.get(d.company_id, 0) + 1
    petrol = petrol_company(s)
    counts[petrol.id] = counts.get(petrol.id, 0) + counts.pop(None, 0)      # drivers with no company = Petrol
    rates = s.query(CompanyLaneRate).options(joinedload(CompanyLaneRate.lane).joinedload(Lane.pickup),
                                             joinedload(CompanyLaneRate.lane).joinedload(Lane.dropoff)).all()
    rates_by_co = {}
    for r in rates: rates_by_co.setdefault(r.company_id, []).append(r)
    lanes_all = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.active == True).all()
    lanes_all.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    return render(request, "companies.html", cos=cos, counts=counts, rates_by_co=rates_by_co, lanes_all=lanes_all)


@app.post("/companies/save")
async def companies_save(request: Request, s: Session = Depends(get_db)):
    f = await request.form()
    c = s.get(Company, int(f["id"])) if f.get("id") else Company(is_petrol=False)
    name = (f.get("name") or "").strip()
    if not name: return RedirectResponse("/companies?msg=Company+name+is+required", status_code=303)
    c.name = name
    c.short_name = (f.get("short_name") or "").strip() or None
    c.dispatch_phone = (f.get("dispatch_phone") or "").strip() or None
    c.notes = (f.get("notes") or "").strip() or None
    c.has_samsara = True if c.is_petrol else bool(f.get("has_samsara"))
    c.active = bool(f.get("active")) if f.get("id") else True
    if not c.is_petrol:
        pct = fnum(f.get("share_pct"))
        if pct is None or not (0 < pct <= 100):
            return RedirectResponse("/companies?msg=Enter+the+sub-hauler's+share+as+a+percent+between+1+and+100", status_code=303)
        c.share_pct = pct
        c.petrol_owned = bool(f.get("petrol_owned"))
        c.priority = fint(f.get("priority")) or (1 if c.petrol_owned else 2)
    else:
        c.priority = 1
    s.add(c); s.commit()
    return RedirectResponse(f"/companies?msg=Saved+{c.name}", status_code=303)


@app.post("/companies/{co_id}/lane")
async def company_lane_rate(request: Request, co_id: int, s: Session = Depends(get_db)):
    f = await request.form()
    lane_id = fint(f.get("lane_id"))
    if f.get("action") == "delete":
        s.query(CompanyLaneRate).filter(CompanyLaneRate.company_id == co_id, CompanyLaneRate.lane_id == lane_id).delete()
        s.commit()
        return RedirectResponse("/companies?msg=Special+rate+removed", status_code=303)
    pct, flat = fnum(f.get("share_pct")), fnum(f.get("flat_bbl"))
    if not lane_id or (pct is None and flat is None):
        return RedirectResponse("/companies?msg=Pick+a+lane+and+enter+either+a+%25+or+a+$/bbl", status_code=303)
    r = s.query(CompanyLaneRate).filter(CompanyLaneRate.company_id == co_id, CompanyLaneRate.lane_id == lane_id).first() \
        or CompanyLaneRate(company_id=co_id, lane_id=lane_id)
    r.share_pct = pct if flat is None else None
    r.flat_bbl = flat
    r.notes = (f.get("notes") or "").strip() or None
    s.add(r); s.commit()
    return RedirectResponse("/companies?msg=Special+lane+rate+saved", status_code=303)


# ---------------- Drivers ----------------
@app.get("/drivers", response_class=HTMLResponse)
def drivers(request: Request, show_inactive: int = 0, company: str = "", s: Session = Depends(get_db)):
    cos = _companies(s)
    petrol = petrol_company(s)
    qry = s.query(Driver).options(joinedload(Driver.yard), joinedload(Driver.company))
    if not show_inactive: qry = qry.filter(Driver.active == True)
    rows = qry.all()
    if company == "petrol": rows = [d for d in rows if not d.is_sub]
    elif company == "subs": rows = [d for d in rows if d.is_sub]
    elif company.isdigit(): rows = [d for d in rows if d.company_id == int(company)]
    rows.sort(key=lambda d: (d.is_sub, (d.company.name if d.company else ""), (d.yard.name if d.yard else ""), d.name))
    st = settings_dict(s)
    return render(request, "drivers.html", rows=rows, show_inactive=show_inactive, company=company, cos=cos, petrol=petrol, st=st,
                  has_samsara=bool(samsara.token()), inactive_count=s.query(Driver).filter(Driver.active == False).count(),
                  no_yard=sum(1 for d in rows if not d.yard and d.active))


@app.post("/drivers/import")
def drivers_import(s: Session = Depends(get_db)):
    return RedirectResponse(f"/drivers?show_inactive=1&msg={import_drivers(s)}+Set+each+new+driver's+home+yard.", status_code=303)


@app.post("/drivers/{drv_id}/toggle")
def driver_toggle(drv_id: int, s: Session = Depends(get_db)):
    d = s.get(Driver, drv_id)
    d.active = not d.active
    s.commit()
    return RedirectResponse(f"/drivers?show_inactive=1&msg={d.name}+is+now+{'ACTIVE' if d.active else 'INACTIVE'}", status_code=303)


@app.post("/drivers/samsara")
def drivers_samsara(s: Session = Depends(get_db)):
    return RedirectResponse(f"/drivers?msg={samsara.sync_drivers(s)}", status_code=303)


@app.post("/day/{plan_date}/samsara")
def day_samsara(plan_date: str, s: Session = Depends(get_db)):
    return RedirectResponse(f"/day/{plan_date}?msg={samsara.pull_hos(s, plan_date)}#drivers", status_code=303)


def _driver_form(request, s, drv):
    yards = s.query(Location).filter(Location.kind == "yard", Location.active == True).order_by(Location.name).all()
    st = settings_dict(s)
    return render(request, "driver_form.html", drv=drv, yards=yards, days=DAYS, st=st, cos=_companies(s), petrol=petrol_company(s),
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
    d.company_id = fint(f.get("company_id")) or petrol_company(s).id
    d.active = bool(f.get("active"))
    d.can_gauge = bool(f.get("can_gauge"))
    d.truck = (f.get("truck") or "").strip() or None
    d.usual_shift = f.get("usual_shift") or None
    d.max_drive_hours = fnum(f.get("max_drive_hours"))
    d.max_duty_hours = fnum(f.get("max_duty_hours"))
    d.days_off = ",".join(x for x in DAYS if f.get("off_" + x)) or None
    d.notes = f.get("notes") or None
    s.add(d); s.commit()
    s.refresh(d)
    yard = s.get(Location, d.yard_id).name if d.yard_id else "NO YARD"
    co = s.get(Company, d.company_id)
    return RedirectResponse(f"/drivers?msg=Saved+{d.name}:+{co.name if co else 'Petrol'},+yard+{yard},+shift+{d.usual_shift or '-'},+truck+{d.truck or '-'},+days+off+{d.days_off or 'none'}", status_code=303)


# ---------------- Daily plan: drivers available + loads called in ----------------
def apply_standing_orders(s: Session, plan_date: str) -> int:
    """Create this day's loads from active standing orders (once per order per day). Returns how many were added."""
    weekday = DAYS[datetime.strptime(plan_date, "%Y-%m-%d").weekday()]
    flex_days = int(settings_dict(s).get("flex_days") or urg.DEFAULT_FLEX_DAYS)
    done = {l.standing_order_id for l in s.query(LoadRequest).filter(LoadRequest.plan_date == plan_date,
                                                                      LoadRequest.standing_order_id != None).all()}
    n = 0
    for so in s.query(StandingOrder).filter(StandingOrder.active == True).all():
        if so.id in done or weekday not in (so.days or "").split(","): continue
        if so.start_date and plan_date < so.start_date: continue
        if so.end_date and plan_date > so.end_date: continue
        pri = urg.norm(so.priority)
        line = LoadRequest(plan_date=plan_date, lane_id=so.lane_id, count=so.count or 1, priority=pri,
                           must_go_by=urg.deadline_for(pri, plan_date, flex_days), earliest_pickup=so.earliest_pickup,
                           latest_pickup=so.latest_pickup, standing_order_id=so.id, notes=so.notes, tank_id=so.tank_id)
        s.add(line); s.flush()
        ldm.sync_line(s, line, plan_date, note=f"standing order for {plan_date}")
        n += 1
    if n: s.commit()
    return n


def annotate_legs(s: Session, plan: dict):
    """Mark every leg of every shift with its route-approval status (and count unreviewed legs)."""
    approved = {(d.origin_id, d.dest_id): d for d in s.query(Distance).filter(Distance.approved == True).all()}
    unreviewed, seen = 0, set()
    for sh in plan.get("shifts", []):
        prev = None
        for st in sh.get("stops", []):
            lid = st.get("loc_id")
            if prev is not None and lid is not None and prev != lid:
                d = approved.get((prev, lid))
                st["leg_from"] = prev
                st["leg_ok"] = bool(d)
                st["leg_kind"] = d.route_kind if d else None
                if not d and (prev, lid) not in seen:
                    unreviewed += 1; seen.add((prev, lid))
            prev = lid
    plan["unreviewed_legs"] = unreviewed


def _day_ctx(s: Session, plan_date: str):
    added = apply_standing_orders(s, plan_date)
    m = money_ctx(s)
    drivers = s.query(Driver).options(joinedload(Driver.yard), joinedload(Driver.company)).filter(Driver.active == True).all()
    drivers.sort(key=lambda d: (d.is_sub, (d.company.name if d.company else ""), (d.yard.name if d.yard else ""), d.name))
    dd = {x.driver_id: x for x in s.query(DriverDay).filter(DriverDay.plan_date == plan_date).all()}
    weekday = DAYS[datetime.strptime(plan_date, "%Y-%m-%d").weekday()]
    drv_rows = []
    for d in drivers:
        x = dd.get(d.id)
        off_today = weekday in (d.days_off or "").split(",")
        call = d.is_sub and not d.company.has_samsara          # no Samsara: dispatch phones the sub for hours
        drv_rows.append(dict(d=d, x=x, available=(x.available if x else not off_today), shift=(x.shift if x else (d.usual_shift or "AM")),
                             off_today=off_today, call=call, phone=(d.company.dispatch_phone if call else None)))
    all_lines = s.query(LoadRequest).options(joinedload(LoadRequest.lane).joinedload(Lane.pickup),
                                             joinedload(LoadRequest.lane).joinedload(Lane.dropoff), joinedload(LoadRequest.tank)) \
        .filter(LoadRequest.plan_date == plan_date, LoadRequest.status.notin_(["cancelled", "removed"])).all()
    loads = [l for l in all_lines if l.status in ldm.LINE_OPEN]
    done_lines = [l for l in all_lines if l.status not in ldm.LINE_OPEN]
    sums = {l.id: ldm.summary(s, l) for l in all_lines}
    tanks_by_loc = {}
    for tk in s.query(Tank).filter(Tank.active == True).order_by(Tank.name).all():
        tanks_by_loc.setdefault(tk.location_id, []).append(tk)
    gaugers_today = [r["d"].name for r in drv_rows if r["available"] and r["d"].can_gauge]
    needs_gauge = any(l.lane.pickup.requires_gauging or l.gauge in ("haul", "only") for l in loads)
    flex_days = int(m["st"].get("flex_days") or urg.DEFAULT_FLEX_DAYS)
    load_rows = []
    tot = dict(count=0, rev=0.0, rev_fsc=0.0, pay=0.0)
    for l in loads:
        mm = lane_money(l.lane, m["min_bbl"], m["fsc"], bbl=l.bbl_override)
        load_rows.append(dict(l=l, u=urg.urgency(l.must_go_by, l.priority, plan_date, flex_days), sm=sums.get(l.id, {}), **mm))
        tot["count"] += l.count; tot["rev"] += mm["rev"] * l.count; tot["rev_fsc"] += mm["rev_fsc"] * l.count; tot["pay"] += mm["pay"] * l.count
    load_rows.sort(key=lambda r: (r["u"]["days_left"], r["l"].lane.pickup.name))
    # open loads left on earlier days (not marked hauled/cancelled) — offer to bring them forward to this day
    leftover = s.query(LoadRequest).options(joinedload(LoadRequest.lane).joinedload(Lane.pickup), joinedload(LoadRequest.lane).joinedload(Lane.dropoff)) \
        .filter(LoadRequest.plan_date < plan_date, LoadRequest.plan_date >= (datetime.strptime(plan_date, "%Y-%m-%d").date() - timedelta(days=14)).isoformat(),
                LoadRequest.status.in_(ldm.LINE_OPEN), LoadRequest.count > 0).order_by(LoadRequest.plan_date).all()
    done_rows = [dict(l=l, sm=sums.get(l.id, {})) for l in done_lines]
    active_drivers = sorted(s.query(Driver).filter(Driver.active == True).all(), key=lambda d: d.name)
    lanes_all = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.active == True).all()
    lanes_all.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    d0 = datetime.strptime(plan_date, "%Y-%m-%d").date()
    latest = s.query(Plan).filter(Plan.plan_date == plan_date).order_by(Plan.id.desc()).first()
    plan = json.loads(latest.result_json) if latest else None
    if plan: annotate_legs(s, plan)
    return dict(plan=plan, plan_row=latest, plan_date=plan_date, standing_added=added,
                today=date.today().isoformat(), tomorrow=(date.today() + timedelta(days=1)).isoformat(),
                weekday_long=d0.strftime("%A"), pretty_date=d0.strftime("%B %-d, %Y"), short_date=d0.strftime("%b %-d"), weekday=weekday, drv_rows=drv_rows, load_rows=load_rows, tot=tot, lanes_all=lanes_all,
                prev=(d0 - timedelta(days=1)).isoformat(), next=(d0 + timedelta(days=1)).isoformat(), fsc_pct=m["fsc"],
                avail=sum(1 for r in drv_rows if r["available"]), priorities=PRIORITIES, st=m["st"], flex_days=flex_days, leftover=leftover,
                done_rows=done_rows, active_drivers=active_drivers,
                has_samsara=bool(samsara.token()), tanks_by_loc=tanks_by_loc, gaugers_today=gaugers_today, needs_gauge=needs_gauge,
                lane_tanks_json=json.dumps({ln.id: [[tk.id, tk.name] for tk in tanks_by_loc.get(ln.pickup_id, [])] for ln in lanes_all}),
                lane_gauge_json=json.dumps({ln.id: bool(ln.pickup.requires_gauging) for ln in lanes_all}))



# ---------------- Load Log (every load, its tracking number and history) ----------------
LOG_LIMIT = 1000


def _loadlog_query(s: Session, from_: str, to: str, by: str, status: str, q: str, line: int | None):
    qry = s.query(Load).options(joinedload(Load.lane).joinedload(Lane.pickup), joinedload(Load.lane).joinedload(Lane.dropoff),
                                joinedload(Load.tank), joinedload(Load.planned_driver), joinedload(Load.hauled_by))
    if line: qry = qry.filter(Load.line_id == line)
    col = {"created": Load.created_date, "outcome": Load.outcome_date}.get(by, Load.plan_date)
    if from_: qry = qry.filter(col >= from_)
    if to: qry = qry.filter(col <= to)
    if status: qry = qry.filter(Load.status == status)
    rows = qry.order_by(Load.id.desc()).limit(LOG_LIMIT).all()
    if q:
        ql = q.lower()
        def hit(ld):
            hay = " ".join([ld.lane.pickup.name, ld.lane.dropoff.name, ld.lane.account or "", ld.outcome_note or "", ld.ref,
                            ld.planned_driver.name if ld.planned_driver else "", ld.hauled_by.name if ld.hauled_by else ""]).lower()
            return ql in hay
        rows = [ld for ld in rows if hit(ld)]
    return rows


@app.get("/loads", response_class=HTMLResponse)
def loadlog(request: Request, from_: str = "", to: str = "", by: str = "plan", status: str = "", q: str = "", line: int | None = None,
            s: Session = Depends(get_db)):
    if not (from_ or to or line or status or q):
        from_ = (date.today() - timedelta(days=7)).isoformat()
    rows = _loadlog_query(s, from_, to, by, status, q, line)
    counts = {}
    for ld in rows: counts[ld.status] = counts.get(ld.status, 0) + 1
    qs = f"from_={from_}&to={to}&by={by}&status={status}&q={q}" + (f"&line={line}" if line else "")
    return render(request, "loadlog.html", rows=rows, counts=counts, limit=LOG_LIMIT, qs=qs,
                  q=dict(from_=from_, to=to, by=by, status=status, q=q))


@app.get("/loads.csv")
def loadlog_csv(from_: str = "", to: str = "", by: str = "plan", status: str = "", q: str = "", line: int | None = None,
                s: Session = Depends(get_db)):
    import csv, io
    rows = _loadlog_query(s, from_, to, by, status, q, line)
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["tracking", "pickup", "dropoff", "account", "tank", "entered", "planned_for", "status", "outcome_date", "driver", "note", "bbl_actual"])
    for ld in rows:
        drv = ld.hauled_by.name if ld.hauled_by else (ld.planned_driver.name if ld.planned_driver else "")
        w.writerow([ld.ref, ld.lane.pickup.name, ld.lane.dropoff.name, ld.lane.account or "", ld.tank.name if ld.tank else "", ld.created_date,
                    ld.plan_date, ld.status, ld.outcome_date or "", drv, ld.outcome_note or "", ld.bbl_actual or ""])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="loads_{from_ or "all"}_{to or "all"}.csv"'})


@app.get("/loads/{load_id}", response_class=HTMLResponse)
def load_detail(request: Request, load_id: int, s: Session = Depends(get_db)):
    ld = s.get(Load, load_id)
    if not ld: return RedirectResponse("/loads?msg=No+such+load", status_code=303)
    events = s.query(LoadEvent).filter(LoadEvent.load_id == load_id).order_by(LoadEvent.at, LoadEvent.id).all()
    return render(request, "load_detail.html", ld=ld, events=events)


@app.post("/loads/{load_id}/reopen")
def load_reopen(load_id: int, s: Session = Depends(get_db)):
    ld = s.get(Load, load_id)
    if ld and ld.status != "open":
        ldm.reopen(s, ld, ld.plan_date); s.commit()
        return RedirectResponse(f"/loads/{load_id}?msg={ld.ref}+is+open+again+on+{ld.plan_date}", status_code=303)
    return RedirectResponse(f"/loads/{load_id}", status_code=303)


# ---------------- Standing orders (recurring daily loads) ----------------
@app.get("/standing", response_class=HTMLResponse)
def standing(request: Request, s: Session = Depends(get_db)):
    rows = s.query(StandingOrder).options(joinedload(StandingOrder.lane).joinedload(Lane.pickup),
                                          joinedload(StandingOrder.lane).joinedload(Lane.dropoff)).all()
    rows.sort(key=lambda o: (not o.active, o.lane.pickup.name, o.lane.dropoff.name))
    lanes_all = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff)).filter(Lane.active == True).all()
    lanes_all.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    return render(request, "standing.html", rows=rows, lanes_all=lanes_all, days=DAYS, priorities=PRIORITIES)


@app.post("/standing/save")
async def standing_save(request: Request, s: Session = Depends(get_db)):
    f = await request.form()
    o = s.get(StandingOrder, int(f["id"])) if f.get("id") else StandingOrder()
    o.lane_id = int(f["lane_id"]); o.count = fint(f.get("count")) or 1
    o.days = ",".join(x for x in DAYS if f.get("d_" + x)) or None
    o.priority = urg.norm(f.get("priority"))
    o.earliest_pickup = f.get("earliest_pickup") or None; o.latest_pickup = f.get("latest_pickup") or None
    o.start_date = f.get("start_date") or None; o.end_date = f.get("end_date") or None
    o.active = bool(f.get("active")); o.notes = f.get("notes") or None
    s.add(o); s.commit()
    return RedirectResponse("/standing?msg=Standing+order+saved.+It+will+appear+on+each+matching+day+the+first+time+that+day+is+opened.", status_code=303)


@app.post("/standing/{so_id}/delete")
def standing_delete(so_id: int, s: Session = Depends(get_db)):
    o = s.get(StandingOrder, so_id)
    if o: s.delete(o); s.commit()
    return RedirectResponse("/standing?msg=Deleted", status_code=303)


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
    return RedirectResponse(f"/day/{plan_date}?msg=Driver+availability+saved#drivers", status_code=303)


@app.post("/day/{plan_date}/loads/add")
async def day_load_add(request: Request, plan_date: str, s: Session = Depends(get_db)):
    f = await request.form()
    pri = urg.norm(f.get("priority"))
    flex_days = int(settings_dict(s).get("flex_days") or urg.DEFAULT_FLEX_DAYS)
    l = LoadRequest(plan_date=plan_date, lane_id=int(f["lane_id"]), count=fint(f.get("count")) or 1,
                    priority=pri, must_go_by=f.get("must_go_by") or urg.deadline_for(pri, plan_date, flex_days),
                    earliest_pickup=f.get("earliest_pickup") or None, latest_pickup=f.get("latest_pickup") or None,
                    bbl_override=fnum(f.get("bbl_override")), notes=f.get("notes") or None,
                    tank_id=fint(f.get("tank_id")), gauge=f.get("gauge") or None)
    s.add(l); s.flush()
    ldm.sync_line(s, l, plan_date)
    s.commit()
    return RedirectResponse(f"/day/{plan_date}?msg=Load+added#loads", status_code=303)


def _apply_load_row(s, l: LoadRequest, f, plan_date: str, flex_days: int, sfx: str = ""):
    """Save one load line from the (single) loads form. Fields are suffixed with the load id."""
    g = lambda k: f.get(k + sfx)
    if g("count") is None: return False                     # this row wasn't in the form
    l.count = fint(g("count")) or 1
    new_pri = urg.norm(g("priority"))
    posted_deadline = g("must_go_by") or None
    if new_pri != urg.urgency(l.must_go_by, l.priority, plan_date, flex_days)["code"]:
        l.must_go_by = urg.deadline_for(new_pri, plan_date, flex_days)     # the word was changed: it sets the deadline
    elif posted_deadline is not None:
        l.must_go_by = posted_deadline or l.must_go_by                     # otherwise a typed date wins
    l.priority = new_pri
    l.earliest_pickup = g("earliest_pickup") or None
    l.latest_pickup = g("latest_pickup") or None
    l.bbl_override = fnum(g("bbl_override"))
    l.status = g("status") or l.status
    l.notes = g("notes") or None
    if g("tank_id") is not None: l.tank_id = fint(g("tank_id"))
    if g("gauge") is not None: l.gauge = g("gauge") or None
    if l.status == "done" and l.count > 0: l.status = "open"        # count typed back in on a finished line
    ldm.sync_line(s, l, plan_date)
    return True


@app.post("/day/{plan_date}/loads/save")
async def day_loads_save(request: Request, plan_date: str, s: Session = Depends(get_db)):
    """One Save for every load line on the day, so a change on one line is never lost by saving another."""
    f = await request.form()
    flex_days = int(settings_dict(s).get("flex_days") or urg.DEFAULT_FLEX_DAYS)
    n = 0
    for l in s.query(LoadRequest).filter(LoadRequest.plan_date == plan_date, LoadRequest.status.in_(ldm.LINE_OPEN)).all():
        if _apply_load_row(s, l, f, plan_date, flex_days, f"_{l.id}"): n += 1
    s.commit()
    return RedirectResponse(f"/day/{plan_date}?msg=Loads+saved+({n}+lines)#loads", status_code=303)


@app.post("/day/{plan_date}/loads/bring")
async def day_loads_bring(request: Request, plan_date: str, s: Session = Depends(get_db)):
    """Bring open loads from earlier days forward to this day (their deadlines stay, so they get more urgent)."""
    f = await request.form()
    action = f.get("action") or "bring"
    if action.startswith("haul_") or action.startswith("cancel_"):
        kind, lid = action.split("_", 1)
        l = s.get(LoadRequest, int(lid))
        if l:
            note = (f.get(f"why_{l.id}") or "").strip()
            if kind == "haul":
                k = ldm.resolve(s, l, l.count, "hauled", l.plan_date, note, plan_date)
                msg = f"{k} load(s) marked hauled on {l.plan_date}"
            else:
                k = ldm.resolve(s, l, l.count, "cancelled", date.today().isoformat(), note or "left unhauled", plan_date)
                msg = f"{k} load(s) cancelled"
        else: msg = "Line not found"
        s.commit()
        return RedirectResponse(f"/day/{plan_date}?msg={msg}#loads", status_code=303)
    ids = [int(x) for x in f.getlist("ids")]
    n = 0
    for l in s.query(LoadRequest).filter(LoadRequest.id.in_(ids)).all() if ids else []:
        n += ldm.move(s, l, l.count, plan_date, plan_date)
    s.commit()
    return RedirectResponse(f"/day/{plan_date}?msg={n}+load(s)+brought+forward#loads", status_code=303)


@app.post("/day/{plan_date}/loads/{load_id}")
async def day_load_update(request: Request, plan_date: str, load_id: int, s: Session = Depends(get_db)):
    """Per-line buttons (Move / Delete). The rest of the form is saved first so nothing typed is lost."""
    f = await request.form()
    flex_days = int(settings_dict(s).get("flex_days") or urg.DEFAULT_FLEX_DAYS)
    for l in s.query(LoadRequest).filter(LoadRequest.plan_date == plan_date, LoadRequest.status.in_(ldm.LINE_OPEN)).all():
        _apply_load_row(s, l, f, plan_date, flex_days, f"_{l.id}")
    l = s.get(LoadRequest, load_id)
    action = f.get("action")
    msg = "Updated"
    if l and action == "delete":
        ldm.remove_line(s, l, plan_date); msg = "Removed (kept in the load log)"
    elif l and action == "move":
        n = fint(f.get("move_n" + f"_{l.id}")) or l.count
        moved = ldm.move(s, l, n, f.get("new_date") or l.plan_date, plan_date)
        msg = f"{moved} load(s) moved to {f.get('new_date')}" if moved else "Nothing moved"
    elif l and action == "outcome":
        kind = f.get(f"oc_kind_{l.id}") or "hauled"
        n = fint(f.get(f"oc_n_{l.id}")) or l.count
        when = f.get(f"oc_date_{l.id}") or plan_date
        note = (f.get(f"oc_note_{l.id}") or "").strip()
        drv = fint(f.get(f"oc_driver_{l.id}"))
        if kind == "moved":
            moved = ldm.move(s, l, n, when, plan_date)
            msg = f"{moved} load(s) moved to {when}"
        else:
            k = ldm.resolve(s, l, n, kind, when, note, plan_date, drv)
            msg = f"{k} load(s) marked {kind}" + (f" on {when}" if kind == "hauled" else "")
    s.commit()
    return RedirectResponse(f"/day/{plan_date}?msg={msg}#loads", status_code=303)


@app.post("/day/{plan_date}/plan")
def day_plan(plan_date: str, s: Session = Depends(get_db)):
    res = optimizer.solve(s, plan_date)
    if not res.get("ok"):
        return RedirectResponse(f"/day/{plan_date}?msg={res['error']}", status_code=303)
    optimizer.save_plan(s, res)
    ldm.record_plan(s, res); s.commit()
    t = res["totals"]
    msg = f"Plan built: {t['loads']} loads on {t['drivers_used']} drivers, ${t['revenue']:,.0f} base revenue, ${t['per_hour']:,.0f}/hr"
    if t["unassigned"]: msg += f" — {t['unassigned']} load(s) could not be fitted" + (f" ({t['unassigned_must']} MUST)" if t["unassigned_must"] else "")
    return RedirectResponse(f"/day/{plan_date}?msg={msg}#plan", status_code=303)


@app.get("/plan/{plan_id}/print", response_class=HTMLResponse)
def plan_print(request: Request, plan_id: int, s: Session = Depends(get_db)):
    row = s.get(Plan, plan_id)
    plan = json.loads(row.result_json); annotate_legs(s, plan)
    return render(request, "plan_print.html", plan=plan, plan_row=row)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
