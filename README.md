# SFH ROI Analyzer

A Streamlit web app that does buy-and-hold rental investment math for **Single Family Homes (SFH)** anywhere in the United States. Paste a Zillow or Redfin listing URL, pick your state, tweak your financing assumptions, and see live ROI metrics — cap rate, cash-on-cash, monthly cash flow, 5-year IRR, plus the classic 1% rule, GRM, and DSCR.

## Features

- 📋 **Paste a listing URL** → auto-fills price, beds, baths, sqft, address, zip from Zillow or Redfin (and pulls Zestimate rent when available)
- 🕵️ **Listing intelligence** — scrapes (or accepts pasted) property history, then surfaces days on market, prior failed listings, price cuts, long-term appreciation, red/yellow/green flags, and a **recommended offer band** based on seller motivation
- 📍 **Your search area** — set one or more target zip codes in the sidebar; properties are tagged `in target area`, `same region (zip3 match)`, or `outside`. Defaults to `20901` (Silver Spring, MD) — change `DEFAULT_TARGET_ZIPS` near the top of `app.py` to set a different default for your fork, or just edit the value in the UI.
- 🇺🇸 **State dropdown** → pre-fills property tax rate and average insurance for all 50 states + DC
- 🎚️ **Sidebar sliders** for mortgage rate, down %, vacancy, mgmt fee, maintenance, appreciation
- 📊 **Live ROI metrics** — cap rate, cash-on-cash, monthly CF, 5-yr IRR, 1% rule, GRM, DSCR
- 💾 **Save to portfolio** — compare multiple properties side-by-side
- 📥 **Export / 📤 Import portfolio CSV** — persistent across sessions (and across devices, since portfolio lives in your browser)
- 📍 **State reference table** — every US state's effective property tax + average insurance, sortable

## Try it locally

Requires Python 3.10+.

```bash
git clone https://github.com/ashenafiwk/sfh-roi-analyzer.git
cd sfh-roi-analyzer
pip install -r requirements.txt
streamlit run app.py
```

On Windows you can double-click `run.bat` instead — it creates a venv and installs deps on first run, then launches Streamlit.

The app opens at http://localhost:8501.

## Deploy to Streamlit Community Cloud

1. **Fork or create a new repo** with these files pushed to GitHub.
2. Go to **https://share.streamlit.io** and sign in with your GitHub account.
3. Click **"New app"**, pick your repo + branch + `app.py` as the main file.
4. Click **Deploy**. First boot takes 1-2 minutes while it installs dependencies from `requirements.txt`.
5. Your app will be live at `https://<your-username>-sfh-roi-analyzer-app-<hash>.streamlit.app`.

No secrets or environment variables needed — everything works out of the box. The portfolio uses session_state (no filesystem writes), so it survives Streamlit Cloud's ephemeral filesystem.

### After deploying

- Update the GitHub link in the sidebar of `app.py` to point at your fork.
- State property tax rates in `states.csv` shift slowly (year-over-year). Refresh from Tax Foundation / SmartAsset / Bankrate when needed and open a PR.

## Project structure

```
sfh-roi-analyzer/
├── app.py              Streamlit UI (entry point)
├── roi.py              ROI math — Assumptions, analyze(), verdict()
├── signals.py          Listing-history analysis — flags + recommended offer
├── scrapers.py         Zillow / Redfin URL + history parsers (JSON walker)
├── states.csv          Per-state property tax + avg insurance defaults (50 + DC)
├── roi_analyzer.py     CLI for bulk-analyzing a properties.csv
├── requirements.txt    Python dependencies
├── run.bat             Windows one-click launcher (creates venv + launches)
├── .streamlit/
│   └── config.toml     Theme + server config
├── .gitignore
├── LICENSE             MIT
└── README.md           You are here
```

## How the math works

For a given price × rent × tax rate, the app computes:

- **Cap rate** = NOI / Price (returns ignoring financing)
- **Cash-on-cash** = annual cash flow / (down + closing)
- **Monthly cash flow** = effective rent − tax − insurance − maintenance − mgmt − HOA − P&I
- **5-yr total return** = appreciation gain + principal paid down + cumulative cash flow
- **5-yr approx IRR** = `(1 + total_return / cash_in)^(1/5) - 1`

Defaults:
- 20% down, 30-yr conventional @ 7.25% (typical investor rate in 2026)
- 6% vacancy, 8% maintenance, 8% property mgmt (all % of gross rent)
- 3% closing costs
- 3.5% annual appreciation, 3% annual rent growth
- Property tax and insurance pre-fill from `states.csv` based on selected state

