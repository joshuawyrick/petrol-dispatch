"""Fuel surcharge (FSC) and the weekly EIA California diesel price.

FSC rule (Josh's Master Fuel Surcharge sheet): base price $5.50/gal; for every $0.10 the EIA weekly California
No. 2 ULSD retail average sits above the base, 0.60% is added to the freight charge (barrels x rate).
The surcharge is billed to the customer but NOT paid through to drivers.
"""
import io, math
from datetime import datetime
import httpx
from sqlalchemy.orm import Session
from db import Setting

EIA_XLS_URL = "https://www.eia.gov/dnav/pet/xls/PET_PRI_GND_DCUS_SCA_W.xls"
EIA_API_URL = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
EIA_SERIES = "EMD_EPD2D_PTE_SCA_DPG"      # California No 2 Diesel Retail Prices, weekly, $/gal


def fsc_pct(diesel_price, base=5.50, step=0.10, step_pct=0.006):
    """Surcharge as a fraction of freight (0.036 = 3.6%). 5.50-5.59 -> 0.6%, 5.60-5.69 -> 1.2%, ..."""
    if not diesel_price or diesel_price < base:
        return 0.0
    steps = math.floor((diesel_price - base) / step + 1e-9) + 1
    return round(steps * step_pct, 6)


def fsc_from_settings(st: dict):
    return fsc_pct(st.get("diesel_price"), st.get("fsc_base_price") or 5.50,
                   st.get("fsc_step_price") or 0.10, st.get("fsc_step_pct") or 0.006)


def _set(s: Session, key, value, label=None, unit=None, note=None, sort=99):
    row = s.get(Setting, key)
    if not row:
        row = Setting(key=key, label=label or key, unit=unit, note=note, sort=sort); s.add(row)
    row.value = value


def fetch_eia_price(api_key: str | None = None):
    """Return (price, week_ending 'YYYY-MM-DD', source). Tries the EIA API (if a key is set), then the public XLS."""
    if api_key:
        try:
            params = {"api_key": api_key, "frequency": "weekly", "data[0]": "value", "facets[series][]": EIA_SERIES,
                      "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 1}
            r = httpx.get(EIA_API_URL, params=params, timeout=30)
            r.raise_for_status()
            row = r.json()["response"]["data"][0]
            return float(row["value"]), str(row["period"])[:10], "EIA API"
        except Exception as e:
            api_err = f"EIA API: {e}"
    else:
        api_err = None
    # public spreadsheet (no key needed)
    import pandas as pd
    r = httpx.get(EIA_XLS_URL, timeout=60, follow_redirects=True, headers={"User-Agent": "petrol-dispatch/1.0"})
    r.raise_for_status()
    xls = pd.ExcelFile(io.BytesIO(r.content))
    for sheet in xls.sheet_names:
        if not sheet.lower().startswith("data"): continue
        df = xls.parse(sheet, header=None)
        key_rows = df.index[df.iloc[:, 0].astype(str).str.strip().str.lower() == "sourcekey"].tolist()
        if not key_rows: continue
        keys = df.iloc[key_rows[0]].astype(str).str.strip().tolist()
        if EIA_SERIES not in keys: continue
        col = keys.index(EIA_SERIES)
        data = df.iloc[key_rows[0] + 2:, [0, col]].dropna()
        data = data[pd.to_numeric(data.iloc[:, 1], errors="coerce").notna()]
        last = data.iloc[-1]
        date = pd.to_datetime(last.iloc[0]).strftime("%Y-%m-%d")
        return float(last.iloc[1]), date, "EIA XLS"
    raise RuntimeError((api_err + "; " if api_err else "") + f"series {EIA_SERIES} not found in EIA spreadsheet")


def refresh_diesel_price(s: Session, api_key: str | None = None, force: bool = False):
    """Update the diesel price from EIA if it is older than a day (or forced). Returns a status message."""
    st = {x.key: x for x in s.query(Setting).all()}
    last = st.get("eia_last_check")
    if not force and last and last.note:
        try:
            if (datetime.utcnow() - datetime.fromisoformat(last.note)).total_seconds() < 24 * 3600:
                return None
        except ValueError:
            pass
    try:
        price, week, source = fetch_eia_price(api_key)
    except Exception as e:
        _set(s, "eia_last_check", None, "EIA last check", "", datetime.utcnow().isoformat(timespec="seconds"), 90)
        s.commit()
        return f"Could not fetch EIA diesel price ({type(e).__name__}: {str(e)[:120]}). Enter it manually on Settings."
    _set(s, "diesel_price", price)
    _set(s, "eia_price_week", None, "EIA price week ending", "", week, 91)
    _set(s, "eia_last_check", None, "EIA last check", "", datetime.utcnow().isoformat(timespec="seconds"), 90)
    s.commit()
    return f"EIA California diesel: ${price:.3f}/gal (week ending {week}, via {source})."
