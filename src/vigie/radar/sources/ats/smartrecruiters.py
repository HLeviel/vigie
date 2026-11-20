"""SmartRecruiters : ``https://api.smartrecruiters.com/v1/companies/{token}/postings``.

Structure vérifiée sur de vraies réponses (20/11/2026) : ``{"offset", "limit", "totalFound",
"content": [...]}``, filtre ``country=fr`` et pagination ``offset``/``limit`` (100 au plus) ;
``location.{city,region,country,postalCode,remote,hybrid,fullLocation}``, ``releasedDate`` ISO,
``typeOfEmployment.id`` (observé : permanent, contract). La liste n'a **pas** de description : elle
vient de ``/postings/{id}`` (``jobAd.sections``), appelée seulement pour les offres retenues, dans
la limite de ``MAX_DETAILS``. Un identifiant d'entreprise inconnu renvoie 200 avec une liste vide.
"""

from datetime import datetime
from typing import Any

from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.radar.models import ContractType, Offer, RemoteMode
from vigie.radar.normalize import html_to_text, parse_location
from vigie.radar.sources.ats.common import contract_or_title, join_text, parse_iso
from vigie.radar.sources.ats.location import Place

LIST_URL = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
DETAIL_URL = LIST_URL + "/{id}"
PUBLIC_URL = "https://jobs.smartrecruiters.com/{token}/{id}"
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_DETAILS = 50

_CONTRACT = {"permanent": ContractType.CDI, "contract": ContractType.CDD}


class SmartRecruitersAdapter:
    platform = "smartrecruiters"

    def __init__(self) -> None:
        self._details_fetched = 0

    def fetch_jobs(self, client: SourceHttpClient, token: str) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            response = client.request(
                "GET",
                LIST_URL.format(token=token),
                params={"country": "fr", "limit": str(PAGE_SIZE), "offset": str(page * PAGE_SIZE)},
            )
            body = response.json()
            content = body.get("content") or []
            jobs.extend(content)
            if not content or (page + 1) * PAGE_SIZE >= int(body.get("totalFound") or 0):
                break
        return jobs

    def place(self, raw: dict[str, Any]) -> Place:
        location = raw.get("location") or {}
        country = location.get("country")
        return Place(
            text=str(location.get("fullLocation") or ""),
            countries=(str(country),) if country else (),
            remote=bool(location.get("remote")),
        )

    def _description(
        self, client: SourceHttpClient, token: str, raw: dict[str, Any]
    ) -> tuple[str, str]:
        """(description, URL publique). Vide si le détail n'est pas récupérable."""
        default_url = PUBLIC_URL.format(token=token, id=raw["id"])
        if self._details_fetched >= MAX_DETAILS:
            return "", default_url
        self._details_fetched += 1
        try:
            detail = client.request("GET", DETAIL_URL.format(token=token, id=raw["id"])).json()
        except SourceHttpError:
            return "", default_url
        sections = ((detail.get("jobAd") or {}).get("sections")) or {}
        text = join_text(
            *(
                join_text(str(s.get("title") or ""), html_to_text(str(s.get("text") or "")))
                for s in sections.values()
                if isinstance(s, dict)
            )
        )
        return text, str(detail.get("postingUrl") or default_url)

    def to_offer(
        self,
        raw: dict[str, Any],
        company: str,
        token: str,
        fetched_at: datetime,
        client: SourceHttpClient,
    ) -> Offer:
        location = raw.get("location") or {}
        label, postal, department = parse_location(
            ", ".join(p for p in (location.get("city"), location.get("region")) if p) or None,
            location.get("postalCode"),
        )
        extra: dict[str, Any] = {}
        if location.get("remote"):
            extra["remote"] = RemoteMode.FULL
        elif location.get("hybrid"):
            extra["remote"] = RemoteMode.PARTIAL
        description, url = self._description(client, token, raw)
        employment = _CONTRACT.get(str((raw.get("typeOfEmployment") or {}).get("id") or ""))
        return Offer(
            source=f"ats:{self.platform}",
            source_id=str(raw["id"]),
            title=raw["name"],
            company=company,
            location_label=label,
            postal_code=postal,
            department=department,
            contract_type=contract_or_title(employment, raw["name"]),
            description=description,
            url=url,
            published_at=parse_iso(raw["releasedDate"]),
            fetched_at=fetched_at,
            raw=raw,
            **extra,
        )
