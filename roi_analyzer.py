"""
SFH Buy-and-Hold ROI Analyzer — generic US CLI.

Bulk-analyze candidate properties from a CSV. Each row needs at minimum:
  address, state, price, est_rent
Optional: zip, beds, baths, sqft, property_tax_rate (overrides state default),
          insurance_yr (overrides state default), hoa_mo, source, link, notes

Defaults pulled from states.csv (effective property tax + avg insurance per state).

Usage:
  python roi_analyzer.py
  python roi_analyzer.py --rate 0.0575 --down-pct 0.40
  python roi_analyzer.py --properties my_deals.csv
"""

import argparse
import csv
import math
import os

from roi import Assumptions, analyze

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_state_defaults(path: str) -> dict[str, dict]:
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["state"].upper()] = {
                "property_tax_rate": float(r["property_tax_rate"]),
                "insurance_yr": float(r["avg_insurance_yr"]),
                "state_name": r["state_name"],
            }
    return out


def rank_score(m: dict) -> float:
    """Composite ranking — emphasizes cash flow + IRR, penalizes negative CF."""
    s = 0.0
    s += m["cap_rate"] * 100 * 1.5
    s += m["coc_return"] * 100 * 1.0
    s += m["irr_approx_5yr"] * 100 * 0.8
    if m["monthly_cash_flow"] < 0:
        s -= 5
    s += min(m["one_pct_rule"] * 100, 1.0) * 5
    return s


def analyze_properties(path: str, base_a: Assumptions, state_defaults: dict):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            state = (r.get("state") or "").strip().upper()
            sd = state_defaults.get(state, {})

            price = float(r["price"])
            rent = float(r["est_rent"])
            tax = float(r.get("property_tax_rate") or sd.get("property_tax_rate") or 0.011)
            ins = float(r.get("insurance_yr") or sd.get("insurance_yr") or 1400)
            hoa = float(r.get("hoa_mo") or 0)

            a = Assumptions(
                down_pct=base_a.down_pct,
                mortgage_rate=base_a.mortgage_rate,
                loan_years=base_a.loan_years,
                insurance_yr=ins,
                vacancy_pct=base_a.vacancy_pct,
                maintenance_pct=base_a.maintenance_pct,
                mgmt_pct=base_a.mgmt_pct,
                appreciation=base_a.appreciation,
                rent_growth=base_a.rent_growth,
            )
            m = analyze(price, rent, tax, a, hoa_mo=hoa)
            m["address"] = r.get("address", "")
            m["state"] = state
            m["zip"] = r.get("zip", "")
            m["notes"] = r.get("notes", "")
            m["source"] = r.get("source", "")
            m["link"] = r.get("link", "")
            m["rank_score"] = rank_score(m)
            rows.append(m)
    rows.sort(key=lambda x: x["rank_score"], reverse=True)
    return rows


def write_csv(path: str, rows: list, fields: list):
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                for k, v in list(r.items()):
                    if isinstance(v, float) and not math.isinf(v):
                        r[k] = round(v, 2)
                w.writerow(r)
    except PermissionError:
        print(f"(skipped writing {os.path.basename(path)} — no write permission)")


def fmt_money(x):
    return f"${x:>10,.0f}"


def print_table(rows):
    print(f"{'Rank':<5}{'Addr':<40}{'St':<4}{'Price':>11}{'Rent':>8}"
          f"{'Cap%':>7}{'CoC%':>7}{'CF/mo':>9}{'5yr IRR%':>10}{'Score':>8}")
    for i, m in enumerate(rows, 1):
        addr = (m.get("address") or "")[:39]
        print(f"{i:<5}{addr:<40}{m.get('state', ''):<4}"
              f"{fmt_money(m['price'])}{m['rent_mo']:>8,.0f}"
              f"{m['cap_rate']*100:>7.2f}{m['coc_return']*100:>7.2f}"
              f"{m['monthly_cash_flow']:>9,.0f}"
              f"{m['irr_approx_5yr']*100:>10.2f}{m['rank_score']:>8.2f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--states", default=os.path.join(SCRIPT_DIR, "states.csv"))
    p.add_argument("--properties", default=os.path.join(SCRIPT_DIR, "properties.csv"))
    p.add_argument("--down-pct", type=float, default=0.20)
    p.add_argument("--rate", type=float, default=0.0725)
    p.add_argument("--mgmt-pct", type=float, default=0.08)
    p.add_argument("--vacancy-pct", type=float, default=0.06)
    args = p.parse_args()

    state_defaults = load_state_defaults(args.states)

    a = Assumptions(
        down_pct=args.down_pct,
        mortgage_rate=args.rate,
        mgmt_pct=args.mgmt_pct,
        vacancy_pct=args.vacancy_pct,
    )

    if not os.path.exists(args.properties):
        print(f"No properties file at {args.properties}.")
        print("Create a CSV with columns: address, state, price, est_rent (+ optional fields).")
        return

    print("\n=== INDIVIDUAL PROPERTIES ===\n")
    pr = analyze_properties(args.properties, a, state_defaults)
    print_table(pr)
    write_csv(os.path.join(SCRIPT_DIR, "property_report.csv"), pr, [
        "address", "state", "zip", "price", "rent_mo",
        "cap_rate", "coc_return", "monthly_cash_flow", "irr_approx_5yr",
        "rank_score", "source", "link", "notes",
    ])


if __name__ == "__main__":
    main()
