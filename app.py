"""
SFH ROI Analyzer — Streamlit web app for single-family rental investment math.

Cloud-ready: portfolio lives in session_state (no filesystem writes).
Users export/import their portfolio as CSV.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""

import concurrent.futures
import io
import math
import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st

from roi import Assumptions, SCENARIOS, analyze, analyze_scenarios, verdict
from scrapers import parse_listing
from signals import HistoryEvent, analyze_history, normalize_event, parse_event_date, parse_history_paste

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_CSV = os.path.join(SCRIPT_DIR, "states.csv")

# Empty by default — users opt in to area filtering by typing one or more zips.
# This keeps the app friendly for anyone, anywhere; without a target, no badge
# is shown.
DEFAULT_TARGET_ZIPS = ""
DEFAULT_RADIUS_MILES = 25

st.set_page_config(page_title="SFH ROI Analyzer", page_icon="🏠", layout="wide")


# ─── Reference data ──────────────────────────────────────────────────────────
@st.cache_data
def load_states():
    return pd.read_csv(STATES_CSV, dtype={"state": str})


states = load_states()
STATES_BY_CODE = {row["state"]: row for _, row in states.iterrows()}
STATE_LABELS = [f"{r['state']} — {r['state_name']}" for _, r in states.iterrows()]
LABEL_TO_CODE = {f"{r['state']} — {r['state_name']}": r["state"] for _, r in states.iterrows()}

STATE_IN_ADDR = re.compile(r"\b([A-Z]{2})\b[\s,]+\d{5}")


def state_from_address(addr: str) -> str | None:
    if not addr:
        return None
    m = STATE_IN_ADDR.search(addr.upper())
    if m and m.group(1) in STATES_BY_CODE:
        return m.group(1)
    return None


@st.cache_resource(show_spinner=False)
def _zip_geocoder():
    """Lazy-load pgeocode US zip→lat/lon. Cached across reruns. Returns None
    if pgeocode isn't installed or its data download fails — callers degrade
    gracefully via the 'unknown' match state."""
    try:
        import pgeocode
        return pgeocode.Nominatim("us")
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def _zip_to_latlon(zip_code: str) -> tuple[float, float] | None:
    """Look up (lat, lon) for a US zip. Returns None if missing or geocoder
    unavailable. Cached per zip — first call per session may take a moment
    while pgeocode warms up; subsequent calls are instant."""
    nomi = _zip_geocoder()
    if nomi is None or not zip_code or not zip_code.isdigit() or len(zip_code) != 5:
        return None
    try:
        rec = nomi.query_postal_code(zip_code)
    except Exception:
        return None
    lat = getattr(rec, "latitude", None)
    lon = getattr(rec, "longitude", None)
    if lat is None or lon is None or pd.isna(lat) or pd.isna(lon):
        return None
    return (float(lat), float(lon))


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    R = 3958.7613  # earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


def zip_match(
    zip_code: str, targets: set[str], radius_miles: float
) -> tuple[str, str | None, float | None]:
    """
    Classify a zip relative to the user's target zips.

    Returns (state, nearest_target_zip, distance_miles):
      - 'exact'     — zip is in the target list
      - 'in_radius' — within radius_miles of nearest target
      - 'out'       — geocoded fine, but no target is close enough
      - 'unknown'   — zip lookup failed (offline, bad zip, etc.) — no judgment
    """
    if not zip_code or not targets:
        return ("out", None, None)
    if zip_code in targets:
        return ("exact", zip_code, 0.0)
    src = _zip_to_latlon(zip_code)
    if src is None:
        return ("unknown", None, None)

    nearest = None
    nearest_d = math.inf
    any_target_ok = False
    for t in targets:
        dst = _zip_to_latlon(t)
        if dst is None:
            continue
        any_target_ok = True
        d = _haversine_miles(src[0], src[1], dst[0], dst[1])
        if d < nearest_d:
            nearest_d = d
            nearest = t

    if not any_target_ok:
        return ("unknown", None, None)

    state = "in_radius" if nearest_d <= radius_miles else "out"
    return (state, nearest, nearest_d)


# ─── Session state initialization ────────────────────────────────────────────
if "portfolio" not in st.session_state:
    st.session_state.portfolio = []
if "fetched" not in st.session_state:
    st.session_state.fetched = {}
if "history" not in st.session_state:
    st.session_state.history = []  # list[HistoryEvent]
if "price_input" not in st.session_state:
    st.session_state.price_input = 400_000
if "asking_price" not in st.session_state:
    st.session_state.asking_price = None  # what the listing actually asks


def _safe_zip(data: dict, address: str) -> str:
    """Return the property zip. The scraper's `zip` field sometimes captures
    the street number, so prefer the *last* 5-digit run in the address."""
    addr_zips = re.findall(r"\b(\d{5})\b", address or "")
    if addr_zips:
        return addr_zips[-1]
    z = str(data.get("zip") or "")
    return z if (z.isdigit() and len(z) == 5) else ""


def _analyze_one_url(url: str, base_a: Assumptions) -> dict:
    """Fetch + analyze a single URL. Returns a row dict for the batch table,
    or {'url': ..., 'error': ...} on failure."""
    try:
        r = parse_listing(url)
    except Exception as e:
        return {"url": url, "error": f"fetch crashed: {type(e).__name__}: {e}"}

    if "error" in r and not r.get("partial"):
        return {"url": url, "error": r["error"]}

    data = r if "error" not in r else r["partial"]

    price = data.get("price")
    if not price:
        return {"url": url, "error": "no price extracted"}

    address = data.get("address") or ""
    zip_code = _safe_zip(data, address)
    state_code = state_from_address(address)
    if state_code and state_code in STATES_BY_CODE:
        state_row = STATES_BY_CODE[state_code]
        tax_rate = float(state_row["property_tax_rate"])
        insurance_yr = float(state_row["avg_insurance_yr"])
    else:
        tax_rate = 0.011
        insurance_yr = 1400

    rent_mo = data.get("rent_zestimate") or round(price * 0.008)
    hoa_mo = data.get("hoa_mo") or 0

    # Build per-property base assumptions: state insurance overrides the sidebar
    # placeholder; scenarios will override financing fields on top of this.
    a = Assumptions(
        down_pct=base_a.down_pct,
        mortgage_rate=base_a.mortgage_rate,
        loan_years=base_a.loan_years,
        insurance_yr=insurance_yr,
        vacancy_pct=base_a.vacancy_pct,
        maintenance_pct=base_a.maintenance_pct,
        mgmt_pct=base_a.mgmt_pct,
        appreciation=base_a.appreciation,
        rent_growth=base_a.rent_growth,
    )
    scenarios = analyze_scenarios(price, rent_mo, tax_rate, a, hoa_mo=hoa_mo)
    best_name, best_m = max(scenarios.items(), key=lambda kv: kv[1]["irr_approx_5yr"])

    # Leverage from property history if scraped.
    history = data.get("history") or []
    leverage = ""
    dom = None
    cuts = 0
    rec_offer_mid = None
    if history:
        sig = analyze_history(history, current_price=price)
        leverage = sig.leverage_label
        dom = sig.days_on_market
        cuts = sig.cuts_current_run
        if sig.recommended_offer_low and sig.recommended_offer_high:
            rec_offer_mid = round(
                (sig.recommended_offer_low + sig.recommended_offer_high) / 2 / 1000
            ) * 1000

    # Deal score — mirrors roi_analyzer.rank_score with a leverage bonus.
    score = (
        best_m["cap_rate"] * 150
        + best_m["coc_return"] * 100
        + best_m["irr_approx_5yr"] * 80
    )
    if best_m["monthly_cash_flow"] < 0:
        score -= 5
    score += min(best_m["one_pct_rule"] * 100, 1.0) * 5
    if leverage.startswith("STRONG"):
        score += 10
    elif leverage.startswith("MILD"):
        score += 5
    elif leverage.startswith("HOT"):
        score -= 3

    return {
        "url": url,
        "address": address,
        "state": state_code or "?",
        "zip": zip_code,
        "price": price,
        "beds": data.get("beds"),
        "baths": data.get("baths"),
        "sqft": data.get("sqft"),
        "rent_mo": rent_mo,
        "best_scenario": best_name,
        "monthly_cf": best_m["monthly_cash_flow"],
        "irr_5yr": best_m["irr_approx_5yr"],
        "cap_rate": best_m["cap_rate"],
        "coc": best_m["coc_return"],
        "breakeven_rent": best_m["breakeven_rent_mo"],
        "leverage": leverage,
        "dom": dom,
        "cuts": cuts,
        "rec_offer": rec_offer_mid,
        "score": score,
    }


def add_to_portfolio(row: dict):
    st.session_state.portfolio.append(row)


def portfolio_df() -> pd.DataFrame:
    if not st.session_state.portfolio:
        return pd.DataFrame()
    return pd.DataFrame(st.session_state.portfolio)


# ─── Sidebar: Search area ────────────────────────────────────────────────────
st.sidebar.header("Your search area (optional)")
target_zips_raw = st.sidebar.text_input(
    "Target zip code(s)",
    value=DEFAULT_TARGET_ZIPS,
    placeholder="e.g. 20901, 22102",
    help=(
        "Comma-separate one or more US zips. Properties within your radius "
        "get an 'in radius' badge; everything else is informational, not "
        "blocked. Leave empty to skip area filtering entirely."
    ),
)
target_zips = {z.strip() for z in target_zips_raw.split(",") if z.strip().isdigit()}
target_radius = st.sidebar.slider(
    "Search radius (miles)",
    min_value=0, max_value=200, value=DEFAULT_RADIUS_MILES, step=5,
    help=(
        "Properties within this many miles of any target zip get the "
        "'in radius' badge. Set to 0 to require an exact zip match."
    ),
)

st.sidebar.markdown("---")

# ─── Sidebar: Assumptions ────────────────────────────────────────────────────
st.sidebar.header("Assumptions")
rate = st.sidebar.slider("Mortgage rate (%)", 3.0, 12.0, 7.25, 0.05) / 100
down = st.sidebar.slider("Down payment (%)", 3, 50, 20, 1) / 100
vacancy = st.sidebar.slider("Vacancy (%)", 0, 15, 6, 1) / 100
mgmt = st.sidebar.slider("Property mgmt (% of rent)", 0, 12, 8, 1) / 100
maint = st.sidebar.slider("Maintenance (% of rent)", 0, 15, 8, 1) / 100
appreciation = st.sidebar.slider("Long-run appreciation (%)", 0.0, 8.0, 3.5, 0.5) / 100
rent_growth = st.sidebar.slider("Long-run rent growth (%)", 0.0, 8.0, 3.0, 0.5) / 100
loan_years = st.sidebar.selectbox("Loan term (years)", [15, 20, 30], index=2)

with st.sidebar.expander("💡 Scenario tips"):
    st.markdown(
        "**Refi later?** Drop the rate slider by 1-2 points to model a refinance.\n\n"
        "**House hack?** Set down to 5% (FHA owner-occupant). Use room-rental totals as rent.\n\n"
        "**All cash?** Set down to 50%+ — cash-on-cash converges to cap rate.\n\n"
        "**Self-manage?** Set property mgmt to 0%."
    )

st.sidebar.markdown("---")
st.sidebar.caption(
    "🛠️ [View source on GitHub](https://github.com/ashenafiwk/sfh-roi-analyzer)"
)

# ─── Header ──────────────────────────────────────────────────────────────────
st.title("🏠 SFH ROI Analyzer")
st.caption(
    "**SFH** = Single Family Home. Buy-and-hold rental investment math for any US property: "
    "cap rate, cash-on-cash, monthly cash flow, 5-year IRR, plus the classic 1% rule, GRM, and DSCR."
)

tab_eval, tab_batch, tab_portfolio, tab_states, tab_about = st.tabs(
    ["Evaluate property", "Batch screen", "My portfolio", "State reference", "About"]
)


# ─── TAB 1: Evaluate ─────────────────────────────────────────────────────────
with tab_eval:
    col_url, col_btn = st.columns([5, 1])
    with col_url:
        url = st.text_input(
            "Paste a Zillow or Redfin listing URL (optional)",
            placeholder="https://www.zillow.com/homedetails/...  or  https://www.redfin.com/...",
            key="url_input",
        )
    with col_btn:
        st.write("")
        st.write("")
        do_fetch = st.button("Fetch", use_container_width=True)

    if do_fetch and url:
        with st.spinner("Fetching listing..."):
            result = parse_listing(url)
        if "error" in result:
            st.warning(f"⚠️ {result['error']}")
            if result.get("partial"):
                st.session_state.fetched = result["partial"]
                st.info("Pre-filled what could be extracted — please fill in the rest manually.")
        else:
            st.session_state.fetched = result
            if result.get("history"):
                st.session_state.history = result["history"]
            if result.get("price"):
                st.session_state.price_input = int(result["price"])
                st.session_state.asking_price = int(result["price"])
            extras = []
            if result.get("property_type"):
                extras.append(result["property_type"])
            if result.get("year_built"):
                extras.append(f"built {int(result['year_built'])}")
            extras_str = f" ({', '.join(extras)})" if extras else ""
            msg = f"Loaded: {result.get('address', 'listing')}{extras_str}"
            if result.get("history"):
                msg += f" • {len(result['history'])} history events"
            st.success(msg)

    fetched = st.session_state.fetched

    # ─── State selector — drives default tax + insurance ─────────────────────
    detected_state = state_from_address(fetched.get("address", ""))
    default_state_idx = 0
    if detected_state:
        try:
            default_state_idx = list(STATES_BY_CODE).index(detected_state)
        except ValueError:
            default_state_idx = 0

    st.markdown("### Location")
    sc1, sc2 = st.columns([2, 5])
    state_label = sc1.selectbox("State", STATE_LABELS, index=default_state_idx)
    state_code = LABEL_TO_CODE[state_label]
    state_row = STATES_BY_CODE[state_code]
    sc2.caption(
        f"Defaults for **{state_row['state_name']}**: "
        f"property tax {state_row['property_tax_rate']*100:.2f}% • "
        f"insurance ~${int(state_row['avg_insurance_yr']):,}/yr"
        + (f" — {state_row['notes']}" if isinstance(state_row.get('notes'), str) and state_row['notes'] else "")
    )

    # ─── Property details ────────────────────────────────────────────────────
    st.markdown("### Property details")
    c1, c2, c3 = st.columns(3)
    address = c1.text_input("Address", value=fetched.get("address", ""))
    zip_code = c2.text_input("Zip code", value=str(fetched.get("zip") or ""), max_chars=5)
    price = c3.number_input("Price ($)", min_value=20_000, max_value=10_000_000,
                            step=5000, key="price_input")

    # ─── Search-area badge ───────────────────────────────────────────────────
    if zip_code and target_zips:
        state, nearest, dist = zip_match(zip_code, target_zips, target_radius)
        if state == "exact":
            target_str = ", ".join(sorted(target_zips))
            st.success(f"📍 **{zip_code}** is in your search area ({target_str}).")
        elif state == "in_radius":
            st.info(
                f"📍 **{zip_code}** is **{dist:.0f} mi** from {nearest} — "
                f"within your {target_radius}-mile radius."
            )
        elif state == "unknown":
            st.caption(
                f"📍 Couldn't geocode **{zip_code}** — area badge skipped. "
                "(First-time geocoder download can take a moment; try again.)"
            )
        else:
            # 'out' — informational, not a warning
            base = f"📍 **{zip_code}**"
            if dist is not None and nearest is not None:
                base += f" is **{dist:.0f} mi** from your nearest target ({nearest})"
            base += f" — outside your {target_radius}-mile radius. Still worth a look if the numbers are right."
            st.info(base)

    c4, c5, c6 = st.columns(3)
    beds = c4.number_input("Beds", 1, 10, int(fetched.get("beds") or 3))
    baths = c5.number_input("Baths", 1.0, 10.0, float(fetched.get("baths") or 2), step=0.5)
    sqft = c6.number_input("Sqft", 200, 20_000, int(fetched.get("sqft") or 1500), step=50)

    # Default rent: Zestimate if scraped, else 1% rule (price × 0.008)
    default_rent = int(fetched.get("rent_zestimate") or max(round(price * 0.008), 800))
    default_tax = float(state_row["property_tax_rate"])
    default_insurance = int(state_row["avg_insurance_yr"])

    c7, c8, c9 = st.columns(3)
    rent_mo = c7.number_input("Expected rent ($/mo)", 200, 30_000, default_rent, step=50)
    tax_rate = c8.number_input("Property tax rate", 0.001, 0.035, default_tax,
                               step=0.0005, format="%.4f",
                               help="State default shown — override for your county/city.")
    hoa_mo = c9.number_input("HOA ($/mo)", 0, 2000, int(fetched.get("hoa_mo") or 0), step=10)

    insurance_yr = st.number_input(
        "Annual insurance ($)", 300, 10_000, default_insurance, step=100,
        help="State average shown — override for coastal/wildfire/etc. specifics.",
    )

    # ─── Listing intelligence ────────────────────────────────────────────────
    st.markdown("### Listing intelligence")
    st.caption(
        "How long has it been on market? Did it fail to sell before? "
        "Listing history reveals seller motivation — and your negotiating leverage."
    )

    asking = st.session_state.asking_price or price

    intel_tab_view, intel_tab_paste, intel_tab_manual = st.tabs(
        ["History & signals", "Paste history", "Manual entry"]
    )

    with intel_tab_paste:
        st.caption(
            "On a Redfin/Zillow listing page, scroll to **Property history**, "
            "select the date+event+price rows, copy, and paste below. The parser "
            "picks out dates, events, and prices automatically."
        )
        paste_text = st.text_area(
            "Paste property history text",
            height=180,
            placeholder=(
                "Apr 16, 2026   Listed   Redfin   $1,050,000\n"
                "Dec 11, 2025   Listing removed   Redfin   $1,149,000\n"
                "Oct 1, 2025    Listed   Redfin   $1,149,000\n"
                "..."
            ),
            key="paste_history_text",
        )
        c_parse, c_clear = st.columns([1, 1])
        if c_parse.button("Parse & use", use_container_width=True):
            parsed = parse_history_paste(paste_text)
            if parsed:
                st.session_state.history = parsed
                st.success(f"Parsed {len(parsed)} events.")
                st.rerun()
            else:
                st.warning("Couldn't find any date+event+price triples in that text.")
        if c_clear.button("Clear history", use_container_width=True):
            st.session_state.history = []
            st.rerun()

    with intel_tab_manual:
        st.caption("Edit rows directly. Date format: YYYY-MM-DD. Event: Listed, Listing removed, Sold, Price changed, Pending.")
        rows = [
            {"date": e.date.isoformat(), "event": e.raw_event or e.event, "price": e.price or 0}
            for e in st.session_state.history
        ] or [{"date": "", "event": "", "price": 0}]
        edited = st.data_editor(
            rows,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "date": st.column_config.TextColumn("Date", help="YYYY-MM-DD or 'Apr 16, 2026'"),
                "event": st.column_config.TextColumn("Event"),
                "price": st.column_config.NumberColumn("Price", format="$%d"),
            },
            key="history_editor",
        )
        if st.button("Save manual events", key="save_manual"):
            new_history: list[HistoryEvent] = []
            for r in edited:
                d = parse_event_date(r.get("date"))
                ev_raw = r.get("event") or ""
                if d and ev_raw.strip():
                    new_history.append(HistoryEvent(
                        event=normalize_event(ev_raw),
                        date=d,
                        price=float(r["price"]) if r.get("price") else None,
                        raw_event=ev_raw,
                        source="manual",
                    ))
            st.session_state.history = new_history
            st.success(f"Saved {len(new_history)} events.")
            st.rerun()

    with intel_tab_view:
        history = st.session_state.history
        if not history:
            st.info(
                "No listing history yet. Either fetch a URL (auto-scrape), or use the "
                "**Paste history** / **Manual entry** tabs to add events."
            )
        else:
            sig = analyze_history(history, current_price=asking)

            # ── Leverage badge + headline numbers ────────────────────────────
            st.markdown(f"**Leverage:** :{sig.leverage_color}[{sig.leverage_label}]")

            hc1, hc2, hc3, hc4 = st.columns(4)
            hc1.metric(
                "Days on market",
                f"{sig.days_on_market}" if sig.days_on_market is not None else "—",
            )
            hc2.metric(
                "Cuts (current)",
                f"{sig.cuts_current_run}",
            )
            hc3.metric(
                "Prior failed",
                f"{sig.prior_failed_attempts}",
                help="Times this property was listed in the past and removed without selling.",
            )
            hc4.metric(
                "Long-term appr.",
                f"{sig.long_term_appreciation_pct:.1f}%/yr" if sig.long_term_appreciation_pct is not None else "—",
                help="Annualized appreciation since the last recorded sale.",
            )

            # ── Flag bullets ──────────────────────────────────────────────────
            if sig.flags_red:
                st.markdown("**:red[🚩 Red flags]**")
                for f in sig.flags_red:
                    st.markdown(f"- {f}")
            if sig.flags_yellow:
                st.markdown("**:orange[⚠️ Yellow flags]**")
                for f in sig.flags_yellow:
                    st.markdown(f"- {f}")
            if sig.flags_green:
                st.markdown("**:green[✅ Supporting context]**")
                for f in sig.flags_green:
                    st.markdown(f"- {f}")

            # ── Recommended offer ─────────────────────────────────────────────
            if sig.recommended_offer_low and sig.recommended_offer_high:
                off_low = sig.recommended_offer_low
                off_high = sig.recommended_offer_high
                off_mid = round((off_low + off_high) / 2 / 1000) * 1000
                pct_off = (asking - off_mid) / asking * 100 if asking else 0
                st.markdown("---")
                oc1, oc2 = st.columns([3, 2])
                with oc1:
                    st.markdown(
                        f"**Recommended offer band:** ${off_low:,.0f} – ${off_high:,.0f}  \n"
                        f"_(midpoint ~${off_mid:,.0f}, about {pct_off:.1f}% off ${asking:,.0f} ask)_"
                    )
                with oc2:
                    if st.button(
                        f"Use ${off_mid:,.0f} as offer price",
                        type="primary",
                        use_container_width=True,
                    ):
                        st.session_state.price_input = int(off_mid)
                        st.rerun()

            # ── Events table ──────────────────────────────────────────────────
            with st.expander(f"Events ({len(history)})", expanded=False):
                ev_df = pd.DataFrame([
                    {
                        "Date": e.date.isoformat(),
                        "Event": e.raw_event or e.event,
                        "Price": f"${e.price:,.0f}" if e.price else "—",
                        "Source": e.source,
                    }
                    for e in sorted(history, key=lambda x: x.date, reverse=True)
                ])
                st.dataframe(ev_df, hide_index=True, use_container_width=True)

    # ── If user has shifted price away from ask, show a side-by-side compare ─
    if (
        st.session_state.asking_price
        and price != st.session_state.asking_price
        and abs(price - st.session_state.asking_price) > 1000
    ):
        st.info(
            f"📊 Modeling at **${price:,.0f}** — listing asks **${st.session_state.asking_price:,.0f}** "
            f"({(st.session_state.asking_price - price) / st.session_state.asking_price * 100:+.1f}%). "
            "Results below reflect the offer price, not the ask."
        )

    a = Assumptions(
        down_pct=down,
        mortgage_rate=rate,
        loan_years=loan_years,
        insurance_yr=insurance_yr,
        vacancy_pct=vacancy,
        maintenance_pct=maint,
        mgmt_pct=mgmt,
        appreciation=appreciation,
        rent_growth=rent_growth,
    )

    m = analyze(price, rent_mo, tax_rate, a, hoa_mo=hoa_mo)
    label, color = verdict(m)

    st.markdown("---")
    st.markdown(f"### Result: :{color}[{label}]")

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Cap rate", f"{m['cap_rate']*100:.2f}%")
    g2.metric("Cash-on-cash", f"{m['coc_return']*100:.2f}%")
    g3.metric("Monthly cash flow", f"${m['monthly_cash_flow']:,.0f}")
    g4.metric("5-yr approx IRR", f"{m['irr_approx_5yr']*100:.2f}%")

    g5, g6, g7, g8 = st.columns(4)
    g5.metric("1% rule", f"{m['one_pct_rule']*100:.2f}%",
              help=">= 1.0% is the classic threshold")
    g6.metric("GRM", f"{m['grm']:.1f}", help="Gross Rent Multiplier — lower is better")
    g7.metric("DSCR", f"{m['dscr']:.2f}",
              help=">= 1.25 typically required by lenders")
    g8.metric("Cash needed", f"${m['cash_in']:,.0f}",
              help="Down payment + closing costs")

    with st.expander("Monthly breakdown"):
        breakdown = pd.DataFrame({
            "Item": ["Gross rent", "Vacancy loss", "Property tax", "Insurance",
                     "Maintenance", "Property mgmt", "HOA", "Mortgage P&I", "PMI",
                     "= Cash flow"],
            "Amount/mo": [
                f"${rent_mo:,.0f}",
                f"-${rent_mo * vacancy:,.0f}",
                f"-${m['monthly_tax']:,.0f}",
                f"-${m['monthly_insurance']:,.0f}",
                f"-${m['monthly_maintenance']:,.0f}",
                f"-${m['monthly_mgmt']:,.0f}",
                f"-${m['monthly_hoa']:,.0f}",
                f"-${m['monthly_pi']:,.0f}",
                f"-${m['monthly_pmi']:,.0f}",
                f"${m['monthly_cash_flow']:,.0f}",
            ],
        })
        st.dataframe(breakdown, hide_index=True, use_container_width=True)

    with st.expander("5-year projection"):
        st.markdown(
            f"- **Future home value:** ${m['fv_price_5yr']:,.0f} (at {appreciation*100:.1f}%/yr)\n"
            f"- **Appreciation gain:** ${m['equity_from_appreciation_5yr']:,.0f}\n"
            f"- **Principal paid down:** ${m['principal_paid_5yr']:,.0f}\n"
            f"- **Cumulative cash flow (5yr):** ${m['cum_cf_5yr']:,.0f}\n"
            f"- **Total return:** ${m['total_return_5yr']:,.0f} on ${m['cash_in']:,.0f} cash in"
        )

    # ─── Compare financing strategies ────────────────────────────────────────
    st.markdown("---")
    st.markdown("### Compare financing strategies")
    st.caption(
        "Same property, same operating assumptions — three different financing paths. "
        "Sidebar sliders still drive vacancy / maintenance / mgmt / appreciation; only "
        "down payment, rate, PMI, and occupancy switch per scenario."
    )

    scen_results = analyze_scenarios(price, rent_mo, tax_rate, a, hoa_mo=hoa_mo)
    scen_cols = st.columns(len(scen_results))

    for col, (name, sm) in zip(scen_cols, scen_results.items()):
        with col:
            st.markdown(f"**{name}**")
            st.caption(
                f"{sm['down_pct_used']*100:.0f}% down @ {sm['rate_used']*100:.2f}% • "
                + (f"PMI {sm['pmi_pct_used']*100:.2f}%" if sm['pmi_pct_used'] > 0 else "no PMI")
                + (f" • yr1 owner-occ" if sm['owner_occupy_years_used'] > 0 else "")
            )

            # Color the CF and IRR rows by sign / threshold.
            cf = sm["monthly_cash_flow"]
            cf_color = "green" if cf >= 0 else ("orange" if cf > -300 else "red")
            irr = sm["irr_approx_5yr"] * 100
            irr_color = "green" if irr >= 10 else ("orange" if irr >= 5 else "red")

            st.markdown(f"- Cash needed: **${sm['cash_in']:,.0f}**")
            st.markdown(f"- Monthly CF (rented): :{cf_color}[**${cf:,.0f}**]")
            st.markdown(f"- 5-yr IRR: :{irr_color}[**{irr:.1f}%**]")
            st.markdown(f"- Cash-on-cash: **{sm['coc_return']*100:.1f}%**")
            st.markdown(f"- DSCR: **{sm['dscr']:.2f}**")
            st.markdown(f"- Break-even rent: **${sm['breakeven_rent_mo']:,.0f}/mo**")
            if sm["owner_occupy_years_used"] > 0:
                st.markdown(
                    f"- Yr-1 carry (you live there): "
                    f":red[**-${sm['monthly_carry_owner_occupied']:,.0f}/mo**]"
                )

            badge = {
                "Primary residence (5% down)": ":red[⚠️ occupancy rules apply]",
                "Live-in BRRRR (yr1 owner-occ)": ":green[✅ legally clean path]",
                "Investment loan (25% down)": ":green[✅ rental day-one OK]",
            }.get(name, "")
            if badge:
                st.markdown(badge)
            st.caption(sm["caveat"])

    # Quick "what rent gap am I staring at?" summary across scenarios.
    gaps = [(n, sm["breakeven_rent_mo"] - rent_mo) for n, sm in scen_results.items()]
    best_name, best_gap = min(gaps, key=lambda x: x[1])
    if best_gap > 0:
        st.info(
            f"📉 At **${rent_mo:,.0f}/mo** rent, every scenario is short of break-even. "
            f"Smallest gap is **{best_name}** at **${best_gap:,.0f}/mo short**. "
            f"To turn any of these cash-flow positive you need either a lower price, "
            f"a higher achievable rent, or a different financing structure."
        )
    elif best_gap <= 0:
        st.success(
            f"✅ **{best_name}** clears break-even at ${rent_mo:,.0f}/mo rent."
        )

    if st.button("💾 Save to portfolio", type="primary"):
        # Re-compute listing signals so the snapshot is consistent.
        sig_save = analyze_history(st.session_state.history, current_price=price) \
            if st.session_state.history else None
        row = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "address": address, "state": state_code, "zip": zip_code,
            "in_target_area": zip_match(zip_code, target_zips, target_radius)[0],
            "price": price, "rent_mo": rent_mo,
            "beds": beds, "baths": baths, "sqft": sqft,
            "cap_rate_pct": round(m["cap_rate"] * 100, 2),
            "coc_pct": round(m["coc_return"] * 100, 2),
            "monthly_cash_flow": round(m["monthly_cash_flow"], 0),
            "irr_5yr_pct": round(m["irr_approx_5yr"] * 100, 2),
            "rate_used_pct": round(rate * 100, 2),
            "down_pct_used": round(down * 100, 1),
            "verdict": label,
            "asking_price": st.session_state.asking_price or price,
            "leverage": sig_save.leverage_label if sig_save else "",
            "days_on_market": sig_save.days_on_market if sig_save else "",
            "prior_failed": sig_save.prior_failed_attempts if sig_save else "",
            "rec_offer_low": sig_save.recommended_offer_low if sig_save else "",
            "rec_offer_high": sig_save.recommended_offer_high if sig_save else "",
            "source_url": url,
        }
        add_to_portfolio(row)
        st.success("Saved to portfolio (in-session).")


# ─── TAB 2: Batch screen ─────────────────────────────────────────────────────
with tab_batch:
    st.markdown("### Batch screen URLs")
    st.caption(
        "Paste 5–20 Zillow or Redfin listing URLs (one per line). Each is fetched, "
        "ROI-analyzed under all three financing scenarios, ranked by deal score, "
        "and tagged with leverage/days-on-market where history is available. "
        "Sidebar assumptions still apply; tax + insurance come from each property's "
        "detected state."
    )

    urls_text = st.text_area(
        "Listing URLs (one per line)",
        height=180,
        placeholder=(
            "https://www.redfin.com/MD/Silver-Spring/...\n"
            "https://www.zillow.com/homedetails/...\n"
            "https://www.redfin.com/..."
        ),
        key="batch_urls",
    )

    do_batch = st.button("Analyze all", type="primary", key="do_batch")

    if do_batch and urls_text.strip():
        urls = [u.strip() for u in urls_text.splitlines() if u.strip().startswith("http")]
        # Dedupe while preserving order.
        seen: set[str] = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        if not urls:
            st.warning("No valid URLs found. Each URL must start with http(s)://")
        else:
            base_a = Assumptions(
                down_pct=down,
                mortgage_rate=rate,
                loan_years=loan_years,
                insurance_yr=1400,  # per-property state default overrides this
                vacancy_pct=vacancy,
                maintenance_pct=maint,
                mgmt_pct=mgmt,
                appreciation=appreciation,
                rent_growth=rent_growth,
            )

            results: list[dict] = []
            errors: list[dict] = []
            progress = st.progress(0.0, text=f"Analyzing {len(urls)} listings…")

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futures = {ex.submit(_analyze_one_url, u, base_a): u for u in urls}
                for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                    row = fut.result()
                    if "error" in row:
                        errors.append(row)
                    else:
                        results.append(row)
                    progress.progress(i / len(urls), text=f"Analyzed {i}/{len(urls)}")
            progress.empty()

            if results:
                # Add target-area badge per row (target_zips/target_radius in scope).
                for r in results:
                    r["in_area"] = zip_match(r["zip"], target_zips, target_radius)[0]

                results.sort(key=lambda x: x["score"], reverse=True)

                df = pd.DataFrame([{
                    "Rank": i + 1,
                    "Address": (r["address"] or "—")[:48],
                    "Zip": r["zip"] or "—",
                    "Area": {"exact": "✅ in area",
                              "in_radius": "🟢 in radius",
                              "out": "⚪ outside",
                              "unknown": "—"}.get(r["in_area"], "—"),
                    "Price": f"${r['price']:,.0f}",
                    "BD/BA": f"{r['beds'] or '?'}/{r['baths'] or '?'}",
                    "Sqft": r["sqft"] or "—",
                    "Est rent": f"${r['rent_mo']:,.0f}",
                    "Best scenario": r["best_scenario"],
                    "CF/mo": f"${r['monthly_cf']:,.0f}",
                    "5-yr IRR": f"{r['irr_5yr']*100:.1f}%",
                    "CoC": f"{r['coc']*100:.1f}%",
                    "Cap": f"{r['cap_rate']*100:.2f}%",
                    "BE rent": f"${r['breakeven_rent']:,.0f}",
                    "Leverage": r["leverage"] or "—",
                    "DOM": r["dom"] if r["dom"] is not None else "—",
                    "Cuts": r["cuts"],
                    "Rec offer": f"${r['rec_offer']:,.0f}" if r["rec_offer"] else "—",
                    "Score": round(r["score"], 1),
                    "URL": r["url"],
                } for i, r in enumerate(results)])

                st.markdown(f"#### Ranked results ({len(results)} of {len(urls)})")
                st.dataframe(
                    df,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "URL": st.column_config.LinkColumn("Link", display_text="open ↗"),
                        "Score": st.column_config.NumberColumn("Score", format="%.1f"),
                    },
                )

                top = results[0]
                lev_str = f" • leverage: **{top['leverage']}**" if top["leverage"] else ""
                area_str = {"exact": " • ✅ in search area",
                             "in_radius": " • 🟢 in radius",
                             "out": " • ⚪ outside radius",
                             "unknown": ""}.get(top["in_area"], "")
                st.success(
                    f"🥇 **Top pick:** {top['address']}  \n"
                    f"Best scenario *{top['best_scenario']}* → "
                    f"${top['monthly_cf']:,.0f}/mo CF, "
                    f"{top['irr_5yr']*100:.1f}% 5-yr IRR, "
                    f"cap {top['cap_rate']*100:.2f}%"
                    f"{lev_str}{area_str}"
                )

                buf = io.StringIO()
                pd.DataFrame(results).to_csv(buf, index=False)
                st.download_button(
                    "📥 Download as CSV",
                    buf.getvalue(),
                    file_name=f"batch_screen_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                    mime="text/csv",
                )

            if errors:
                with st.expander(f"⚠️ Errors ({len(errors)})", expanded=False):
                    for e in errors:
                        st.markdown(f"- `{e['url']}` — {e['error']}")
                st.caption(
                    "Tip: the scrapers occasionally get blocked or hit a non-detail page. "
                    "Try the URL one-at-a-time in the **Evaluate property** tab — that flow "
                    "also exposes a 'paste history' fallback when auto-scraping is partial."
                )


# ─── TAB 3: Portfolio ────────────────────────────────────────────────────────
with tab_portfolio:
    st.markdown("### Saved properties")
    st.caption(
        "Portfolio is held in your browser session. Use **Download CSV** to keep a "
        "permanent copy and **Upload CSV** to reload it next time."
    )

    pf = portfolio_df()

    col_up, col_dl, col_clr = st.columns([2, 1, 1])
    with col_up:
        uploaded = st.file_uploader("📤 Upload a previously-saved portfolio.csv",
                                    type=["csv"], key="pf_upload")
        if uploaded is not None:
            try:
                df = pd.read_csv(uploaded, dtype={"zip": str, "state": str})
                st.session_state.portfolio = df.to_dict("records")
                st.success(f"Loaded {len(df)} properties.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not load CSV: {e}")

    if pf.empty:
        st.info("No saved properties yet. Evaluate one and click **Save to portfolio**.")
    else:
        pf_display = pf.sort_values("irr_5yr_pct", ascending=False)
        st.dataframe(pf_display, hide_index=True, use_container_width=True)

        buf = io.StringIO()
        pf.to_csv(buf, index=False)
        col_dl.download_button(
            "📥 Download CSV",
            buf.getvalue(),
            file_name=f"portfolio_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        if col_clr.button("🗑️ Clear", use_container_width=True):
            st.session_state.portfolio = []
            st.rerun()


# ─── TAB 4: State reference ──────────────────────────────────────────────────
with tab_states:
    st.markdown("### State property tax + insurance reference")
    st.caption(
        "Effective property tax rates and average homeowner-insurance premiums by US state. "
        "These are the defaults the **Evaluate** tab uses when you pick a state. "
        "Edit `states.csv` to refresh. County/city rates can vary widely — always verify."
    )

    df = states.copy()
    df["Property tax %"] = (df["property_tax_rate"] * 100).round(2)
    df["Avg insurance $/yr"] = df["avg_insurance_yr"].astype(int)
    df = df[["state", "state_name", "Property tax %", "Avg insurance $/yr", "notes"]]
    df.columns = ["State", "Name", "Property tax %", "Avg insurance $/yr", "Notes"]
    df = df.sort_values("Property tax %")
    st.dataframe(df, hide_index=True, use_container_width=True)


# ─── TAB 5: About ────────────────────────────────────────────────────────────
with tab_about:
    st.markdown("""
