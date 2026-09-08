"""The dispatch engine: turns one day's loads + available drivers into the most profitable set of shifts.

Model (Google OR-Tools routing solver):
  * each available driver is a "vehicle" that starts and ends at their home yard
  * each load is a pickup node + a drop-off node that must be served by the same driver, in order,
    and the truck holds one load at a time (capacity 1) so a driver goes pickup -> drop-off -> next pickup
  * two clocks per driver: on-duty minutes (driving + loading + offloading + waiting) capped at the 16-hour wall
    minus a safety buffer, and driving minutes capped at 10 hours minus a buffer; both can be lowered per driver per day
  * site open/close windows and per-load pickup windows are honored; a driver may wait at a site for it to open
  * two drivers sharing a truck: the second cannot leave the yard until the first is back plus handoff time
  * objective: maximize profit = base revenue - driver pay - fuel, where fuel = miles / mpg x diesel price.
    Leaving a load unhauled costs its profit ("must" loads cost far more, so they are only dropped when impossible).
  * fuel surcharge is reported alongside but deliberately NOT part of the objective (Josh's $135/hr target is pre-FSC).
"""
import json, math
from datetime import datetime, timedelta
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from sqlalchemy.orm import Session, joinedload
from db import Setting, Location, Lane, Distance, Driver, DriverDay, LoadRequest, Plan
import fsc as fscmod
from mileage import haversine_miles, STRAIGHT_LINE_FACTOR

HORIZON_MIN = 48 * 60          # plan clock runs from plan-date midnight for 48 hours (PM shifts cross midnight)
MUST_PENALTY = 5_000_000       # cents — makes dropping a "must" load a last resort
NORMAL_BONUS = 5_000           # cents — small nudge to haul "normal" loads over "flexible" ones at equal profit
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def hm_to_min(hm: str | None, default: int | None = None):
    if not hm: return default
    h, m = hm.split(":")[:2]
    return int(h) * 60 + int(m)


def min_to_hm(m: int):
    m = int(round(m)); d, r = divmod(m, 1440)
    return f"{r // 60:02d}:{r % 60:02d}" + ("" if d == 0 else f" +{d}d")


