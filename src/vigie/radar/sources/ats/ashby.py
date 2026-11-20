"""Ashby : ``https://api.ashbyhq.com/posting-api/job-board/{token}``.

Structure vérifiée sur une vraie réponse (20/11/2026) : ``{"jobs": [...]}``, ``location`` en
texte (« Remote - European Union », « Remote - US »…), ``secondaryLocations``, ``isRemote``,
``workplaceType`` (observé : Remote), ``employmentType`` (observé : FullTime), ``publishedAt`` ISO,
``isListed``, ``descriptionPlain``, ``jobUrl``. Les salaires (``includeCompensation``) ne sont pas
repris.
"""

from datetime import datetime
from typing import Any

from vigie.radar.http import SourceHttpClient
from vigie.radar.models import ContractType, Offer, RemoteMode
from vigie.radar.sources.ats.common import contract_or_title, parse_iso
from vigie.radar.sources.ats.location import Place

URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"

_WORKPLACE = {"remote": RemoteMode.FULL, "hybrid": RemoteMode.PARTIAL, "onsite": RemoteMode.NONE}


class AshbyAdapter:
    platform = "ashby"

    def fetch_jobs(self, client: SourceHttpClient, token: str) -> list[dict[str, Any]]:
        response = client.request("GET", URL.format(token=token))
        jobs = response.json().get("jobs") or []
        return [job for job in jobs if job.get("isListed", True)]

    def place(self, raw: dict[str, Any]) -> Place:
        texts = [str(raw.get("location") or "")]
        texts += [str(s.get("location") or "") for s in raw.get("secondaryLocations") or []]
        return Place(
            text=" / ".join(t for t in texts if t),
            remote=bool(raw.get("isRemote")) or str(raw.get("workplaceType")).lower() == "remote",
        )

    def to_offer(
        self,
        raw: dict[str, Any],
        company: str,
        token: str,
        fetched_at: datetime,
        client: SourceHttpClient,
    ) -> Offer:
        extra: dict[str, Any] = {}
        workplace = _WORKPLACE.get(str(raw.get("workplaceType") or "").lower())
        if workplace is not None:
            extra["remote"] = workplace
        employment = ContractType.STAGE if raw.get("employmentType") == "Intern" else None
        return Offer(
            source=f"ats:{self.platform}",
            source_id=str(raw["id"]),
            title=raw["title"],
            company=company,
            location_label=raw.get("location"),
            contract_type=contract_or_title(employment, raw["title"]),
            description=str(raw.get("descriptionPlain") or ""),
            url=raw["jobUrl"],
            published_at=parse_iso(raw["publishedAt"]),
            fetched_at=fetched_at,
            raw=raw,
            **extra,
        )
