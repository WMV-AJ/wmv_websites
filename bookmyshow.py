"""BookMyShow scraper — Tier 2 (Cloudflare-fronted, but no browser needed).

BMS blocks datacenter TLS fingerprints at the Cloudflare edge (plain requests
=> 403). The cheap workaround is `curl_cffi`, which impersonates a real Chrome
TLS/JA3 fingerprint and sails past the passive challenge — no headless browser,
no residential proxy for this level of protection.

Discovered contract (reverse-engineered from window.__INITIAL_STATE__ + the
app-explore JS chunk, verified live):

    GET https://in.bookmyshow.com/api/explore/v1/discover/<slug>?<query>
      required headers: x-app-code: MOBAND2, x-platform: WEB, x-bms-id: guest
      required cookie:  rgn=|Code=<REGION>|      (Bengaluru => BANG)
      -> server-driven-UI JSON: { listings: [ { cards: [...] } ], scrollId, ... }

Event cards carry `ctaUrl` (booking link, embeds the ETxxxx event code) and a
`text.components` stack: [title, venue, sub-type, price].
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))   # BMS showtimes are local wall-clock

from curl_cffi import requests as cr

from schema import Event

API = "https://in.bookmyshow.com/api/explore/v1/discover/"
DETAIL = "https://in.bookmyshow.com/api/le/v3/bottomsheet/"   # per-event date/time/lineup
IMPERSONATE = "chrome124"

# "Sat, Jul 25, 2026 | 09:00 PM"  -> date part, time part
_DT_RE = re.compile(r"([A-Z][a-z]{2},\s*[A-Z][a-z]{2}\s*\d{1,2},\s*\d{4})\s*\|\s*(\d{1,2}:\d{2}\s*[AP]M)")

# City -> BMS region code. Extend as needed (codes are stable BMS internals).
REGIONS = {
    "bengaluru": "BANG", "bangalore": "BANG",
    "mumbai": "MUMBAI", "delhi": "NCR", "hyderabad": "HYD",
    "chennai": "CHEN", "pune": "PUNE", "kolkata": "KOLK",
}


def _headers(region_code: str) -> dict:
    return {
        "Accept": "application/json",
        "x-app-code": "MOBAND2",
        "x-platform": "WEB",
        "x-bms-id": "guest",
        "x-region-code": region_code,
        "x-subregion-code": region_code,
        "lang": "en",
        "Referer": "https://in.bookmyshow.com/",
    }


def _text_components(node) -> list[str]:
    """Flatten every {type:text,text:...} leaf under a card, in order."""
    out = []
    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "text" and isinstance(o.get("text"), str):
                out.append(o["text"].strip())
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(node)
    return out


def _is_event_card(card: dict) -> bool:
    url = card.get("ctaUrl") or ""
    return bool(re.search(r"/(events|plays|sports)/", url)) and "ET" in url.upper()


def _price_from(texts: list[str]):
    for t in texts:
        if "₹" in t or re.search(r"\bonwards\b", t, re.I) or re.match(r"^\s*(INR|Rs)\b", t, re.I):
            return t
    return None


def normalise(card: dict, city: str) -> Event:
    texts = _text_components(card)
    a = card.get("analytics", {}) or {}
    url = card.get("ctaUrl") or ""
    code_m = re.search(r"/(ET\d+)", url.upper())
    title = a.get("title") or (texts[0] if texts else "")
    venue = texts[1] if len(texts) > 1 else None
    price = _price_from(texts)
    image = card.get("image") or (card.get("metaImages") or [{}])[0].get("url") if card.get("metaImages") else card.get("image")
    return Event(
        source="bookmyshow",
        source_id=code_m.group(1) if code_m else (a.get("id") or url),
        title=title.strip(),
        url=url,
        city=city,
        venue=venue,
        date_label=None,          # per-show datetime lives in the /api/le/v3/bottomsheet/<code> detail call
        price=price,
        currency="INR",
        categories=[c for c in (a.get("category") or "").split("|") if c],
        lineup=[],
        image=card.get("image"),
        raw=card,
    )


def enrich(session, ev: Event, delay: float = 0.3) -> Event:
    """Second, lazy pass: pull real date/time + lineup from the event detail sheet."""
    code = ev.source_id
    if not (isinstance(code, str) and code.upper().startswith("ET")):
        return ev
    try:
        r = session.get(f"{DETAIL}{code}", params={"isPromoted": "true"}, timeout=30)
        if r.status_code != 200:
            return ev
        data = r.json().get("data", {}) or {}
    except Exception as e:
        print(f"    enrich {code}: {e}", file=sys.stderr)
        return ev

    # 1) date + time from the "Sat, Jul 25, 2026 | 09:00 PM" text leaf
    for t in _text_components(data):
        m = _DT_RE.search(t)
        if m:
            date_part, time_part = m.group(1).strip(), m.group(2).strip()
            ev.date_label = date_part
            ev.start_time = time_part
            try:
                dt = datetime.strptime(f"{date_part} {time_part}", "%a, %b %d, %Y %I:%M %p")
                ev.start = dt.replace(tzinfo=IST).isoformat()
            except ValueError:
                pass
            break

    # 2) lineup + language from meta.analytics
    a = (data.get("meta", {}) or {}).get("analytics", {}) or {}
    if a.get("label"):
        ev.lineup = [x.strip() for x in a["label"].split(",") if x.strip()]
    ev.raw["detail_meta"] = a

    # 3) raw description = longest text leaf (the synopsis; may name more artists for AI)
    texts = [t for t in _text_components(data) if len(t) > 60]
    if texts:
        ev.description = max(texts, key=len)[:2000]

    time.sleep(delay)
    return ev


def scrape(path_and_query: str, city: str, region_code: str, max_pages: int = 20, delay: float = 0.6):
    """path_and_query e.g. 'comedy-shows-bengaluru?daygroups=custom:20260725-20260725'"""
    session = cr.Session(impersonate=IMPERSONATE)
    session.headers.update(_headers(region_code))
    session.cookies.update({"rgn": f"|Code={region_code}|"})

    slug, _, query = path_and_query.partition("?")
    events, seen = [], set()
    scroll_id = None
    for page in range(max_pages):
        url = API + slug
        params = {}
        if query:
            params.update(dict(p.split("=", 1) for p in query.split("&") if "=" in p))
        if scroll_id:
            params["scrollId"] = scroll_id
        r = session.get(url, params=params, timeout=30)
        if r.status_code != 200:
            print(f"  page {page}: HTTP {r.status_code} {r.text[:120]}", file=sys.stderr)
            break
        data = r.json()
        new = 0
        for L in data.get("listings", []) or []:
            for card in L.get("cards", []) or []:
                if not _is_event_card(card):
                    continue
                ev = normalise(card, city)
                if ev.source_id in seen:
                    continue
                seen.add(ev.source_id)
                events.append(ev)
                new += 1
        scroll_id = data.get("scrollId")
        print(f"  page {page}: +{new} event cards (total {len(events)})", file=sys.stderr)
        if new == 0:
            break
        time.sleep(delay)
    return events, session


def main():
    ap = argparse.ArgumentParser(description="Scrape BookMyShow explore listings via its discover API (curl_cffi, no browser).")
    ap.add_argument("--path", default="comedy-shows-bengaluru?daygroups=custom:20260725-20260725",
                    help="explore slug + query, e.g. 'comedy-shows-bengaluru?daygroups=custom:20260725-20260725'")
    ap.add_argument("--city", default="Bengaluru")
    ap.add_argument("--out", default="bookmyshow_events")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip the per-event detail call (faster, but no date/time/lineup)")
    args = ap.parse_args()

    region = REGIONS.get(args.city.lower())
    if not region:
        sys.exit(f"Unknown city {args.city!r}. Known: {', '.join(sorted(set(REGIONS)))}. Add its BMS region code to REGIONS.")

    print(f"[bookmyshow] {args.city} ({region}) :: {args.path}", file=sys.stderr)
    events, session = scrape(args.path, args.city, region)

    if not args.no_enrich:
        print(f"[bookmyshow] enriching {len(events)} events with date/time/lineup...", file=sys.stderr)
        for i, ev in enumerate(events, 1):
            enrich(session, ev)
            if i % 10 == 0:
                print(f"    enriched {i}/{len(events)}", file=sys.stderr)

    rows = [e.to_dict() for e in events]

    with open(f"{args.out}.json", "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    if rows:
        cols = [c for c in rows[0] if c != "raw"]
        with open(f"{args.out}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("|".join(v) if isinstance(v, list) else v) for k, v in r.items() if k != "raw"})
    print(f"[bookmyshow] {len(rows)} events -> {args.out}.json / .csv", file=sys.stderr)


if __name__ == "__main__":
    main()
