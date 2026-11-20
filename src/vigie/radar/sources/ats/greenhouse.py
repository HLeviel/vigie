"""Greenhouse : ``https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``.

Structure vérifiée sur une vraie réponse (20/11/2026) : ``{"jobs": [...]}``, ``content`` en HTML
échappé en entités, dates ISO avec fuseau, lieu dans ``location.name``. Pas de champ de contrat,
de salaire ni de télétravail.
"""

from datetime import datetime
from typing import Any

from vigie.radar.http import SourceHttpClient
from vigie.radar.models import ContractType, Offer
from vigie.radar.sources.ats.common import contract_or_title, parse_iso, unescape_html_to_text
from vigie.radar.sources.ats.location import Place

URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class GreenhouseAdapter:
    platform = "greenhouse"

    def fetch_jobs(self, client: SourceHttpClient, token: str) -> list[dict[str, Any]]:
        response = client.request("GET", URL.format(token=token), params={"content": "true"})
        return list(response.json().get("jobs") or [])

    def place(self, raw: dict[str, Any]) -> Place:
        return Place(text=str((raw.get("location") or {}).get("name") or ""))

    def to_offer(
        self,
        raw: dict[str, Any],
        company: str,
        token: str,
        fetched_at: datetime,
        client: SourceHttpClient,
    ) -> Offer:
        return Offer(
            source=f"ats:{self.platform}",
            source_id=str(raw["id"]),
            title=raw["title"],
            company=company,
            location_label=(raw.get("location") or {}).get("name"),
            contract_type=contract_or_title(None, raw["title"]) or ContractType.AUTRE,
            description=unescape_html_to_text(str(raw.get("content") or "")),
            url=raw["absolute_url"],
            published_at=parse_iso(raw.get("first_published") or raw["updated_at"]),
            fetched_at=fetched_at,
            raw=raw,
        )
