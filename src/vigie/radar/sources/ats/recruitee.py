"""Recruitee : ``https://{token}.recruitee.com/api/offers/``.

Structure vérifiée sur une vraie réponse (20/11/2026) : ``{"offers": [...]}``, ``title``,
``description`` et ``requirements`` en HTML, ``careers_url``, ``city``, ``country_code``,
``location``, ``locations[]`` (chacun avec ``country_code``), ``remote``/``hybrid``/``on_site``,
``employment_type_code`` (observé : fulltime_permanent, fulltime_fixed_term), ``published_at`` au
format ``2026-11-18 16:23:32 UTC``, ``status``, ``salary`` ({min, max, period, currency}, vides sur
toutes les offres observées : la conversion de période n'a donc jamais été vue sur des données).
"""

from datetime import datetime
from typing import Any

from vigie.radar.http import SourceHttpClient
from vigie.radar.models import ContractType, Offer, RemoteMode
from vigie.radar.normalize import html_to_text, parse_location
from vigie.radar.sources.ats.common import contract_or_title, join_text, parse_utc_text
from vigie.radar.sources.ats.location import Place

URL = "https://{token}.recruitee.com/api/offers/"


def _contract(code: str) -> ContractType | None:
    code = code.lower()
    if "fixed_term" in code:
        return ContractType.CDD
    if "permanent" in code:
        return ContractType.CDI
    if "intern" in code:
        return ContractType.STAGE
    if "freelance" in code or "contractor" in code:
        return ContractType.FREELANCE
    return None


def _salary(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    salary = raw.get("salary") or {}
    low, high = salary.get("min"), salary.get("max")
    if (low is None and high is None) or str(salary.get("currency") or "").upper() != "EUR":
        return None, None
    period = str(salary.get("period") or "").lower()
    factor = {"year": 1.0, "yearly": 1.0, "month": 12.0, "monthly": 12.0}.get(period)
    if factor is None:
        return None, None
    return (
        float(low) * factor if low is not None else None,
        float(high) * factor if high is not None else None,
    )


class RecruiteeAdapter:
    platform = "recruitee"

    def fetch_jobs(self, client: SourceHttpClient, token: str) -> list[dict[str, Any]]:
        response = client.request("GET", URL.format(token=token))
        offers = response.json().get("offers") or []
        return [o for o in offers if o.get("status", "published") == "published"]

    def place(self, raw: dict[str, Any]) -> Place:
        locations = raw.get("locations") or []
        countries = [raw.get("country_code"), *(loc.get("country_code") for loc in locations)]
        return Place(
            text=str(raw.get("location") or ""),
            countries=tuple(dict.fromkeys(str(c) for c in countries if c)),
            remote=bool(raw.get("remote")),
        )

    def to_offer(
        self,
        raw: dict[str, Any],
        company: str,
        token: str,
        fetched_at: datetime,
        client: SourceHttpClient,
    ) -> Offer:
        label, postal, department = parse_location(raw.get("location"), raw.get("postal_code"))
        extra: dict[str, Any] = {}
        if raw.get("remote"):
            extra["remote"] = RemoteMode.FULL
        elif raw.get("hybrid"):
            extra["remote"] = RemoteMode.PARTIAL
        elif raw.get("on_site"):
            extra["remote"] = RemoteMode.NONE
        salary_min, salary_max = _salary(raw)
        return Offer(
            source=f"ats:{self.platform}",
            source_id=str(raw["id"]),
            title=raw["title"],
            company=company,
            location_label=label,
            postal_code=postal,
            department=department,
            contract_type=contract_or_title(
                _contract(str(raw.get("employment_type_code") or "")), raw["title"]
            ),
            salary_min=salary_min,
            salary_max=salary_max,
            description=join_text(
                html_to_text(str(raw.get("description") or "")),
                html_to_text(str(raw.get("requirements") or "")),
            ),
            url=raw["careers_url"],
            published_at=parse_utc_text(raw.get("published_at") or raw["created_at"]),
            fetched_at=fetched_at,
            raw=raw,
            **extra,
        )
