"""
SFH ROI Analyzer — Streamlit web app for single-family rental investment math.

Cloud-ready: portfolio lives in session_state (no filesystem writes).
Users export/import their portfolio as CSV.

Run locally:
    pip install -r requirements.txt
    streamlit run app.py
"""

import io
import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st

from roi import Assumptions, analyze, verdict
from scrapers import parse_listing
from signals import HistoryEvent, analyze_history, normalize_event, parse_event_date, parse_history_paste

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATES_CSV = os.path.join(SCRIPT_DIR, "states.csv")

st.set_page_config(page_title="SFH ROI Analyzer", page_icon="🏠", layout="wide")


# ─── Reference data ──────────────────────────────────────────────────────────
@st.cache_data
def load_states():
    return pd.read_csv(STATES_CSV, dtype={"state": str})


states = load_states()
STATES_BY_CODE = {row["state"]: row for _, row in states.iterrows()}
STATE_LABELS = [f"{r['state']} — {r['state_name']}" for _, r in states.iterrows()]
LABEL_TO_CODE = {f"{r['state']} — {r['state_name']}": r["state"] for _, r in states.iterrows()}

STATE_IN_ADDR = re.compile(r"\b([A-Z]{2})\b\s*\d{5}")


def state_from_address(addr: str) -> str | None:
    if not addr:
        return None
    m = STATE_IN_ADDR.search(addr.upper())
    if m and m.group(1) in STATES_BY_CODE:
        return m.group(1)
    return None


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


def add_to_portfolio(row: dict):
    st.session_state.portfolio.append(row)


def portfolio_df() -> pd.DataFrame:
    if not st.session_state.portfolio:
        return pd.DataFrame()
    return pd.DataFrame(st.session_state.portfolio)


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

tab_eval, tab_portfolio, tab_states, tab_about = st.tabs(
    ["Evaluate property", "My portfolio", "State reference", "About"]
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
                     "Maintenance", "Property mgmt", "HOA", "Mortgage P&I", "= Cash flow"],
            "Amount/mo": [
                f"${rent_mo:,.0f}",
                f"-${rent_mo * vacancy:,.0f}",
                f"-${m['monthly_tax']:,.0f}",
                f"-${m['monthly_insurance']:,.0f}",
                f"-${m['monthly_maintenance']:,.0f}",
                f"-${m['monthly_mgmt']:,.0f}",
                f"-${m['monthly_hoa']:,.0f}",
                f"-${m['monthly_pi']:,.0f}",
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

    if st.button("💾 Save to portfolio", type="primary"):
        # Re-compute listing signals so the snapshot is consistent.
        sig_save = analyze_history(st.session_state.history, current_price=price) \
            if st.session_state.history else None
        row = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "address": address, "state": state_code, "zip": zip_code,
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


# ─── TAB 2: Portfolio ────────────────────────────────────────────────────────
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


# ─── TAB 3: State reference ──────────────────────────────────────────────────
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


# ─── TAB 4: About ────────────────────────────────────────────────────────────
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
