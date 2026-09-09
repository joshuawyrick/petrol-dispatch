"""Journey Management Plan (JMP) generator.

Builds a PDF per lane from the approved route, structured after IOGP Report 365 (land transportation safety,
journey management): trip summary with GPS coordinates, mapped route with turn-by-turn directions, route hazards
and controls, driver fitness / driving-hour limits, communications & check-ins, emergency response, approvals.
"""
import io, json, os, re, html
from datetime import datetime
import httpx
from sqlalchemy.orm import Session
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak, KeepTogether)
from db import Setting, Location, Lane, Distance, CompanyInfo, JmpDoc
import mileage as mileage_mod

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
STATIC_URL = "https://maps.googleapis.com/maps/api/staticmap"


# ---------------------------------------------------------------- data helpers
def company(s: Session) -> CompanyInfo:
    c = s.query(CompanyInfo).first()
    if not c:
        c = CompanyInfo(id=1, name="Petrol Transport Inc.", address="5502 S Granite Rd, Bakersfield, CA 93308",
                        dispatch_phone="661-393-6514 (24 hours)", emergency_phone="661-393-6514 (24 hours)",
                        prepared_by_title="Company Safety Representative", approved_by_title="Reviewed by", require_manager_signature=True,
                        checkin_rule="Driver checks in with dispatch by phone or Samsara message at: departure from yard, "
                                          "arrival at pickup, departure loaded, arrival at drop-off, and return to yard.",
                        overdue_rule="If a check-in is more than 30 minutes overdue, dispatch calls the driver. If there is no contact "
                                     "within 15 more minutes, dispatch notifies the safety contact and begins the emergency procedure.")
        s.add(c); s.commit()
    return c


def leg(s: Session, a: int, b: int) -> Distance | None:
    return s.query(Distance).filter_by(origin_id=a, dest_id=b).first()


def ensure_steps(s: Session, d: Distance, api_key: str) -> list:
    """Return turn-by-turn steps for an approved leg, fetching them from Google's Routes API (through the saved via
    points) if they weren't stored at approval time."""
    if d.steps_json:
        try: return json.loads(d.steps_json)
        except Exception: pass
    if not api_key: return []
    o, t = d.origin, d.dest
    body = {"origin": {"location": {"latLng": {"latitude": o.lat, "longitude": o.lon}}},
            "destination": {"location": {"latLng": {"latitude": t.lat, "longitude": t.lon}}},
            "travelMode": "DRIVE", "routingPreference": "TRAFFIC_UNAWARE", "units": "IMPERIAL"}
    via = []
    try: via = json.loads(d.via_json) if d.via_json else []
    except Exception: via = []
    if via:
        body["intermediates"] = [{"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}, "via": True} for p in via]
    headers = {"X-Goog-Api-Key": api_key,
               "X-Goog-FieldMask": "routes.distanceMeters,routes.polyline.encodedPolyline,routes.legs.steps.navigationInstruction,routes.legs.steps.distanceMeters"}
    try:
        r = httpx.post(ROUTES_URL, json=body, headers=headers, timeout=30)
        r.raise_for_status()
        route = r.json()["routes"][0]
        steps = []
        for lg in route.get("legs", []):
            for st in lg.get("steps", []):
                ins = (st.get("navigationInstruction") or {}).get("instructions", "")
                if ins: steps.append({"instruction": ins, "miles": round(st.get("distanceMeters", 0) / 1609.344, 1)})
        d.steps_json = json.dumps(steps)
        if not d.polyline: d.polyline = (route.get("polyline") or {}).get("encodedPolyline")
        s.commit()
        return steps
    except Exception:
        return []


def static_map(d: Distance, api_key: str, size="640x400") -> bytes | None:
    """PNG of the approved route from Google's Static Maps API (server key). None if unavailable."""
    if not api_key: return None
    o, t = d.origin, d.dest
    params = {"size": size, "scale": "2", "maptype": "hybrid", "key": api_key,
              "markers": [f"color:green|label:P|{o.lat},{o.lon}", f"color:red|label:D|{t.lat},{t.lon}"]}
    if d.polyline: params["path"] = f"color:0x1a56dbff|weight:5|enc:{d.polyline}"
    try:
        r = httpx.get(STATIC_URL, params=params, timeout=30)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"): return r.content
    except Exception:
        pass
    return None


def hazards_of(lane: Lane) -> list:
    try: return json.loads(lane.jmp_hazards) if lane.jmp_hazards else []
    except Exception: return []


# ---------------------------------------------------------------- PDF
def _styles():
    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName="Helvetica", fontSize=9.5, leading=12.5)
    return dict(
        base=base,
        small=ParagraphStyle("small", parent=base, fontSize=8, leading=10, textColor=colors.HexColor("#555555")),
        h1=ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=16, leading=20, spaceAfter=2),
        h2=ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=10, spaceAfter=4,
                          textColor=colors.HexColor("#1F3864")),
        cell=ParagraphStyle("cell", parent=base, fontSize=8.8, leading=11),
        cellb=ParagraphStyle("cellb", parent=base, fontName="Helvetica-Bold", fontSize=8.8, leading=11),
    )


