# SFH ROI Analyzer

A Streamlit web app that does buy-and-hold rental investment math for **Single Family Homes (SFH)**. Paste a Zillow or Redfin listing URL, tweak your financing assumptions, and see live ROI metrics — cap rate, cash-on-cash, monthly cash flow, 5-year IRR, plus the classic 1% rule, GRM, and DSCR.

> **DC Metro Edition** — the reference market data covers 26 zip codes within 20 miles of Silver Spring, MD (20901). You can analyze any property, but rent and tax-rate auto-fill only triggers for these zips.

## Features

- 📋 **Paste a listing URL** → auto-fills price, beds, baths, sqft, address, zip from Zillow or Redfin
- 🎚️ **Sidebar sliders** for mortgage rate, down %, vacancy, mgmt fee, maintenance, appreciation
- 📊 **Live ROI metrics** — cap rate, cash-on-cash, monthly CF, 5-yr IRR, 1%, GRM, DSCR
- 💾 **Save to portfolio** — compare multiple properties side-by-side
- 📥 **Export / 📤 Import portfolio CSV** — persistent across sessions
- 📍 **Submarket reference table** — 26 zips ranked live as you change assumptions

## Try it locally

Requires Python 3.10+.

```bash
git clone https://github.com/your-username/sfh-roi-analyzer.git
cd sfh-roi-analyzer
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

## Deploy to Streamlit Community Cloud

1. **Fork or create a new repo** with these files pushed to GitHub.
2. Go to **https://share.streamlit.io** and sign in with your GitHub account.
3. Click **"New app"**, pick your repo + branch + `app.py` as the main file.
4. Click **Deploy**. First boot takes 1-2 minutes while it installs dependencies from `requirements.txt`.
5. Your app will be live at `https://<your-username>-sfh-roi-analyzer-app-<hash>.streamlit.app`.

No secrets or environment variables needed — everything works out of the box.

### After deploying

- Update the GitHub link in `app.py` (search for `your-username`) so the "View source" button points to your fork.
- The reference data in `submarkets.csv` becomes stale every 3-6 months. Open a PR (or edit directly) when median prices shift.

## Project structure

```
sfh-roi-analyzer/
├── app.py              Streamlit UI (entry point)
├── roi.py              ROI math — Assumptions, analyze(), verdict()
├── scrapers.py         Zillow / Redfin URL parsers (JSON-LD walker)
├── submarkets.csv      DC Metro reference data (26 zips)
├── requirements.txt    Python dependencies
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
- $1,200/yr landlord insurance
- 3.5% annual appreciation, 3% annual rent growth

Verdict thresholds (in `roi.py:verdict()`):
- **STRONG BUY** — positive CF + 5-yr IRR > 10%
- **BUY (near breakeven CF)** — CF > -$200/mo + 5-yr IRR > 10%
- **HOLD (appreciation play)** — 5-yr IRR > 8%
- **MARGINAL** — 5-yr IRR > 5%
- **AVOID** — below all of the above

## Customizing for your own market

The `submarkets.csv` file holds median SFH prices, rent estimates, and tax rates per zip. To target a different metro:

1. Open `submarkets.csv` in Excel or any editor.
2. Replace the rows with your local zips.
3. Update the banner text in `app.py` (search for "DC Metro Edition").
4. Push to your fork. Streamlit Cloud auto-redeploys.

## Roadmap

- [ ] Multi-family mode — duplex/triplex/fourplex with per-unit rent table
- [ ] House-hack scenario — owner-occupant FHA at 3.5% down, exclude one unit from rental income
- [ ] BRRRR analyzer — Buy, Rehab, Rent, Refi, Repeat scenario flow
- [ ] STR / Airbnb mode — nightly rate × occupancy assumption
- [ ] Sensitivity heat-map — price vs rent vs rate

## Caveats

- **Not financial advice.** Run any deal past a licensed agent, CPA, and inspector.
- **URL scraping breaks periodically** when portals change their HTML. Manual entry always works.
- **Rent estimates are at-zip averages** in the reference data, not at-property. Verify with Rentometer / Zillow Rentals / actual rental comps before bidding.

## License

[MIT](LICENSE) — do whatever you want with it.

## Contributing

Issues and PRs welcome. Particularly looking for:
- Submarket data refreshes
- Additional portal scrapers (Realtor.com, Homes.com)
- Improved verdict scoring