### What this is

An ROI calculator for **buy-and-hold single-family rental** investments anywhere
in the United States. Paste a listing URL or enter property details, pick your
state, tweak the financing/operating assumptions in the sidebar, and see live
ROI math.

### Glossary

- **SFH** — Single Family Home (detached house intended for one household).
- **Cap rate** — Net Operating Income ÷ Price. Returns ignoring financing.
- **Cash-on-cash (CoC)** — Annual cash flow ÷ cash invested (down + closing).
  This is what's actually in your pocket each year vs. what you put in.
- **NOI** — Net Operating Income. Rent minus all operating expenses (tax,
  insurance, maintenance, mgmt, vacancy), *before* mortgage payment.
- **DSCR** — Debt Service Coverage Ratio. NOI ÷ debt service. Lenders typically
  require ≥ 1.25 for investor loans.
- **GRM** — Gross Rent Multiplier. Price ÷ annual gross rent. Lower is better.
- **1% rule** — Monthly rent ≥ 1% of price. A classic screen; rarely passed
  in major metros right now.
- **5-yr IRR** — Approximate annualized total return over 5 years, including
  appreciation, principal paydown, and cumulative cash flow.

### Verdict thresholds

- **STRONG BUY** — positive monthly CF and 5-yr IRR > 10%
- **BUY (near breakeven CF)** — CF > -$200/mo and 5-yr IRR > 10%
- **HOLD (appreciation play)** — 5-yr IRR > 8% (you bleed cash but build equity)
- **MARGINAL** — 5-yr IRR > 5%
- **AVOID** — below all of the above

