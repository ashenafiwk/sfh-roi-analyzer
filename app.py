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
from datetime import datetime

import pandas as pd
import streamlit as st

from roi import Assumptions, analyze, verdict
from scrapers import parse_listing

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBMARKETS_CSV = os.path.join(SCRIPT_DIR, "submarkets.csv")

st.set_page_config(page_title="SFH ROI Analyzer", page_icon="🏠", layout="wide")


@st.cache_data
def load_submarkets():
    df = pd.read_csv(SUBMARKETS_CSV, dtype={"zip": str})
    return df


submarkets = load_submarkets()
SUBMARKETS_BY_ZIP = {row["zip"]: row for _, row in submarkets.iterrows()}


# ─── Session state initialization ────────────────────────────────────────────
if "portfolio" not in st.session_state:
    st.session_state.portfolio = []
if "fetched" not in st.session_state:
    st.session_state.fetched = {}


def add_to_portfolio(row: dict):
    st.session_state.portfolio.append(row)


def portfolio_df() -> pd.DataFrame:
    if not st.session_state.portfolio:
        return pd.DataFrame()
    return pd.DataFrame(st.session_state.portfolio)


# ─── Sidebar: Assumptions ────────────────────────────────────────────────────
st.sidebar.header("Assumptions")
rate = st.sidebar.slider("Mortgage rate (%)", 4.0, 10.0, 7.25, 0.05) / 100
down = st.sidebar.slider("Down payment (%)", 5, 50, 20, 5) / 100
vacancy = st.sidebar.slider("Vacancy (%)", 0, 15, 6, 1) / 100
mgmt = st.sidebar.slider("Property mgmt (% of rent)", 0, 12, 8, 1) / 100
maint = st.sidebar.slider("Maintenance (% of rent)", 0, 15, 8, 1) / 100
insurance = st.sidebar.number_input("Annual insurance ($)", 500, 5000, 1200, 100)
appreciation = st.sidebar.slider("Long-run appreciation (%)", 0.0, 8.0, 3.5, 0.5) / 100
rent_growth = st.sidebar.slider("Long-run rent growth (%)", 0.0, 8.0, 3.0, 0.5) / 100
loan_years = st.sidebar.selectbox("Loan term (years)", [15, 20, 30], index=2)

a = Assumptions(
    down_pct=down,
    mortgage_rate=rate,
    loan_years=loan_years,
    insurance_yr=insurance,
    vacancy_pct=vacancy,
    maintenance_pct=maint,
    mgmt_pct=mgmt,
    appreciation=appreciation,
    rent_growth=rent_growth,
)

with st.sidebar.expander("💡 Scenario tips"):
    st.markdown(
        "**Refi later?** Drop the rate slider to 5.75% to simulate a refinance.\n\n"
        "**House hack?** Set down to 5% (FHA owner-occupant). Use room-rental totals as rent.\n\n"
        "**All cash?** Set down to 50%+ — cash-on-cash converges to cap rate."
    )

st.sidebar.markdown("---")
st.sidebar.caption(
    "🛠️ [View source on GitHub](https://github.com/ashenafiwk/sfh-roi-analyzer)"
)

# ─── Header ──────────────────────────────────────────────────────────────────
st.title("🏠 SFH ROI Analyzer")
st.caption(
    "**SFH** = Single Family Home. Buy-and-hold rental investment math: cap rate, "
    "cash-on-cash, monthly cash flow, 5-year IRR, plus the classic 1% rule, GRM, and DSCR."
)
st.info(
    "📍 **DC Metro Edition** — reference market data covers 26 zip codes within "
    "20 miles of 20901 (Silver Spring, MD). You can analyze any property, but the "
    "rent/tax auto-fill only triggers for these zips.",
    icon="📍",
)

