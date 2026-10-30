# Petrol Dispatch – Project Notes (handoff file)

**Read this first in every new session.** It is the shared memory for this project: what the app is, how it is built,
what we decided and why, what has been delivered, and what is still open. Keep it updated whenever something changes
(add to "Change log" and fix any section that became wrong).

Last updated: 2026-10-30 (through phase 12; code checked against the repo on Josh's computer).

---

## 0. Project instructions (paste into the Claude project's instructions)

> This project is the Petrol Transport dispatch app (repo joshuawyrick/petrol-dispatch, deployed on Render).
> Start every chat by reading PROJECT_NOTES.md. Josh is a beginner: give one step at a time and wait for him to say it's
> done before the next. The code lives in his folder Documents\GitHub\petrol-dispatch; read the real files before
> changing anything, edit them there, and have him Commit + Push in GitHub Desktop. Update PROJECT_NOTES.md (status,
> open items, change log) at the end of every change. Keep other projects (time sheet app, shift planner) out of this one.

## 1. Who and how to work with Josh

- Josh owns **Petrol Transport Inc.** (Bakersfield, CA), a 24/7 oil-hauling company (crude, naphtha, diesel) paid per barrel.
  Office: 5502 S Granite Rd, Bakersfield, CA 93308. 24-hour dispatch phone 661-393-6514.
- Josh is a **complete novice at coding**. Give **step-by-step** instructions, **one step at a time**, and wait for him to
  confirm each step is done before giving the next. Plain language, no jargon.
- Workflow: GitHub repo `joshuawyrick/petrol-dispatch` → Render auto-deploys on push. Claude cannot push to GitHub. Josh
  commits and pushes with **GitHub Desktop** (earlier phases he uploaded via the GitHub website). If the repo folder is
  connected to the session, edit the files in place and tell him to Commit + Push in GitHub Desktop. If not connected,
  deliver a full zip of the app that he extracts over his repo folder.
- Name zips by phase number and keep numbers increasing (he caught a mislabeled zip once). Latest delivered: **phase 10**.
- Keep the UI intuitive and clean (his repeated preference).
- Other projects exist and are **separate repos, not part of this one**: Shift Planner (sellable driver-less planner,
  `shift-planner`) and PTI Time Sheet (driver time sheet app + office website, `pti-timesheet`). Do not mix them into this app.

## 2. What the app does

A daily **profit-maximizing dispatch planner** for dispatchers. Loads called in for a day + drivers available → the most
profitable set of driver shifts that respects hours-of-service. Long-term vision: grow into a full dispatch + ticketing
system (Android tablets for drivers, BOL printing on Brother RJ4230B-class thermal printers, billing, driver pay).
Dispatchers are hard to find and train to hit the profit targets, hence the optimizer.

## 3. Deployment

- **Render Blueprint** (`render.yaml`), both services "Blueprint managed", Oregon. Checked from Josh's screenshots 10/22/2026:
  - Web service `petrol-dispatch` = **Free** plan (0.1 CPU, 512 MB). It spins down when idle, so the first visit can take
    50+ seconds, and the optimizer runs on 0.1 CPU. Starter is $7/mo (0.5 CPU). URL: https://petrol-dispatch.onrender.com
  - Database `petrol-dispatch-db` = **paid `0.1c-256mb` plan** (PostgreSQL 18, 1 GB storage, 8.6% used on 10/22,
    storage autoscaling off; dashboard showed it "updated 7d" ago). Being on a paid plan, it is safe from Render's free-database deletion
    (free DBs expire after 30 days + 14 days grace, then get deleted).
  - `render.yaml` still says `plan: free` for BOTH. Make the yaml match the real plans so a Blueprint sync can't
    try to change them.
  - The database allows connections from anywhere (0.0.0.0/0, password-protected). The app only needs Render's internal
    network, so the rule could be removed (re-add it only if connecting from a PC).
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Build: `pip install -r requirements.txt`.
- **Gotchas:** must use `psycopg[binary]` v3 and pin Python 3.12 (Render defaulted to 3.14 and psycopg2 broke).
- App runs in **Pacific time** (`TZ` set at startup from `APP_TZ`, default America/Los_Angeles).
- Environment variables (Render → service → Environment):
  - `DATABASE_URL` (from the database, automatic)
  - `DISPATCH_PASSWORD` – shared login password; `SESSION_SECRET` optional
  - `GOOGLE_MAPS_API_KEY` – server key (Routes API, Static Maps)
  - `GOOGLE_MAPS_BROWSER_KEY` – browser-restricted key (Maps JavaScript + Directions) for the route-approval map
  - `SAMSARA_API_TOKEN` – needs "Read Drivers", "Read ELD Compliance Settings (US)", and for time-in-status also
    "Read ELD Hours of Service (US)" (Josh may not have added that scope yet)
  - `EIA_API_KEY` – optional (diesel price also works from the public XLS)
  - `APP_TZ` – optional timezone override
- Local run: `sh run.sh` (SQLite at `data/dispatch.db`, seeds from `data/*.csv` on first start).
- Not yet done: custom domain on a petroltransportinc.com subdomain (company site is Wix); deferred until the engine is solid.

## 4. Code map (FastAPI + SQLAlchemy 2 + Jinja2 + OR-Tools)

| File | Purpose |
|---|---|
| `main.py` | All routes (~1,200 lines): login, settings, locations, lanes, mileage, route approval, JMP, companies, drivers, load board/log, standing orders, daily plan, exports |
| `db.py` | Models + `init_db()` which creates tables and does forward-only `ALTER TABLE ADD COLUMN` migrations |
| `seed.py` | Settings defaults, companies, first-run import of `data/locations.csv`, `lanes.csv`, `drivers.csv` |
| `optimizer.py` | OR-Tools routing engine (`solve`, `save_plan`) |
| `hos.py` | Hours-of-service projection for today vs later days (see §6) |
| `samsara.py` | Driver sync, HOS clocks, status-since (HOS logs) |
| `mileage.py` | Road-mile cache: needed pairs, Google Routes fetch, straight-line fallback (×1.30), overrides |
| `jmp.py` | Journey Management Plan PDF generator (reportlab, IOGP 365 style) |
| `loads.py` | Per-load tracking (Load / LoadEvent), mark hauled / cancelled / rejected / moved |
| `urgency.py` | today / tomorrow / flex priority from deadline |
| `fsc.py` | Fuel surcharge + EIA weekly California diesel price |
| `templates/` | Jinja2 pages (day.html is the daily plan, ~250 lines) |
| `static/style.css` | Styling |
| `data/*.csv` | Starting data only (locations 209 rows, lanes 184, drivers). Live data is in the database |

Pages: dashboard, settings, locations (+tanks, restrictions), lanes (scenarios), mileage, route approval (`/route/{a}/{b}`),
JMPs, company info, companies (sub-haulers), drivers, load log, standing orders, daily plan (`/day/{date}`), plan print.
CSV exports: `/export/locations.csv`, `/export/lanes.csv`, `/export/mileage.csv`, `/loads.csv`, `/day/{date}/hours.csv` (driver hours for a day).

Main tables: Setting, Location (kind pickup|dropoff|both|yard; lat/lon; avg_bbl_history / avg_bbl_override; open/close
times; max trucks; requires_gauging), Tank, LocationRestriction (per driver or per truck), Lane (scenario: account,
pickup, dropoff, rate, driver_pay, product, JMP fields), Distance (google miles + override + approved route, polyline,
steps), Company, CompanyLaneRate, Driver, DriverDay (per-day availability, hours, live Samsara snapshot), LoadRequest
(a line on a day), Load (one row per physical load, id like L000123), LoadEvent, StandingOrder, CompanyInfo, JmpDoc, Plan.

## 5. Business rules and decisions (all stated by Josh)

**Money**
- Paid per barrel; each scenario (pickup → drop-off, per account) has a company $/bbl and a driver $/bbl.
- **Minimum load = 150 bbl** (a 130-bbl load bills as 150); applies to driver pay too. Setting `min_bbl`.
- Avg barrels live **per pickup location** (not per scenario); dispatcher can pin an override. Load entry bbl is optional
  (blank = pickup average). Locations with no history use the default.
- Driver pay ≈ 22.5–23.5% of company rate (per-scenario driver pay is in lanes) + **45 min/day at California minimum wage**
  for pre/post-trip inspection. Trucks ~5.5 mpg. Fuel cost = miles / mpg × diesel price.
- **Target: $135 per shift hour gross, yard to yard**, measured WITHOUT fuel surcharge; show both with and without FSC.
  OK to lose on some loads if daily total profit is higher.
- **Fuel surcharge:** base $5.50/gal; every $0.10 the EIA weekly California No. 2 ULSD average is above base adds 0.60% to
  the freight charge (bbl × rate). Billed to customers, **not paid to drivers**; sub-haulers keep all of it. Doesn't change routing.

**Sub-haulers / companies**
- Petrol keeps 10% (sub gets 90%) for MKB, King D, Copperhead Oil Field Services, Quail Canyon, Flying B; Petrol keeps 25%
  (sub gets 75%) for J&V, Transportillo, California Coast Services, J Oregon Trucking, Maye Trucking, Lucas Trucking.
  Some lanes have special per-sub rates (CompanyLaneRate).
- **Assignment priority tiers:** Petrol drivers/trucks and Petrol-owned subs (Copperhead, Quail Canyon, Flying B) = tier 1;
  25% subs = tier 2; 10% non-owned subs = tier 3. Setting `priority_step` (default 30%) controls how strongly tiers matter.
- Petrol-owned subs are a hybrid: sub share for settlement, but truck revenue / driver pay like Petrol drivers.
- Not all subs have Samsara; dispatch phones them for hours. Show sub drivers like company drivers.

**Hours of service**
- Shift ≤ 16 h on duty (hard wall, minus buffer `duty_buffer_hours` 0.5), ≤ 10 h driving (minus `drive_buffer_hours` 0.5),
  10 h min off between shifts, 34 h reset after 80 h on duty; per-driver limits and days off; reset patterns vary per driver.
- Yards: Petrol Bakersfield (35.4544, -119.036), Petrol Santa Maria (34.9219, -120.468), Petrol Coalinga (36.1142, -120.372).
  Trucks leave from and return to the same home yard. Some drivers share trucks (handoff 30 min).
- Default speed 41 mph; load 60 min, unload 60 min (overridable per location); gauge 15 min.

**Loads**
- Priorities: **today / tomorrow / flex** (flex = within N days, setting `flex_days` = 5). Stored as a deadline
  (`must_go_by`); the word shown is computed from days left, so a slid load gets more urgent automatically.
- Standing orders create recurring daily loads per weekday (auto-added the first time a day is opened).
- Every physical load has a tracking number and event log; nothing is deleted (removed = marked). Outcomes: hauled (with
  completion date + driver), cancelled, rejected (e.g. high BS&W, low temperature), moved to another day. Partial marking allowed.
- Gauging: per tank per batch, ~15 min, gauger must be on site before others haul; only certain drivers can gauge; the
  gauger gauges while loading when he hauls. Restrictions are per truck (size) or per driver (training), no allow-only sites.
- Hazmat heavy trucks can't use some map routes: each leg's route is **approved once** (draggable Google Directions map);
  engine then uses approved miles (and the reverse direction). Unreviewed legs show yellow in plans.
- JMP (Journey Management Plan) per lane: customers always want GPS coords for pickup and drop-off; accepted in Petrol's
  format if it follows a standard (IOGP 365). Signatures: Company Safety Representative + reviewer.

**Lost Hills #2 → AFS N Midway (biggest lane)**
- One loading lane (rack) at the pickup, 3 offload lanes at the drop-off; loads are always 160 bbl; yard ↔ lease ~1.25 h
  each way; drivers on this lane are dedicated to it all shift (~7–8 h of loads within 10 drive / 16 duty).
- Demurrage on this lane is billed $125/hr in quarter-hour steps (7.5+ min = 0.25 h, 22.5+ min = 0.5 h …). CRC wants
  wait time eliminated. Wait caused by low oil, the operator, or site issues is chargeable; waiting on Petrol's own trucks
  (including Josh's sub-haulers at Lost Hills) is not.

## 6. HOS projection (phase 10, `hos.py`)

- **Today:** plan with live Samsara clocks (drive/shift/cycle remaining).
- **Any later day:** worst-case estimate. Assume every working (ticked IN) driver uses ALL remaining hours today:
  cycle tomorrow = Samsara "cycle tomorrow" − min(shift hours remaining, cycle remaining). A driver showing 10 drive / 16
  shift hasn't started and is assumed to work the full shift. New day starts with full 10 drive / 16 shift; later days chain
  (each working day uses a full shift or what the cycle allows). Real Samsara numbers replace the estimate when that day arrives.
- **Earliest legal start tomorrow** = worst-case end of today's shift (live clock if on shift now; otherwise planned start +
  16 h) + 10 h. Enforced by the optimizer unless the dispatcher typed a start time; a typed earlier start shows "before legal start".
- **Time in current status** shown (off-duty/sleeper treated as one stretch), with "reset in …" / "reset ✓" against the 10 h.
- **Low cycle warning:** a working driver whose cycle (today's live or later-day estimate) is under `low_cycle_hours` (16)
  is highlighted amber with an "Are they really working?" banner; buttons "Working — limit to X h" / "Not working".

## 7. Delivery history

| Phase | Date | What |
|---|---|---|
| 1 | 9/8/2026 | Master data (locations, lanes, yards), settings, Google road-mileage cache (7,423 pairs) |
| 2 | 9/8 | Shared password login, FSC + EIA diesel, Drivers page, Daily Plan page |
| 3 | 9/8 | Optimizer (OR-Tools), plan view with $/hr with/without FSC, print view |
| 3b | 9/8 | Samsara driver sync + HOS pull; Standing Orders; deployed to Render |
| 4 | 9/8 | Per-leg route approval with draggable Google map |
| 5 | 9/9 | JMP PDF generator, company details, JMP list |
| 6 | 9/9 | Companies / sub-hauler pay, driver import |
| 7 | 9/9 | Priority tiers, gauging + tanks, location restrictions, approve-and-rebuild |
| 8 | 9/10 | today/tomorrow/flex deadline urgency, one form for load lines, move/remove, bring-forward notice |
| 9 | 9/10 | Per-load tracking, Mark hauled/cancelled/rejected/moved, Load Log, undo/reopen |
| 10 | 10/5 | HOS projection, time in status, low-cycle warning, Pacific time (zip also named phase9 earlier: same code) |
| 12 | 10/30 | Driver hours export: Step 2 buttons "⬇ Hours for <next day>" and "⬇ Hours for <this day>" → `/day/{date}/hours.csv` (same numbers as the Step 2 table, plus usable on-duty/drive hours = shift or drive capped by cycle). Numbered 12 to avoid confusion with the withdrawn phase-11 zip |

(A "phase 11" zip contained a time-sheet feature built inside this app; it was **withdrawn**. The time sheet is a separate
app. Ignore that zip.)

## 7b. Where things stand (checked 2026-10-22)

- GitHub `main` = phase 10. Last push 10/5/2026 ("Update driver hours algorithm for tomorrow shift predictions").
  25 commits since 9/8. The code on Josh's computer is identical to the phase-10 zip.
- Smoke test of the repo code (fresh SQLite database, no API keys): all 26 pages and CSV exports load (HTTP 200),
  adding a load and building a plan work. There are **no automated tests in the repo** yet.
- `PROJECT_NOTES.md` was not in the repo yet on 10/22 (Josh still needs to commit it).

## 8. Open items / next ideas

- Phase 10 is LIVE on Render (deploy 1bb4a4e, confirmed 10/22). Database already on a paid plan (confirmed 10/22).
- Decide whether to move the web service from Free to Starter ($7/mo): no 50-second wake-up, faster plan building.
- Confirm the Samsara token has the "Read ELD Hours of Service (US)" scope.
- **Bug/gap found 10/22: "Max trucks at once" on a location is saved but the optimizer ignores it.** Matters most at
  Lost Hills #2 (one loading rack): the planner can send several trucks to arrive together, creating wait time.
  Fix: limit overlapping load/unload visits per site in `optimizer.py`, or stagger arrivals.
- `render.yaml` still says `plan: free` for both services while the database is really on `0.1c-256mb`. Update it to
  match the real plans so a Blueprint sync can't change anything.
- Housekeeping: README.md is stale (still says "Phase 1"); `.replit` is a leftover from Replit; `@app.on_event`
  is deprecated in FastAPI (works today). Add a small automated test (like the 10/22 smoke test) so every change is checked.
- Login: one shared password, no lock-out after wrong tries. Fine for now; consider per-dispatcher logins later.
- **Open-loads report import** (not built): Josh's report `TICKETS_EXPORT_97.xlsx` (Bill Of Lading #, Load Called in Date,
  Pickup Account Name, Pickup Name, Tank Name, Pickup Operator, Drop Off Account Name, Drop Off Name, Scenario Name; 78 rows).
  Proposal: "Import open loads" button with preview, group into load lines, BOL numbers as tracking IDs, dedupe on re-import,
  name alias mapping, flag lanes without rates. **Three unanswered questions:** which day imported loads land on; what to do
  when a load disappears from the next report (lean: flag only); what system produces the report (PakEnergy).
- Kern Energy → General Carbon lane has no rate yet.
- 150-bbl floor: Josh asked for a scenario average-barrels sheet with blanks and values under 150 set to 150. Ask whether the
  app should apply that floor to location averages (the engine already floors valuation at `min_bbl`).
- Upgrade Postgres from free tier; custom domain; Samsara yard-out/yard-in timestamps (planned vs actual shift hours);
  tablet driver app + BOL ticketing (measurement engine: API 11.1 math, call-in batches, meter factors that persist
  effective-dated) as the long-term phases; invoicing and driver pay.
- Known data note: the dispatch app has no static full mileage table; legs are fetched from Google on demand and stored in
  `distances`. A leg may be a Google value, a manual override, or a straight-line estimate (marked as such).

## 9. Change log

- 2026-10-15: Created this file.
- 2026-10-22: Checked the real repo; added status (§7b), Render free-database deadline, the ignored "Max trucks at once"
  gap, render.yaml/README housekeeping, Lost Hills lane facts, and the "Project instructions" block (§0).
- 2026-10-22 (later): Josh's Render screenshots: phase 10 live; DB already paid (0.1c-256mb); web service is on FREE
  (not Starter as earlier notes said); DB open to 0.0.0.0/0.
- 2026-10-30: Phase 12, driver hours CSV export (main.py `driver_hours_rows` + route, day.html buttons, style.css `.btn.small`). Files written straight into Josh's folder; he commits + pushes.
