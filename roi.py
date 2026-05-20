"""Pure ROI math — no I/O. Imported by both the CLI and the Streamlit app."""

from dataclasses import dataclass


@dataclass
class Assumptions:
    down_pct: float = 0.20
    mortgage_rate: float = 0.0725
    loan_years: int = 30
    insurance_yr: float = 1200
    vacancy_pct: float = 0.06
    maintenance_pct: float = 0.08
    mgmt_pct: float = 0.08
    closing_pct: float = 0.03
    appreciation: float = 0.035
    rent_growth: float = 0.03
    pmi_annual_pct: float = 0.0
    owner_occupy_years: int = 0


def replace(a: "Assumptions", **overrides) -> "Assumptions":
    """Return a copy of `a` with the given fields overridden."""
    from dataclasses import replace as _replace
    return _replace(a, **overrides)


# ─── Scenario presets ────────────────────────────────────────────────────────
# Each preset overrides only the *financing* fields on top of the user's base
# Assumptions (sliders for vacancy / maintenance / mgmt / appreciation / etc.
# pass through unchanged). Keep these generic — they apply to any US investor.

SCENARIOS: dict[str, dict] = {
    "Primary residence (5% down)": {
        "down_pct": 0.05,
        "mortgage_rate": 0.06375,
        "pmi_annual_pct": 0.005,
        "owner_occupy_years": 0,
        "caveat": (
            "Owner-occupant terms. Lender requires the borrower to occupy "
            "for ~12 months. Renting from day one is a breach of the "
            "mortgage contract and potential occupancy fraud. Headline "
            "cash-flow here assumes rental — *legal only after the "
            "occupancy period*."
        ),
    },
    "Live-in BRRRR (yr1 owner-occ)": {
        "down_pct": 0.05,
        "mortgage_rate": 0.06375,
        "pmi_annual_pct": 0.005,
        "owner_occupy_years": 1,
        "caveat": (
            "Same primary-residence loan, used legally: occupy yr 1, "
            "renovate while you live there, then convert to rental yr 2+. "
            "Yr-1 carry is pure cost — see *Yr-1 carry* line. The 5-yr IRR "
            "averages the lost year in."
        ),
    },
    "Investment loan (25% down)": {
        "down_pct": 0.25,
        "mortgage_rate": 0.075,
        "pmi_annual_pct": 0.0,
        "owner_occupy_years": 0,
        "caveat": (
            "Non-owner-occupied loan. Higher down + ~1pt rate premium, "
            "no PMI, no occupancy requirement. Rent from day one is "
            "legal. DSCR-style underwriting available at higher LTV."
        ),
    },
}


def assumptions_for_scenario(name: str, base: Assumptions) -> Assumptions:
    """Build a scenario's Assumptions by overlaying its financing fields on
    top of the user's base assumptions."""
    overrides = {k: v for k, v in SCENARIOS[name].items() if k != "caveat"}
    return replace(base, **overrides)


