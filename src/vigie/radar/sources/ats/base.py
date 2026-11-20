"""Interface d'un adaptateur de plateforme ATS."""

from datetime import datetime
from typing import Any, Protocol

from vigie.radar.http import SourceHttpClient
from vigie.radar.models import Offer
from vigie.radar.sources.ats.location import Place


class AtsAdapter(Protocol):
    platform: str

    def fetch_jobs(self, client: SourceHttpClient, token: str) -> list[dict[str, Any]]:
        """Offres brutes de l'entreprise. Lève ``SourceHttpError`` (404 : token invalide)."""
        ...

    def place(self, raw: dict[str, Any]) -> Place: ...

    def to_offer(
        self,
        raw: dict[str, Any],
        company: str,
        token: str,
        fetched_at: datetime,
        client: SourceHttpClient,
    ) -> Offer: ...
