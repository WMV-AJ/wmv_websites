"""Shared event schema — every site normalizes into this shape.

Keeping one schema means RA.co (GraphQL), SortMyScene (JSON API), BookMyShow,
and any future site all produce identical records for storage / dedup.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Event:
    source: str                       # "sortmyscene" | "bookmyshow" | "ra.co" ...
    source_id: str                    # site's own id (for dedup)
    title: str
    url: str
    city: Optional[str] = None
    venue: Optional[str] = None
    # Where the venue physically is. schema.org Event carries these inside
    # location.geo / location.address and every JSON-LD source publishes them,
    # but they were being dropped. They are the strongest signal for matching an
    # event to a venue we already know: a name alone cannot tell two branches of
    # one chain apart, a coordinate can.
    venue_lat: Optional[float] = None
    venue_lng: Optional[float] = None
    venue_address: Optional[str] = None
    start: Optional[str] = None        # ISO 8601 if known
    end: Optional[str] = None          # ISO 8601 if known
    date_label: Optional[str] = None   # human string as shown on site
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    price: Optional[str] = None        # raw, keep currency, e.g. "₹499" / "From INR 300"
    currency: Optional[str] = None
    categories: list[str] = field(default_factory=list)
    lineup: list[str] = field(default_factory=list)
    description: Optional[str] = None  # raw event blurb/text — unstructured, for AI to mine (artists etc.)
    image: Optional[str] = None
    raw: dict = field(default_factory=dict)   # untouched source record, for reprocessing

    def __post_init__(self) -> None:
        """Last resort: dig the coordinate out of the untouched source record.

        Each site hands us a different shape — District nests JSON-LD, SortMyScene
        puts lat/lng at the top level, Platinumlist hides it in a Google Maps
        link. Rather than four hand-written extractors that rot when a site
        changes, walk  for anything coordinate-shaped. A scraper that sets
        the fields explicitly always wins; this only fills what is still empty.

        Worth the trouble because the coordinate is the single strongest signal
        for matching an event to a venue we already know — without it, a name
        alone cannot tell two branches of one chain apart.
        """
        if self.venue_lat is None or self.venue_lng is None:
            lat, lng = _find_coords(self.raw)
            if self.venue_lat is None:
                self.venue_lat = lat
            if self.venue_lng is None:
                self.venue_lng = lng
        if not self.venue_address:
            self.venue_address = _find_address(self.raw)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Coordinate / address recovery from an arbitrary source payload.
# ---------------------------------------------------------------------------

_LAT_KEYS = ("latitude", "lat", "venue_lat", "venuelatitude", "y")
_LNG_KEYS = ("longitude", "lng", "lon", "long", "venue_lng", "venuelongitude", "x")
_ADDR_KEYS = ("streetaddress", "venue_address", "address_line", "full_address", "address")


def _num(v):
    """Coordinates arrive as floats or as strings. 0 is not a place, it is a
    geocoder that gave up, and out-of-range values are noise."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f == 0 or not (-180 <= f <= 180) else f


def _find_coords(node, _depth: int = 0):
    """Walk a nested dict/list for a latitude+longitude PAIR.

    Only accepts the two together and from the same object — a stray "lat" with
    no partner is more likely to be a map centre or a user's location than the
    venue's, and District's own request headers carry exactly that.
    """
    if _depth > 6 or node is None:
        return None, None
    if isinstance(node, dict):
        lower = {str(k).lower(): v for k, v in node.items()}
        lat = next((_num(lower[k]) for k in _LAT_KEYS if k in lower and _num(lower[k]) is not None), None)
        lng = next((_num(lower[k]) for k in _LNG_KEYS if k in lower and _num(lower[k]) is not None), None)
        if lat is not None and lng is not None and abs(lat) <= 90:
            return lat, lng
        for v in node.values():
            got = _find_coords(v, _depth + 1)
            if got[0] is not None:
                return got
    elif isinstance(node, list):
        for v in node[:20]:
            got = _find_coords(v, _depth + 1)
            if got[0] is not None:
                return got
    return None, None


def _find_address(node, _depth: int = 0):
    """A postal address, assembled from parts when the source splits them.
    The postcode is the piece that actually separates two branches of a chain."""
    if _depth > 6 or node is None:
        return None
    if isinstance(node, dict):
        lower = {str(k).lower(): v for k, v in node.items()}
        if "streetaddress" in lower:
            parts = [lower.get(k) for k in
                     ("streetaddress", "addresslocality", "addressregion", "postalcode")]
            joined = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
            if joined:
                return joined
        for k in _ADDR_KEYS:
            v = lower.get(k)
            if isinstance(v, str) and len(v.strip()) > 8:
                return v.strip()
        for v in node.values():
            got = _find_address(v, _depth + 1)
            if got:
                return got
    elif isinstance(node, list):
        for v in node[:20]:
            got = _find_address(v, _depth + 1)
            if got:
                return got
    return None
