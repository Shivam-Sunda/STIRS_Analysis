"""
SOFR SR1 / SR3 Dashboard  –  v4
Run:  streamlit run sofr_dashboard.py
Requires: sofr_data.xlsx  (columns: date, rate, icap[optional])
Persistence: sofr_state.json (auto-created alongside script)
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

CASES    = ["Case1", "Case2", "Case3", "Case4", "Case5"]
DV01_SR1 = 25.0
DV01_SR3 = 25.0
YESTERDAY = date.today() - timedelta(days=1)

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


def save_state(state: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        st.warning(f"Could not save state: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# EXCEL DATA LOADER  — now also reads optional ICAP column
# ═══════════════════════════════════════════════════════════════════════════════

def load_actual_sofr() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (actual_df[date,rate], icap_df[date,icap])."""
    empty_a = pd.DataFrame(columns=["date", "rate"])
    empty_i = pd.DataFrame(columns=["date", "icap"])
    if not os.path.exists(EXCEL_PATH):
        return empty_a, empty_i
    raw = pd.read_excel(EXCEL_PATH)
    raw.columns = [c.strip().lower() for c in raw.columns]
    raw["date"] = pd.to_datetime(raw[raw.columns[0]]).dt.date
    # actual SOFR
    actual = raw[["date", raw.columns[1]]].copy()
    actual.columns = ["date", "rate"]
    actual = actual.dropna().drop_duplicates("date").sort_values("date").reset_index(drop=True)
    # ICAP (optional third column named 'icap')
    icap = empty_i
    if "icap" in raw.columns:
        icap = raw[["date", "icap"]].dropna().drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return actual, icap


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE BUILDER  — Date | Day | Days | Actual SOFR | ICAP | Case1–5 | Notes
# ═══════════════════════════════════════════════════════════════════════════════

def build_multi_case_table(start: date, end_excl: date,
                            actual_df: pd.DataFrame, icap_df: pd.DataFrame,
                            state: dict, contract: str) -> pd.DataFrame:
    act_lookup  = actual_df.set_index("date")["rate"].to_dict()
    icap_lookup = icap_df.set_index("date")["icap"].to_dict() if not icap_df.empty else {}
    rows = []
    for d in business_days(start, end_excl):
        iso        = d.isoformat()
        actual_val = act_lookup.get(d, None)
        icap_val   = icap_lookup.get(d, None)
        dc         = day_count_for(d)
        is_locked  = (d <= YESTERDAY)   # past + today-1: lock all case cells
        row = {
            "Date":        d,
            "Day":         d.strftime("%a"),
            "Days":        dc,
            "Actual SOFR": actual_val,
            "ICAP":        icap_val,
        }
        for c in CASES:
            if actual_val is not None:
                # actual present → pre-populate case with actual, still lock
                row[c] = actual_val
            elif is_locked:
                row[c] = None          # past but no actual → blank & locked
            else:
                row[c] = state[contract][c].get(iso, None)
        row["Notes"] = state["notes"][contract].get(iso, "")
        rows.append(row)
    return pd.DataFrame(rows)


