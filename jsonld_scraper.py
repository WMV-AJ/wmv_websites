"""Generic schema.org JSON-LD event scraper.

Many event sites embed schema.org <script type="application/ld+json"> Event
objects for SEO (Google's event rich-results). That is free, structured,
render-free data — one extractor works across allevents.in, highape.com, and
any other site that ships Event JSON-LD. This is the "scrape all of them"
lever: no per-site API reverse-engineering.

Fields come straight from the schema.org Event vocabulary:
  name, startDate/endDate, location.name (+address), offers.price, url, performer[].
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time

from curl_cffi import requests as cr

from schema import Event
from sortmyscene import parse_lineup_from_title

IMPERSONATE = "chrome124"
_LD_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _collect_events(node, out):
    if isinstance(node, dict):
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and "Event" in x for x in types):
            out.append(node)
        for v in node.values():
            _collect_events(v, out)
    elif isinstance(node, list):
        for x in node:
            _collect_events(x, out)


def extract_ld_events(html: str) -> list[dict]:
    out = []
    for block in _LD_RE.findall(html):
        block = block.strip()
        try:
            _collect_events(json.loads(block), out)
        except json.JSONDecodeError:
            # some sites emit multiple concatenated objects or trailing commas
            try:
                _collect_events(json.loads(block.rstrip(";")), out)
            except Exception:
                continue
    return out


def _venue_and_city(loc):
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    if not isinstance(loc, dict):
        return (str(loc) if loc else None), None
    venue = loc.get("name")
    city = None
    addr = loc.get("address")
    if isinstance(addr, dict):
        city = addr.get("addressLocality") or addr.get("addressRegion")
    elif isinstance(addr, str):
        city = addr
    return venue, city


def _price(offers):
    if not offers:
        return None, None
    if isinstance(offers, dict):
        offers = [offers]
    prices, cur = [], None
    for o in offers:
        if not isinstance(o, dict):
            continue
        cur = o.get("priceCurrency") or cur
        for key in ("price", "lowPrice"):
            v = o.get(key)
            if v not in (None, "", "0"):
                try:
                    prices.append(float(str(v).replace(",", "")))
                except ValueError:
                    pass
    if not prices:
        return None, cur
    lo = min(prices)
    sym = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get(cur, "")
    label = f"{sym}{int(lo) if lo.is_integer() else lo}"
    return (label + (" onwards" if len(prices) > 1 else "")), cur


def _performers(perf):
    if not perf:
        return []
    if isinstance(perf, dict):
        perf = [perf]
    out = []
    for p in perf:
        if isinstance(p, dict) and p.get("name"):
            out.append(p["name"].strip())
        elif isinstance(p, str) and p.strip():
            out.append(p.strip())
    return out


def normalise(e: dict, source: str, default_city: str | None) -> Event:
    venue, city = _venue_and_city(e.get("location"))
    price, cur = _price(e.get("offers"))
    url = e.get("url") or ""
    if isinstance(url, list):
        url = url[0] if url else ""
    img = e.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url")
    # lineup: structured performer if present, else parse the title
    lineup = _performers(e.get("performer")) or parse_lineup_from_title(e.get("name") or "")
    return Event(
        source=source,
        source_id=(url or e.get("name", "")),
        title=(e.get("name") or "").strip(),
        url=url,
        city=city or default_city,
        venue=venue,
        start=e.get("startDate"),
        end=e.get("endDate"),
        date_label=e.get("startDate"),
        price=price,
        currency=cur,
        categories=[],
        lineup=lineup,
        description=(e.get("description") or None),
        image=img,
        raw=e,
    )


def enrich_lineup_from_detail(events, delay: float = 0.4):
    """For sites whose listing lacks performers (allevents), fetch each event's
    detail page and pull the schema.org `performer` names from its JSON-LD."""
    session = cr.Session(impersonate=IMPERSONATE)
    filled = 0
    for i, ev in enumerate(events, 1):
        if ev.lineup or not ev.url:
            continue
        try:
            r = session.get(ev.url, timeout=25)
            if r.status_code == 200:
                for det in extract_ld_events(r.text):
                    names = _performers(det.get("performer"))
                    if names:
                        ev.lineup = names
                        filled += 1
                        break
        except Exception:
            pass
        if i % 25 == 0:
            print(f"    detail {i}/{len(events)} ({filled} lineups)", file=sys.stderr)
        time.sleep(delay)
    return filled


def scrape(url_template: str, source: str, city: str | None, pages: int = 1, delay: float = 0.6):
    """url_template may contain '{page}' for pagination, else pages is ignored."""
    session = cr.Session(impersonate=IMPERSONATE)
    events, seen = [], set()
    for page in range(1, pages + 1):
        url = url_template.format(page=page) if "{page}" in url_template else url_template
        try:
            r = session.get(url, timeout=30)
        except Exception as e:
            print(f"  page {page}: ERR {e}", file=sys.stderr)
            break
        if r.status_code != 200:
            print(f"  page {page}: HTTP {r.status_code}", file=sys.stderr)
            break
        raw = extract_ld_events(r.text)
        new = 0
        for e in raw:
            ev = normalise(e, source, city)
            if not ev.title or ev.source_id in seen:
                continue
            seen.add(ev.source_id)
            events.append(ev)
            new += 1
        print(f"  page {page}: +{new} events (total {len(events)})", file=sys.stderr)
        if "{page}" not in url_template or new == 0:
            break
        time.sleep(delay)
    return events


def write_out(events, out):
    rows = [e.to_dict() for e in events]
    with open(f"{out}.json", "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    if rows:
        cols = [c for c in rows[0] if c != "raw"]
        with open(f"{out}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("|".join(v) if isinstance(v, list) else v) for k, v in r.items() if k != "raw"})
    return len(rows)


# --- site registry: add any JSON-LD-friendly site here ---
SITES = {
    "allevents": {"url": "https://allevents.in/{city_slug}/all?page={page}", "pages": 5},
    "highape":   {"url": "https://highape.com/{city_slug}",                  "pages": 1},
}
CITY_SLUG = {"allevents": {"bengaluru": "bengaluru"}, "highape": {"bengaluru": "bangalore"}}


def main():
    ap = argparse.ArgumentParser(description="Generic schema.org JSON-LD event scraper.")
    ap.add_argument("site", choices=list(SITES), help="which registered site")
    ap.add_argument("--city", default="Bengaluru")
    ap.add_argument("--out", default=None)
    ap.add_argument("--enrich", action="store_true",
                    help="fetch each event's detail page for lineup (schema.org performer)")
    args = ap.parse_args()

    cfg = SITES[args.site]
    slug = CITY_SLUG.get(args.site, {}).get(args.city.lower(), args.city.lower())
    url_tmpl = cfg["url"].replace("{city_slug}", slug)
    out = args.out or f"{args.site}_{args.city.lower()}"

    print(f"[{args.site}] {args.city} :: {url_tmpl}", file=sys.stderr)
    events = scrape(url_tmpl, args.site, args.city, pages=cfg.get("pages", 1))
    if args.enrich:
        print(f"[{args.site}] enriching lineup from detail pages...", file=sys.stderr)
        got = enrich_lineup_from_detail(events)
        print(f"[{args.site}] +{got} lineups from detail", file=sys.stderr)
    n = write_out(events, out)
    print(f"[{args.site}] {n} events -> {out}.json / .csv", file=sys.stderr)


if __name__ == "__main__":
    main()
