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

    def to_dict(self) -> dict:
        return asdict(self)
