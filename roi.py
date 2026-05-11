"""Pure ROI math — no I/O. Imported by the Streamlit app."""

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
    cash_flow = noi - debt_service

    cap_rate = noi / price
    coc_return = cash_flow / cash_in if cash_in else 0
    one_pct = rent_mo / price
    gross_rent_multiplier = price / annual_rent
    dscr = noi / debt_service if debt_service else float("inf")

    fv_price = price * (1 + a.appreciation) ** 5
    equity_from_appreciation = fv_price - price
    r_m = a.mortgage_rate / 12
    n = a.loan_years * 12
    balance_60 = loan * ((1 + r_m) ** n - (1 + r_m) ** 60) / ((1 + r_m) ** n - 1)
    principal_paid = loan - balance_60
    cum_cf = sum(
        (rent_mo * 12 * (1 + a.rent_growth) ** y * (1 - a.vacancy_pct)
         - (tax_yr * (1 + a.appreciation) ** y)
         - insurance
         - rent_mo * 12 * (1 + a.rent_growth) ** y * (a.maintenance_pct + a.mgmt_pct)
         - hoa_yr
         - debt_service)
        for y in range(5)
    )
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
        "monthly_maintenance": maintenance / 12,
        "monthly_mgmt": mgmt / 12,
        "monthly_hoa": hoa_mo,
        "monthly_opex": opex / 12,
        "monthly_cash_flow": cash_flow / 12,
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


def verdict(m: dict) -> tuple[str, str]:
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
