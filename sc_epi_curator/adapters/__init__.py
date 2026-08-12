"""Source adapters used by the CellNote domain core."""

from .accession import AccessionSeedAdapter
from .crawl_run import CrawlRunAdapter
from .europe_pmc import EuropePmcAdapter
from .fixture import FixtureAdapter
from .ncbi import NcbiEntrezAdapter
from .web import WebPageAdapter

__all__ = [
    "AccessionSeedAdapter",
    "CrawlRunAdapter",
    "EuropePmcAdapter",
    "FixtureAdapter",
    "NcbiEntrezAdapter",
    "WebPageAdapter",
]
