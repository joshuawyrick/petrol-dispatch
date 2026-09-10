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
    Leaving a load unhauled costs its profit plus an urgency bonus that grows as its deadline nears; loads due today cost far more, so they are only dropped when impossible.
  * sub-hauler drivers: Petrol's take on a load is only its margin (load pay minus the sub's share), and the sub covers
    fuel, driver pay and inspections. Modelled as a per-vehicle cost on each drop-off equal to (sub's pay - what our own
    driver would have been paid), so the engine fills company trucks first and hands subs what's left — unless a sub's
    special lane rate makes them the cheaper option.
  * fuel surcharge is reported alongside but deliberately NOT part of the objective (Josh's $135/hr target is pre-FSC).
    Subs receive the whole surcharge, so on their loads it is neither revenue nor cost to Petrol.
"""
import json, math
from datetime import datetime, timedelta
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from sqlalchemy.orm import Session, joinedload
from db import Setting, Location, Lane, Distance, Driver, DriverDay, LoadRequest, Plan, Company, CompanyLaneRate, LocationRestriction, Tank, Load
import fsc as fscmod
import urgency as urg
from mileage import haversine_miles, STRAIGHT_LINE_FACTOR

HORIZON_MIN = 48 * 60          # plan clock runs from plan-date midnight for 48 hours (PM shifts cross midnight)
MUST_PENALTY = 5_000_000       # cents — makes dropping a "today" load a last resort
URGENCY_BONUS = 5_000          # cents per day of urgency — a load due tomorrow is worth more to haul now than one due in 5 days
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
        self.gauge_min = int(st.get("gauge_minutes") or 15)
        self.flex_days = int(st.get("flex_days") or urg.DEFAULT_FLEX_DAYS)
        self.priority_step = (st.get("priority_step") if st.get("priority_step") is not None else 30) / 100.0   # share of a load's profit per tier step
        self.weekday = DAYS[datetime.strptime(plan_date, "%Y-%m-%d").weekday()]

        # who/what can't go where: location id -> (set of driver ids, set of truck numbers)
        self.restrict = {}
        for r in s.query(LocationRestriction).all():
            drs, trs = self.restrict.setdefault(r.location_id, (set(), set()))
            if r.driver_id: drs.add(r.driver_id)
            if r.truck: trs.add(r.truck.strip().upper())

        # sub-hauler deals: company default share + per-lane exceptions
        self.lane_rates = {(r.company_id, r.lane_id): r for r in s.query(CompanyLaneRate).all()}

        # drivers available today
        dd = {x.driver_id: x for x in s.query(DriverDay).filter(DriverDay.plan_date == plan_date).all()}
        self.drivers = []
        for d in s.query(Driver).options(joinedload(Driver.yard), joinedload(Driver.company)).filter(Driver.active == True).all():
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
            co = d.company if (d.company and not d.company.is_petrol) else None
            owned = bool(co and co.petrol_owned)
            truck = (d.truck or "").strip().upper() or None
            self.drivers.append(dict(driver=d, yard=d.yard, shift=shift, start=start, drive_cap=drive, duty_cap=duty,
                                     truck=truck, truck_key=(co.id if co else 0, truck) if truck else None,   # truck "02" at two subs ≠ same truck
                                     company=co, company_name=(co.short_name or co.name) if co else "",
                                     share_pct=(co.share_pct if co and co.share_pct is not None else 0),
                                     owned=owned, econ_sub=bool(co and not owned),         # Petrol-owned subs are costed like our own trucks
                                     tier=int((co.priority if co and co.priority else 1) if co else 1),
                                     can_gauge=bool(d.can_gauge)))
        # AM before PM so truck handoffs are AM -> PM
        self.drivers.sort(key=lambda v: (0 if v["shift"] == "AM" else 1, v["start"], v["driver"].name))

        # loads: one unit per load
        reqs = s.query(LoadRequest).options(joinedload(LoadRequest.lane).joinedload(Lane.pickup),
                                            joinedload(LoadRequest.lane).joinedload(Lane.dropoff)) \
            .filter(LoadRequest.plan_date == plan_date, LoadRequest.status.in_(["open", "planned"])).all()
        rows_by_line = {}
        for ld in s.query(Load).filter(Load.plan_date == plan_date, Load.status == "open").order_by(Load.id).all():
            rows_by_line.setdefault(ld.line_id, []).append(ld)
        self.loads = []
        for r in reqs:
            lane = r.lane
            if not lane or not lane.rate: continue
            bbl = r.bbl_override or lane.pickup.billable_bbl(self.min_bbl)
            bbl = max(bbl, self.min_bbl)
            rev = lane.rate * bbl
            pay = (lane.driver_pay or 0) * bbl
            u = urg.urgency(r.must_go_by, r.priority, plan_date, self.flex_days)
            pri, days_left = u["code"], u["days_left"]
            gauge = r.gauge or ("haul" if lane.pickup.requires_gauging else "none")
            rows = rows_by_line.get(r.id, [])
            for k in range(r.count or 1):
                row = rows[k] if k < len(rows) else None
                self.loads.append(dict(req=r, lane=lane, unit=k + 1, bbl=bbl, rev=rev, pay=pay, priority=pri, days_left=days_left, deadline=u["deadline"],
                                       load_row_id=(row.id if row else None), ref=(row.ref if row else f"#{r.id}-{k + 1}"),
                                       earliest=hm_to_min(r.earliest_pickup), latest=hm_to_min(r.latest_pickup),
                                       tank_id=r.tank_id, tank=(r.tank.name if r.tank else None), gauge=gauge,
                                       gkey=(lane.pickup_id, r.tank_id) if gauge != "none" else None))
        # gauging groups: one per (pickup, tank) that needs a gauge today. Mode 'only' wins if any line asks for it.
        self.ggroups = {}
        for L in self.loads:
            if L["gkey"]: self.ggroups.setdefault(L["gkey"], []).append(L)
        self.gmode = {k: ("only" if any(L["gauge"] == "only" for L in v) else "haul") for k, v in self.ggroups.items()}

        # distances (override > google > straight-line estimate)
        self.locs = {l.id: l for l in s.query(Location).all()}
        self.dist = {}
        for d in s.query(Distance).all():
            m = d.override_miles if d.override_miles else d.google_miles
            if m is not None: self.dist[(d.origin_id, d.dest_id)] = m

    def allowed(self, drv: dict, loc_id: int) -> bool:
        """False if this driver or their truck is barred from the location."""
        r = self.restrict.get(loc_id)
        if not r: return True
        return drv["driver"].id not in r[0] and (drv["truck"] or "") not in r[1]

    def sub_pay(self, drv: dict, L: dict) -> float:
        """What a sub-hauler company gets for this load (base pay, before the surcharge they also receive in full)."""
        co = drv.get("company")
        if not co: return 0.0
        x = self.lane_rates.get((co.id, L["lane"].id))
        if x and x.flat_bbl: return x.flat_bbl * L["bbl"]
        pct = x.share_pct if (x and x.share_pct is not None) else (co.share_pct or 0)
        return L["rev"] * pct / 100.0

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
    # gauge-only visits: a gauger stops at the tank, samples, and leaves; the loads there wait for it
    gauge_nodes = {}
    for key, mode in inp.gmode.items():
        if mode == "only":
            gauge_nodes[key] = len(nodes)
            nodes.append(dict(loc=inp.ggroups[key][0]["lane"].pickup, kind="gauge", load=None, gkey=key, tank=inp.ggroups[key][0]["tank"]))
    n_nodes, n_veh = len(nodes), len(inp.drivers)
    starts = [yard_node[v["yard"].id] for v in inp.drivers]
    mgr = pywrapcp.RoutingIndexManager(n_nodes, n_veh, starts, starts)
    routing = pywrapcp.RoutingModel(mgr)
    solver = routing.solver()

    # ---------- callbacks ----------
    loc_of = [nd["loc"] for nd in nodes]
    def node_service(i):
        nd = nodes[i]
        if nd["kind"] == "yard": return 0
        if nd["kind"] == "gauge": return inp.gauge_min
        return inp.service_min(nd["loc"], nd["kind"])
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
    # per-vehicle cost = fuel (not for outside subs: they buy their own) + margin we give up on a sub's drop-off
    #                    + a priority step so tier-1 companies fill up before tier 2, and tier 2 before tier 3
    def make_cb(extra, use_fuel):
        def cb(fi, ti):
            i, j = mgr.IndexToNode(fi), mgr.IndexToNode(ti)
            return (fuel_cents[i][j] if use_fuel else 0) + extra[j]
        return cb
    for v, drv in enumerate(inp.drivers):
        extra = [0] * n_nodes
        tier_frac = min(0.9, inp.priority_step * (drv["tier"] - 1))     # tier 2 = 30% of the load's profit, tier 3 = 60% (default)
        for L in inp.loads:
            profit_c = max(0, int(round((L["rev"] - L["pay"]) * 100)))
            margin_c = max(0, int(round((inp.sub_pay(drv, L) - L["pay"]) * 100))) if drv["econ_sub"] else 0
            # the bigger of "margin we give up" and "tier preference" — always below the drop penalty, so hauling still beats dropping
            extra[L["d_node"]] = min(profit_c, max(margin_c, int(tier_frac * profit_c)))
        if drv["econ_sub"] or tier_frac:
            routing.SetArcCostEvaluatorOfVehicle(routing.RegisterTransitCallback(make_cb(extra, not drv["econ_sub"])), v)

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
        routing.SetFixedCostOfVehicle(0 if drv["econ_sub"] else int(inp.inspection_cost * 100), v)
        # prefer leaving as early as possible / returning early: finalize end times in the objective
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(en_i))
        routing.AddVariableMinimizedByFinalizer(time_dim.CumulVar(st_i))
    trucks = {}
    for v, drv in enumerate(inp.drivers):
        if drv["truck_key"]: trucks.setdefault(drv["truck_key"], []).append(v)
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
        # due today (or overdue): near-hard. Otherwise: its profit plus a bonus that grows as the deadline gets closer.
        pen = MUST_PENALTY if L["days_left"] <= 0 else profit_cents + URGENCY_BONUS * max(0, inp.flex_days - L["days_left"])
        routing.AddDisjunction([pi], max(pen, 1))
        routing.AddDisjunction([di], 0)
        # per-load pickup window (today only)
        if L["earliest"] is not None or L["latest"] is not None:
            lo = L["earliest"] if L["earliest"] is not None else 0
            hi = L["latest"] if L["latest"] is not None else 1440
            time_dim.CumulVar(pi).SetRange(lo, hi)
    # ---------- who can't go where ----------
    for L in inp.loads:
        pi, di = mgr.NodeToIndex(L["p_node"]), mgr.NodeToIndex(L["d_node"])
        L["banned"] = [v for v, drv in enumerate(inp.drivers)
                       if not (inp.allowed(drv, L["lane"].pickup_id) and inp.allowed(drv, L["lane"].dropoff_id))]
        for v in L["banned"]:
            routing.VehicleVar(pi).RemoveValue(v); routing.VehicleVar(di).RemoveValue(v)
    for key, gn in gauge_nodes.items():
        gi = mgr.NodeToIndex(gn)
        for v, drv in enumerate(inp.drivers):
            if not inp.allowed(drv, nodes[gn]["loc"].id): routing.VehicleVar(gi).RemoveValue(v)

    # ---------- gauging ----------
    gaugers = [v for v, drv in enumerate(inp.drivers) if drv["can_gauge"]]
    non_gaugers = [v for v in range(n_veh) if v not in gaugers]
    for key, group in inp.ggroups.items():
        group.sort(key=lambda L: (L["req"].id, L["unit"]))
        mode = inp.gmode[key]
        if mode == "haul":
            # the first load from this tank is hauled by a gauger; everyone else loads only after that gauge is done
            G = group[0]; G["is_gauge_load"] = True
            gi = mgr.NodeToIndex(G["p_node"])
            for v in non_gaugers: routing.VehicleVar(gi).RemoveValue(v)
            for L in group: L["needs_gauger"] = not gaugers
            for O in group[1:]:
                oi = mgr.NodeToIndex(O["p_node"])
                solver.Add(routing.ActiveVar(oi) <= routing.ActiveVar(gi))
                solver.Add(time_dim.CumulVar(oi) + (1 - routing.ActiveVar(oi)) * HORIZON_MIN >= time_dim.CumulVar(gi) + inp.gauge_min)
        else:
            gn = gauge_nodes[key]; gi = mgr.NodeToIndex(gn)
            for v in non_gaugers: routing.VehicleVar(gi).RemoveValue(v)
            routing.AddDisjunction([gi], 0)                 # free to skip — but then none of the tank's loads can go
            for L in group:
                L["needs_gauger"] = not gaugers
                oi = mgr.NodeToIndex(L["p_node"])
                solver.Add(routing.ActiveVar(oi) <= routing.ActiveVar(gi))
                solver.Add(time_dim.CumulVar(oi) + (1 - routing.ActiveVar(oi)) * HORIZON_MIN >= time_dim.CumulVar(gi) + inp.gauge_min)
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
    shifts, assigned, gauged = [], set(), set()
    for v, drv in enumerate(inp.drivers):
        idx = routing.Start(v)
        stops, prev_node, loaded_mi, empty_mi, drive_m = [], mgr.IndexToNode(idx), 0.0, 0.0, 0
        t_start = sol.Value(time_dim.CumulVar(idx))
        rev = pay = subpay = 0.0; n_loads = n_gauges = 0
        is_sub = bool(drv["company"]); owned = drv["owned"]
        stops.append(dict(kind="yard", name=drv["yard"].name, loc_id=drv["yard"].id, arrive=t_start, depart=t_start, miles=0))
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
            if nd["kind"] == "gauge":
                stops.append(dict(kind="gauge", name=nd["loc"].name, loc_id=nd["loc"].id, arrive=arrive, depart=arrive + svc, miles=mi, drive_min=dm,
                                  tank=nd.get("tank"), lane="", account="", priority=""))
                n_gauges += 1; gauged.add(nd["gkey"]); prev_node = node; idx = sol.Value(routing.NextVar(idx)); continue
            stop = dict(kind=nd["kind"], name=nd["loc"].name, loc_id=nd["loc"].id, arrive=arrive, depart=arrive + svc, miles=mi, drive_min=dm,
                        load_id=L["req"].id, unit=L["unit"], load_row_id=L["load_row_id"], ref=L["ref"], lane=f'{L["lane"].pickup.name} → {L["lane"].dropoff.name}',
                        account=L["lane"].account or "", bbl=L["bbl"], priority=L["priority"], deadline=L["deadline"], tank=L.get("tank"),
                        gauge=bool(L.get("is_gauge_load")) and nd["kind"] == "pickup")
            if nd["kind"] == "dropoff":
                sp = inp.sub_pay(drv, L) if is_sub else 0.0
                stop.update(rev=L["rev"], rev_fsc=L["rev"] * (1 + inp.fsc), pay=L["pay"] if (not is_sub or owned) else 0.0,
                            sub_pay=round(sp, 2), margin=round(L["rev"] - sp, 2) if is_sub else None)
                rev += L["rev"]; n_loads += 1; assigned.add(id(L))
                if L.get("is_gauge_load"): gauged.add(L["gkey"])
                if is_sub: subpay += sp
                if not is_sub or owned: pay += L["pay"]
            stops.append(stop)
            prev_node = node
            idx = sol.Value(routing.NextVar(idx))
        end_node = mgr.IndexToNode(idx)
        mi = miles_mat[prev_node][end_node]; dm = drive_mat[prev_node][end_node]
        empty_mi += mi; drive_m += dm
        t_end = sol.Value(time_dim.CumulVar(idx))
        stops.append(dict(kind="yard", name=drv["yard"].name, loc_id=drv["yard"].id, arrive=t_end, depart=t_end, miles=mi, drive_min=dm))
        duty_m = t_end - t_start
        used = n_loads > 0 or n_gauges > 0
        hrs = duty_m / 60 if duty_m else 0
        if is_sub and not owned:
            # an outside sub-hauler: Petrol's side is margin only; the sub gets its share plus the whole surcharge
            fuel = 0.0; insp = 0.0
            profit = rev - subpay
            profit_fsc = profit
        else:
            # our own truck (or a Petrol-owned sub, which is really our truck with a different name on the door)
            fuel = (loaded_mi + empty_mi) / inp.mpg * inp.diesel
            insp = inp.inspection_cost if used else 0
            profit = rev - pay - fuel - insp
            profit_fsc = rev * (1 + inp.fsc) - pay - fuel - insp
        shifts.append(dict(
            driver=drv["driver"].name, driver_id=drv["driver"].id, yard=drv["yard"].name, shift=drv["shift"], truck=drv["truck"],
            company=drv["company_name"], is_sub=is_sub, owned=owned, share_pct=drv["share_pct"], tier=drv["tier"], can_gauge=drv["can_gauge"],
            used=used, loads=n_loads, gauges=n_gauges, stops=stops if used else [], start=t_start, end=t_end,
            duty_hours=round(hrs, 2), drive_hours=round(drive_m / 60, 2), loaded_miles=round(loaded_mi, 1), empty_miles=round(empty_mi, 1),
            revenue=round(rev, 2), revenue_fsc=round(rev * (1 + inp.fsc), 2), driver_pay=round(pay, 2), fuel=round(fuel, 2),
            inspection=round(insp, 2), sub_pay=round(subpay, 2), sub_fsc=round(rev * inp.fsc, 2) if is_sub else 0.0,
            profit=round(profit, 2), profit_fsc=round(profit_fsc, 2),
            per_hour=round(rev / hrs, 2) if hrs else 0, per_hour_fsc=round(rev * (1 + inp.fsc) / hrs, 2) if hrs else 0,
            margin_per_hour=round((rev - subpay) / hrs, 2) if (hrs and is_sub) else None,
            drive_cap_h=drv["drive_cap"] / 60, duty_cap_h=drv["duty_cap"] / 60,
            meets_target=(rev / hrs >= inp.target) if hrs else False))

    unassigned = []
    for L in inp.loads:
        if id(L) not in assigned:
            why = ""
            if L.get("needs_gauger"): why = "needs a gauger — no driver who can gauge is working today"
            elif L.get("gkey") and L["gkey"] not in gauged: why = "the gauge for this tank didn't fit in the gauger's day"
            elif len(L.get("banned", [])) == n_veh: why = "every available driver/truck is barred from the pickup or drop-off"
            elif L.get("banned"): why = f'{len(L["banned"])} driver(s) barred from this site'
            unassigned.append(dict(load_id=L["req"].id, unit=L["unit"], load_row_id=L["load_row_id"], ref=L["ref"], lane=f'{L["lane"].pickup.name} → {L["lane"].dropoff.name}',
                                   account=L["lane"].account or "", priority=L["priority"], deadline=L["deadline"], days_left=L["days_left"], rev=round(L["rev"], 2),
                                   profit=round(L["rev"] - L["pay"], 2), tank=L.get("tank"), why=why))
    used_shifts = [x for x in shifts if x["used"]]
    own = [x for x in used_shifts if not x["is_sub"] or x["owned"]]   # $/hr target is about our own trucks (incl. Petrol-owned subs)
    own_hours = sum(x["duty_hours"] for x in own)
    tot_hours = sum(x["duty_hours"] for x in used_shifts)
    totals = dict(
        loads=sum(x["loads"] for x in used_shifts), drivers_used=len(used_shifts), drivers_available=len(shifts),
        own_loads=sum(x["loads"] for x in own), sub_loads=sum(x["loads"] for x in used_shifts if x["is_sub"] and not x["owned"]),
        gauges=sum(x["gauges"] for x in used_shifts),
        revenue=round(sum(x["revenue"] for x in used_shifts), 2), revenue_fsc=round(sum(x["revenue_fsc"] for x in used_shifts), 2),
        driver_pay=round(sum(x["driver_pay"] for x in used_shifts), 2), fuel=round(sum(x["fuel"] for x in used_shifts), 2),
        inspection=round(sum(x["inspection"] for x in used_shifts), 2),
        sub_pay=round(sum(x["sub_pay"] for x in used_shifts), 2), sub_fsc=round(sum(x["sub_fsc"] for x in used_shifts), 2),
        profit=round(sum(x["profit"] for x in used_shifts), 2), profit_fsc=round(sum(x["profit_fsc"] for x in used_shifts), 2),
        hours=round(tot_hours, 2), own_hours=round(own_hours, 2), miles=round(sum(x["loaded_miles"] + x["empty_miles"] for x in used_shifts), 1),
        per_hour=round(sum(x["revenue"] for x in own) / own_hours, 2) if own_hours else 0,
        per_hour_fsc=round(sum(x["revenue_fsc"] for x in own) / own_hours, 2) if own_hours else 0,
        unassigned=len(unassigned), unassigned_must=sum(1 for u in unassigned if u["priority"] == "today"),
        target=inp.target, fsc_pct=inp.fsc, diesel=inp.diesel, solver_seconds=time_limit_s)
    # per-company roll-up (what each sub is owed, and Petrol's margin on their loads)
    by_co = {}
    for x in used_shifts:
        key = x["company"] or "Petrol Transport (own trucks)"
        c = by_co.setdefault(key, dict(company=key, is_sub=x["is_sub"], owned=x["owned"], drivers=0, loads=0, bbl=0.0, revenue=0.0, sub_pay=0.0, sub_fsc=0.0, profit=0.0, hours=0.0))
        c["drivers"] += 1; c["loads"] += x["loads"]; c["hours"] += x["duty_hours"]
        c["revenue"] += x["revenue"]; c["sub_pay"] += x["sub_pay"]; c["sub_fsc"] += x["sub_fsc"]; c["profit"] += x["profit"]
        c["bbl"] += sum(st.get("bbl", 0) for st in x["stops"] if st["kind"] == "dropoff")
    by_company = sorted(by_co.values(), key=lambda c: (c["is_sub"] and not c["owned"], c["is_sub"], c["company"]))
    for c in by_company:
        for k in ("bbl", "revenue", "sub_pay", "sub_fsc", "profit", "hours"): c[k] = round(c[k], 2)
    return dict(ok=True, plan_date=plan_date, generated_at=datetime.utcnow().isoformat(timespec="seconds"),
                shifts=shifts, unassigned=unassigned, totals=totals, by_company=by_company)


def save_plan(s: Session, result: dict) -> Plan:
    t = result["totals"]
    p = Plan(plan_date=result["plan_date"], result_json=json.dumps(result),
             summary=f'{t["loads"]} loads, {t["drivers_used"]} drivers, ${t["revenue"]:,.0f} base (${t["per_hour"]:,.0f}/hr), '
                     f'{t["unassigned"]} unassigned')
    s.add(p); s.commit()
    return p