class DayInputs:
    """Everything the solver needs, pulled from the database once."""

    def __init__(self, s: Session, plan_date: str):
        self.plan_date = plan_date
        st = {x.key: x.value for x in s.query(Setting).all()}
        self.st = st
        self.speed = st.get("avg_speed_mph") or 41
        self.load_min = int(st.get("load_minutes") or 60)
        self.unload_min = int(st.get("unload_minutes") or 60)
        self.min_bbl = st.get("min_bbl") or 150
        self.mpg = st.get("mpg") or 5.5
        self.diesel = st.get("diesel_price") or 0
        self.fsc = fscmod.fsc_from_settings(st)
        self.drive_cap = int(((st.get("max_drive_hours") or 10) - (st.get("drive_buffer_hours") or 0)) * 60)
        self.duty_cap = int(((st.get("max_duty_hours") or 16) - (st.get("duty_buffer_hours") or 0)) * 60)
        self.handoff = int(st.get("truck_handoff_minutes") or 30)
        self.inspection_cost = (st.get("inspection_minutes") or 45) / 60 * (st.get("min_wage") or 0)
        self.target = st.get("target_per_hour") or 135
        self.weekday = DAYS[datetime.strptime(plan_date, "%Y-%m-%d").weekday()]

        # drivers available today
        dd = {x.driver_id: x for x in s.query(DriverDay).filter(DriverDay.plan_date == plan_date).all()}
        self.drivers = []
        for d in s.query(Driver).options(joinedload(Driver.yard)).filter(Driver.active == True).all():
            x = dd.get(d.id)
            off = self.weekday in (d.days_off or "").split(",")
            available = x.available if x else not off
            if not available or not d.yard: continue
            shift = (x.shift if x else None) or d.usual_shift or "AM"
            default_start = int((st.get("am_start_hour") if shift == "AM" else st.get("pm_start_hour")) or (5 if shift == "AM" else 17)) * 60
            start = hm_to_min(x.start_time if x else None, default_start)
            drive = int(min(self.drive_cap, ((x.drive_hours_left if x and x.drive_hours_left else None) or d.max_drive_hours or 99) * 60))
            duty = int(min(self.duty_cap, ((x.duty_hours_left if x and x.duty_hours_left else None) or d.max_duty_hours or 99) * 60))
            if x and x.cycle_hours_left: duty = int(min(duty, x.cycle_hours_left * 60))
            self.drivers.append(dict(driver=d, yard=d.yard, shift=shift, start=start, drive_cap=drive, duty_cap=duty,
                                     truck=(d.truck or "").strip().upper() or None))
        # AM before PM so truck handoffs are AM -> PM
        self.drivers.sort(key=lambda v: (0 if v["shift"] == "AM" else 1, v["start"], v["driver"].name))

        # loads: one unit per load
        reqs = s.query(LoadRequest).options(joinedload(LoadRequest.lane).joinedload(Lane.pickup),
                                            joinedload(LoadRequest.lane).joinedload(Lane.dropoff)) \
            .filter(LoadRequest.plan_date == plan_date, LoadRequest.status.in_(["open", "planned"])).all()
        self.loads = []
        for r in reqs:
            lane = r.lane
            if not lane or not lane.rate: continue
            bbl = r.bbl_override or lane.pickup.billable_bbl(self.min_bbl)
            bbl = max(bbl, self.min_bbl)
            rev = lane.rate * bbl
            pay = (lane.driver_pay or 0) * bbl
            pri = r.priority or "normal"
            if r.must_go_by and r.must_go_by <= plan_date: pri = "must"
            for k in range(r.count or 1):
                self.loads.append(dict(req=r, lane=lane, unit=k + 1, bbl=bbl, rev=rev, pay=pay, priority=pri,
                                       earliest=hm_to_min(r.earliest_pickup), latest=hm_to_min(r.latest_pickup)))

        # distances (override > google > straight-line estimate)
        self.locs = {l.id: l for l in s.query(Location).all()}
        self.dist = {}
        for d in s.query(Distance).all():
            m = d.override_miles if d.override_miles else d.google_miles
            if m is not None: self.dist[(d.origin_id, d.dest_id)] = m

    def miles(self, a: int, b: int) -> float:
        if a == b: return 0.0
        m = self.dist.get((a, b))
        if m is None:
            m = round(haversine_miles(self.locs[a], self.locs[b]) * STRAIGHT_LINE_FACTOR, 1)
            self.dist[(a, b)] = m
        return m

    def drive_min(self, a: int, b: int) -> int:
        return int(round(self.miles(a, b) / self.speed * 60))

    def service_min(self, loc: Location, kind: str) -> int:
        return int(loc.load_minutes or (self.load_min if kind == "pickup" else self.unload_min))


