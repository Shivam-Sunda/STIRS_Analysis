"""
SOFR SR1 / SR3 Dashboard  –  v5
Run:  streamlit run sofr_dashboard.py
Excel:  date | sofr | icap | gc   (icap and gc optional)
State:  sofr_state.json  (auto-created alongside script)
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import date, timedelta
import calendar, os, json

# ═══════════════════════════════════════════════════════════════════════════════
# PATHS & CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
EXCEL_PATH = os.path.join(BASE_DIR, "sofr_data.xlsx")
STATE_PATH = os.path.join(BASE_DIR, "sofr_state.json")

CASES     = ["Case1", "Case2", "Case3", "Case4", "Case5"]
ALL_COLS  = ["ICAP"] + CASES          # columns shown in output cards
DV01_SR1  = 25.0
DV01_SR3  = 25.0
TC_PER_LOT = 1.0                      # $1 round-turn per lot
YESTERDAY  = date.today() - timedelta(days=1)

# ═══════════════════════════════════════════════════════════════════════════════
# CORE FUNCTIONS  — UNCHANGED
# ═══════════════════════════════════════════════════════════════════════════════

def get_third_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(2 - first.weekday()) % 7) + timedelta(weeks=2)


def get_third_tuesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(1 - first.weekday()) % 7) + timedelta(weeks=2)


def business_days(start: date, end_excl: date) -> list[date]:
    out, d = [], start
    while d < end_excl:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def day_count_for(d: date) -> int:
    return 3 if d.weekday() == 4 else 1


def build_rate_series(start: date, end_excl: date,
                      actual_df: pd.DataFrame, forward_rate: float) -> pd.DataFrame:
    lookup = actual_df.set_index("date")["rate"].to_dict()
    rows = []
    for d in business_days(start, end_excl):
        dc  = day_count_for(d)
        src = "actual" if d in lookup else "forward"
        rows.append({"date": d, "rate": lookup.get(d, forward_rate),
                     "source": src, "day_count": dc})
    return pd.DataFrame(rows)


def compute_sr1(year: int, month: int,
                actual_df: pd.DataFrame, forward_rate: float) -> dict:
    start  = date(year, month, 1)
    end    = date(year, month, calendar.monthrange(year, month)[1]) + timedelta(days=1)
    series = build_rate_series(start, end, actual_df, forward_rate)
    series["running_avg"] = series["rate"].expanding().mean()
    sr1_rate = series["rate"].mean()
    return {"rate": sr1_rate, "price": 100.0 - sr1_rate, "series": series}


def compute_sr3(start_year: int, start_month: int,
                actual_df: pd.DataFrame, forward_rate: float) -> dict:
    period_start = get_third_wednesday(start_year, start_month)
    em = start_month + 3
    ey = start_year + (em - 1) // 12
    em = (em - 1) % 12 + 1
    period_end = get_third_tuesday(ey, em)
    total_days = (period_end - period_start).days + 1
    series = build_rate_series(period_start, period_end + timedelta(days=1),
                               actual_df, forward_rate)
    series["factor"]         = 1.0 + (series["rate"] / 100.0) * (series["day_count"] / 360.0)
    series["compound_index"] = series["factor"].cumprod()
    sr3_rate = (series["compound_index"].iloc[-1] - 1.0) * (360.0 / total_days) * 100.0
    return {"rate": sr3_rate, "price": 100.0 - sr3_rate, "series": series,
            "period_start": period_start, "period_end": period_end, "total_days": total_days}


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
# EXCEL LOADER  — date | sofr | icap | gc  (icap, gc optional)
# ═══════════════════════════════════════════════════════════════════════════════

def load_excel() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (actual_df[date,rate], icap_df[date,icap], gc_df[date,gc])."""
    empty_a = pd.DataFrame(columns=["date", "rate"])
    empty_i = pd.DataFrame(columns=["date", "icap"])
    empty_g = pd.DataFrame(columns=["date", "gc"])
    if not os.path.exists(EXCEL_PATH):
        return empty_a, empty_i, empty_g
    raw = pd.read_excel(EXCEL_PATH)
    raw.columns = [c.strip().lower() for c in raw.columns]
    raw["date"] = pd.to_datetime(raw[raw.columns[0]]).dt.date

    # SOFR — always col index 1 (or column named 'sofr')
    sofr_col = "sofr" if "sofr" in raw.columns else raw.columns[1]
    actual = raw[["date", sofr_col]].rename(columns={sofr_col: "rate"})
    actual = actual.dropna(subset=["rate"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)

    icap = empty_i
    if "icap" in raw.columns:
        icap = raw[["date", "icap"]].dropna(subset=["icap"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)

    gc = empty_g
    if "gc" in raw.columns:
        gc = raw[["date", "gc"]].dropna(subset=["gc"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)

    return actual, icap, gc


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE BUILDER
# Date | Day | Days | Actual SOFR | GC Repo | ICAP | Case1–5 | Notes
# ═══════════════════════════════════════════════════════════════════════════════

def build_table(start: date, end_excl: date,
                actual_df: pd.DataFrame, icap_df: pd.DataFrame, gc_df: pd.DataFrame,
                state: dict, contract: str) -> pd.DataFrame:
    act_lk  = actual_df.set_index("date")["rate"].to_dict()
    icap_lk = icap_df.set_index("date")["icap"].to_dict()  if not icap_df.empty  else {}
    gc_lk   = gc_df.set_index("date")["gc"].to_dict()      if not gc_df.empty    else {}
    rows = []
    for d in business_days(start, end_excl):
        iso       = d.isoformat()
        act_val   = act_lk.get(d)
        is_locked = (d <= YESTERDAY)
        row = {
            "Date":        d,
            "Day":         d.strftime("%a"),
            "Days":        day_count_for(d),
            "Actual SOFR": act_val,
            "GC Repo":     gc_lk.get(d),
            "ICAP":        icap_lk.get(d),
        }
        for c in CASES:
            if act_val is not None:
                row[c] = act_val          # pre-fill with actual, will be locked
            elif is_locked:
                row[c] = None
            else:
                row[c] = state[contract][c].get(iso)
        row["Notes"] = state["notes"][contract].get(iso, "")
        rows.append(row)
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVE FINAL PER-CASE  →  {case: DataFrame[date,rate,day_count]}
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
    """ICAP case: actual wins, else icap, else NaN."""
    act_lk  = actual_df.set_index("date")["rate"].to_dict()
    icap_lk = icap_df.set_index("date")["icap"].to_dict() if not icap_df.empty else {}
    rows = []
    for d in business_days(start, end_excl):
        r = act_lk.get(d, icap_lk.get(d))
        rows.append({"date": d, "rate": r, "day_count": day_count_for(d)})
    return pd.DataFrame(rows).dropna(subset=["rate"])


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_all_sr1(final_map: dict, icap_adf: pd.DataFrame) -> dict:
    results = {}
    for c in CASES:
        adf = final_map[c]
        results[c] = compute_sr1(sel_year, sel_month, adf, 0.0) if not adf.empty else None
    if not icap_adf.empty:
        results["ICAP"] = compute_sr1(sel_year, sel_month, icap_adf, 0.0)
    else:
        results["ICAP"] = None
    return results


def compute_all_sr3(final_map: dict, icap_adf: pd.DataFrame) -> dict:
    results = {}
    for c in CASES:
        adf = final_map[c]
        results[c] = compute_sr3(sel_year, sel_month, adf, 0.0) if not adf.empty else None
    if not icap_adf.empty:
        results["ICAP"] = compute_sr3(sel_year, sel_month, icap_adf, 0.0)
    else:
        results["ICAP"] = None
    return results


def fwd_avg_result(year: int, month: int, fwd_avg: float,
                   actual_df: pd.DataFrame, compute_fn) -> dict:
    """Run compute_fn using actual data + forward_rate=fwd_avg for remaining days."""
    return compute_fn(year, month, actual_df, fwd_avg)


# ═══════════════════════════════════════════════════════════════════════════════
# ICAP → Case1
# ═══════════════════════════════════════════════════════════════════════════════

def copy_icap_to_case1(contract: str, start: date, end_excl: date,
                       icap_df: pd.DataFrame, actual_dates: set, state: dict):
    lk = icap_df.set_index("date")["icap"].to_dict() if not icap_df.empty else {}
    ck = contract.lower()
    for d in business_days(start, end_excl):
        if d > YESTERDAY and d not in actual_dates and d in lk:
            state[ck]["Case1"][d.isoformat()] = float(lk[d])
    save_state(state)


# ═══════════════════════════════════════════════════════════════════════════════
# CHART BUILDER  — daily rate per series
# ═══════════════════════════════════════════════════════════════════════════════

def build_fixing_chart(cases_results: dict, table: pd.DataFrame,
                       actual_df: pd.DataFrame, icap_df: pd.DataFrame) -> pd.DataFrame | None:
    frames = {}
    # Actual SOFR
    act_lk = actual_df.set_index("date")["rate"].to_dict()
    act_rows = [{"date": pd.to_datetime(d), "rate": r} for d, r in act_lk.items()]
    if act_rows:
        tmp = pd.DataFrame(act_rows).set_index("date")["rate"]
        frames["Actual SOFR"] = tmp
    # ICAP
    if not icap_df.empty:
        icap_lk = icap_df.set_index("date")["icap"].to_dict()
        tmp2 = pd.Series({pd.to_datetime(d): r for d, r in icap_lk.items()}, name="ICAP")
        frames["ICAP"] = tmp2
    # Cases
    for c, res in cases_results.items():
        if c == "ICAP" or res is None:
            continue
        s = res["series"].copy()
        s["date"] = pd.to_datetime(s["date"])
        frames[c] = s.set_index("date")["rate"]
    if not frames:
        return None
    return pd.DataFrame(frames).sort_index()


# ═══════════════════════════════════════════════════════════════════════════════
# ICAP SPREAD TABLE
# ═══════════════════════════════════════════════════════════════════════════════

def icap_spread_table(table: pd.DataFrame, icap_df: pd.DataFrame) -> pd.DataFrame | None:
    if icap_df.empty:
        return None
    lk = icap_df.set_index("date")["icap"].to_dict()
    rows = []
    for _, row in table.iterrows():
        d = row["Date"]
        if isinstance(d, pd.Timestamp): d = d.date()
        iv = lk.get(d)
        if iv is None:
            continue
        r = {"Date": d, "Day": row["Day"], "ICAP (%)": round(iv, 5)}
        for c in CASES:
            cv = row[c]
            r[f"{c}−ICAP"] = round((cv - iv) * 100, 2) if pd.notna(cv) else None
        rows.append(r)
    return pd.DataFrame(rows) if rows else None


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
.metric-card.hl  { border-color: #3b82f6; border-width: 2px; }
.metric-card.icap-card { border-color: #f59e0b; }
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
    display:inline-block; background:#78350f; color:#fde68a;
    font-size:10px; border-radius:3px; padding:1px 5px; margin-left:4px;
}
.fwd-box {
    background:#161a24; border:1px solid #2a3147; border-radius:8px;
    padding:12px 16px; margin-bottom:10px;
}
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION STATE & DATA
# ═══════════════════════════════════════════════════════════════════════════════

if "state" not in st.session_state:
    st.session_state.state = load_state()
state = st.session_state.state

actual_df, icap_df, gc_df = load_excel()
actual_dates = set(actual_df["date"].tolist())
has_icap     = not icap_df.empty
has_gc       = not gc_df.empty

file_status = (
    f"✅ **{len(actual_df)}** SOFR"
    + (f" · **{len(icap_df)}** ICAP" if has_icap else "")
    + (f" · **{len(gc_df)}** GC"     if has_gc   else "")
    + " rows loaded"
    if len(actual_df) else "⚠️ `sofr_data.xlsx` not found"
)

# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("### 📂 Data")
    st.markdown(file_status)
    if st.button("🔄 Reload Excel"):
        actual_df, icap_df, gc_df = load_excel()
        actual_dates = set(actual_df["date"].tolist())
        has_icap = not icap_df.empty
        has_gc   = not gc_df.empty
        st.success("Reloaded.")

    st.markdown("---")
    st.markdown("### 🗓 Contract Month")
    today     = date.today()
    sel_year  = st.number_input("Year",  min_value=2020, max_value=2040, value=today.year)
    sel_month = st.selectbox("Month", list(range(1, 13)), index=today.month - 1,
                             format_func=lambda m: calendar.month_name[m])

    st.markdown("---")
    st.markdown("### 📐 Forward Avg Estimator")
    fwd_avg = st.number_input("Forward Average SOFR (%)", value=5.30,
                               step=0.01, format="%.4f",
                               help="Applied to all unfilled future days to estimate fixing & price")

    st.markdown("---")
    st.markdown("### ⚡ Fast Fill")
    ff_contract = st.selectbox("Contract", ["SR1", "SR3"])
    ff_case     = st.selectbox("Case", CASES)
    ff_val      = st.number_input("Rate (%)", value=5.30, step=0.01, format="%.4f")
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
    sh_bps      = st.number_input("Shift (bps)", value=0.0, step=0.5, format="%.1f")
    if st.button("Apply shift"):
        ck = sh_contract.lower()
        for iso_str, val in state[ck][sh_case].items():
            d = date.fromisoformat(iso_str)
            if d not in actual_dates and d > YESTERDAY:
                state[ck][sh_case][iso_str] = round(val + sh_bps / 100.0, 6)
        save_state(state)
        st.success(f"Shifted {sh_case} by {sh_bps:+.1f} bps.")

    st.markdown("---")
    st.markdown("### 📊 Position")
    sr1_lots  = st.number_input("SR1 Lots",        value=10,    step=1)
    sr3_lots  = st.number_input("SR3 Lots",        value=5,     step=1)
    sr1_entry = st.number_input("SR1 Entry Price", value=94.50, step=0.01, format="%.4f")
    sr3_entry = st.number_input("SR3 Entry Price", value=94.75, step=0.01, format="%.4f")

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


# ═══════════════════════════════════════════════════════════════════════════════
# EDITABLE TABLE RENDERER
# ═══════════════════════════════════════════════════════════════════════════════

def render_table(contract: str, start: date, end_excl: date, key: str) -> pd.DataFrame:
    scaffold = build_table(start, end_excl, actual_df, icap_df, gc_df, state, contract)

    col_cfg = {
        "Date":        st.column_config.DateColumn("Date",           disabled=True),
        "Day":         st.column_config.TextColumn("Day",            disabled=True, width="small"),
        "Days":        st.column_config.NumberColumn("Days",         disabled=True, width="small"),
        "Actual SOFR": st.column_config.NumberColumn("Actual (%)",   disabled=True, format="%.5f"),
        "GC Repo":     st.column_config.NumberColumn("GC Repo (%)",  disabled=True, format="%.5f"),
        "ICAP":        st.column_config.NumberColumn("ICAP (%)",     disabled=True, format="%.5f"),
        "Notes":       st.column_config.TextColumn("Notes"),
    }
    for c in CASES:
        col_cfg[c] = st.column_config.NumberColumn(c, format="%.4f",
                                                    min_value=0.0, max_value=20.0)

    edited = st.data_editor(
        scaffold, column_config=col_cfg,
        use_container_width=True, hide_index=True,
        key=key, num_rows="fixed",
    )

    # Note-row badges
    note_rows = edited[edited["Notes"].notna() & (edited["Notes"].str.strip() != "")]
    if not note_rows.empty:
        badges = " ".join(
            f'<span class="note-badge">{r["Day"]} {r["Date"]}</span>'
            for _, r in note_rows.iterrows())
        st.markdown(f"🟡 Notes on: {badges}", unsafe_allow_html=True)

    # Persist — future, non-actual only
    changed = False
    for _, row in edited.iterrows():
        d = row["Date"]
        if isinstance(d, pd.Timestamp): d = d.date()
        iso = d.isoformat()
        if d > YESTERDAY and d not in actual_dates:
            for c in CASES:
                val = row[c]
                if pd.notna(val):
                    if state[contract][c].get(iso) != float(val):
                        state[contract][c][iso] = float(val)
                        changed = True
        note = (row["Notes"] or "").strip()
        if state["notes"][contract].get(iso, "") != note:
            state["notes"][contract][iso] = note
            changed = True
    if changed:
        save_state(state)
    return edited


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("# 📈 SOFR SR1 / SR3 Dashboard")
st.markdown(
    '<p style="color:#4b5563;font-size:13px;margin-top:-10px;">'
    'One-Month &amp; Three-Month SOFR Futures — Multi-Case Trading Desk View</p>',
    unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN TABS
# ═══════════════════════════════════════════════════════════════════════════════

tab_sr1, tab_sr3, tab_pnl = st.tabs(
    ["📅 SR1 — One Month", "📆 SR3 — Three Month", "💰 PnL & Summary"])

# ╔══════════════════════════════════════════════════════════════════════════════
# SR1 TAB
# ╚══════════════════════════════════════════════════════════════════════════════
with tab_sr1:
    section(f"SR1 · {calendar.month_name[sel_month]} {sel_year} · {sr1_start} → {sr1_end_excl - timedelta(days=1)}")

    # Tools row
    tool_c1, tool_c2 = st.columns([1, 4])
    with tool_c1:
        if has_icap and st.button("📋 Copy ICAP → Case1", key="cp_icap_sr1"):
            copy_icap_to_case1("sr1", sr1_start, sr1_end_excl, icap_df, actual_dates, state)
            st.success("Done.")

    edited_sr1 = render_table("sr1", sr1_start, sr1_end_excl, f"tbl_sr1_{sel_year}_{sel_month}")
    st.caption("🔒 Past rows locked · 🟡 Fri = 3-day accrual · Actual / GC / ICAP are read-only")

    # ICAP spread
    spr1 = icap_spread_table(edited_sr1, icap_df)
    if spr1 is not None:
        with st.expander("📊 Case − ICAP Spread (bps)"):
            st.dataframe(spr1, use_container_width=True, hide_index=True)

    # Compute
    icap_adf_sr1 = icap_as_df(sr1_start, sr1_end_excl, actual_df, icap_df)
    final_sr1    = resolve_final(edited_sr1, actual_df)
    sr1_res      = compute_all_sr1(final_sr1, icap_adf_sr1)

    # Forward-avg estimate
    fwd_sr1 = fwd_avg_result(sel_year, sel_month, fwd_avg, actual_df, compute_sr1)

    # ── Output cards  ICAP + Case1–5 ──────────────────────────────────────────
    section("SR1 Prices & Rates")
    st.markdown(
        f'<div class="fwd-box"><span style="color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.1em">Forward Avg Estimate</span>'
        f'&nbsp;&nbsp; Price <b style="color:#38bdf8;font-size:18px">{fwd_sr1["price"]:.4f}</b>'
        f'&nbsp;&nbsp; Rate <span style="color:#94a3b8">{fwd_sr1["rate"]:.5f}%</span>'
        f'&nbsp;&nbsp;<span style="color:#4b5563;font-size:11px">(fwd avg = {fwd_avg:.4f}% applied to remaining days)</span></div>',
        unsafe_allow_html=True)

    cards = st.columns(6)
    for i, col_key in enumerate(ALL_COLS):       # ICAP, Case1…Case5
        res = sr1_res.get(col_key)
        with cards[i]:
            if res:
                extra = "icap-card" if col_key == "ICAP" else ""
                st.markdown(
                    price_card(col_key, f"{res['price']:.4f}", f"{res['rate']:.5f}%",
                               extra_cls=extra, hl=(col_key == "Case1")),
                    unsafe_allow_html=True)
            else:
                st.markdown(price_card(col_key, "—", "—"), unsafe_allow_html=True)

    # Chart
    chart1 = build_fixing_chart(sr1_res, edited_sr1, actual_df, icap_df)
    if chart1 is not None:
        section("Daily SOFR Fixing — All Series")
        st.line_chart(chart1, use_container_width=True, height=240)

# ╔══════════════════════════════════════════════════════════════════════════════
# SR3 TAB
# ╚══════════════════════════════════════════════════════════════════════════════
with tab_sr3:
    section(f"SR3 · {sr3_start} → {sr3_end_incl} · {sr3_cal_days} calendar days")

    tool_c1, tool_c2 = st.columns([1, 4])
    with tool_c1:
        if has_icap and st.button("📋 Copy ICAP → Case1", key="cp_icap_sr3"):
            copy_icap_to_case1("sr3", sr3_start, sr3_end_excl, icap_df, actual_dates, state)
            st.success("Done.")

    edited_sr3 = render_table("sr3", sr3_start, sr3_end_excl, f"tbl_sr3_{sel_year}_{sel_month}")
    st.caption("🔒 Past rows locked · 🟡 Fri = 3-day accrual · factor = 1+(r/100)×(dc/360)")

    spr3 = icap_spread_table(edited_sr3, icap_df)
    if spr3 is not None:
        with st.expander("📊 Case − ICAP Spread (bps)"):
            st.dataframe(spr3, use_container_width=True, hide_index=True)

    icap_adf_sr3 = icap_as_df(sr3_start, sr3_end_excl, actual_df, icap_df)
    final_sr3    = resolve_final(edited_sr3, actual_df)
    sr3_res      = compute_all_sr3(final_sr3, icap_adf_sr3)

    fwd_sr3 = fwd_avg_result(sel_year, sel_month, fwd_avg, actual_df, compute_sr3)

    section("SR3 Prices & Rates")
    st.markdown(
        f'<div class="fwd-box"><span style="color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.1em">Forward Avg Estimate</span>'
        f'&nbsp;&nbsp; Price <b style="color:#38bdf8;font-size:18px">{fwd_sr3["price"]:.4f}</b>'
        f'&nbsp;&nbsp; Rate <span style="color:#94a3b8">{fwd_sr3["rate"]:.5f}%</span>'
        f'&nbsp;&nbsp;<span style="color:#4b5563;font-size:11px">(fwd avg = {fwd_avg:.4f}% applied to remaining days)</span></div>',
        unsafe_allow_html=True)

    cards = st.columns(6)
    for i, col_key in enumerate(ALL_COLS):
        res = sr3_res.get(col_key)
        with cards[i]:
            if res:
                extra = "icap-card" if col_key == "ICAP" else ""
                st.markdown(
                    price_card(col_key, f"{res['price']:.4f}", f"{res['rate']:.5f}%",
                               extra_cls=extra, hl=(col_key == "Case1")),
                    unsafe_allow_html=True)
            else:
                st.markdown(price_card(col_key, "—", "—"), unsafe_allow_html=True)

    chart3 = build_fixing_chart(sr3_res, edited_sr3, actual_df, icap_df)
    if chart3 is not None:
        section("Daily SOFR Fixing — All Series")
        st.line_chart(chart3, use_container_width=True, height=240)

# ╔══════════════════════════════════════════════════════════════════════════════
# PNL TAB
# ╚══════════════════════════════════════════════════════════════════════════════
with tab_pnl:
    section("PnL — All Cases (Gross / Net after TC)")

    summary_rows = []
    for col_key in ALL_COLS:           # ICAP + Case1–5
        r1  = sr1_res.get(col_key)
        r3  = sr3_res.get(col_key)
        g1  = compute_pnl(r1["price"], sr1_entry, sr1_lots, DV01_SR1) if r1 else np.nan
        g3  = compute_pnl(r3["price"], sr3_entry, sr3_lots, DV01_SR3) if r3 else np.nan
        tc1 = sr1_lots * TC_PER_LOT
        tc3 = sr3_lots * TC_PER_LOT
        n1  = g1 - tc1 if not np.isnan(g1) else np.nan
        n3  = g3 - tc3 if not np.isnan(g3) else np.nan
        gt  = g1 + g3  if not (np.isnan(g1) or np.isnan(g3)) else np.nan
        nt  = n1 + n3  if not (np.isnan(n1) or np.isnan(n3)) else np.nan
        tc  = tc1 + tc3

        def fmt(v): return f"{v:+,.0f}" if not np.isnan(v) else "—"
        summary_rows.append({
            "":              col_key,
            "SR1 Price":     f"{r1['price']:.4f}"  if r1 else "—",
            "SR1 Rate":      f"{r1['rate']:.5f}%"  if r1 else "—",
            "SR3 Price":     f"{r3['price']:.4f}"  if r3 else "—",
            "SR3 Rate":      f"{r3['rate']:.5f}%"  if r3 else "—",
            "SR1 Gross $":   fmt(g1),
            "SR3 Gross $":   fmt(g3),
            "Total Gross $": fmt(gt),
            "TC $":          f"−{tc:.0f}",
            "Net PnL $":     fmt(nt),
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    # Per-column PnL cards
    section("PnL Cards")
    cards = st.columns(6)
    for i, col_key in enumerate(ALL_COLS):
        r1 = sr1_res.get(col_key)
        r3 = sr3_res.get(col_key)
        with cards[i]:
            if r1:
                g1  = compute_pnl(r1["price"], sr1_entry, sr1_lots, DV01_SR1)
                tc1 = sr1_lots * TC_PER_LOT
                st.markdown(pnl_card_html(f"{col_key} SR1", g1, g1 - tc1, tc1,
                                          sr1_entry, r1["price"]), unsafe_allow_html=True)
            if r3:
                g3  = compute_pnl(r3["price"], sr3_entry, sr3_lots, DV01_SR3)
                tc3 = sr3_lots * TC_PER_LOT
                st.markdown(pnl_card_html(f"{col_key} SR3", g3, g3 - tc3, tc3,
                                          sr3_entry, r3["price"]), unsafe_allow_html=True)
            if r1 and r3:
                g1   = compute_pnl(r1["price"], sr1_entry, sr1_lots, DV01_SR1)
                g3   = compute_pnl(r3["price"], sr3_entry, sr3_lots, DV01_SR3)
                tc   = (sr1_lots + sr3_lots) * TC_PER_LOT
                net  = g1 + g3 - tc
                cls  = "pnl-pos" if net >= 0 else "pnl-neg"
                sign = "+" if net >= 0 else ""
                st.markdown(
                    f'<div class="metric-card hl">'
                    f'<div class="metric-label">{col_key} Total</div>'
                    f'<div class="metric-price {cls}">{sign}${net:,.0f}</div>'
                    f'<div class="metric-rate">Gross {g1+g3:+,.0f} · TC −${tc:.0f}</div>'
                    f'</div>',
                    unsafe_allow_html=True)

    with st.expander("ℹ️  Assumptions"):
        st.markdown(f"""
| | SR1 | SR3 |
|---|---|---|
| DV01/lot | ${DV01_SR1:.0f}/bp | ${DV01_SR3:.0f}/bp |
| Price | 100 − simple avg SOFR | 100 − compounded annualised SOFR |
| Day count | 1 all biz days | 1 Mon–Thu, **3 Fri** |
| TC | ${TC_PER_LOT:.0f} / round-turn / lot | same |

**Gross PnL** = (Price − Entry) × 100 bps × DV01 × Lots  
**Net PnL**   = Gross PnL − (Lots × TC)  
**State file:** `{STATE_PATH}`
""")
