"""Listing-history intelligence.

Given a price-history feed (from a Zillow/Redfin scrape, manual entry, or a
pasted block of text), derive negotiation signals: how long the place has been
on market, prior failed attempts, price cuts, long-term appreciation, and a
recommended offer band.

The seller's history is leverage. A house that failed to sell at $1.15M in
October and is back asking $1.05M in April is a different deal than a fresh
listing — even though the asking price is identical to the model.

Pure Python — no I/O, no third-party deps beyond stdlib.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional


# ─── Event model ─────────────────────────────────────────────────────────────

# Normalized event kinds. The raw text from listing sites is messy
# ("Listed", "Listing removed", "Price changed", "Sold (Public Records)" etc.)
# so we collapse it to a small vocabulary.
EVENT_LISTED = "listed"
EVENT_RELISTED = "relisted"
EVENT_PRICE_CHANGED = "price_changed"
EVENT_PENDING = "pending"
EVENT_CONTINGENT = "contingent"
EVENT_SOLD = "sold"
EVENT_DELISTED = "delisted"  # withdrawn / removed / expired without selling
EVENT_OTHER = "other"


@dataclass
class HistoryEvent:
    event: str            # one of the EVENT_* constants
    date: date
    price: Optional[float] = None
    raw_event: str = ""   # original text (kept for display)
    source: str = ""      # "zillow" / "redfin" / "manual" / "paste"


# ─── Normalization ───────────────────────────────────────────────────────────

def normalize_event(raw: str) -> str:
    s = (raw or "").lower()
    # Order matters — "listing removed" must match delisted before "listed".
    if any(k in s for k in ("removed", "withdrawn", "expired", "delisted",
                            "off market", "off-market", "cancel")):
        return EVENT_DELISTED
    if "sold" in s or "closed" in s:
        return EVENT_SOLD
    if "pending" in s:
        return EVENT_PENDING
    if "contingent" in s or "under contract" in s:
        return EVENT_CONTINGENT
    if "price chang" in s or "price reduc" in s or "price increas" in s or "reprice" in s:
        return EVENT_PRICE_CHANGED
    if "relist" in s or "back on market" in s:
        return EVENT_RELISTED
    if "listed" in s or "for sale" in s or "active" in s:
        return EVENT_LISTED
    return EVENT_OTHER


def parse_event_date(raw) -> Optional[date]:
    """Accept ISO strings, common US formats, or epoch milliseconds."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Epoch — Redfin uses ms, Zillow's `time` field is also ms.
        v = float(raw)
        if v > 1e11:  # ms
            v /= 1000
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip time component if present.
    s = s.split("T")[0].split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s) else s
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Last resort: try to find a date inside the string.
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def parse_price(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v > 0 else None
    s = str(raw).replace(",", "").replace("$", "").strip()
    if not s or s == "—" or s.lower() in ("n/a", "na"):
        return None
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


# ─── Paste-text parser ───────────────────────────────────────────────────────

# Redfin/Zillow property-history blocks copied into the clipboard end up
# looking roughly like:
#   Apr 16, 2026
#   Listed
#   Redfin
#   $1,050,000
#   Dec 11, 2025
#   Listing removed
#   ...
# We tolerate any line ordering as long as a date, event-text, and price
# (in that *rough* proximity) are present per row.

_DATE_TOKEN = re.compile(
    r"\b("
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")\b",
    re.IGNORECASE,
)
_PRICE_TOKEN = re.compile(r"\$\s*([\d,]{4,})(?!\s*/(?:sq|sf))", re.IGNORECASE)
_EVENT_TOKEN = re.compile(
    r"\b(Listed|Listing removed|Listing Removed|Delisted|Withdrawn|Expired|"
    r"Sold|Pending|Contingent|Under Contract|Price changed|Price Changed|"
    r"Price Reduced|Price Increased|Re[- ]?listed|Back on market|Back on Market)\b",
    re.IGNORECASE,
)


def parse_history_paste(text: str) -> list[HistoryEvent]:
    """Best-effort: pull (date, event, price) tuples out of pasted text.

    Strategy: tokenize the text into dates, events, and prices in
    document-order. Walk through them grouping each date with the nearest
    following event + price (within a small window).
    """
    if not text:
        return []
    tokens: list[tuple[int, str, str]] = []  # (pos, kind, value)
    for m in _DATE_TOKEN.finditer(text):
        tokens.append((m.start(), "date", m.group(1)))
    for m in _EVENT_TOKEN.finditer(text):
        tokens.append((m.start(), "event", m.group(1)))
    for m in _PRICE_TOKEN.finditer(text):
        tokens.append((m.start(), "price", m.group(1)))
    tokens.sort(key=lambda t: t[0])

    events: list[HistoryEvent] = []
    i = 0
    while i < len(tokens):
        if tokens[i][1] != "date":
            i += 1
            continue
        d = parse_event_date(tokens[i][2])
        ev = None
        pr = None
        # Look at the next ~6 tokens; stop at the next date.
        j = i + 1
        while j < len(tokens) and j - i <= 6 and tokens[j][1] != "date":
            if tokens[j][1] == "event" and ev is None:
                ev = tokens[j][2]
            elif tokens[j][1] == "price" and pr is None:
                pr = parse_price(tokens[j][2])
            j += 1
        if d and (ev or pr is not None):
            events.append(HistoryEvent(
                event=normalize_event(ev or ""),
                date=d,
                price=pr,
                raw_event=ev or "",
                source="paste",
            ))
        i = j if j > i else i + 1
    return events


# ─── Signals ─────────────────────────────────────────────────────────────────

@dataclass
class ListingSignals:
    days_on_market: Optional[int] = None
    original_ask_current: Optional[float] = None
    pct_off_original: Optional[float] = None
    cuts_current_run: int = 0
    prior_failed_attempts: int = 0
    prior_high_water_ask: Optional[float] = None        # highest prior ask that didn't sell
    prior_high_water_date: Optional[date] = None
    pct_off_high_water: Optional[float] = None          # current ask vs. that high
    last_sold_price: Optional[float] = None
    last_sold_date: Optional[date] = None
    long_term_appreciation_pct: Optional[float] = None
    recommended_offer_low: Optional[float] = None
    recommended_offer_high: Optional[float] = None
    leverage_label: str = ""    # "STRONG LEVERAGE" / "MILD LEVERAGE" / "HOT LISTING" / "NEUTRAL"
    leverage_color: str = "gray"
    flags_red: list[str] = field(default_factory=list)
    flags_yellow: list[str] = field(default_factory=list)
    flags_green: list[str] = field(default_factory=list)
    summary: str = ""


def _split_current_run(events: list[HistoryEvent]) -> tuple[list[HistoryEvent], list[HistoryEvent]]:
    """Split events into (prior, current_run).

    The current run starts after the most recent terminal event (sold/delisted),
    or at the start if no terminal event has occurred.
    """
    last_terminal = -1
    for i, e in enumerate(events):
        if e.event in (EVENT_SOLD, EVENT_DELISTED):
            last_terminal = i
    if last_terminal == len(events) - 1 and events[last_terminal].event == EVENT_SOLD:
        # House already sold — no current listing.
        return events, []
    return events[: last_terminal + 1], events[last_terminal + 1 :]


def _count_failed_attempts(prior: list[HistoryEvent]) -> int:
    """Count distinct prior listings that ended in delisted (not sold)."""
    failed = 0
    in_attempt = False
    for e in prior:
        if e.event in (EVENT_LISTED, EVENT_RELISTED):
            in_attempt = True
        elif e.event == EVENT_DELISTED and in_attempt:
            failed += 1
            in_attempt = False
        elif e.event == EVENT_SOLD:
            in_attempt = False
    return failed


def analyze_history(
    events: list[HistoryEvent],
    current_price: float,
    today: Optional[date] = None,
) -> ListingSignals:
    if today is None:
        today = date.today()
    events = [e for e in events if e.date is not None]
    events.sort(key=lambda e: e.date)

    sig = ListingSignals()
    if not events or not current_price:
        sig.leverage_label = "NO HISTORY"
        sig.summary = "No listing history available."
        return sig

    prior, current = _split_current_run(events)

    # ── Current-run signals ──
    first_in_run = next((e for e in current if e.event in (EVENT_LISTED, EVENT_RELISTED)), None)
    if first_in_run:
        sig.days_on_market = max(0, (today - first_in_run.date).days)
        sig.original_ask_current = first_in_run.price
        if sig.original_ask_current and sig.original_ask_current > 0:
            sig.pct_off_original = (sig.original_ask_current - current_price) / sig.original_ask_current * 100
    sig.cuts_current_run = sum(
        1
        for e in current
        if e.event == EVENT_PRICE_CHANGED
        and e.price is not None
        and sig.original_ask_current
        and e.price < sig.original_ask_current
    )

    # ── Prior failed attempts ──
    sig.prior_failed_attempts = _count_failed_attempts(prior)

    # ── Prior high-water ask (highest priced listing that didn't sell) ──
    # Walk prior in reverse to find the most recent run of listing activity
    # that ended in a delisted event, then take the max ask within it.
    high_water_run: list[HistoryEvent] = []
    saw_delisted_terminal = False
    for e in reversed(prior):
        if e.event == EVENT_SOLD:
            break  # earlier history is a different cycle
        if e.event == EVENT_DELISTED:
            saw_delisted_terminal = True
        if saw_delisted_terminal and e.event in (EVENT_LISTED, EVENT_RELISTED,
                                                  EVENT_PRICE_CHANGED, EVENT_DELISTED):
            if e.price:
                high_water_run.append(e)
    if high_water_run:
        peak = max(high_water_run, key=lambda e: e.price or 0)
        sig.prior_high_water_ask = peak.price
        sig.prior_high_water_date = peak.date
        if peak.price and current_price:
            sig.pct_off_high_water = (peak.price - current_price) / peak.price * 100

    # ── Last sale + long-term appreciation ──
    last_sold = next((e for e in reversed(prior) if e.event == EVENT_SOLD and e.price), None)
    if last_sold:
        sig.last_sold_price = last_sold.price
        sig.last_sold_date = last_sold.date
        years = (today - last_sold.date).days / 365.25
        if years > 0.5 and last_sold.price and current_price:
            sig.long_term_appreciation_pct = ((current_price / last_sold.price) ** (1 / years) - 1) * 100

    # ── Flags ──
    if sig.prior_failed_attempts >= 1:
        s = "s" if sig.prior_failed_attempts > 1 else ""
        sig.flags_red.append(
            f"Failed to sell {sig.prior_failed_attempts} time{s} previously — "
            "the market has already disagreed with the seller's price."
        )
    if sig.pct_off_high_water is not None and sig.pct_off_high_water >= 3:
        sig.flags_yellow.append(
            f"Relisted ~{sig.pct_off_high_water:.1f}% below a prior unsold ask of "
            f"${sig.prior_high_water_ask:,.0f} — the market already rejected the higher number."
        )
    if sig.days_on_market is not None and sig.days_on_market > 60:
        sig.flags_red.append(
            f"On market {sig.days_on_market} days — well past the fast-moving window. "
            "Carrying costs and seller fatigue are working in your favor."
        )
    if sig.cuts_current_run >= 2:
        sig.flags_red.append(
            f"{sig.cuts_current_run} price cuts in the current run — initial pricing was meaningfully off."
        )

    if sig.days_on_market is not None and 30 < sig.days_on_market <= 60:
        sig.flags_yellow.append(
            f"On market {sig.days_on_market} days — buyers have hesitated."
        )
    if sig.pct_off_original is not None and sig.pct_off_original >= 5:
        sig.flags_yellow.append(
            f"Already {sig.pct_off_original:.1f}% off original ask — leverage exists but the seller may dig in."
        )
    if sig.cuts_current_run == 1:
        sig.flags_yellow.append(
            "One price cut already — momentum suggests another may follow if no offers arrive."
        )

    if sig.days_on_market is not None and sig.days_on_market < 14 and sig.cuts_current_run == 0:
        sig.flags_green.append(
            f"Fresh listing ({sig.days_on_market}d) — seller still expects close to ask."
        )
    if sig.long_term_appreciation_pct is not None and sig.long_term_appreciation_pct > 4:
        sig.flags_green.append(
            f"Strong long-term appreciation (~{sig.long_term_appreciation_pct:.1f}%/yr since last sale) — the area has held value."
        )
    elif sig.long_term_appreciation_pct is not None and sig.long_term_appreciation_pct < 2:
        sig.flags_yellow.append(
            f"Weak long-term appreciation (~{sig.long_term_appreciation_pct:.1f}%/yr since last sale) — "
            "this specific property may underperform the broader market."
        )

    # ── Recommended offer band ──
    discount = 0.0
    if sig.days_on_market is not None:
        if sig.days_on_market > 120:
            discount += 0.07
        elif sig.days_on_market > 90:
            discount += 0.06
        elif sig.days_on_market > 60:
            discount += 0.045
        elif sig.days_on_market > 30:
            discount += 0.025
        elif sig.days_on_market > 14:
            discount += 0.01
    if sig.prior_failed_attempts >= 1:
        discount += 0.02
    if sig.prior_failed_attempts >= 2:
        discount += 0.01
    if sig.pct_off_high_water is not None and sig.pct_off_high_water >= 5:
        # Seller has already publicly accepted a meaningful haircut from a
        # higher number — the new ask is the floor of their flexibility, not
        # the ceiling. Push a little further.
        discount += 0.01
    if sig.cuts_current_run >= 2:
        discount += 0.02
    elif sig.cuts_current_run == 1:
        discount += 0.01

    # Cap. A 10%+ ask-off offer is functionally an insult in most markets and
    # rarely productive even with strong leverage.
    discount = min(discount, 0.10)

    if discount >= 0.05:
        sig.leverage_label = "STRONG LEVERAGE"
        sig.leverage_color = "green"
    elif discount >= 0.025:
        sig.leverage_label = "MILD LEVERAGE"
        sig.leverage_color = "orange"
    elif discount > 0:
        sig.leverage_label = "SOME LEVERAGE"
        sig.leverage_color = "orange"
    else:
        sig.leverage_label = "HOT LISTING — pay near ask"
        sig.leverage_color = "red"

    if discount > 0:
        sig.recommended_offer_high = round(current_price * (1 - discount + 0.01) / 1000) * 1000
        sig.recommended_offer_low = round(current_price * (1 - discount - 0.01) / 1000) * 1000
    else:
        sig.recommended_offer_high = current_price
        sig.recommended_offer_low = round(current_price * 0.98 / 1000) * 1000

    # ── Summary ──
    parts = []
    if sig.days_on_market is not None:
        parts.append(f"{sig.days_on_market}d on market")
    if sig.cuts_current_run:
        parts.append(f"{sig.cuts_current_run} cut{'s' if sig.cuts_current_run > 1 else ''}")
    if sig.prior_failed_attempts:
        parts.append(f"{sig.prior_failed_attempts} prior failed attempt{'s' if sig.prior_failed_attempts > 1 else ''}")
    if sig.long_term_appreciation_pct is not None:
        parts.append(f"{sig.long_term_appreciation_pct:.1f}%/yr appreciation")
    sig.summary = " • ".join(parts) if parts else "Listing history present."
    return sig
