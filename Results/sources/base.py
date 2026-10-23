"""Source ABC and shared dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class ResultPage:
    source: str
    event: str
    year: int
    label: str           # 'primary' | 'Race 1' | 'Series Overall' | …
    url: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "event": self.event,
            "year": self.year,
            "label": self.label,
            "url": self.url,
        }


@dataclass
class ResultRow:
    page: ResultPage
    place: str
    place_int: Optional[int]
    boat_name: str
    sail_number: Optional[str]
    boat_type: Optional[str]
    skipper: Optional[str]
    yacht_club: Optional[str]
    rating: Optional[str]
    elapsed: Optional[str]
    corrected: Optional[str]
    score: Optional[str]
    class_name: Optional[str]


class Source:
    name: str = ""

    def __init__(self, http_client=None):
        self.http_client = http_client

    def discover(self) -> Iterator[ResultPage]:
        raise NotImplementedError

    def parse(self, html: bytes, page: ResultPage) -> Iterator[ResultRow]:
        raise NotImplementedError
