"""CYC Seattle ClubSpot results — Phase 2+ stub."""

from typing import Iterator
from .base import ResultPage, ResultRow, Source


class CYCClubspotSource(Source):
    name = "cyc-clubspot"

    def discover(self) -> Iterator[ResultPage]:
        raise NotImplementedError(
            f"{self.name} not implemented yet — add it in a later phase."
        )

    def parse(self, html: bytes, page: ResultPage) -> Iterator[ResultRow]:
        raise NotImplementedError(
            f"{self.name} not implemented yet — add it in a later phase."
        )