def resolve_final(table: pd.DataFrame, actual_df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Per case: actual wins; else use case value. Returns {case: DataFrame[date,rate,day_count]}."""
    lookup  = actual_df.set_index("date")["rate"].to_dict()
    results = {}
    for c in CASES:
        rows = []
        for _, row in table.iterrows():
            d  = row["Date"]
            if isinstance(d, pd.Timestamp): d = d.date()
            dc = int(row["Days"])
            r  = lookup.get(d, row[c])
            rows.append({"date": d, "rate": r, "day_count": dc})
        results[c] = pd.DataFrame(rows).dropna(subset=["rate"])
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG & LIGHT THEME CSS
# ═══════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="SOFR SR1/SR3 Dashboard", page_icon="📈", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

/* ── Dark theme base ── */
html, body, [class*="css"], [data-testid="stAppViewContainer"],
[data-testid="stMain"], section.main {
    font-family: 'IBM Plex Sans', sans-serif;
    background: #0e1117 !important;
    color: #e5e7eb !important;
}
h1, h2, h3 {
    font-family: 'IBM Plex Mono', monospace;
    color: #f9fafb;
}

/* ── Sidebar ── */
div[data-testid="stSidebar"] {
    background: #111827 !important;
    border-right: 1px solid #1f2937;
}

/* ── Metric card (dark) ── */
.metric-card {
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 10px;
    padding: 14px 18px;
    margin-bottom: 8px;
    box-shadow: 0 2px 6px rgba(0,0,0,.4);
}
.metric-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    letter-spacing: .12em;
    color: #9ca3af;
    text-transform: uppercase;
    margin-bottom: 3px;
}
.metric-price {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 26px;
    font-weight: 600;
    color: #60a5fa;
    line-height: 1.1;
}
.metric-rate {
    font-size: 12px;
    color: #9ca3af;
    margin-top: 3px;
}
.metric-sub {
    font-size: 11px;
    color: #6b7280;
    margin-top: 2px;
}
.pnl-pos { color: #10b981; }
.pnl-neg { color: #ef4444; }

/* ── Section title ── */
.section-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    letter-spacing: .1em;
    color: #9ca3af;
    text-transform: uppercase;
    border-bottom: 1px solid #1f2937;
    padding-bottom: 5px;
    margin: 20px 0 10px;
}

/* ── Dataframe / table styling ── */
[data-testid="stDataFrame"] {
    background-color: #0e1117;
    color: #e5e7eb;
}
thead tr th {
    background-color: #111827 !important;
    color: #9ca3af !important;
}
tbody tr {
    background-color: #0e1117 !important;
}
tbody tr:nth-child(even) {
    background-color: #111827 !important;
}

/* ── Inputs ── */
input, textarea, .stNumberInput input {
    background-color: #111827 !important;
    color: #e5e7eb !important;
    border: 1px solid #1f2937 !important;
}

/* ── Buttons ── */
.stButton>button {
    background-color: #1f2937;
    color: #e5e7eb;
    border: 1px solid #374151;
    border-radius: 6px;
}
.stButton>button:hover {
    background-color: #374151;
    border-color: #4b5563;
}

/* ── Expander ── */
.streamlit-expanderHeader {
    background-color: #111827;
    color: #e5e7eb;
}

/* ── Notes highlight ── */
.note-row-hint {
    background: #78350f;
    border-left: 3px solid #f59e0b;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 11px;
    color: #fef3c7;
}
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ═══════════════════════════════════════════════════════════════════════════════

if "state" not in st.session_state:
    st.session_state.state = load_state()
state = st.session_state.state

# ═══════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ═══════════════════════════════════════════════════════════════════════════════

actual_df, icap_df = load_actual_sofr()
actual_dates = set(actual_df["date"].tolist())
has_icap     = not icap_df.empty
file_status  = (f"✅ **{len(actual_df)}** fixings" +
                (f" · **{len(icap_df)}** ICAP rows" if has_icap else "") +
                " from `sofr_data.xlsx`"
                if len(actual_df) else "⚠️ `sofr_data.xlsx` not found")

# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("### 📂 Data")
    st.markdown(file_status)
    if st.button("🔄 Reload Excel"):
        actual_df, icap_df = load_actual_sofr()
        actual_dates = set(actual_df["date"].tolist())
        has_icap     = not icap_df.empty
        st.success("Reloaded.")

    st.markdown("---")
    st.markdown("### 🗓 Contract Month")
    today     = date.today()
    sel_year  = st.number_input("Year",  min_value=2020, max_value=2040, value=today.year)
    sel_month = st.selectbox("Month", list(range(1, 13)), index=today.month - 1,
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
        em = sel_month + 3; ey = sel_year + (em - 1) // 12; em = (em - 1) % 12 + 1
        e  = (date(sel_year, sel_month,
                   calendar.monthrange(sel_year, sel_month)[1]) + timedelta(days=1)
              if ck == "sr1" else get_third_tuesday(ey, em) + timedelta(days=1))
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
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("# 📈 SOFR SR1 / SR3 Dashboard")
st.markdown(
    '<p style="color:#6b7280;font-size:13px;margin-top:-10px;">'
    'One-Month &amp; Three-Month SOFR Futures Pricer — Multi-Case</p>',
    unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def price_card(case_label, price, rate, highlight=False):
    border = "border-color:#1d4ed8; border-width:2px;" if highlight else ""
    return (f'<div class="metric-card" style="{border}">'
            f'<div class="metric-label">{case_label}</div>'
            f'<div class="metric-price">{price}</div>'
            f'<div class="metric-rate">Rate: {rate}</div>'
            f'</div>')


def pnl_html(label, value, entry, current, sub=""):
    cls  = "pnl-pos" if value >= 0 else "pnl-neg"
    sign = "+" if value >= 0 else ""
    return (f'<div class="metric-card">'
            f'<div class="metric-label">{label}</div>'
            f'<div class="metric-price {cls}">{sign}${value:,.0f}</div>'
            f'<div class="metric-sub">Entry {entry:.4f} → {current:.4f} &nbsp;{sub}</div>'
            f'</div>')


# ═══════════════════════════════════════════════════════════════════════════════
# EDITABLE TABLE RENDERER
# ═══════════════════════════════════════════════════════════════════════════════

def render_table(contract: str, start: date, end_excl: date, table_key: str) -> pd.DataFrame:
    scaffold = build_multi_case_table(start, end_excl, actual_df, icap_df, state, contract)

    # Determine which rows are editable (future only)
    # We pass the full scaffold; locking is enforced by disabled=True on past rows
    # via column_config — data_editor doesn't support per-row locking directly,
    # so we split: past rows rendered as static display, future rows editable.

    # Build column config
    col_cfg = {
        "Date":        st.column_config.DateColumn("Date",         disabled=True),
        "Day":         st.column_config.TextColumn("Day",          disabled=True, width="small"),
        "Days":        st.column_config.NumberColumn("Days",       disabled=True, width="small"),
        "Actual SOFR": st.column_config.NumberColumn("Actual (%)", disabled=True, format="%.5f"),
        "ICAP":        st.column_config.NumberColumn("ICAP (%)",   disabled=True, format="%.5f"),
        "Notes":       st.column_config.TextColumn("Notes"),
    }
    # Lock case cells for past/actual rows by pre-filling; editable for future
    # data_editor applies column-level disable, so we enforce past-lock via
    # overwriting with actual on persist (see below).
    for c in CASES:
        col_cfg[c] = st.column_config.NumberColumn(c, format="%.4f",
                                                    min_value=0.0, max_value=20.0)

    # Add ICAP spread columns (read-only calculated, shown separately below table)
    display_df = scaffold.copy()

    edited = st.data_editor(
        display_df,
        column_config=col_cfg,
        use_container_width=True,
        hide_index=True,
        key=table_key,
        num_rows="fixed",
    )

    # Highlight rows with notes
    note_rows = edited[edited["Notes"].notna() & (edited["Notes"] != "")]
    if not note_rows.empty:
        st.markdown(
            '<div class="note-row-hint">🟡 Rows with notes: ' +
            ", ".join(str(r) for r in note_rows["Date"].tolist()) +
            "</div>", unsafe_allow_html=True)

    # Persist: only future non-actual rows are written back
    changed = False
    for _, row in edited.iterrows():
        d = row["Date"]
        if isinstance(d, pd.Timestamp): d = d.date()
        iso = d.isoformat()
        # Only save if future and no actual
        if d > YESTERDAY and d not in actual_dates:
            for c in CASES:
                val = row[c]
                if pd.notna(val):
                    if state[contract][c].get(iso) != float(val):
                        state[contract][c][iso] = float(val)
                        changed = True
        note = row["Notes"] or ""
        if state["notes"][contract].get(iso, "") != note:
            state["notes"][contract][iso] = note
            changed = True

    if changed:
        save_state(state)

    return edited


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTE ALL CASES
# ═══════════════════════════════════════════════════════════════════════════════

def compute_all_cases_sr1(final_map: dict) -> dict:
    results = {}
    for c in CASES:
        adf = final_map[c]
        if adf.empty or adf["rate"].isna().all():
            results[c] = None
        else:
            results[c] = compute_sr1(sel_year, sel_month, adf, 0.0)
    return results


def compute_all_cases_sr3(final_map: dict) -> dict:
    results = {}
    for c in CASES:
        adf = final_map[c]
        if adf.empty or adf["rate"].isna().all():
            results[c] = None
        else:
            results[c] = compute_sr3(sel_year, sel_month, adf, 0.0)
    return results


def icap_as_case(contract: str, start: date, end_excl: date) -> pd.DataFrame:
    """Build a case-compatible df from ICAP rates over the window."""
    icap_lookup = icap_df.set_index("date")["icap"].to_dict() if has_icap else {}
    act_lookup  = actual_df.set_index("date")["rate"].to_dict()
    rows = []
    for d in business_days(start, end_excl):
        dc  = day_count_for(d)
        r   = act_lookup.get(d, icap_lookup.get(d, None))
        rows.append({"date": d, "rate": r, "day_count": dc})
    df = pd.DataFrame(rows).dropna(subset=["rate"])
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# ICAP → Case1 helper
# ═══════════════════════════════════════════════════════════════════════════════

def copy_icap_to_case1(contract: str, start: date, end_excl: date):
    icap_lookup = icap_df.set_index("date")["icap"].to_dict()
    ck = contract.lower()
    for d in business_days(start, end_excl):
        if d > YESTERDAY and d not in actual_dates and d in icap_lookup:
            state[ck]["Case1"][d.isoformat()] = float(icap_lookup[d])
    save_state(state)


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY RATE CHART  — replaces running_avg / compound_index
# ═══════════════════════════════════════════════════════════════════════════════

def build_rate_chart(cases_results: dict, icap_start: date, icap_end_excl: date,
                     contract: str) -> pd.DataFrame | None:
    """Collect daily rate series for all cases + ICAP into one wide DataFrame."""
    frames = {}
    for c, res in cases_results.items():
        if res:
            s = res["series"].copy()
            s["date"] = pd.to_datetime(s["date"])
            frames[c] = s.set_index("date")["rate"]
    if has_icap:
        icap_s = icap_as_case(contract, icap_start, icap_end_excl)
        if not icap_s.empty:
            icap_s["date"] = pd.to_datetime(icap_s["date"])
            frames["ICAP"] = icap_s.set_index("date")["rate"]
    if not frames:
        return None
    return pd.DataFrame(frames)


# ═══════════════════════════════════════════════════════════════════════════════
# ICAP SPREAD TABLE
# ═══════════════════════════════════════════════════════════════════════════════

def icap_spread_table(table_df: pd.DataFrame) -> pd.DataFrame | None:
    """Returns a table of CaseX - ICAP spreads (in bps) for each business day."""
    if not has_icap:
        return None
    icap_lookup = icap_df.set_index("date")["icap"].to_dict()
    rows = []
    for _, row in table_df.iterrows():
        d = row["Date"]
        if isinstance(d, pd.Timestamp): d = d.date()
        icap_val = icap_lookup.get(d, None)
        if icap_val is None:
            continue
        r = {"Date": d, "Day": row["Day"]}
        for c in CASES:
            cv = row[c]
            r[f"{c}−ICAP (bps)"] = round((cv - icap_val) * 100, 2) if pd.notna(cv) else None
        rows.append(r)
    return pd.DataFrame(rows) if rows else None


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN TABS
# ═══════════════════════════════════════════════════════════════════════════════

tab_sr1, tab_sr3, tab_pnl = st.tabs(
    ["📅 SR1 — One Month", "📆 SR3 — Three Month", "💰 PnL & Summary"])

# ────────────────────────────────────────────────────────────────────────────────
# SR1 TAB
# ────────────────────────────────────────────────────────────────────────────────
with tab_sr1:
    sr1_label = f"SR1 — {calendar.month_name[sel_month]} {sel_year} · {sr1_start} → {sr1_end_excl - timedelta(days=1)}"
    st.markdown(f'<div class="section-title">{sr1_label}</div>', unsafe_allow_html=True)

    # Copy ICAP → Case1 button
    if has_icap:
        if st.button("📋 Copy ICAP → Case1  (SR1)", key="cp_icap_sr1"):
            copy_icap_to_case1("sr1", sr1_start, sr1_end_excl)
            st.success("ICAP values copied to Case1 for SR1 future days.")

    edited_sr1 = render_table("sr1", sr1_start, sr1_end_excl, f"tbl_sr1_{sel_year}_{sel_month}")
    st.caption("🔒 Past rows locked. 🟡 Friday = 3-day accrual. Actual SOFR and ICAP are read-only.")

    # ICAP spread table
    spread1 = icap_spread_table(edited_sr1)
    if spread1 is not None:
        with st.expander("📊 ICAP Spread (Case − ICAP, bps)"):
            st.dataframe(spread1, use_container_width=True, hide_index=True)

    final_sr1 = resolve_final(edited_sr1, actual_df)
    sr1_cases = compute_all_cases_sr1(final_sr1)

    # ── Metric cards: PRICE large, rate small ──
    st.markdown('<div class="section-title">SR1 Prices & Rates — All Cases</div>',
                unsafe_allow_html=True)
    cols = st.columns(5)
    for i, c in enumerate(CASES):
        res = sr1_cases[c]
        with cols[i]:
            if res:
                st.markdown(price_card(c, f"{res['price']:.4f}",
                                       f"{res['rate']:.5f}%", highlight=(i == 0)),
                            unsafe_allow_html=True)
            else:
                st.markdown(price_card(c, "—", "—"), unsafe_allow_html=True)

    # ── Daily SOFR chart ──
    chart1 = build_rate_chart(sr1_cases, sr1_start, sr1_end_excl, "sr1")
    if chart1 is not None:
        st.markdown('<div class="section-title">Daily SOFR Rate — All Cases</div>',
                    unsafe_allow_html=True)
        st.line_chart(chart1, use_container_width=True, height=240)

# ────────────────────────────────────────────────────────────────────────────────
# SR3 TAB
# ────────────────────────────────────────────────────────────────────────────────
with tab_sr3:
    sr3_label = f"SR3 — {sr3_start} → {sr3_end_incl} ({sr3_cal_days} cal. days)"
    st.markdown(f'<div class="section-title">{sr3_label}</div>', unsafe_allow_html=True)

    if has_icap:
        if st.button("📋 Copy ICAP → Case1  (SR3)", key="cp_icap_sr3"):
            copy_icap_to_case1("sr3", sr3_start, sr3_end_excl)
            st.success("ICAP values copied to Case1 for SR3 future days.")

    edited_sr3 = render_table("sr3", sr3_start, sr3_end_excl, f"tbl_sr3_{sel_year}_{sel_month}")
    st.caption("🔒 Past rows locked. 🟡 Friday = 3-day accrual. factor = 1 + (r/100)×(dc/360).")

    spread3 = icap_spread_table(edited_sr3)
    if spread3 is not None:
        with st.expander("📊 ICAP Spread (Case − ICAP, bps)"):
            st.dataframe(spread3, use_container_width=True, hide_index=True)

    final_sr3 = resolve_final(edited_sr3, actual_df)
    sr3_cases = compute_all_cases_sr3(final_sr3)

    st.markdown('<div class="section-title">SR3 Prices & Rates — All Cases</div>',
                unsafe_allow_html=True)
    cols = st.columns(5)
    for i, c in enumerate(CASES):
        res = sr3_cases[c]
        with cols[i]:
            if res:
                st.markdown(price_card(c, f"{res['price']:.4f}",
                                       f"{res['rate']:.5f}%", highlight=(i == 0)),
                            unsafe_allow_html=True)
            else:
                st.markdown(price_card(c, "—", "—"), unsafe_allow_html=True)

    chart3 = build_rate_chart(sr3_cases, sr3_start, sr3_end_excl, "sr3")
    if chart3 is not None:
        st.markdown('<div class="section-title">Daily SOFR Rate — All Cases</div>',
                    unsafe_allow_html=True)
        st.line_chart(chart3, use_container_width=True, height=240)

# ────────────────────────────────────────────────────────────────────────────────
# PNL TAB
# ────────────────────────────────────────────────────────────────────────────────
with tab_pnl:
    st.markdown('<div class="section-title">PnL — All Cases</div>', unsafe_allow_html=True)

    summary_rows = []
    for c in CASES:
        r1 = sr1_cases.get(c)
        r3 = sr3_cases.get(c)
        p1 = compute_pnl(r1["price"], sr1_entry, sr1_lots, DV01_SR1) if r1 else np.nan
        p3 = compute_pnl(r3["price"], sr3_entry, sr3_lots, DV01_SR3) if r3 else np.nan
        summary_rows.append({
            "Case":          c,
            "SR1 Price":     f"{r1['price']:.4f}"  if r1 else "—",
            "SR1 Rate (%)":  f"{r1['rate']:.5f}"   if r1 else "—",
            "SR3 Price":     f"{r3['price']:.4f}"  if r3 else "—",
            "SR3 Rate (%)":  f"{r3['rate']:.5f}"   if r3 else "—",
            "SR1 PnL ($)":   f"{p1:+,.0f}"         if not np.isnan(p1) else "—",
            "SR3 PnL ($)":   f"{p3:+,.0f}"         if not np.isnan(p3) else "—",
            "Total PnL ($)": (f"{p1+p3:+,.0f}"     if not (np.isnan(p1) or np.isnan(p3)) else "—"),
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    st.markdown('<div class="section-title">PnL Cards</div>', unsafe_allow_html=True)
    cols = st.columns(5)
    for i, c in enumerate(CASES):
        r1 = sr1_cases.get(c)
        r3 = sr3_cases.get(c)
        with cols[i]:
            if r1:
                p1 = compute_pnl(r1["price"], sr1_entry, sr1_lots, DV01_SR1)
                st.markdown(pnl_html(f"{c} SR1", p1, sr1_entry, r1["price"], f"{sr1_lots}L"),
                            unsafe_allow_html=True)
            if r3:
                p3 = compute_pnl(r3["price"], sr3_entry, sr3_lots, DV01_SR3)
                st.markdown(pnl_html(f"{c} SR3", p3, sr3_entry, r3["price"], f"{sr3_lots}L"),
                            unsafe_allow_html=True)
            if r1 and r3:
                tot  = (compute_pnl(r1["price"], sr1_entry, sr1_lots, DV01_SR1) +
                        compute_pnl(r3["price"], sr3_entry, sr3_lots, DV01_SR3))
                cls  = "pnl-pos" if tot >= 0 else "pnl-neg"
                sign = "+" if tot >= 0 else ""
                st.markdown(
                    f'<div class="metric-card" style="border:2px solid #1d4ed8;">'
                    f'<div class="metric-label">{c} Total</div>'
                    f'<div class="metric-price {cls}">{sign}${tot:,.0f}</div>'
                    f'<div class="metric-sub">SR1 + SR3</div></div>',
                    unsafe_allow_html=True)

    with st.expander("ℹ️  Assumptions"):
        st.markdown(f"""
| | SR1 | SR3 |
|---|---|---|
| DV01/lot | ${DV01_SR1:.0f}/bp | ${DV01_SR3:.0f}/bp |
| Price | 100 − simple avg SOFR | 100 − compounded annualised SOFR |
| Day count | 1 all biz days | 1 Mon–Thu, **3 Fri** |
| Compounding | — | Π(1 + r/100 × dc/360) |

**PnL** = (Current price − Entry price) × 100 bps × DV01 × Lots  
**Persistence:** `{STATE_PATH}`  
**ICAP:** {"loaded" if has_icap else "not present in Excel"}
""")