tab_eval, tab_portfolio, tab_market, tab_about = st.tabs(
    ["Evaluate property", "My portfolio", "Market reference", "About"]
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
            extras = []
            if result.get("property_type"):
                extras.append(result["property_type"])
            if result.get("year_built"):
                extras.append(f"built {int(result['year_built'])}")
            extras_str = f" ({', '.join(extras)})" if extras else ""
            st.success(f"Loaded: {result.get('address', 'listing')}{extras_str}")

    fetched = st.session_state.fetched

    st.markdown("### Property details")
    c1, c2, c3 = st.columns(3)
    address = c1.text_input("Address", value=fetched.get("address", ""))
    zip_code = c2.text_input("Zip code", value=str(fetched.get("zip", "20707")), max_chars=5)
    price = c3.number_input("Price ($)", min_value=50000, max_value=2_000_000,
                            value=int(fetched.get("price") or 430000), step=5000)

    c4, c5, c6 = st.columns(3)
    beds = c4.number_input("Beds", 1, 10, int(fetched.get("beds") or 3))
    baths = c5.number_input("Baths", 1.0, 10.0, float(fetched.get("baths") or 2), step=0.5)
    sqft = c6.number_input("Sqft", 400, 10000, int(fetched.get("sqft") or 1500), step=50)

    sub_row = SUBMARKETS_BY_ZIP.get(zip_code)
    default_rent = int(fetched.get("rent_zestimate") or
                       (sub_row["est_3br_sfh_rent"] if sub_row is not None else 3000))
    default_tax = float(sub_row["property_tax_rate"]) if sub_row is not None else 0.0087

    c7, c8, c9 = st.columns(3)
    rent_mo = c7.number_input("Expected rent ($/mo)", 1000, 10000, default_rent, step=50)
    tax_rate = c8.number_input("Property tax rate", 0.005, 0.025, default_tax,
                               step=0.0005, format="%.4f")
    hoa_mo = c9.number_input("HOA ($/mo)", 0, 1000, int(fetched.get("hoa_mo") or 0), step=10)

    if sub_row is not None:
        st.caption(
            f"📍 {sub_row['area']} ({sub_row['county']} County, "
            f"{sub_row['distance_mi_from_20901']}mi from 20901) — "
            f"median SFH ${int(sub_row['median_sfh_price']):,}, "
            f"median 3BR rent ${int(sub_row['est_3br_sfh_rent']):,}"
        )
    else:
        st.caption("📍 Zip not in DC Metro reference dataset — enter rent and tax rate manually.")

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
        add_to_portfolio({
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "address": address, "zip": zip_code, "price": price, "rent_mo": rent_mo,
            "beds": beds, "baths": baths, "sqft": sqft,
            "cap_rate_pct": round(m["cap_rate"] * 100, 2),
            "coc_pct": round(m["coc_return"] * 100, 2),
            "monthly_cash_flow": round(m["monthly_cash_flow"], 0),
            "irr_5yr_pct": round(m["irr_approx_5yr"] * 100, 2),
            "rate_used_pct": round(rate * 100, 2),
            "down_pct_used": round(down * 100, 1),
            "verdict": label,
            "source_url": url,
        })
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
                df = pd.read_csv(uploaded, dtype={"zip": str})
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


# ─── TAB 3: Market reference ─────────────────────────────────────────────────
with tab_market:
    st.markdown("### Submarket reference — 26 zips within 20 mi of 20901")
    st.caption(
        "Median SFH price × median 3BR rent for each submarket, ranked by your current "
        "sidebar assumptions. Move the sliders to see how the ranking changes."
    )

    rows = []
    for _, r in submarkets.iterrows():
        m = analyze(float(r["median_sfh_price"]), float(r["est_3br_sfh_rent"]),
                    float(r["property_tax_rate"]), a)
        rows.append({
            "Zip": r["zip"],
            "Area": r["area"],
            "County": r["county"],
            "Dist (mi)": r["distance_mi_from_20901"],
            "Median $": int(r["median_sfh_price"]),
            "Rent": int(r["est_3br_sfh_rent"]),
            "Cap %": round(m["cap_rate"] * 100, 2),
            "CoC %": round(m["coc_return"] * 100, 2),
            "CF/mo": round(m["monthly_cash_flow"], 0),
            "5yr IRR %": round(m["irr_approx_5yr"] * 100, 2),
        })
    df = pd.DataFrame(rows).sort_values("5yr IRR %", ascending=False)
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.caption("⚠️ Data snapshot ~mid-2026. Verify with Redfin/Zillow before acting.")


# ─── TAB 4: About ────────────────────────────────────────────────────────────
with tab_about:
    st.markdown("""
### What this is

An ROI calculator for **buy-and-hold single-family rental** investments. Paste a
listing URL or enter property details, tweak the financing/operating assumptions
in the sidebar, and see live ROI math.

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

### Caveats

- **Rent estimates are approximations.** Verify with Zillow Rental Manager,
  Rentometer, and recent rental comps before bidding.
- **The URL scraper breaks periodically.** Zillow and Redfin change their HTML
  to deter automated requests. Manual entry always works.
- **Property tax rates** in the reference dataset apply to *primary residences*
  in MD. Investment property tax can be ~5-15% higher in some jurisdictions
  due to lost Homestead credits.
- **Insurance** uses a generic $1,200/yr default. Get real quotes for actual
  underwriting decisions.
- **This is not financial advice.** Run any deal past a licensed agent, CPA,
  and inspector before committing.
""")

    st.markdown("---")
    st.caption(
        "Built with Streamlit. [Source on GitHub](https://github.com/your-username/sfh-roi-analyzer)"
    )
