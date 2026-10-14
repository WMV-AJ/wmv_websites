"""Platinumlist scraper (Middle East: Dubai, Abu Dhabi, Doha, Riyadh, Jeddah...).

Platinumlist is pure server-rendered HTML — no JSON-LD, no public API, no data
island. So we HTML-parse two levels:
  * listing (e.g. /calendar/today or a category): event cards -> title, price, url, id
  * detail page (per event): venue name + date (not present on the card)

City = subdomain: https://<city>.platinumlist.net  (dubai, abudhabi, doha, ...).
Prices are geo-localised by the request IP (shows INR from India, AED from UAE).
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
from datetime import datetime

from curl_cffi import requests as cr

from schema import Event
from sortmyscene import parse_lineup_from_title

IMPERSONATE = "chrome124"
_ANCHOR = re.compile(r'href="(https://[a-z]+\.platinumlist\.net/event-tickets/(\d+)/([a-z0-9-]+))"')
_TITLE = re.compile(r'title="([^"]{3,120})"')
_PRICE = re.compile(r'([\d,]+(?:\.\d+)?\s*(?:AED|INR|USD|SAR|QAR))')
_VENUE = re.compile(r'event-item__venue-name-text[^>]*>\s*([^<]{2,80})<', re.I)
_DATE = re.compile(r'\b(\d{1,2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?:\s\d{4})?)\b')
_DESC = re.compile(r'<meta[^>]+name="description"[^>]+content="([^"]{20,600})"', re.I)


def parse_listing(page_html: str, city: str) -> list[dict]:
    """Split the grid into cards on each event-tickets anchor; pull title + price + url."""
    anchors = list(_ANCHOR.finditer(page_html))
    cards, seen = [], set()
    for i, m in enumerate(anchors):
        url, eid, slug = m.group(1), m.group(2), m.group(3)
        if eid in seen:
            continue
        seen.add(eid)
        block = page_html[m.start(): anchors[i + 1].start() if i + 1 < len(anchors) else m.start() + 2500]
        title = _TITLE.search(block)
        price = _PRICE.search(block)
        cards.append({
            "id": eid, "slug": slug, "url": url,
            "title": html.unescape(title.group(1)).strip() if title else slug.replace("-", " ").title(),
            "price": price.group(1).strip() if price else None,
        })
    return cards


def enrich_detail(session, card: dict):
    """Fetch the event page for venue + date (absent from the listing card)."""
    try:
        r = session.get(card["url"], timeout=25)
        if r.status_code != 200:
            return
        h = r.text
        v = _VENUE.search(h)
        if v:
            card["venue"] = html.unescape(v.group(1)).strip()
        d = _DATE.search(h)
        if d:
            card["date"] = d.group(1).strip()
        desc = _DESC.search(h)
        if desc:
            card["description"] = html.unescape(desc.group(1)).strip()
        # price fallback if the listing card lacked one
        if not card.get("price"):
            p = _PRICE.search(h)
            if p:
                card["price"] = p.group(1).strip()
    except Exception:
        pass


_DAY_MONTH_YEAR = re.compile(r'(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})')
_DAY_MONTH = re.compile(r'(\d{1,2})\s+([A-Za-z]{3,9})\b')


def _iso_dates(label: str | None) -> tuple[str | None, str | None]:
    """'24 October 2026' -> ('2026-10-24', None);
    '24 October 2026 - 26 October 2026' -> ('2026-10-24', '2026-10-26').

    The detail page only shows a human date, and the importer keeps an event
    only when it can read an ISO date, so without this every Platinumlist
    event arrived dateless.
    """
    out = []
    for d, mon, y in _DAY_MONTH_YEAR.findall(label or ""):
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                out.append(datetime.strptime(f"{d} {mon} {y}", fmt).date().isoformat())
                break
            except ValueError:
                continue
    if not out:
        # "31 Dec" / "6 October" — no year. A date up to ~2 months back is a run
        # that started recently (left in this year; the importer drops past
        # dates); further back means it is next year's ("10 Jan" seen in Dec).
        today = datetime.now().date()
        for d, mon in _DAY_MONTH.findall(label or ""):
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    day = datetime.strptime(f"{d} {mon} {today.year}", fmt).date()
                except ValueError:
                    continue
                if (today - day).days > 60:
                    day = day.replace(year=today.year + 1)
                out.append(day.isoformat())
                break
    return (out[0] if out else None, out[1] if len(out) > 1 else None)


def normalise(card: dict, city: str) -> Event:
    price = card.get("price")
    cur = None
    if price:
        m = re.search(r'(AED|INR|USD|SAR|QAR)', price)
        cur = m.group(1) if m else None
    return Event(
        source="platinumlist",
        source_id=card["id"],
        title=card["title"],
        url=card["url"],
        city=city.title(),
        venue=card.get("venue"),
        start=_iso_dates(card.get("date"))[0],
        end=_iso_dates(card.get("date"))[1],
        date_label=card.get("date"),
        price=price,
        currency=cur,
        categories=[],
        lineup=parse_lineup_from_title(card["title"]),   # weak; real artists live in `description` for AI
        description=card.get("description"),
        raw=card,
    )


# Category listing paths (each paginated via ?page=N). Extend freely.
CATEGORY_PATHS = ["/concert", "/nightlife", "/comedy", "/theatre", "/festival",
                  "/events", "/art-events", "/classical-events", "/calendar/today"]


def _fetch_listing_paths(session, base, paths, max_pages=6, delay=0.4):
    """Walk each category with ?page=N until a page yields no new event ids."""
    cards, seen = [], set()
    for path in paths:
        for page in range(1, max_pages + 1):
            url = f"{base}{path}" + (f"?page={page}" if page > 1 else "")
            try:
                r = session.get(url, timeout=30)
            except Exception:
                break
            if r.status_code != 200:
                break
            page_cards = [c for c in parse_listing(r.text, "") if c["id"] not in seen]
            for c in page_cards:
                seen.add(c["id"])
                cards.append(c)
            if not page_cards:                 # no new events on this page -> category exhausted
                break
            time.sleep(delay)
    return cards


def scrape(city="dubai", paths=None, enrich=True, currency="AED", delay=0.35):
    base = f"https://{city}.platinumlist.net"
    session = cr.Session(impersonate=IMPERSONATE)
    session.cookies.update({"user_currency": currency})   # else price geo-localises to the request IP
    paths = paths or CATEGORY_PATHS
    cards = _fetch_listing_paths(session, base, paths, delay=delay)
    print(f"[platinumlist] {city}: {len(cards)} unique events across {len(paths)} categories", file=sys.stderr)
    if enrich:
        for i, c in enumerate(cards, 1):
            enrich_detail(session, c)
            if i % 25 == 0:
                print(f"    detail {i}/{len(cards)}", file=sys.stderr)
            time.sleep(delay)
    return [normalise(c, city) for c in cards]


def main():
    ap = argparse.ArgumentParser(description="Scrape Platinumlist (Middle East) events.")
    ap.add_argument("--city", default="dubai", help="subdomain city: dubai, abudhabi, doha, riyadh, jeddah")
    ap.add_argument("--paths", default=None,
                    help="comma list of listing paths (default: all categories). e.g. /concert,/nightlife")
    ap.add_argument("--no-enrich", action="store_true", help="skip detail fetch (no venue/date/description)")
    ap.add_argument("--currency", default="AED", help="user_currency cookie: AED, USD, SAR, QAR, INR...")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = args.out or f"platinumlist_{args.city}"
    paths = [p.strip() for p in args.paths.split(",")] if args.paths else None
    events = scrape(city=args.city, paths=paths, enrich=not args.no_enrich, currency=args.currency)
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
    print(f"[platinumlist] {len(rows)} events -> {out}.json / .csv", file=sys.stderr)


if __name__ == "__main__":
    main()
