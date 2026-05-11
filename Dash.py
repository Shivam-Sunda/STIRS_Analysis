"""
SOFR SR1 / SR3 Dashboard  –  v8
Run:  streamlit run sofr_dashboard.py
Excel:  date | sofr | icap | gc   (icap and gc optional)
State:  sofr_state.json  (auto-created alongside script)

Weekend handling: Excel contains ONLY business days. day_count for each
business day is computed as the calendar gap to the NEXT business day
(not hardcoded Friday=3). This correctly absorbs any weekend or holiday
gap — including month-start weekends such as March 1 (Sun) being carried
by Feb 27 (Fri) with day_count=3. No weekend rows are ever displayed.
SR3 compounding uses factor = 1 + (r/100) * (day_count/360).
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import date, timedelta
import calendar, os, json
import altair as alt

# ═══════════════════════════════════════════════════════════════════════════════
# PATHS & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATE_PATH  = os.path.join(BASE_DIR, "sofr_state.json")

GSHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1hNLXTFHkT42UI6grUxvyKbB1VFZwJy4OeGKxHntkWxQ/export?format=csv"
)

CASES       = ["Case1", "Case2", "Case3", "Case4", "Case5"]
ALL_COLS    = ["ICAP"] + CASES
DV01_SR1    = 25.0
DV01_SR3    = 25.0
TC_PER_LOT  = 1.0

TODAY       = date.today()
YESTERDAY   = TODAY - timedelta(days=1)

SOFR_Y_MIN  = 3.50          # fixed SOFR chart y-axis
SOFR_Y_MAX  = 3.75

# ═══════════════════════════════════════════════════════════════════════════════
# ROUNDING  — round-half-up (Decimal-style), not Python banker's rounding
# ═══════════════════════════════════════════════════════════════════════════════

import decimal as _dec

def round_half_up(value: float, decimals: int) -> float:
    q = _dec.Decimal(10) ** -decimals
    return float(_dec.Decimal(str(value)).quantize(q, rounding=_dec.ROUND_HALF_UP))


# ═══════════════════════════════════════════════════════════════════════════════
# CORE FUNCTIONS  — structure UNCHANGED; rounding added as post-step only
# ═══════════════════════════════════════════════════════════════════════════════

def get_third_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7) + timedelta(weeks=2)


def get_third_tuesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(1 - first.weekday()) % 7) + timedelta(weeks=2)


def business_days(start: date, end_excl: date) -> list[date]:
    """Return only Mon–Fri dates in [start, end_excl). Weekends are never included."""
    out, d = [], start
    while d < end_excl:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def calendar_gap_day_count(bd: date, next_bd) -> int:
    """
    Return the number of calendar days this business day represents.
    Computed as (next_business_day - current_business_day).days so that
    any weekend or holiday gap between two consecutive business days is
    automatically absorbed into the earlier day.

    Examples:
      Mon -> Tue  : gap = 1
      Fri -> Mon  : gap = 3  (Fri + Sat + Sun)
      Thu -> Mon  : gap = 4  (Thu + Fri + Sat + Sun, e.g. Easter Fri holiday)
      last day    : gap = 1  (fallback)

    This replaces the old hardcoded Friday=3 logic and correctly handles
    month-start weekends: Feb 27 (Fri) -> Mar 2 (Mon) gives day_count=3,
    so March 1 (Sun) is included in Feb 27's accrual.
    """
    if next_bd is None:
        return 1
    return (next_bd - bd).days


def build_rate_series(start: date, end_excl: date,
                      actual_df: pd.DataFrame, forward_rate: float) -> pd.DataFrame:
    """
    Build business-day-only rate series with calendar-gap day counts.

    Algorithm:
      1. Enumerate all business days in [start, end_excl).
      2. For each business day i:
           day_count = next_business_day[i] - business_day[i]  (calendar days)
         The last business day uses day_count = 1 (fallback).
      3. This ensures every calendar day -- including weekends/holidays
         between consecutive business days, or a month starting on a
         weekend (e.g. March 1 Sun) -- is counted via the preceding
         business day's day_count. No Saturday/Sunday rows are created.
    """
    lookup = actual_df.set_index("date")["rate"].to_dict()
    bds    = business_days(start, end_excl)
    rows   = []
    for i, d in enumerate(bds):
        next_bd = bds[i + 1] if i + 1 < len(bds) else None
        dc      = calendar_gap_day_count(d, next_bd)
        src     = "actual" if d in lookup else "forward"
        rows.append({"date": d, "rate": lookup.get(d, forward_rate),
                     "source": src, "day_count": dc})
    return pd.DataFrame(rows)


def compute_sr1(year: int, month: int,
                actual_df: pd.DataFrame, forward_rate: float) -> dict:
    """
    SR1: simple average of SOFR over ALL calendar days in the month.

    Algorithm:
      1. Build a row for every calendar day from the 1st to the last of the
         month (inclusive).
      2. Known actuals are taken from actual_df.
      3. Future dates with no actual use forward_rate.
      4. Weekends and any other gaps are forward-filled from the last known
         rate (e.g. March 1 Sun inherits the Feb 27 Fri fixing).
      5. If the very first day of the month has no rate (month starts on a
         weekend before any fixing exists), seed it from the last actual
         available BEFORE the month start.
      6. sr1_rate = mean of all calendar-day rates.

    The "series" returned contains one row per calendar day so that the
    running_avg and chart reflect the true daily rate sequence.
    """
    start     = date(year, month, 1)
    last_day  = date(year, month, calendar.monthrange(year, month)[1])
    end_excl  = last_day + timedelta(days=1)

    # Build lookup: known actuals
    lookup = actual_df.set_index("date")["rate"].to_dict()

    # Step 5 — seed value for forward-fill if month starts with no actual
    pre_actuals = actual_df[actual_df["date"] < start]
    seed_rate   = pre_actuals.iloc[-1]["rate"] if not pre_actuals.empty else None

    # Step 1-4 — iterate every calendar day
    rows        = []
    last_known  = seed_rate   # carries forward-filled rate
    d           = start
    while d < end_excl:
        if d in lookup:
            rate       = lookup[d]
            src        = "actual"
            last_known = rate
        elif last_known is not None:
            # forward-fill: weekend / future day inherits previous rate
            rate = last_known
            src  = "forward_fill" if d.weekday() >= 5 else "forward"
        else:
            # no prior rate at all — use forward_rate parameter
            rate       = forward_rate
            src        = "forward"
            last_known = rate
        rows.append({"date": d, "rate": rate, "source": src, "day_count": 1})
        d += timedelta(days=1)

    series = pd.DataFrame(rows)
    series["running_avg"] = series["rate"].expanding().mean()
    sr1_rate = series["rate"].mean()
    return {"rate": sr1_rate, "price": 100.0 - sr1_rate, "series": series}


def compute_sr3(start_year: int, start_month: int,
                actual_df: pd.DataFrame, forward_rate: float) -> dict:
    """
    SR3: compounded SOFR over the 3-month period (3rd Wed → 3rd Tue+3M).
    factor = 1 + (rate/100) * (day_count/360)  where day_count=3 for Friday.
    This correctly accounts for weekend carry in the compounding.
    Structure UNCHANGED.
    """
    period_start = get_third_wednesday(start_year, start_month)
    em = start_month + 3
    ey = start_year + (em - 1) // 12
    em = (em - 1) % 12 + 1
    period_end = get_third_tuesday(ey, em)
    total_days = (period_end - period_start).days + 1
    series = build_rate_series(period_start, period_end + timedelta(days=1),
                               actual_df, forward_rate)
    # factor = 1 + (rate/100) * (day_count/360)  — day_count=3 for Friday
    series["factor"]         = 1.0 + (series["rate"] / 100.0) * (series["day_count"] / 360.0)
    series["compound_index"] = series["factor"].cumprod()
    sr3_rate = (series["compound_index"].iloc[-1] - 1.0) * (360.0 / total_days) * 100.0
    return {"rate": sr3_rate, "price": 100.0 - sr3_rate, "series": series,
            "period_start": period_start, "period_end": period_end, "total_days": total_days}


def apply_rounding(result: dict, contract: str) -> dict:
    """Post-step: round rate and recompute price with round-half-up."""
    if result is None:
        return None
    r = result.copy()
    if contract == "sr1":
        r["rate"]  = round_half_up(r["rate"], 3)
        r["price"] = round_half_up(100.0 - r["rate"], 3)
    else:  # sr3
        r["rate"]  = round_half_up(r["rate"], 4)
        r["price"] = round_half_up(100.0 - r["rate"], 4)
    return r


def compute_pnl(current_price: float, entry_price: float, lots: int, dv01: float) -> float:
    return (current_price - entry_price) * 100.0 * dv01 * lots


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    empty = {"sr1": {c: {} for c in CASES}, "sr3": {c: {} for c in CASES},
             "notes": {"sr1": {}, "sr3": {}}}
    if not os.path.exists(STATE_PATH):
        return empty
    try:
        with open(STATE_PATH) as f:
            raw = json.load(f)
        for contract in ("sr1", "sr3"):
            raw.setdefault(contract, {})
            for c in CASES:
                raw[contract].setdefault(c, {})
        raw.setdefault("notes", {"sr1": {}, "sr3": {}})
        raw["notes"].setdefault("sr1", {})
        raw["notes"].setdefault("sr3", {})
        return raw
    except Exception:
        return empty


def save_state(st_obj: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(st_obj, f, indent=2, default=str)
    except Exception as e:
        st.warning(f"Could not save state: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS LOADER  — date | sofr | icap | gc  (icap, gc optional)
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=0)
def load_gsheet() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    empty_a = pd.DataFrame(columns=["date", "rate"])
    empty_i = pd.DataFrame(columns=["date", "icap"])
    empty_g = pd.DataFrame(columns=["date", "gc"])
    try:
        raw = pd.read_csv(GSHEET_CSV_URL)
    except Exception as e:
        st.error(f"Failed to load Google Sheet: {e}")
        return empty_a, empty_i, empty_g
    raw.columns = [c.strip().lower() for c in raw.columns]
    raw["date"] = pd.to_datetime(raw[raw.columns[0]], dayfirst=False, errors="coerce").dt.date
    raw = raw.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    sofr_col = "sofr" if "sofr" in raw.columns else raw.columns[1]
    raw[sofr_col] = pd.to_numeric(raw[sofr_col], errors="coerce")
    actual = (raw[["date", sofr_col]].rename(columns={sofr_col: "rate"})
              .dropna(subset=["rate"]).drop_duplicates("date")
              .sort_values("date").reset_index(drop=True))
    if "icap" in raw.columns:
        raw["icap"] = pd.to_numeric(raw["icap"], errors="coerce")
        icap = (raw[["date", "icap"]].dropna(subset=["icap"]).drop_duplicates("date")
                .sort_values("date").reset_index(drop=True))
    else:
        icap = empty_i
    if "gc" in raw.columns:
        raw["gc"] = pd.to_numeric(raw["gc"], errors="coerce")
        gc = (raw[["date", "gc"]].dropna(subset=["gc"]).drop_duplicates("date")
              .sort_values("date").reset_index(drop=True))
    else:
        gc = empty_g
    return actual, icap, gc


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE BUILDER
# Columns: Date | Day | Days | Actual SOFR | GC Repo | ICAP | Case1-5 | Notes
# Only business days (Mon-Fri). Days = calendar gap to next business day.
# ═══════════════════════════════════════════════════════════════════════════════

def build_table(start: date, end_excl: date,
                actual_df: pd.DataFrame, icap_df: pd.DataFrame, gc_df: pd.DataFrame,
                state: dict, contract: str) -> pd.DataFrame:
    act_lk  = actual_df.set_index("date")["rate"].to_dict()
    icap_lk = icap_df.set_index("date")["icap"].to_dict() if not icap_df.empty else {}
    gc_lk   = gc_df.set_index("date")["gc"].to_dict()     if not gc_df.empty   else {}
    bds  = business_days(start, end_excl)

    # Determine previous business day's SOFR for today fallback
    prev_sofr = None
    if not actual_df.empty:
        past = actual_df[actual_df["date"] < TODAY]
        if not past.empty:
            prev_sofr = past.iloc[-1]["rate"]

    rows = []
    for i, d in enumerate(bds):
        next_bd  = bds[i + 1] if i + 1 < len(bds) else None
        dc       = calendar_gap_day_count(d, next_bd)
        iso      = d.isoformat()
        act_val  = act_lk.get(d)
        locked   = (d <= YESTERDAY)
        is_today = (d == TODAY)
        row = {
            "Date":        d,
            "Day":         d.strftime("%a"),
            "Days":        dc,
            "Actual SOFR": act_val,
            "GC Repo":     gc_lk.get(d),
            "ICAP":        icap_lk.get(d),
            "_locked":     locked,
            "_today":      is_today,
        }
        for c in CASES:
            if act_val is not None:
                row[c] = act_val              # actual fixing available — use it
            elif locked:
                row[c] = None                 # past, no actual -> blank & locked
            elif is_today:
                # Today: prefill with best available value (priority order)
                saved = state[contract][c].get(iso)
                if saved is not None:
                    row[c] = saved            # user has already edited this
                elif icap_lk.get(d) is not None:
                    row[c] = icap_lk[d]       # ICAP available
                elif prev_sofr is not None:
                    row[c] = prev_sofr         # previous BD's SOFR
                else:
                    row[c] = None
            else:
                row[c] = state[contract][c].get(iso)
        row["Notes"] = state["notes"][contract].get(iso, "")
        rows.append(row)
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVE FINAL  — {case: DataFrame[date, rate, day_count]}
# ═══════════════════════════════════════════════════════════════════════════════

def resolve_final(table: pd.DataFrame, actual_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    act_lk = actual_df.set_index("date")["rate"].to_dict()
    results = {}
    for c in CASES:
        rows = []
        for _, row in table.iterrows():
            d  = row["Date"]
            if isinstance(d, pd.Timestamp): d = d.date()
            r  = act_lk.get(d, row[c])
            rows.append({"date": d, "rate": r, "day_count": int(row["Days"])})
        results[c] = pd.DataFrame(rows).dropna(subset=["rate"])
    return results


def icap_as_df(start: date, end_excl: date,
               actual_df: pd.DataFrame, icap_df: pd.DataFrame) -> pd.DataFrame:
    act_lk  = actual_df.set_index("date")["rate"].to_dict()
    icap_lk = icap_df.set_index("date")["icap"].to_dict() if not icap_df.empty else {}
    bds  = business_days(start, end_excl)
    rows = []
    for i, d in enumerate(bds):
        next_bd = bds[i + 1] if i + 1 < len(bds) else None
        dc      = calendar_gap_day_count(d, next_bd)
        r = act_lk.get(d, icap_lk.get(d))
        rows.append({"date": d, "rate": r, "day_count": dc})
    return pd.DataFrame(rows).dropna(subset=["rate"])


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTE HELPERS  — apply rounding after each compute
# ═══════════════════════════════════════════════════════════════════════════════

def _run_sr1(adf, fwd=0.0):
    if adf is None or adf.empty: return None
    return apply_rounding(compute_sr1(sel_year, sel_month, adf, fwd), "sr1")

def _run_sr3(adf, fwd=0.0):
    if adf is None or adf.empty: return None
    return apply_rounding(compute_sr3(sel_year, sel_month, adf, fwd), "sr3")

def compute_all_sr1(final_map: dict, icap_adf: pd.DataFrame) -> dict:
    results = {c: _run_sr1(final_map[c]) for c in CASES}
    results["ICAP"] = _run_sr1(icap_adf) if not icap_adf.empty else None
    return results

def compute_all_sr3(final_map: dict, icap_adf: pd.DataFrame) -> dict:
    results = {c: _run_sr3(final_map[c]) for c in CASES}
    results["ICAP"] = _run_sr3(icap_adf) if not icap_adf.empty else None
    return results

def fwd_avg_result_sr1(fwd: float) -> dict:
    return apply_rounding(compute_sr1(sel_year, sel_month, actual_df, fwd), "sr1")

def fwd_avg_result_sr3(fwd: float) -> dict:
    return apply_rounding(compute_sr3(sel_year, sel_month, actual_df, fwd), "sr3")


# ═══════════════════════════════════════════════════════════════════════════════
# ICAP → Case1
# ═══════════════════════════════════════════════════════════════════════════════

def copy_icap_to_case1(contract: str, start: date, end_excl: date):
    lk = icap_df.set_index("date")["icap"].to_dict() if not icap_df.empty else {}
    ck = contract.lower()
    for d in business_days(start, end_excl):
        if d > YESTERDAY and d not in actual_dates and d in lk:
            state[ck]["Case1"][d.isoformat()] = float(lk[d])
    save_state(state)


# ═══════════════════════════════════════════════════════════════════════════════
# CHART BUILDERS  — Altair for fixed y-axis control
# ═══════════════════════════════════════════════════════════════════════════════

def sofr_fixing_chart(cases_results: dict,
                      actual_df: pd.DataFrame, icap_df: pd.DataFrame) -> alt.Chart | None:
    """Daily SOFR fixing: Actual + ICAP + Case1–5. Fixed y: SOFR_Y_MIN–SOFR_Y_MAX."""
    records = []
    act_lk = actual_df.set_index("date")["rate"].to_dict()
    for d, r in act_lk.items():
        records.append({"date": pd.to_datetime(d), "rate": r, "series": "Actual SOFR"})
    if not icap_df.empty:
        for d, r in icap_df.set_index("date")["icap"].to_dict().items():
            records.append({"date": pd.to_datetime(d), "rate": r, "series": "ICAP"})
    for c, res in cases_results.items():
        if c == "ICAP" or res is None:
            continue
        for _, row in res["series"].iterrows():
            records.append({"date": pd.to_datetime(row["date"]),
                            "rate": row["rate"], "series": c})
    if not records:
        return None
    df = pd.DataFrame(records)
    chart = (alt.Chart(df)
             .mark_line(point=False)
             .encode(
                 x=alt.X("date:T", title="Date"),
                 y=alt.Y("rate:Q", title="Rate (%)",
                         scale=alt.Scale(domain=[SOFR_Y_MIN, SOFR_Y_MAX])),
                 color=alt.Color("series:N", legend=alt.Legend(title="Series")),
                 tooltip=["date:T", "series:N", alt.Tooltip("rate:Q", format=".5f")],
             )
             .properties(height=260)
             .configure_view(fill="#0d0f14")
             .configure_axis(gridColor="#1e2436", labelColor="#94a3b8", titleColor="#64748b")
             .configure_legend(labelColor="#c9d1e0", titleColor="#94a3b8")
             .interactive())
    return chart


def gc_chart(gc_df: pd.DataFrame, actual_df: pd.DataFrame) -> alt.Chart | None:
    """GC Repo + SOFR Actual chart with auto y-axis."""
    if gc_df.empty and actual_df.empty:
        return None

    records = []

    # GC data
    if not gc_df.empty:
        df_gc = gc_df.copy()
        df_gc["date"] = pd.to_datetime(df_gc["date"])
        for _, row in df_gc.iterrows():
            records.append({
                "date": row["date"],
                "rate": row["gc"],
                "series": "GC Repo"
            })

    # SOFR actual data
    if not actual_df.empty:
        df_sofr = actual_df.copy()
        df_sofr["date"] = pd.to_datetime(df_sofr["date"])
        for _, row in df_sofr.iterrows():
            records.append({
                "date": row["date"],
                "rate": row["rate"],
                "series": "SOFR Actual"
            })

    df = pd.DataFrame(records)

    chart = (
        alt.Chart(df)
        .mark_line(point=False)
        .encode(
            x=alt.X("date:T", title="Date"),
            y=alt.Y("rate:Q", title="Rate (%)", scale=alt.Scale(zero=False)),
            color=alt.Color("series:N", title=""),
            tooltip=["date:T", "series:N", alt.Tooltip("rate:Q", format=".5f")],
        )
        .properties(height=220)
        .configure_view(fill="#0d0f14")
        .configure_axis(
            gridColor="#1e2436",
            labelColor="#94a3b8",
            titleColor="#64748b"
        )
        .configure_legend(labelColor="#c9d1e0")
        .interactive()
    )

    return chart


def past_month_actual_chart(actual_df: pd.DataFrame,
                             start: date, end_excl: date) -> alt.Chart | None:
    """Past-month mode: only Actual SOFR in window."""
    act_lk = actual_df.set_index("date")["rate"].to_dict()
    records = [{"date": pd.to_datetime(d), "rate": r}
               for d, r in act_lk.items() if start <= d < end_excl]
    if not records:
        return None
    df = pd.DataFrame(records)
    chart = (alt.Chart(df)
             .mark_line(color="#38bdf8", point=True)
             .encode(
                 x=alt.X("date:T", title="Date"),
                 y=alt.Y("rate:Q", title="Actual SOFR (%)",
                         scale=alt.Scale(zero=False)),
                 tooltip=["date:T", alt.Tooltip("rate:Q", format=".5f")],
             )
             .properties(height=240)
             .configure_view(fill="#0d0f14")
             .configure_axis(gridColor="#1e2436", labelColor="#94a3b8", titleColor="#64748b")
             .interactive())
    return chart


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & DARK CSS
# ═══════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="SOFR Dashboard", page_icon="📈", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

html, body, [class*="css"],
[data-testid="stAppViewContainer"], [data-testid="stMain"], section.main {
    font-family: 'IBM Plex Sans', sans-serif;
    background: #0d0f14 !important;
    color: #c9d1e0 !important;
}
h1, h2, h3 { font-family: 'IBM Plex Mono', monospace; color: #e2e8f0; }

div[data-testid="stSidebar"] {
    background: #10131b !important;
    border-right: 1px solid #1e2436;
}
div[data-testid="stSidebar"] * { color: #c9d1e0 !important; }

.metric-card {
    background: #161a24;
    border: 1px solid #2a3147;
    border-radius: 8px;
    padding: 13px 16px;
    margin-bottom: 7px;
}
.metric-card.hl       { border-color: #3b82f6; border-width: 2px; }
.metric-card.icap-card{ border-color: #f59e0b; }
.metric-card.settle   { border-color: #34d399; border-width: 2px; }

.metric-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; letter-spacing: .12em;
    color: #64748b; text-transform: uppercase; margin-bottom: 3px;
}
.metric-price {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 24px; font-weight: 600; color: #38bdf8; line-height: 1.1;
}
.metric-price.icap { color: #f59e0b; }
.metric-price.settle { color: #34d399; font-size: 32px; }
.metric-rate { font-size: 11px; color: #94a3b8; margin-top: 3px; }
.metric-sub  { font-size: 11px; color: #64748b; margin-top: 2px; }
.pnl-pos { color: #34d399; }
.pnl-neg { color: #f87171; }

.section-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px; letter-spacing: .1em; color: #4b5563;
    text-transform: uppercase;
    border-bottom: 1px solid #1e2436;
    padding-bottom: 5px; margin: 18px 0 10px;
}
.note-badge {
    display: inline-block; background: #78350f; color: #fde68a;
    font-size: 10px; border-radius: 3px; padding: 1px 5px; margin-left: 4px;
}
.fwd-box {
    background: #161a24; border: 1px solid #2a3147;
    border-radius: 8px; padding: 12px 16px; margin-bottom: 10px;
}
.today-badge {
    display: inline-block; background: #1e3a5f; color: #93c5fd;
    font-size: 10px; border-radius: 3px; padding: 1px 6px;
    border: 1px solid #3b82f6; margin-left: 6px; vertical-align: middle;
}
.past-month-banner {
    background: #1a1a2e; border: 1px solid #3730a3;
    border-radius: 8px; padding: 10px 16px; margin-bottom: 12px;
    color: #a5b4fc; font-size: 12px;
}
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION STATE & DATA
# ═══════════════════════════════════════════════════════════════════════════════

if "state" not in st.session_state:
    st.session_state.state = load_state()
state = st.session_state.state

actual_df, icap_df, gc_df = load_gsheet()
actual_dates = set(actual_df["date"].tolist())
has_icap     = not icap_df.empty
has_gc       = not gc_df.empty

file_status = (
    f"✅ **{len(actual_df)}** SOFR"
    + (f" · **{len(icap_df)}** ICAP" if has_icap else "")
    + (f" · **{len(gc_df)}** GC"     if has_gc   else "")
    + " rows loaded"
    if len(actual_df) else "⚠️ Google Sheet returned no data"
)

# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("### 📂 Data")
    st.markdown(file_status)

    st.markdown("---")
    st.markdown("### 🗓 Contract Month")
    today_ref = date.today()
    sel_year  = st.number_input("Year",  min_value=2020, max_value=2040, value=today_ref.year)
    sel_month = st.selectbox("Month", list(range(1, 13)), index=today_ref.month - 1,
                             format_func=lambda m: calendar.month_name[m])

    st.markdown("---")
    st.markdown("### ⚡ Fast Fill")
    ff_contract = st.selectbox("Contract", ["SR1", "SR3"])
    ff_case     = st.selectbox("Case", CASES)
    ff_val      = st.number_input("Rate (%)", value=3.64, step=0.01, format="%.2f")
    if st.button("Apply to all remaining days"):
        ck = ff_contract.lower()
        s  = (date(sel_year, sel_month, 1) if ck == "sr1"
              else get_third_wednesday(sel_year, sel_month))
        _em = sel_month + 3; _ey = sel_year + (_em - 1) // 12; _em = (_em - 1) % 12 + 1
        e   = (date(sel_year, sel_month,
                    calendar.monthrange(sel_year, sel_month)[1]) + timedelta(days=1)
               if ck == "sr1" else get_third_tuesday(_ey, _em) + timedelta(days=1))
        for d in business_days(s, e):
            if d not in actual_dates and d > YESTERDAY:
                state[ck][ff_case][d.isoformat()] = ff_val
        save_state(state)
        st.success(f"Filled {ff_case} for {ff_contract}.")

    st.markdown("---")
    st.markdown("### ↕ Shift Case")
    sh_contract = st.selectbox("Contract ", ["SR1", "SR3"], key="sh_con")
    sh_case     = st.selectbox("Case ", CASES, key="sh_case")
    sh_bps      = st.number_input("Shift (bps)", value=0, step=1)
    if st.button("Apply shift"):
        ck = sh_contract.lower()
        for iso_str, val in list(state[ck][sh_case].items()):
            d = date.fromisoformat(iso_str)
            if d not in actual_dates and d > YESTERDAY:
                state[ck][sh_case][iso_str] = round(val + sh_bps / 100.0, 6)
        save_state(state)
        st.success(f"Shifted {sh_case} by {sh_bps:+.1f} bps.")

    st.markdown("---")
    st.markdown("### 🗑 Clear State")
    if st.button("🗑 Clear saved state"):
        st.session_state.state = load_state.__wrapped__() if hasattr(load_state, '__wrapped__') else load_state()
        save_state(st.session_state.state)
        st.success("State cleared.")

# ═══════════════════════════════════════════════════════════════════════════════
# CONTRACT WINDOWS
# ═══════════════════════════════════════════════════════════════════════════════

sr1_start    = date(sel_year, sel_month, 1)
sr1_end_excl = date(sel_year, sel_month,
                    calendar.monthrange(sel_year, sel_month)[1]) + timedelta(days=1)
sr3_start    = get_third_wednesday(sel_year, sel_month)
_em = sel_month + 3; _ey = sel_year + (_em - 1) // 12; _em = (_em - 1) % 12 + 1
sr3_end_incl = get_third_tuesday(_ey, _em)
sr3_end_excl = sr3_end_incl + timedelta(days=1)
sr3_cal_days = (sr3_end_incl - sr3_start).days + 1

# Fully-past month flag: last day of month < today
sr1_is_past = (sr1_end_excl - timedelta(days=1)) < TODAY
sr3_is_past = sr3_end_incl < TODAY

# Forward avg default (sidebar estimator removed; kept for internal calc compatibility)
fwd_avg = 0.0

# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def price_card(label, price_str, rate_str, extra_cls="", hl=False):
    card_cls = f"metric-card {'hl' if hl else ''} {extra_cls}".strip()
    p_cls    = "metric-price icap" if "icap-card" in extra_cls else "metric-price"
    return (f'<div class="{card_cls}">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="{p_cls}">{price_str}</div>'
            f'<div class="metric-rate">Rate: {rate_str}</div>'
            f'</div>')


def settle_card(label, price_str, rate_str):
    return (f'<div class="metric-card settle">'
            f'<div class="metric-label">⚖️ {label} — Final Settlement</div>'
            f'<div class="metric-price settle">{price_str}</div>'
            f'<div class="metric-rate">Rate: {rate_str}</div>'
            f'</div>')


def pnl_card_html(label, gross, net, tc, entry, current):
    cls  = "pnl-pos" if net >= 0 else "pnl-neg"
    sign = "+" if net >= 0 else ""
    return (f'<div class="metric-card">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="metric-price {cls}">{sign}${net:,.0f} net</div>'
            f'<div class="metric-rate">Gross {gross:+,.0f} · TC −${tc:.0f}</div>'
            f'<div class="metric-sub">Entry {entry:.4f} → {current:.4f}</div>'
            f'</div>')


def section(txt):
    st.markdown(f'<div class="section-title">{txt}</div>', unsafe_allow_html=True)


def fwd_banner(price, rate, fwd):
    st.markdown(
        f'<div class="fwd-box">'
        f'<span style="color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.1em">Forward Avg Estimate</span>'
        f'&nbsp;&nbsp; Price <b style="color:#38bdf8;font-size:18px">{price:.4f}</b>'
        f'&nbsp;&nbsp; Rate <span style="color:#94a3b8">{rate:.5f}%</span>'
        f'&nbsp;&nbsp;<span style="color:#4b5563;font-size:11px">(fwd avg = {fwd:.4f}% on remaining days)</span>'
        f'</div>',
        unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLE TABLE RENDERER
#
# ONE unified data_editor. Locking rules:
#   - Date <= YESTERDAY  →  ALL case columns + Notes disabled (locked)
#   - Date > YESTERDAY   →  Case columns editable, Notes editable
#   - Actual SOFR, GC Repo, ICAP, Date, Day, Days are always disabled
#
# Weekend handling: build_table() only generates business days.
# Friday rows have Days=3, representing Fri+Sat+Sun carry.
# No Saturday/Sunday rows are ever created or displayed.
# ═══════════════════════════════════════════════════════════════════════════════

def render_table(contract: str, start: date, end_excl: date, key: str) -> pd.DataFrame:
    scaffold = build_table(start, end_excl, actual_df, icap_df, gc_df, state, contract)

    # Today highlight
    today_mask = scaffold["_today"]
    if today_mask.any():
        today_date = scaffold.loc[today_mask, "Date"].iloc[0]
        st.markdown(
            f'Today in period: <span class="today-badge">📅 {today_date} ({today_date.strftime("%a")})</span>',
            unsafe_allow_html=True)

    # Drop internal marker columns before rendering
    display_df = scaffold.drop(columns=["_locked", "_today"])

    # ── Column configuration ──────────────────────────────────────────────────
    # Static (always disabled) columns
    base_cfg = {
        "Date":        st.column_config.DateColumn("Date",          disabled=True),
        "Day":         st.column_config.TextColumn("Day",           disabled=True, width="small"),
        "Days":        st.column_config.NumberColumn("Days",        disabled=True, width="small"),
        "Actual SOFR": st.column_config.NumberColumn("Actual (%)",  disabled=True, format="%.2f"),
        "GC Repo":     st.column_config.NumberColumn("GC Repo (%)", disabled=True, format="%.2f"),
        "ICAP":        st.column_config.NumberColumn("ICAP (%)",    disabled=True, format="%.2f"),
    }

    # Determine which rows are locked (past) vs editable (future/today)
    # We use scaffold's _locked column for this logic
    locked_dates = set(
        scaffold.loc[scaffold["_locked"], "Date"].apply(
            lambda d: d if isinstance(d, date) else d.date()
        ).tolist()
    )
    future_dates = set(
        scaffold.loc[~scaffold["_locked"], "Date"].apply(
            lambda d: d if isinstance(d, date) else d.date()
        ).tolist()
    )

    # If ALL rows are locked (e.g. mid-past-month partial view), disable everything
    all_locked = len(future_dates) == 0

    col_cfg = dict(base_cfg)
    if all_locked:
        # Fully disable case columns and notes
        for c in CASES:
            col_cfg[c] = st.column_config.NumberColumn(c, disabled=True, format="%.2f")
        col_cfg["Notes"] = st.column_config.TextColumn("Notes", disabled=True)
    else:
        # Future case columns are editable; past rows will have their values
        # preserved from actual/state and won't be saved back even if touched
        for c in CASES:
            col_cfg[c] = st.column_config.NumberColumn(
                c, format="%.2f", min_value=0.0, max_value=20.0)
        col_cfg["Notes"] = st.column_config.TextColumn("Notes")

    # ── Single data_editor ────────────────────────────────────────────────────
    edited = st.data_editor(
        display_df,
        column_config=col_cfg,
        use_container_width=True,
        hide_index=True,
        key=f"{key}_table",
        num_rows="fixed",
    )

    # Note badges
    note_mask = edited["Notes"].notna() & (edited["Notes"].str.strip() != "")
    if note_mask.any():
        badges = " ".join(
            f'<span class="note-badge">{r["Day"]} {r["Date"]}</span>'
            for _, r in edited[note_mask].iterrows())
        st.markdown(f"🟡 Notes on: {badges}", unsafe_allow_html=True)

    # ── Persist — future & non-actual rows ONLY ───────────────────────────────
    # Past rows (date <= YESTERDAY) are NEVER written back to state,
    # regardless of what the editor returns for those rows.
    changed = False
    for _, row in edited.iterrows():
        d = row["Date"]
        if isinstance(d, pd.Timestamp): d = d.date()
        iso = d.isoformat()

        if d > YESTERDAY and d not in actual_dates:
            # Editable future row — persist case values
            for c in CASES:
                val = row[c]
                if pd.notna(val):
                    if state[contract][c].get(iso) != float(val):
                        state[contract][c][iso] = float(val)
                        changed = True
            # Persist notes for future rows
            note = (row["Notes"] or "").strip()
            if state["notes"][contract].get(iso, "") != note:
                state["notes"][contract][iso] = note
                changed = True
        elif d <= YESTERDAY:
            # Past row — only persist notes (case values locked / not editable)
            note = (row["Notes"] or "").strip()
            if state["notes"][contract].get(iso, "") != note:
                state["notes"][contract][iso] = note
                changed = True

    if changed:
        save_state(state)

    return edited


# ═══════════════════════════════════════════════════════════════════════════════
# PAST-MONTH VIEW
# ═══════════════════════════════════════════════════════════════════════════════

def render_past_month(contract_label: str, start: date, end_excl: date,
                      compute_fn, rounding_contract: str):
    act_lk = actual_df.set_index("date")["rate"].to_dict()
    bds  = business_days(start, end_excl)
    rows = []
    for i, d in enumerate(bds):
        next_bd = bds[i + 1] if i + 1 < len(bds) else None
        dc      = calendar_gap_day_count(d, next_bd)
        rows.append({"Date": d, "Day": d.strftime("%a"),
                     "Days": dc,
                     "Actual SOFR": act_lk.get(d)})
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Settlement from actuals only
    actual_window = actual_df[
        actual_df["date"].apply(lambda d: start <= d < end_excl)]
    if not actual_window.empty:
        res = apply_rounding(
            compute_fn(sel_year, sel_month, actual_window, 0.0),
            rounding_contract)
        st.markdown(
            settle_card(contract_label, f"{res['price']:.3f}", f"{res['rate']:.3f}%"),
            unsafe_allow_html=True)

    ch = past_month_actual_chart(actual_df, start, end_excl)
    if ch:
        section("Actual SOFR — Historical")
        st.altair_chart(ch, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("# 📈 SOFR SR1 / SR3 Dashboard")
st.markdown(
    '<p style="color:#4b5563;font-size:13px;margin-top:-10px;">'
    'One-Month &amp; Three-Month SOFR Futures — Multi-Case Trading Desk</p>',
    unsafe_allow_html=True)

tab_sr1, tab_sr3 = st.tabs(
    ["📅 SR1 — One Month", "📆 SR3 — Three Month"])

# ╔══════════════════════════════════════════════════════════════════════════════
# SR1 TAB
# ╚══════════════════════════════════════════════════════════════════════════════
with tab_sr1:
    section(f"SR1 · {calendar.month_name[sel_month]} {sel_year} · {sr1_start} → {sr1_end_excl - timedelta(days=1)}")

    if sr1_is_past:
        st.markdown(
            '<div class="past-month-banner">📋 This month is fully in the past. '
            'Showing actuals only — no editable cases.</div>',
            unsafe_allow_html=True)
        render_past_month("SR1", sr1_start, sr1_end_excl, compute_sr1, "sr1")
        # Stubs for PnL tab downstream
        sr1_res = {}
        edited_sr1 = pd.DataFrame()
    else:
        tool_c1, _ = st.columns([1, 4])
        with tool_c1:
            if has_icap and st.button("📋 Copy ICAP → Case1", key="cp_icap_sr1"):
                copy_icap_to_case1("sr1", sr1_start, sr1_end_excl)
                st.success("Done.")

        edited_sr1 = render_table("sr1", sr1_start, sr1_end_excl,
                                  f"tbl_sr1_{sel_year}_{sel_month}")
        st.caption("🔒 Past rows locked (date <= yesterday) · Days = calendar gap to next BD (weekend carry included) · Actual/GC/ICAP read-only")

        icap_adf = icap_as_df(sr1_start, sr1_end_excl, actual_df, icap_df)
        final_sr1 = resolve_final(edited_sr1, actual_df)
        sr1_res   = compute_all_sr1(final_sr1, icap_adf)

        section("SR1 Prices & Rates")

        cards = st.columns(6)
        for i, col_key in enumerate(ALL_COLS):
            res = sr1_res.get(col_key)
            with cards[i]:
                if res:
                    extra = "icap-card" if col_key == "ICAP" else ""
                    st.markdown(price_card(col_key, f"{res['price']:.3f}",
                                          f"{res['rate']:.3f}%",
                                          extra_cls=extra, hl=(col_key == "Case1")),
                                unsafe_allow_html=True)
                else:
                    st.markdown(price_card(col_key, "—", "—"), unsafe_allow_html=True)

        ch1 = sofr_fixing_chart(sr1_res, actual_df, icap_df)
        if ch1:
            section(f"Daily SOFR Fixing  (y: {SOFR_Y_MIN}–{SOFR_Y_MAX}%)")
            st.altair_chart(ch1, use_container_width=True)

        if has_gc:
            section("GC Repo Rate")
            gc_ch = gc_chart(gc_df, actual_df)
            if gc_ch:
                st.altair_chart(gc_ch, use_container_width=True)

# ╔══════════════════════════════════════════════════════════════════════════════
# SR3 TAB
# ╚══════════════════════════════════════════════════════════════════════════════
with tab_sr3:
    section(f"SR3 · {sr3_start} → {sr3_end_incl} · {sr3_cal_days} calendar days")

    if sr3_is_past:
        st.markdown(
            '<div class="past-month-banner">📋 This SR3 period is fully in the past. '
            'Showing actuals only.</div>',
            unsafe_allow_html=True)
        render_past_month("SR3", sr3_start, sr3_end_excl, compute_sr3, "sr3")
        sr3_res = {}
        edited_sr3 = pd.DataFrame()
    else:
        tool_c1, _ = st.columns([1, 4])
        with tool_c1:
            if has_icap and st.button("📋 Copy ICAP → Case1", key="cp_icap_sr3"):
                copy_icap_to_case1("sr3", sr3_start, sr3_end_excl)
                st.success("Done.")

        edited_sr3 = render_table("sr3", sr3_start, sr3_end_excl,
                                  f"tbl_sr3_{sel_year}_{sel_month}")
        st.caption("🔒 Past rows locked (date <= yesterday) · Days = calendar gap to next BD · factor = 1+(r/100)*(day_count/360)")

        icap_adf = icap_as_df(sr3_start, sr3_end_excl, actual_df, icap_df)
        final_sr3 = resolve_final(edited_sr3, actual_df)
        sr3_res   = compute_all_sr3(final_sr3, icap_adf)

        section("SR3 Prices & Rates")

        cards = st.columns(6)
        for i, col_key in enumerate(ALL_COLS):
            res = sr3_res.get(col_key)
            with cards[i]:
                if res:
                    extra = "icap-card" if col_key == "ICAP" else ""
                    st.markdown(price_card(col_key, f"{res['price']:.4f}",
                                          f"{res['rate']:.4f}%",
                                          extra_cls=extra, hl=(col_key == "Case1")),
                                unsafe_allow_html=True)
                else:
                    st.markdown(price_card(col_key, "—", "—"), unsafe_allow_html=True)

        ch3 = sofr_fixing_chart(sr3_res, actual_df, icap_df)
        if ch3:
            section(f"Daily SOFR Fixing  (y: {SOFR_Y_MIN}–{SOFR_Y_MAX}%)")
            st.altair_chart(ch3, use_container_width=True)

        if has_gc:
            section("GC Repo Rate")
            gc_ch = gc_chart(gc_df, actual_df)
            if gc_ch:
                st.altair_chart(gc_ch, use_container_width=True)