Verdict thresholds (in `roi.py:verdict()`):
- **STRONG BUY** — positive CF + 5-yr IRR > 10%
- **BUY (near breakeven CF)** — CF > -$200/mo + 5-yr IRR > 10%
- **HOLD (appreciation play)** — 5-yr IRR > 8%
- **MARGINAL** — 5-yr IRR > 5%
- **AVOID** — below all of the above

## Listing intelligence

The asking price is one number. What's behind it is several:

- How long has the property been on market?
- Has the seller cut the price in the current run?
- Did it fail to sell before — listed, then withdrawn?
- What's the long-term appreciation since the last actual sale?

These shape your negotiating leverage. A fresh listing in week 1 isn't the same deal as a relist that already failed at a higher number and is sitting at month 4. `signals.py:analyze_history()` takes the property history and returns:

- **Leverage label** — `STRONG LEVERAGE` / `MILD LEVERAGE` / `SOME LEVERAGE` / `HOT LISTING — pay near ask`
- **Days on market** in the current run
- **Cuts in current run**, original ask, % off
- **Prior failed attempts** (listed → withdrawn without selling)
- **Prior high-water ask** — the highest unsold ask from the most recent failed cycle
- **Long-term appreciation %/yr** since the last recorded sale
- **Red / yellow / green flags** — human-readable signals
- **Recommended offer band** — a rounded $low–$high range, with a midpoint button that re-runs the ROI math at the recommended offer

### How to feed it data

1. **Auto-scrape**: paste a Zillow/Redfin URL, click Fetch. The history extractor walks all embedded JSON blobs looking for property-history events (resilient to most HTML rearrangement, but not guaranteed).
2. **Paste**: copy the Property History block from the listing page and paste it into the **Paste history** tab. The parser tokenizes dates, event keywords, and prices in document-order — handles the various ways Redfin/Zillow lay out their history rows.
3. **Manual entry**: type rows directly in the **Manual entry** tab.

### Discount heuristic

The recommended-offer discount stacks contributions:

| Signal | Discount |
| --- | --- |
| 14–30 days on market | +1.0% |
| 31–60 days | +2.5% |
| 61–90 days | +4.5% |
| 91–120 days | +6.0% |
| > 120 days | +7.0% |
| Prior failed attempt | +2.0% |
| Multiple prior failed attempts | +1.0% |
| 1 price cut current run | +1.0% |
| 2+ price cuts current run | +2.0% |
| Already > 5% off a prior unsold high-water ask | +1.0% |

Capped at 10% total. The output is a ±1%-wide band around `current_price × (1 − discount)`, rounded to the nearest $1K. Below 2.5% total, the verdict is `HOT LISTING` and the band tightens to a 0–2% discount.

## Bulk analysis from CSV (CLI)

To rank multiple candidate properties at once:

```bash
python roi_analyzer.py --properties my_deals.csv --rate 0.0725 --down-pct 0.20
```

Required columns: `address, state, price, est_rent`.
Optional columns: `zip, beds, baths, sqft, property_tax_rate, insurance_yr, hoa_mo, source, link, notes`.

Per-row `property_tax_rate` / `insurance_yr` overrides the state default for that property. Output is written to `property_report.csv`.

## Customizing the state defaults

`states.csv` holds the per-state defaults. To refresh:

1. Open `states.csv` in Excel or any editor.
2. Update `property_tax_rate` and `avg_insurance_yr` columns from a current source (Tax Foundation, SmartAsset, Bankrate).
3. Save — the app auto-reloads on next refresh.

To customize for a specific submarket (county/city/zip), you have two options:
- Always override the tax/insurance fields manually after picking the state.
- Add per-row overrides in your `properties.csv` for the CLI path.

## Roadmap

- [ ] Multi-family mode — duplex/triplex/fourplex with per-unit rent table
- [ ] House-hack scenario — owner-occupant FHA at 3.5% down, exclude one unit from rental income
- [ ] BRRRR analyzer — Buy, Rehab, Rent, Refi, Repeat scenario flow
- [ ] STR / Airbnb mode — nightly rate × occupancy assumption
- [ ] Sensitivity heat-map — price vs rent vs rate
- [ ] County/city overlay — drill into sub-state markets

## Caveats

- **Not financial advice.** Run any deal past a licensed agent, CPA, and inspector.
- **URL scraping breaks periodically** when portals change their HTML. Manual entry always works.
- **State tax rates are averages.** County/city rates can vary significantly — always verify for your specific market.
- **Insurance** uses state averages. Coastal Florida, wildfire-zone California, and tornado-belt states run much higher than the averages suggest.

## License

[MIT](LICENSE) — do whatever you want with it.

## Contributing

Issues and PRs welcome. Particularly looking for:
- Refreshed `states.csv` data
- Additional portal scrapers (Realtor.com, Homes.com)
- Improved verdict scoring
