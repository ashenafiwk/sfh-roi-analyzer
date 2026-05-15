# SFH ROI Analyzer

A Streamlit web app that does buy-and-hold rental investment math for **Single Family Homes (SFH)** anywhere in the United States. Paste a Zillow or Redfin listing URL, pick your state, tweak your financing assumptions, and see live ROI metrics — cap rate, cash-on-cash, monthly cash flow, 5-year IRR, plus the classic 1% rule, GRM, and DSCR.

## Features

- 📋 **Paste a listing URL** → auto-fills price, beds, baths, sqft, address, zip from Zillow or Redfin (and pulls Zestimate rent when available)
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
├── scrapers.py         Zillow / Redfin URL parsers (JSON-LD walker)
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
