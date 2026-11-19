"""Interface commune des sources d'offres."""

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from vigie.radar.models import Offer, SearchQuery


class Source(Protocol):
    name: str

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]: ...
