"""Lever : ``https://api.lever.co/v0/postings/{token}?mode=json``.

Structure vérifiée sur une vraie réponse (20/11/2026) : liste d'offres, ``createdAt`` en
millisecondes, ``categories.{location,commitment,team,allLocations}``, ``country`` (ISO-2),
``workplaceType`` (``remote``, ``hybrid``, ``onsite``), ``descriptionPlain``, ``lists``,
``additionalPlain``. ``commitment`` observé : Full-time, Internship, Scholarship, Fixed-Term.
"""

from datetime import datetime
from typing import Any

from vigie.radar.http import SourceHttpClient
from vigie.radar.models import ContractType, Offer, RemoteMode
from vigie.radar.normalize import html_to_text
from vigie.radar.sources.ats.common import contract_or_title, join_text, parse_epoch_ms
from vigie.radar.sources.ats.location import Place

URL = "https://api.lever.co/v0/postings/{token}"

_COMMITMENT = {"internship": ContractType.STAGE, "fixed-term": ContractType.CDD}
_REMOTE = {"remote": RemoteMode.FULL, "hybrid": RemoteMode.PARTIAL, "onsite": RemoteMode.NONE}


class LeverAdapter:
    platform = "lever"

    def fetch_jobs(self, client: SourceHttpClient, token: str) -> list[dict[str, Any]]:
        response = client.request("GET", URL.format(token=token), params={"mode": "json"})
        data = response.json()
        return list(data) if isinstance(data, list) else []

    def place(self, raw: dict[str, Any]) -> Place:
        categories = raw.get("categories") or {}
        texts = [
            str(categories.get("location") or ""),
            *map(str, categories.get("allLocations") or []),
        ]
        country = raw.get("country")
        return Place(
            text=" / ".join(t for t in texts if t),
            countries=(str(country),) if country else (),
            remote=raw.get("workplaceType") == "remote",
        )

    def to_offer(
        self,
        raw: dict[str, Any],
        company: str,
        token: str,
        fetched_at: datetime,
        client: SourceHttpClient,
    ) -> Offer:
        categories = raw.get("categories") or {}
        sections = [
            join_text(str(item.get("text") or ""), html_to_text(str(item.get("content") or "")))
            for item in raw.get("lists") or []
        ]
        extra: dict[str, Any] = {}
        workplace = _REMOTE.get(str(raw.get("workplaceType") or ""))
        if workplace is not None:
            extra["remote"] = workplace
        commitment = _COMMITMENT.get(str(categories.get("commitment") or "").lower())
        return Offer(
            source=f"ats:{self.platform}",
            source_id=str(raw["id"]),
            title=raw["text"],
            company=company,
            location_label=categories.get("location"),
            contract_type=contract_or_title(commitment, raw["text"]),
            description=join_text(
                str(raw.get("descriptionPlain") or ""),
                *sections,
                str(raw.get("additionalPlain") or ""),
            ),
            url=raw["hostedUrl"],
            published_at=parse_epoch_ms(int(raw["createdAt"])),
            fetched_at=fetched_at,
            raw=raw,
            **extra,
        )