### Listing intelligence

The asking price is one number — what's behind it is several. The Listing
intelligence section reads the property's history (auto-scraped, pasted, or
manually entered) and surfaces:

- **Days on market** in the current run
- **Cuts** the seller has already publicly accepted
- **Prior failed attempts** — times this house was listed and withdrawn
- **Prior high-water ask** — what the market already rejected
- **Long-term appreciation** since the last actual sale
- **Red / yellow / green flags** — human-readable signals
- **Recommended offer band** — $low–$high range with a button to re-run the ROI math at that price

#### Leverage labels

- **STRONG LEVERAGE** — heavy seller motivation (≥ 5% recommended discount)
- **MILD LEVERAGE** — moderate motivation (2.5–5%)
- **SOME LEVERAGE** — light motivation (< 2.5%)
- **HOT LISTING — pay near ask** — fresh, no history of cuts/failures
- **NO HISTORY** — feed history events to enable this section

#### Discount heuristic (capped at 10%)

| Signal | Stacked discount |
| --- | --- |
| 14–30 days on market | +1.0% |
| 31–60 days | +2.5% |
| 61–90 days | +4.5% |
| 91–120 days | +6.0% |
| > 120 days | +7.0% |
| Prior failed attempt | +2.0% |
| ≥ 2 prior failed attempts | +1.0% |
| 1 price cut, current run | +1.0% |
| 2+ price cuts, current run | +2.0% |
| > 5% off prior unsold high-water ask | +1.0% |

