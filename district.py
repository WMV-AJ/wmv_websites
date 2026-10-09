"""District (by Zomato, ex-Paytm Insider) scraper.

District's SEO pages are fully client-rendered (no JSON-LD), so we hit its
discovery API directly. Two wrinkles solved here:

  1. Akamai bot cookies (ak_bmsc / bm_sv) gate the data — a cookieless call
     returns an empty skeleton. We bootstrap them by GETting the category page
     first in the same curl_cffi session, then reuse the session for the POST.
  2. The endpoint is proxied through the site origin, not the api host:
        POST https://www.district.in/gw/web/get_discovery_results
     headers: platform: district_web, x-app-type: ed_web, x-guest-token: 1212,
              x-city-id, x-place-id (a Google Place id), gps headers
     body:  {"location":{...}, "entity_data":{"entity_id":<cat>,"entity_type":"events"},
             "request_type":"initial"|"load_more", "postback_params":<from prev resp>}

Events live at EDSResponse.rails[].items[].ItemDetails.EventData.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright

from schema import Event

DISCOVERY = "https://www.district.in/gw/web/get_discovery_results"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IST = timezone(timedelta(hours=5, minutes=30))

# Bengaluru location context (a real Google Place id the site accepts).
BENGALURU = {
    "city_id": 4, "city_name": "Bengaluru", "pcity_key": "bengaluru",
    "place_id": "ChIJc_MWwGMUrjsRnYrKXvID-Ic", "lat": 12.9292, "lng": 77.6380,
}
CATEGORIES = ["music", "nightlife", "comedy", "sports", "performances", "food-and-drink"]
GET_BY_SLUG = "https://www.district.in/gw/consumer/events/v1/event/getBySlug/"


def _extract_lineup(detail_json: dict) -> list[str]:
    """District event detail -> artist names from data.lineupsV2[].artists[].tag_id.name"""
    data = detail_json.get("data", detail_json) or {}
    names = []
    for grp in data.get("lineupsV2", []) or []:
        for a in grp.get("artists", []) or []:
            tag = a.get("tag_id") or {}
            nm = tag.get("name") or a.get("name")
            if nm and nm not in names:
                names.append(nm)
    return names


def _headers(loc):
    return {
        "content-type": "application/json",
        "origin": "https://www.district.in",
        "platform": "district_web",
        "x-app-type": "ed_web", "x-app-version": "11.11.11",
        "x-city-id": str(loc["city_id"]), "x-city-name": loc["city_name"],
        "x-country-id": "1", "x-guest-token": "1212",
        "x-pcity-id": str(loc["city_id"]), "x-pcity-key": loc["pcity_key"],
        "x-place-id": loc["place_id"], "x-place-type": "GOOGLE_PLACE",
        "x-screen-type": "desktop",
        "x-gps-lat": str(loc["lat"]), "x-gps-lng": str(loc["lng"]),
        "x-user-lat": str(loc["lat"]), "x-user-lng": str(loc["lng"]),
    }


def _location_body(loc):
    return {"city_id": loc["city_id"], "user_lng": loc["lng"], "user_lat": loc["lat"],
            "gps_lat": loc["lat"], "gps_lng": loc["lng"]}


def _iter_event_items(resp: dict):
    eds = resp.get("EDSResponse", {}) or {}
    for rail in eds.get("rails", []) or []:
        for item in rail.get("items", []) or []:
            det = item.get("ItemDetails") or {}
            ed = det.get("EventData") if isinstance(det, dict) else None
            if ed and ed.get("name"):
                yield item, ed


def normalise(item: dict, ed: dict) -> Event:
    epoch = ed.get("start_time_epoch")
    end_epoch = ed.get("end_time_epoch")
    start_iso = datetime.fromtimestamp(epoch, IST).isoformat() if epoch else None
    end_iso = datetime.fromtimestamp(end_epoch, IST).isoformat() if end_epoch else None
    slug = ed.get("event_slug", "")
    url = f"https://www.district.in/events/{slug}-buy-tickets" if slug else ""
    date_label, start_time = None, None
    dv2 = ed.get("date_string_v2") or ed.get("date_string")
    if dv2 and "," in dv2:                      # "Sun, 09 Aug, 7:00 PM"
        *date_parts, last = [p.strip() for p in dv2.split(",")]
        if any(x in last for x in ("AM", "PM", ":")):
            start_time = last
            date_label = ", ".join(date_parts)
        else:
            date_label = dv2
    else:
        date_label = dv2
    return Event(
        source="district",
        source_id=ed.get("event_id") or item.get("entity_id") or slug,
        title=ed.get("name", "").strip(),
        url=url,
        city=ed.get("city"),
        venue=ed.get("venue_name"),
        start=start_iso,
        end=end_iso,
        date_label=date_label,
        start_time=start_time,
        price=ed.get("price_string") or item.get("price_string"),
        currency="INR",
        categories=[t for t in (ed.get("tags") or []) if t],
        lineup=[],
        image=ed.get("image"),
        raw={"item": item, "event": ed},
    )


def scrape(categories=CATEGORIES, loc=BENGALURU, only_city=None, scrolls=8, headless=True, enrich_lineup=True):
    """Let the real browser do the Akamai dance, and INTERCEPT the discovery API
    responses it fires while we scroll. Bulletproof — we reconstruct nothing,
    just harvest the JSON the page already fetches."""
    events, seen = [], set()

    def harvest(resp_json):
        n = 0
        for item, ed in _iter_event_items(resp_json):
            ev = normalise(item, ed)
            if not ev.source_id or ev.source_id in seen:
                continue
            if only_city and (ev.city or "").lower() != only_city.lower():
                continue
            seen.add(ev.source_id)
            events.append(ev)
            n += 1
        return n

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=UA, locale="en-US",
                                   viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        def on_response(r):
            if "get_discovery_results" in r.url:
                try:
                    harvest(r.json())
                except Exception:
                    pass
        page.on("response", on_response)

        for cat in categories:
            before = len(events)
            url = f"https://www.district.in/events/{cat}-in-bengaluru-book-tickets"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"  [{cat}] goto failed: {e}", file=sys.stderr)
                continue
            page.wait_for_timeout(4000)               # Akamai sensor + first discovery calls
            stagnant = 0
            for _ in range(scrolls):                  # scroll to trigger lazy load_more
                pre = len(events)
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(1800)
                stagnant = stagnant + 1 if len(events) == pre else 0
                if stagnant >= 3:                     # nothing new after 3 scrolls -> done
                    break
            print(f"  [{cat}] +{len(events) - before} events (total {len(events)})", file=sys.stderr)

        # lineup enrichment: hit getBySlug for each event through the live session
        if enrich_lineup:
            print(f"  enriching lineup for {len(events)} events...", file=sys.stderr)
            dh = _headers(loc)
            for i, ev in enumerate(events, 1):
                slug = (ev.raw.get("event") or {}).get("event_slug")
                if not slug:
                    continue
                dh["referer"] = f"https://www.district.in/events/{slug}-buy-tickets"
                try:
                    r = ctx.request.get(GET_BY_SLUG + slug, headers=dh, timeout=20000)
                    if r.ok:
                        detail = r.json()
                        ev.lineup = _extract_lineup(detail)
                        data = detail.get("data", detail) or {}
                        desc = data.get("description") or data.get("meta_description")
                        if isinstance(desc, str) and desc.strip():
                            ev.description = re.sub(r"<[^>]+>", " ", desc).strip()[:2000]
                except Exception:
                    pass
                if i % 40 == 0:
                    print(f"    lineup {i}/{len(events)}", file=sys.stderr)
        browser.close()
    return events


def main():
    ap = argparse.ArgumentParser(description="Scrape District (Zomato) events via its discovery API.")
    ap.add_argument("--categories", default=",".join(CATEGORIES),
                    help="comma list: music,nightlife,comedy,sports,performances,food-and-drink")
    ap.add_argument("--only-city", default="Bengaluru",
                    help="keep only events whose EventData.city matches (blank = keep all/national)")
    ap.add_argument("--out", default="district_bengaluru")
    args = ap.parse_args()

    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    only = args.only_city or None
    print(f"[district] cats={cats} only_city={only!r}", file=sys.stderr)
    events = scrape(categories=cats, only_city=only)
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
    print(f"[district] {len(rows)} events -> {args.out}.json / .csv", file=sys.stderr)


if __name__ == "__main__":
    main()