def solve(s: Session, plan_date: str, time_limit_s: int | None = None) -> dict:
    inp = DayInputs(s, plan_date)
    time_limit_s = int(time_limit_s or inp.st.get("solver_seconds") or 20)
    if not inp.drivers:
        return dict(ok=False, error="No drivers are marked available for this day.", plan_date=plan_date)
    if not inp.loads:
        return dict(ok=False, error="No open loads with a rate for this day.", plan_date=plan_date)

    # ---------- nodes ----------
    # node 0..Y-1 = one node per distinct yard (vehicle starts/ends); then pickup/dropoff node per load
    yards = []
    for v in inp.drivers:
        if v["yard"].id not in [y.id for y in yards]: yards.append(v["yard"])
    yard_node = {y.id: i for i, y in enumerate(yards)}
    nodes = [dict(loc=y, kind="yard", load=None) for y in yards]
    for L in inp.loads:
        L["p_node"] = len(nodes); nodes.append(dict(loc=L["lane"].pickup, kind="pickup", load=L))
        L["d_node"] = len(nodes); nodes.append(dict(loc=L["lane"].dropoff, kind="dropoff", load=L))
    n_nodes, n_veh = len(nodes), len(inp.drivers)
    starts = [yard_node[v["yard"].id] for v in inp.drivers]
    mgr = pywrapcp.RoutingIndexManager(n_nodes, n_veh, starts, starts)
    routing = pywrapcp.RoutingModel(mgr)
    solver = routing.solver()

    # ---------- callbacks ----------
    loc_of = [nd["loc"] for nd in nodes]
    def node_service(i):
        nd = nodes[i]
        return 0 if nd["kind"] == "yard" else inp.service_min(nd["loc"], nd["kind"])
    drive_mat = [[inp.drive_min(loc_of[i].id, loc_of[j].id) for j in range(n_nodes)] for i in range(n_nodes)]
    miles_mat = [[inp.miles(loc_of[i].id, loc_of[j].id) for j in range(n_nodes)] for i in range(n_nodes)]
    fuel_cents = [[int(round(miles_mat[i][j] / inp.mpg * inp.diesel * 100)) for j in range(n_nodes)] for i in range(n_nodes)]

    def time_cb(fi, ti):
        i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
        return drive_mat[i][j] + node_service(i)
    def drive_cb(fi, ti):
        i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
        return drive_mat[i][j]
    def fuel_cb(fi, ti):
        i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
        return fuel_cents[i][j]
    def demand_cb(fi):
        k = nodes[mgr.IndexToNode(fi)]["kind"]
        return 1 if k == "pickup" else (-1 if k == "dropoff" else 0)

    time_idx = routing.RegisterTransitCallback(time_cb)
    drive_idx = routing.RegisterTransitCallback(drive_cb)
    fuel_idx = routing.RegisterTransitCallback(fuel_cb)
    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(fuel_idx)

    # ---------- dimensions ----------
    routing.AddDimension(time_idx, 8 * 60, HORIZON_MIN, False, "Time")     # slack = waiting allowed at a site
    time_dim = routing.GetDimensionOrDie("Time")
    routing.AddDimension(drive_idx, 0, 24 * 60, True, "Drive")
    drive_dim = routing.GetDimensionOrDie("Drive")
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, [1] * n_veh, True, "Load")

    # ---------- vehicles: start windows, hour caps, fixed cost, truck sharing ----------
    for v, drv in enumerate(inp.drivers):
        st_i, en_i = routing.Start(v), routing.End(v)
        time_dim.CumulVar(st_i).SetRange(drv["start"], drv["start"] + 6 * 60)   # may leave up to 6 h after earliest start
        time_dim.SetSpanUpperBoundForVehicle(drv["duty_cap"], v)
        drive_dim.SetSpanUpperBoundForVehicle(drv["drive_cap"], v)
        routing.SetFixedCostOfVehicle(int(inp.inspection_cost * 100), v)
        # prefer leaving as early as possible / returning early: finalize end times in the objective
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(en_i))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(st_i))
    trucks = {}
    for v, drv in enumerate(inp.drivers):
        if drv["truck"]: trucks.setdefault(drv["truck"], []).append(v)
    for tv in trucks.values():
        for a, b in zip(tv, tv[1:]):       # earlier-starting driver first; next driver waits for the truck
            solver.Add(time_dim.CumulVar(routing.Start(b)) >= time_dim.CumulVar(routing.End(a)) + inp.handoff)

    # ---------- loads: pickup/delivery pairs, windows, drop penalties ----------
    for L in inp.loads:
        pi, di = mgr.NodeToIndex(L["p_node"]), mgr.NodeToIndex(L["d_node"])
        routing.AddPickupAndDelivery(pi, di)
        solver.Add(routing.VehicleVar(pi) == routing.VehicleVar(di))
        solver.Add(time_dim.CumulVar(pi) <= time_dim.CumulVar(di))
        profit_cents = int(round((L["rev"] - L["pay"]) * 100))
        pen = MUST_PENALTY if L["priority"] == "must" else profit_cents + (NORMAL_BONUS if L["priority"] == "normal" else 0)
        routing.AddDisjunction([pi], max(pen, 1))
        routing.AddDisjunction([di], 0)
        # per-load pickup window (today only)
        if L["earliest"] is not None or L["latest"] is not None:
            lo = L["earliest"] if L["earliest"] is not None else 0
            hi = L["latest"] if L["latest"] is not None else 1440
            time_dim.CumulVar(pi).SetRange(lo, hi)
    # site open/close windows (repeat on day 2 of the horizon)
    for nd_i, nd in enumerate(nodes):
        if nd["kind"] == "yard": continue
        loc = nd["loc"]
        o, c = hm_to_min(loc.open_time), hm_to_min(loc.close_time)
        if o is None and c is None: continue
        o = o or 0; c = c if c is not None else 1439
        var = time_dim.CumulVar(mgr.NodeToIndex(nd_i))
        if o <= c:
            if o > 0: var.RemoveInterval(0, o - 1)            # closed before opening on day 1
            var.RemoveInterval(c + 1, o + 1440 - 1)          # closed between close and next day's open
            var.RemoveInterval(c + 1440 + 1, HORIZON_MIN)    # closed after day-2 close
        else:                                                # overnight window e.g. 20:00-04:00
            var.RemoveInterval(c + 1, o - 1)
            var.RemoveInterval(c + 1440 + 1, o + 1440 - 1)

    # ---------- search ----------
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(max(3, time_limit_s))
    sol = routing.SolveWithParameters(params)
    if sol is None:
        return dict(ok=False, error="The solver could not find any feasible plan (check windows and hour limits).", plan_date=plan_date)

    # ---------- read the solution ----------
    shifts, assigned = [], set()
    for v, drv in enumerate(inp.drivers):
        idx = routing.Start(v)
        stops, prev_node, loaded_mi, empty_mi, drive_m = [], mgr.IndexToNode(idx), 0.0, 0.0, 0
        t_start = sol.Value(time_dim.CumulVar(idx))
        rev = pay = 0.0; n_loads = 0
        stops.append(dict(kind="yard", name=drv["yard"].name, arrive=t_start, depart=t_start, miles=0))
        idx = sol.Value(routing.NextVar(idx))
        while not routing.IsEnd(idx):
            node = mgr.IndexToNode(idx); nd = nodes[node]
            mi = miles_mat[prev_node][node]; dm = drive_mat[prev_node][node]
            if nd["kind"] == "dropoff": loaded_mi += mi
            else: empty_mi += mi
            drive_m += dm
            arrive = sol.Value(time_dim.CumulVar(idx))
            svc = node_service(node)
            L = nd["load"]
            stop = dict(kind=nd["kind"], name=nd["loc"].name, arrive=arrive, depart=arrive + svc, miles=mi, drive_min=dm,
                        load_id=L["req"].id, unit=L["unit"], lane=f'{L["lane"].pickup.name} → {L["lane"].dropoff.name}',
                        account=L["lane"].account or "", bbl=L["bbl"], priority=L["priority"])
            if nd["kind"] == "dropoff":
                stop.update(rev=L["rev"], rev_fsc=L["rev"] * (1 + inp.fsc), pay=L["pay"])
                rev += L["rev"]; pay += L["pay"]; n_loads += 1; assigned.add(id(L))
            stops.append(stop)
            prev_node = node
            idx = sol.Value(routing.NextVar(idx))
        end_node = mgr.IndexToNode(idx)
        mi = miles_mat[prev_node][end_node]; dm = drive_mat[prev_node][end_node]
        empty_mi += mi; drive_m += dm
        t_end = sol.Value(time_dim.CumulVar(idx))
        stops.append(dict(kind="yard", name=drv["yard"].name, arrive=t_end, depart=t_end, miles=mi, drive_min=dm))
        duty_m = t_end - t_start
        fuel = (loaded_mi + empty_mi) / inp.mpg * inp.diesel
        used = n_loads > 0
        hrs = duty_m / 60 if duty_m else 0
        shifts.append(dict(
            driver=drv["driver"].name, driver_id=drv["driver"].id, yard=drv["yard"].name, shift=drv["shift"], truck=drv["truck"],
            used=used, loads=n_loads, stops=stops if used else [], start=t_start, end=t_end,
            duty_hours=round(hrs, 2), drive_hours=round(drive_m / 60, 2), loaded_miles=round(loaded_mi, 1), empty_miles=round(empty_mi, 1),
            revenue=round(rev, 2), revenue_fsc=round(rev * (1 + inp.fsc), 2), driver_pay=round(pay, 2), fuel=round(fuel, 2),
            inspection=round(inp.inspection_cost, 2) if used else 0,
            profit=round(rev - pay - fuel - (inp.inspection_cost if used else 0), 2),
            profit_fsc=round(rev * (1 + inp.fsc) - pay - fuel - (inp.inspection_cost if used else 0), 2),
            per_hour=round(rev / hrs, 2) if hrs else 0, per_hour_fsc=round(rev * (1 + inp.fsc) / hrs, 2) if hrs else 0,
            drive_cap_h=drv["drive_cap"] / 60, duty_cap_h=drv["duty_cap"] / 60,
            meets_target=(rev / hrs >= inp.target) if hrs else False))

    unassigned = []
    for L in inp.loads:
        if id(L) not in assigned:
            unassigned.append(dict(load_id=L["req"].id, unit=L["unit"], lane=f'{L["lane"].pickup.name} → {L["lane"].dropoff.name}',
                                   account=L["lane"].account or "", priority=L["priority"], rev=round(L["rev"], 2),
                                   profit=round(L["rev"] - L["pay"], 2)))
    used_shifts = [x for x in shifts if x["used"]]
    tot_hours = sum(x["duty_hours"] for x in used_shifts)
    totals = dict(
        loads=sum(x["loads"] for x in used_shifts), drivers_used=len(used_shifts), drivers_available=len(shifts),
        revenue=round(sum(x["revenue"] for x in used_shifts), 2), revenue_fsc=round(sum(x["revenue_fsc"] for x in used_shifts), 2),
        driver_pay=round(sum(x["driver_pay"] for x in used_shifts), 2), fuel=round(sum(x["fuel"] for x in used_shifts), 2),
        inspection=round(sum(x["inspection"] for x in used_shifts), 2),
        profit=round(sum(x["profit"] for x in used_shifts), 2), profit_fsc=round(sum(x["profit_fsc"] for x in used_shifts), 2),
        hours=round(tot_hours, 2), miles=round(sum(x["loaded_miles"] + x["empty_miles"] for x in used_shifts), 1),
        per_hour=round(sum(x["revenue"] for x in used_shifts) / tot_hours, 2) if tot_hours else 0,
        per_hour_fsc=round(sum(x["revenue_fsc"] for x in used_shifts) / tot_hours, 2) if tot_hours else 0,
        unassigned=len(unassigned), unassigned_must=sum(1 for u in unassigned if u["priority"] == "must"),
        target=inp.target, fsc_pct=inp.fsc, diesel=inp.diesel, solver_seconds=time_limit_s)
    return dict(ok=True, plan_date=plan_date, generated_at=datetime.utcnow().isoformat(timespec="seconds"),
                shifts=shifts, unassigned=unassigned, totals=totals)


def save_plan(s: Session, result: dict) -> Plan:
    t = result["totals"]
    p = Plan(plan_date=result["plan_date"], result_json=json.dumps(result),
             summary=f'{t["loads"]} loads, {t["drivers_used"]} drivers, ${t["revenue"]:,.0f} base (${t["per_hour"]:,.0f}/hr), '
                     f'{t["unassigned"]} unassigned')
    s.add(p); s.commit()
    return p