The output is a ±1%-wide band around `current_price × (1 − discount)`, rounded
to the nearest $1K.

### How state defaults work

When you pick a state, property tax rate and annual insurance are pre-filled
from `states.csv` (50 states + DC). These are **starting points** — county/city
rates and property-specific insurance vary significantly. Always override for
your actual situation:

- Maryland's state average is 1.09%, but Montgomery County is 0.87% and
  Prince George's County is 1.15%.
- Florida and Texas have very high average insurance because of wind/hail/flood
  risk; inland properties may pay much less.

### Caveats

- **Rent estimates are approximations.** Verify with Zillow Rental Manager,
  Rentometer, and recent rental comps before bidding.
- **The URL scraper breaks periodically.** Zillow and Redfin change their HTML
  to deter automated requests. Manual entry always works.
- **Property tax rates** in `states.csv` are state-level effective rates for
  primary residences. Investment property tax can be 5-15% higher in some
  jurisdictions due to lost homestead credits.
- **Insurance** uses state averages. Coastal Florida, wildfire-zone California,
  and tornado-belt states run much higher.
- **This is not financial advice.** Run any deal past a licensed agent, CPA,
  and inspector before committing.
""")

    st.markdown("---")
    st.caption(
        "Built with Streamlit. [Source on GitHub](https://github.com/ashenafiwk/sfh-roi-analyzer)"
    )
