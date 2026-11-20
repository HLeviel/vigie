"""Interface commune des sources d'offres."""

from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vigie.radar.models import Offer, SearchQuery


class Source(Protocol):
    name: str

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]: ...


if TYPE_CHECKING:
    from vigie.radar.store import Store


@runtime_checkable
class StoreAware(Protocol):
    """Source qui a besoin de l'état persistant (ex. emails déjà traités)."""

    def attach_store(self, store: "Store") -> None: ...
