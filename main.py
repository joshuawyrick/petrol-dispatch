"""Petrol Dispatch Optimizer — web app (phase 1: master data, settings, mileage cache)."""
import os
from typing import Optional
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from db import SessionLocal, Setting, Location, Lane, Distance, init_db
from seed import seed
import mileage

app = FastAPI(title="Petrol Dispatch Optimizer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

KINDS = ["pickup", "dropoff", "both", "yard"]


@app.on_event("startup")
def _startup():
    init_db()
    print("seed:", seed())


def get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def settings_dict(s: Session):
    return {x.key: x.value for x in s.query(Setting).all()}


def render(request, name, **ctx):
    ctx.setdefault("request", request)
    ctx.setdefault("msg", request.query_params.get("msg"))
    return templates.TemplateResponse(name, ctx)


def fnum(v):  # form field -> float or None
    v = (v or "").strip().replace("$", "").replace(",", "")
    return float(v) if v else None


# ---------------- Dashboard ----------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, s: Session = Depends(get_db)):
    st = settings_dict(s)
    cov = mileage.coverage(s)
    todo = []
    if not st.get("diesel_price"): todo.append("Enter the diesel price on the Settings page.")
    if not st.get("min_wage"): todo.append("Enter the California minimum wage on the Settings page.")
    no_rate = s.query(Lane).filter(Lane.active == True, Lane.rate == None).count()
    if no_rate: todo.append(f"{no_rate} active lane(s) have no rate — see Lanes.")
    if cov["missing"]: todo.append(f"{cov['missing']} of {cov['needed']} mileage pairs still need road miles — see Mileage.")
    counts = {k: s.query(Location).filter(Location.kind == k, Location.active == True).count() for k in KINDS}
    return render(request, "dashboard.html", counts=counts, lanes=s.query(Lane).filter(Lane.active == True).count(),
                  cov=cov, todo=todo, has_key=bool(mileage.get_api_key()))


# ---------------- Settings ----------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_db)):
    rows = s.query(Setting).order_by(Setting.sort).all()
    return render(request, "settings.html", rows=rows)


@app.post("/settings")
async def settings_save(request: Request, s: Session = Depends(get_db)):
    form = await request.form()
    for row in s.query(Setting).all():
        if row.key in form:
            row.value = fnum(form[row.key])
    s.commit()
    return RedirectResponse("/settings?msg=Settings+saved", status_code=303)


# ---------------- Locations ----------------
@app.get("/locations", response_class=HTMLResponse)
def locations(request: Request, kind: str = "", q: str = "", show_inactive: int = 0, s: Session = Depends(get_db)):
    st = settings_dict(s)
    qry = s.query(Location)
    if kind: qry = qry.filter(Location.kind == kind)
    if q: qry = qry.filter(Location.name.ilike(f"%{q}%"))
    if not show_inactive: qry = qry.filter(Location.active == True)
    rows = qry.order_by(Location.kind, Location.name).all()
    return render(request, "locations.html", rows=rows, kind=kind, q=q, show_inactive=show_inactive,
                  min_bbl=st.get("min_bbl") or 150, kinds=KINDS)


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
    loc.load_minutes = int(f["load_minutes"]) if f.get("load_minutes", "").strip() else None
    loc.open_time = f.get("open_time") or None
    loc.close_time = f.get("close_time") or None
    loc.max_trucks_at_once = int(f["max_trucks_at_once"]) if f.get("max_trucks_at_once", "").strip() else None
    loc.notes = f.get("notes") or None
    s.add(loc); s.commit()
    return RedirectResponse(f"/locations/{loc.id}?msg=Saved", status_code=303)


# ---------------- Lanes ----------------
@app.get("/lanes", response_class=HTMLResponse)
def lanes(request: Request, q: str = "", show_inactive: int = 0, s: Session = Depends(get_db)):
    st = settings_dict(s)
    min_bbl = st.get("min_bbl") or 150
    qry = s.query(Lane).options(joinedload(Lane.pickup), joinedload(Lane.dropoff))
    if not show_inactive: qry = qry.filter(Lane.active == True)
    rows = qry.all()
    if q:
        ql = q.lower()
        rows = [l for l in rows if ql in (l.pickup.name + " " + l.dropoff.name + " " + (l.account or "")).lower()]
    rows.sort(key=lambda l: (l.pickup.name, l.dropoff.name))
    view = []
    for l in rows:
        bbl = l.pickup.billable_bbl(min_bbl)
        rev = (l.rate or 0) * bbl
        pay = (l.driver_pay or 0) * bbl
        view.append(dict(l=l, bbl=bbl, rev=rev, pay=pay, margin=rev - pay))
    return render(request, "lanes.html", rows=view, q=q, show_inactive=show_inactive)


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
    msg = f"Google returned {res['fetched']} pairs in {res['requests']} requests."
    if res["errors"]: msg += f" {len(res['errors'])} problem(s): " + " | ".join(res["errors"][:3])
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