def _tbl(rows, widths, header=True, zebra=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFBFBF")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header: st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
    if zebra:
        for i in range(1 if header else 0, len(rows)):
            if i % 2 == 0: st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F3F5F8")))
    t.setStyle(TableStyle(st))
    return t


def _p(text, style): return Paragraph(html.escape(str(text if text is not None else "")).replace("\n", "<br/>"), style)


def _leg_section(story, S, title, d: Distance, st: dict, api_key: str, speed: float, load_min: int, unload_min: int, with_service: bool):
    o, t = d.origin, d.dest
    miles = d.override_miles if d.override_miles else d.google_miles
    drive_min = round((miles or 0) / speed * 60)
    story.append(Paragraph(title, S["h2"]))
    rows = [[_p("From", S["cellb"]), _p(f"{o.name}", S["cell"]), _p("GPS", S["cellb"]), _p(f"{o.lat:.6f}, {o.lon:.6f}", S["cell"])],
            [_p("To", S["cellb"]), _p(f"{t.name}", S["cell"]), _p("GPS", S["cellb"]), _p(f"{t.lat:.6f}, {t.lon:.6f}", S["cell"])],
            [_p("Approved route miles", S["cellb"]), _p(f"{miles:.1f} mi" if miles else "—", S["cell"]),
             _p("Planned drive time", S["cellb"]), _p(f"{drive_min} min at {speed:.0f} mph planning speed", S["cell"])],
            [_p("Route status", S["cellb"]), _p(("Approved by dispatch " + d.approved_at.strftime("%m/%d/%Y")) if d.approved else "NOT YET APPROVED — using map mileage", S["cell"]),
             _p("Route type", S["cellb"]), _p({"custom": "Dispatcher-adjusted route (via points)", "google-default": "Standard road route", "manual-miles": "Miles entered by dispatch"}.get(d.route_kind or "", "Standard road route"), S["cell"])]]
    if with_service:
        rows.append([_p("Time on site", S["cellb"]), _p(f"Loading allowance {load_min} min at pickup; offloading allowance {unload_min} min at drop-off", S["cell"]), "", ""])
    tbl = _tbl(rows, [1.25 * inch, 2.55 * inch, 1.15 * inch, 2.55 * inch], header=False, zebra=False)
    tbl.setStyle(TableStyle([("SPAN", (1, len(rows) - 1), (3, len(rows) - 1))] if with_service else []))
    story.append(tbl)
    png = static_map(d, api_key)
    if png:
        story.append(Spacer(1, 6))
        story.append(Image(io.BytesIO(png), width=7.2 * inch, height=4.5 * inch))
        story.append(_p("Map: approved route (blue), P = pickup, D = drop-off. Satellite imagery with road labels.", S["small"]))
    else:
        story.append(_p("Route map image unavailable (Maps Static API not reachable). The route is defined by the GPS points above and the directions below.", S["small"]))
    try: steps = json.loads(d.steps_json) if d.steps_json else []
    except Exception: steps = []
    if steps:
        story.append(Spacer(1, 6)); story.append(_p("Turn-by-turn directions (approved route)", S["cellb"]))
        srows = [[_p("#", S["cellb"]), _p("Instruction", S["cellb"]), _p("Miles", S["cellb"])]]
        for i, x in enumerate(steps, 1):
            srows.append([_p(i, S["cell"]), _p(re.sub("<[^>]+>", "", x.get("instruction", "")), S["cell"]), _p(f"{x.get('miles', 0):.1f}", S["cell"])])
        story.append(_tbl(srows, [0.4 * inch, 6.3 * inch, 0.8 * inch]))
    story.append(Spacer(1, 4))


def build_pdf(s: Session, lane: Lane, include_yard: Location | None, api_key: str, base_url: str = "") -> tuple[bytes, str]:
    st = {x.key: x.value for x in s.query(Setting).all()}
    speed = st.get("avg_speed_mph") or 41
    load_min, unload_min = int(st.get("load_minutes") or 60), int(st.get("unload_minutes") or 60)
    max_drive, max_duty = st.get("max_drive_hours") or 10, st.get("max_duty_hours") or 16
    c = company(s)
    S = _styles()
    buf = io.BytesIO()
    version = (s.query(JmpDoc).filter_by(lane_id=lane.id).count() or 0) + 1
    title = f"Journey Management Plan — {lane.pickup.name} to {lane.dropoff.name}"
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.65 * inch, rightMargin=0.65 * inch, topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                            title=title, author=c.name or "")
    story = []

    # ---- header
    story.append(Paragraph(html.escape(c.name or "Petrol Transport, Inc."), S["h1"]))
    story.append(_p(c.address or "", S["small"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("JOURNEY MANAGEMENT PLAN", ParagraphStyle("t", parent=S["h1"], fontSize=13, textColor=colors.HexColor("#1F3864"))))
    hdr = [[_p("Lane", S["cellb"]), _p(f"{lane.pickup.name}  →  {lane.dropoff.name}", S["cell"]), _p("JMP version", S["cellb"]), _p(f"{version}  ({datetime.now().strftime('%m/%d/%Y')})", S["cell"])],
           [_p("Customer / account", S["cellb"]), _p(lane.account or "—", S["cell"]), _p("Lease operator", S["cellb"]), _p(lane.operator or "—", S["cell"])],
           [_p("Product", S["cellb"]), _p(lane.product or "Crude oil (hazmat, UN1267 Class 3)", S["cell"]), _p("Vehicle type", S["cellb"]), _p("Tractor with crude tank trailer (DOT 407 / MC 307)", S["cell"])],
           [_p("Scope", S["cellb"]), _p(("Yard to pickup, then " if include_yard else "") + "pickup to drop-off. Prepared per IOGP Report 365 (Land transportation safety recommended practice — journey management).", S["cell"]), "", ""]]
    t = _tbl(hdr, [1.25 * inch, 2.55 * inch, 1.15 * inch, 2.55 * inch], header=False, zebra=False)
    t.setStyle(TableStyle([("SPAN", (1, 3), (3, 3))]))
    story.append(t)

    # ---- 1. journey necessity & summary
    story.append(Paragraph("1. Journey purpose and summary", S["h2"]))
    story.append(_p(f"Purpose: transport of {lane.product or 'crude oil'} from {lane.pickup.name} to {lane.dropoff.name} under contract with {lane.account or 'the customer'}. "
                    f"The journey is necessary (no alternative to road transport exists for this movement). The route below is the company-approved route for this lane; "
                    f"drivers follow it unless a road closure or emergency requires a deviation, which is reported to dispatch.", S["base"]))

    # ---- 2. legs
    legs = []
    if include_yard:
        dy = leg(s, include_yard.id, lane.pickup_id)
        if dy: legs.append((f"2a. Leg: {include_yard.name} (yard) → {lane.pickup.name}", dy, False))
    dl = leg(s, lane.pickup_id, lane.dropoff_id)
    if dl: legs.append((f"{'2b' if include_yard else '2'}. Leg: {lane.pickup.name} → {lane.dropoff.name} (loaded)", dl, True))
    if not legs:
        story.append(_p("No mileage on file for this lane yet.", S["base"]))
    for ttl, d, svc in legs:
        if not d.steps_json: ensure_steps(s, d, api_key)
        _leg_section(story, S, ttl, d, st, api_key, speed, load_min, unload_min, svc)

    # ---- 3. hazards
    story.append(Paragraph("3. Route hazards and controls", S["h2"]))
    hz = hazards_of(lane)
    hrows = [[_p("Hazard", S["cellb"]), _p("Where", S["cellb"]), _p("Control / driver action", S["cellb"])]]
    generic = [("Loaded tank trailer — high center of gravity, liquid surge", "Entire route", "Reduce speed on curves and ramps; brake early and smoothly; no abrupt lane changes."),
               ("Lease / unpaved access roads", "Approach to pickup", "Walking-pace speeds, watch for soft shoulders and washouts; do not enter flooded sections."),
               ("Rail crossings, low-visibility intersections", "As marked on route", "Full stop where required; confirm both directions clear before proceeding."),
               ("Reduced visibility (night, fog, dust)", "Entire route", "Headlights on; increase following distance; stop in a safe place if visibility is unsafe."),
               ("Loss of cell coverage", "Rural sections", "Check in before entering known dead zones; Samsara tracking remains active.")]
    for h in hz: hrows.append([_p(h.get("hazard", ""), S["cell"]), _p(h.get("location", ""), S["cell"]), _p(h.get("control", ""), S["cell"])])
    for g in generic: hrows.append([_p(g[0], S["cell"]), _p(g[1], S["cell"]), _p(g[2], S["cell"])])
    story.append(_tbl(hrows, [2.4 * inch, 1.5 * inch, 3.6 * inch]))
    if lane.jmp_notes: story.append(Spacer(1, 4)); story.append(_p("Lane notes: " + lane.jmp_notes, S["base"]))

    # ---- 4. driver fitness & hours
    story.append(Paragraph("4. Driver fitness, fatigue and driving hours", S["h2"]))
    story.append(_p(f"Drivers must be fit for duty, rested, and free of impairment before the journey. Company limits per shift: maximum {max_drive:.0f} hours driving and "
                    f"{max_duty:.0f} hours on duty from yard departure to yard return; minimum 10 consecutive hours off duty between shifts; 34-hour reset after 80 on-duty hours. "
                    f"A 30-minute break is taken before exceeding 4.5 hours of continuous driving (IOGP 365 Table 1). Hours are recorded on the Samsara ELD and reviewed by dispatch "
                    f"before assignment. Planned rest stop for this lane: {lane.jmp_rest_stop or 'not required — drive time is under 4.5 hours; the loading and offloading periods provide breaks from driving'}.", S["base"]))
    story.append(_p("Pre-trip: full vehicle and trailer inspection (brakes, tires, lights, hoses, valves, placards, spill kit, fire extinguisher, PPE) before leaving the yard. "
                    "Post-trip inspection on return. Defects are reported to dispatch and the vehicle is not dispatched until corrected.", S["base"]))

    # ---- 5. communications
    story.append(Paragraph("5. Communications and check-ins", S["h2"]))
    story.append(_p(c.checkin_rule or "", S["base"]))
    story.append(_p("Overdue procedure: " + (c.overdue_rule or ""), S["base"]))
    crow = [[_p("Dispatch (24 hr)", S["cellb"]), _p(c.dispatch_phone or "—", S["cell"]), _p("Emergency (24 hr)", S["cellb"]), _p(c.emergency_phone or "—", S["cell"])],
            [_p("Safety contact", S["cellb"]), _p(f"{c.safety_contact or '—'}  {c.safety_phone or ''}", S["cell"]), _p("Vehicle tracking", S["cellb"]), _p("Samsara GPS / ELD, monitored by dispatch", S["cell"])]]
    story.append(_tbl(crow, [1.25 * inch, 2.55 * inch, 1.15 * inch, 2.55 * inch], header=False, zebra=False))

    # ---- 6. emergency
    story.append(Paragraph("6. Emergency response", S["h2"]))
    story.append(_p("Injury, fire or serious collision: call 911 first, then dispatch. Move to a safe location upwind if there is a release; keep ignition sources away; "
                    "do not attempt to fight a tank fire. Spill or release: stop the flow if safe to do so, contain with the on-board spill kit, and notify dispatch immediately; "
                    "dispatch makes the required regulatory notifications" + (f" and activates spill response: {c.spill_response}." if c.spill_response else "."), S["base"]))
    story.append(_p("Breakdown: pull fully off the roadway, set warning triangles, remain with the vehicle unless unsafe, and call dispatch. Shipping papers, the SDS for the product, "
                    "and the ERG remain in the cab.", S["base"]))

    # ---- 7. approvals
    story.append(Paragraph("7. Journey authorization and closeout", S["h2"]))
    story.append(_p("This plan is issued for the lane above and remains valid until the route or conditions change, at which point a new version is issued. "
                    "Each trip on this lane is authorized by dispatch when assigned; the driver confirms arrival at the drop-off and return to the yard to close the journey.", S["base"]))
    sig = [[_p(c.prepared_by_title or "Prepared by", S["cellb"]), _p("Signature / date", S["cellb"])] +
           ([_p(c.approved_by_title or "Reviewed by", S["cellb"]), _p("Signature / date", S["cellb"])] if c.require_manager_signature else []),
           [Spacer(1, 26), Spacer(1, 26)] + ([Spacer(1, 26), Spacer(1, 26)] if c.require_manager_signature else [])]
    widths = [1.9 * inch, 1.85 * inch, 1.9 * inch, 1.85 * inch] if c.require_manager_signature else [3.75 * inch, 3.75 * inch]
    story.append(KeepTogether(_tbl(sig, widths, header=False, zebra=False)))
    story.append(Spacer(1, 6))
    story.append(_p(f"Generated by Petrol Dispatch on {datetime.now().strftime('%m/%d/%Y %H:%M')}. Coordinates are WGS84 decimal degrees. "
                    f"Route mileage from the company's approved-route library.", S["small"]))

    def footer(canvas, doc_):
        canvas.saveState(); canvas.setFont("Helvetica", 7.5); canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(0.65 * inch, 0.45 * inch, f"{c.name or ''} — JMP v{version}: {lane.pickup.name} → {lane.dropoff.name}")
        canvas.drawRightString(letter[0] - 0.65 * inch, 0.45 * inch, f"Page {doc_.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    summary = f"v{version} · {lane.pickup.name} → {lane.dropoff.name}" + (f" (+ from {include_yard.name})" if include_yard else "")
    return buf.getvalue(), summary
