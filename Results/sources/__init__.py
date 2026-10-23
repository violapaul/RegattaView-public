"""Source registry. Add new crawlers here."""

from .base import Source, ResultPage, ResultRow
from .styc import STYCSource
from .cyc_archive import CYCArchiveSource
from .cyc_main import CYCMainSource
from .cyc_clubspot import CYCClubspotSource
from .syc import SYCSource

ALL_SOURCES: dict[str, type[Source]] = {
    STYCSource.name: STYCSource,
    CYCArchiveSource.name: CYCArchiveSource,
    CYCMainSource.name: CYCMainSource,
    CYCClubspotSource.name: CYCClubspotSource,
    SYCSource.name: SYCSource,
}


def get_source(name: str) -> Source:
    cls = ALL_SOURCES.get(name)
    if cls is None:
        raise KeyError(f"unknown source {name!r}; have {sorted(ALL_SOURCES)}")
    return cls()
