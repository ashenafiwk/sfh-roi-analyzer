"""Listing scrapers for Zillow and Redfin.

Strategy: pull all JSON-LD blocks from the page, then recursively walk them to
find schema.org RealEstate fields. This works as long as the portal serves
schema.org-compliant structured data, which both Zillow and Redfin currently do
for SEO reasons (they want Google to surface their listings).

Returns a dict with whatever could be extracted, or {"error": "..."} on failure.
"""

import json
import re
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_html(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200 and len(r.text) > 5000:
            return r.text
    except Exception:
        pass
    return None


# ─── JSON-LD walker ──────────────────────────────────────────────────────────

REAL_ESTATE_TYPES = {
    "SingleFamilyResidence", "Residence", "House", "Apartment",
    "Accommodation", "Townhouse", "RealEstateListing",
}


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return set(t)
    return set()


def _walk(node: Any, out: dict):
    if isinstance(node, dict):
        types = _types(node)

        if types & REAL_ESTATE_TYPES:
            if "name" in node and "address" not in out:
                out["address"] = node["name"]
            if "address" in node:
                out["address"] = _format_address(node["address"]) or out.get("address")
            if "numberOfBedrooms" in node:
                out["beds"] = _as_num(node["numberOfBedrooms"])
            if "numberOfBathroomsTotal" in node:
                out["baths"] = _as_num(node["numberOfBathroomsTotal"])
            elif "numberOfRooms" in node and "beds" not in out:
                out["beds"] = _as_num(node["numberOfRooms"])
            if "floorSize" in node and isinstance(node["floorSize"], dict):
                v = node["floorSize"].get("value")
                if v:
                    out["sqft"] = _as_num(v)
            if "yearBuilt" in node:
                out["year_built"] = _as_num(node["yearBuilt"])
            if "accommodationCategory" in node:
                out["property_type"] = node["accommodationCategory"]

        if "Offer" in types or "AggregateOffer" in types:
            p = node.get("price") or node.get("lowPrice")
            if p:
                out["price"] = _as_num(p)

        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def _parse_jsonld_all(html: str) -> dict:
    out: dict = {}
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        text = tag.string or "".join(tag.strings)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        _walk(data, out)
    return out


# ─── Site-specific scrapers ──────────────────────────────────────────────────

def parse_redfin(html: str) -> dict:
    out = _parse_jsonld_all(html)
    out["source"] = "redfin"

    if "price" not in out:
        m = re.search(r'"price":\s*(\d{5,})', html)
        if m:
            out["price"] = int(m.group(1))

    if "address" not in out:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.find("title")
        if title:
            out["address"] = title.text.split(" | ")[0].strip()

    m = re.search(r'"hoaDues":\s*(\d+)', html)
    if m:
        out["hoa_mo"] = int(m.group(1))

    out["zip"] = _zip_from_address(out.get("address", ""))
    return _clean(out)


def parse_zillow(html: str) -> dict:
    out = _parse_jsonld_all(html)
    out["source"] = "zillow"

    soup = BeautifulSoup(html, "html.parser")
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            _walk_zillow_next(data, out)
        except Exception:
            pass

    if "price" not in out:
        m = re.search(r'"price"\s*:\s*(\d{5,})', html)
        if m:
            out["price"] = int(m.group(1))

    if "address" not in out:
        title = soup.find("title")
        if title:
            out["address"] = title.text.split(" | ")[0].strip()

    out["zip"] = _zip_from_address(out.get("address", ""))
    return _clean(out)


def _walk_zillow_next(node: Any, out: dict):
    if isinstance(node, dict):
        if "bedrooms" in node and "bathrooms" in node and "livingArea" in node:
            out.setdefault("beds", _as_num(node.get("bedrooms")))
            out.setdefault("baths", _as_num(node.get("bathrooms")))
            out.setdefault("sqft", _as_num(node.get("livingArea")))
            if node.get("price"):
                out.setdefault("price", _as_num(node["price"]))
            if node.get("monthlyHoaFee"):
                out.setdefault("hoa_mo", _as_num(node["monthlyHoaFee"]))
            if node.get("rentZestimate"):
                out.setdefault("rent_zestimate", _as_num(node["rentZestimate"]))
            if node.get("yearBuilt"):
                out.setdefault("year_built", _as_num(node["yearBuilt"]))
            addr = node.get("streetAddress") or node.get("address")
            if addr:
                if isinstance(addr, dict):
                    out.setdefault("address", _format_address(addr))
                else:
                    out.setdefault("address", addr)
        for v in node.values():
            _walk_zillow_next(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_zillow_next(v, out)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _format_address(a: Any) -> str:
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        parts = [
            a.get("streetAddress") or a.get("street"),
            a.get("addressLocality") or a.get("city"),
            a.get("addressRegion") or a.get("state"),
            a.get("postalCode") or a.get("zipcode"),
        ]
        return ", ".join(str(p) for p in parts if p)
    return ""


def _zip_from_address(addr: str) -> str | None:
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", addr or "")
    return m.group(1) if m else None


def _as_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except Exception:
        return None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", 0)}


# ─── Public entry point ──────────────────────────────────────────────────────

def parse_listing(url: str) -> dict:
    host = urlparse(url).netloc.lower()
    html = fetch_html(url)
    if not html:
        return {"error": "could not fetch URL (portal may be blocking the request)"}

    if "zillow.com" in host:
        result = parse_zillow(html)
    elif "redfin.com" in host:
        result = parse_redfin(html)
    else:
        return {"error": f"unsupported site: {host}"}

    if not result.get("price"):
        return {
            "error": "could not extract price — portal HTML may have changed; please enter manually",
            "partial": result,
        }
    return result