def monthly_mortgage(principal: float, annual_rate: float, years: int) -> float:
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def analyze(price: float, rent_mo: float, tax_rate: float, a: Assumptions, hoa_mo: float = 0):
    down = price * a.down_pct
    closing = price * a.closing_pct
    cash_in = down + closing

    loan = price - down
    pi = monthly_mortgage(loan, a.mortgage_rate, a.loan_years)
    pmi_yr = loan * a.pmi_annual_pct
    monthly_pmi = pmi_yr / 12

    annual_rent = rent_mo * 12
    effective_rent = annual_rent * (1 - a.vacancy_pct)
    tax_yr = price * tax_rate
    insurance = a.insurance_yr
    maintenance = annual_rent * a.maintenance_pct
    mgmt = annual_rent * a.mgmt_pct
    hoa_yr = hoa_mo * 12

    opex = tax_yr + insurance + maintenance + mgmt + hoa_yr
    noi = effective_rent - opex
    debt_service = pi * 12
    cash_flow = noi - debt_service - pmi_yr

    cap_rate = noi / price
    coc_return = cash_flow / cash_in if cash_in else 0
    one_pct = rent_mo / price
    gross_rent_multiplier = price / annual_rent
    dscr = noi / debt_service if debt_service else float("inf")

    # Yr-1 carry if owner-occupied: no rent income, no mgmt/maint % expenses.
    monthly_carry_owner_occupied = (
        pi + tax_yr / 12 + insurance / 12 + monthly_pmi + hoa_mo
    )

    # Break-even rent ($/mo) for the rented steady-state.
    # Solve: annual_rent * [(1-v) - (maint%+mgmt%)]
    #        = tax_yr + insurance + hoa_yr + debt_service + pmi_yr
    rent_coeff = (1 - a.vacancy_pct) - (a.maintenance_pct + a.mgmt_pct)
    fixed_costs = tax_yr + insurance + hoa_yr + debt_service + pmi_yr
    breakeven_rent_mo = (fixed_costs / rent_coeff) / 12 if rent_coeff > 0 else float("inf")

    # 5-year projection
    fv_price = price * (1 + a.appreciation) ** 5
    equity_from_appreciation = fv_price - price
    r_m = a.mortgage_rate / 12
    n = a.loan_years * 12
    balance_60 = loan * ((1 + r_m) ** n - (1 + r_m) ** 60) / ((1 + r_m) ** n - 1)
    principal_paid = loan - balance_60

    def _year_cf(y: int) -> float:
        # Owner-occupied year: no rental income, no mgmt/maintenance %.
        if y < a.owner_occupy_years:
            return (
                -(tax_yr * (1 + a.appreciation) ** y)
                - insurance
                - hoa_yr
                - debt_service
                - pmi_yr
            )
        rent_y = rent_mo * 12 * (1 + a.rent_growth) ** y
        return (
            rent_y * (1 - a.vacancy_pct)
            - tax_yr * (1 + a.appreciation) ** y
            - insurance
            - rent_y * (a.maintenance_pct + a.mgmt_pct)
            - hoa_yr
            - debt_service
            - pmi_yr
        )

    cum_cf = sum(_year_cf(y) for y in range(5))
    total_return_5yr = equity_from_appreciation + principal_paid + cum_cf
    irr_approx = (1 + total_return_5yr / cash_in) ** (1 / 5) - 1 if cash_in else 0

    return {
        "price": price,
        "rent_mo": rent_mo,
        "cash_in": cash_in,
        "loan_amount": loan,
        "monthly_pi": pi,
        "monthly_tax": tax_yr / 12,
        "monthly_insurance": insurance / 12,
        "monthly_pmi": monthly_pmi,
        "monthly_maintenance": maintenance / 12,
        "monthly_mgmt": mgmt / 12,
        "monthly_hoa": hoa_mo,
        "monthly_opex": opex / 12,
        "monthly_cash_flow": cash_flow / 12,
        "monthly_carry_owner_occupied": monthly_carry_owner_occupied,
        "breakeven_rent_mo": breakeven_rent_mo,
        "annual_cash_flow": cash_flow,
        "noi": noi,
        "cap_rate": cap_rate,
        "coc_return": coc_return,
        "one_pct_rule": one_pct,
        "grm": gross_rent_multiplier,
        "dscr": dscr,
        "fv_price_5yr": fv_price,
        "equity_from_appreciation_5yr": equity_from_appreciation,
        "principal_paid_5yr": principal_paid,
        "cum_cf_5yr": cum_cf,
        "total_return_5yr": total_return_5yr,
        "irr_approx_5yr": irr_approx,
    }


def analyze_scenarios(
    price: float,
    rent_mo: float,
    tax_rate: float,
    base: Assumptions,
    hoa_mo: float = 0,
) -> dict[str, dict]:
    """Run analyze() for each preset in SCENARIOS using `base` as the
    starting Assumptions. Returns {scenario_name: result-dict-plus-caveat}."""
    out: dict[str, dict] = {}
    for name in SCENARIOS:
        a = assumptions_for_scenario(name, base)
        m = analyze(price, rent_mo, tax_rate, a, hoa_mo=hoa_mo)
        m["scenario"] = name
        m["caveat"] = SCENARIOS[name]["caveat"]
        m["down_pct_used"] = a.down_pct
        m["rate_used"] = a.mortgage_rate
        m["pmi_pct_used"] = a.pmi_annual_pct
        m["owner_occupy_years_used"] = a.owner_occupy_years
        out[name] = m
    return out


def verdict(m: dict) -> tuple[str, str]:
    """Return (label, color) for a quick visual verdict."""
    cf = m["monthly_cash_flow"]
    irr = m["irr_approx_5yr"]
    if cf > 0 and irr > 0.10:
        return ("STRONG BUY", "green")
    if cf > -200 and irr > 0.10:
        return ("BUY (near breakeven CF)", "green")
    if irr > 0.08:
        return ("HOLD (appreciation play)", "orange")
    if irr > 0.05:
        return ("MARGINAL", "orange")
    return ("AVOID", "red")
