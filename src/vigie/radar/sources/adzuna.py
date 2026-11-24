"""Source Adzuna (API de recherche d'offres, marché français).

La forme des réponses a été vérifiée sur la documentation officielle et sur une vraie réponse
(recherche ``what`` + ``where=Lyon``). Restent non vérifiés : ``where`` avec un nom de
département, ``distance``, ``max_days_old`` et les valeurs de ``contract_type`` autres que
``permanent`` : voir ``docs/specs/radar.md`` (issue #20 pour la pagination).
"""

import logging
import math
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from vigie.radar.geo import department_code_from_name, department_name
from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.radar.models import ContractType, Offer, SearchQuery
from vigie.radar.normalize import contract_from_title, html_to_text
from vigie.settings import MissingSettingsError, Settings

logger = logging.getLogger(__name__)

NAME = "adzuna"
SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/fr/search/{page}"
PER_PAGE = 50
MAX_PAGES = 20  # 1 000 offres au plus par (mot-clé, zone)

# contract_type Adzuna -> ContractType (« permanent » et « contract » sont les valeurs de la doc)
CONTRACT_CODES: dict[str, ContractType] = {
    "permanent": ContractType.CDI,
    "contract": ContractType.CDD,
}


def contract_from_raw(raw: dict[str, Any]) -> ContractType:
    """Un titre explicite (alternance, stage, intérim, freelance) l'emporte sur le code Adzuna."""
    from_title = contract_from_title(str(raw.get("title") or ""))
    if from_title:
        return ContractType(from_title)
    return CONTRACT_CODES.get(str(raw.get("contract_type") or "").lower(), ContractType.AUTRE)


def _department(area: list[Any]) -> str | None:
    for name in area[1:]:
        code = department_code_from_name(str(name))
        if code:
            return code
    return None


def _is_predicted(raw: dict[str, Any]) -> bool:
    return str(raw.get("salary_is_predicted", "0")).strip().lower() in {"1", "true"}


def offer_from_raw(raw: dict[str, Any], fetched_at: datetime) -> Offer:
    """Convertit une offre brute en ``Offer`` (lève ``KeyError`` ou ``ValidationError``)."""
    location = raw.get("location") or {}
    predicted = _is_predicted(raw)
    return Offer(
        source=NAME,
        source_id=str(raw["id"]),
        title=raw["title"],
        company=(raw.get("company") or {}).get("display_name"),
        location_label=location.get("display_name"),
        department=_department(list(location.get("area") or [])),
        contract_type=contract_from_raw(raw),
        # Un salaire estimé par Adzuna n'est pas un salaire annoncé : on ne le reprend pas.
        salary_min=None if predicted else raw.get("salary_min"),
        salary_max=None if predicted else raw.get("salary_max"),
        description=html_to_text(str(raw.get("description") or "")),
        url=raw["redirect_url"],
        published_at=datetime.fromisoformat(raw["created"]),
        fetched_at=fetched_at,
        raw=raw,
    )


class AdzunaSource:
    name = NAME

    def __init__(
        self,
        settings: Settings,
        *,
        client: SourceHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        settings.require(["adzuna"])
        self._settings = settings
        self._client = client or SourceHttpClient(NAME)
        self._now = now
        self.skipped = 0

    def _auth(self) -> dict[str, str]:
        app_id, app_key = self._settings.adzuna_app_id, self._settings.adzuna_app_key
        if app_id is None or app_key is None:
            raise MissingSettingsError(self._settings.missing(["adzuna"]))
        return {"app_id": app_id.get_secret_value(), "app_key": app_key.get_secret_value()}

    def _pages(self, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        for page in range(1, MAX_PAGES + 1):
            response = self._client.request(
                "GET",
                SEARCH_URL.format(page=page),
                params={
                    **self._auth(),
                    **params,
                    "results_per_page": str(PER_PAGE),
                    "content-type": "application/json",
                },
                # Sans en-tête Accept, l'API répond en JSONP par défaut.
                headers={"Accept": "application/json"},
            )
            try:
                body = response.json()
            except ValueError:
                raise SourceHttpError("adzuna : réponse non JSON") from None
            results = body.get("results") or []
            yield from results
            count = body.get("count")
            if len(results) < PER_PAGE or (isinstance(count, int) and page * PER_PAGE >= count):
                return
        logger.warning("Limite de %d pages atteinte : résultats tronqués.", MAX_PAGES)

    @staticmethod
    def _zones(query: SearchQuery) -> list[tuple[dict[str, str], str | None]]:
        """Paramètres de zone et, pour un département, son code (filtre côté client)."""
        if query.commune and query.distance_km:
            return [({"where": query.commune, "distance": str(query.distance_km)}, None)]
        if query.departments:
            zones: list[tuple[dict[str, str], str | None]] = []
            for code in query.departments:
                name = department_name(code)
                if name is None:
                    # Message sans le code : les critères ne doivent pas apparaître dans les logs.
                    raise ValueError("département inconnu dans une recherche")
                zones.append(({"where": name}, code.upper()))
            return zones
        return [({}, None)]

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]:
        now = self._now()
        low = max(since, now - timedelta(days=query.max_age_days))
        max_days_old = max(1, math.ceil((now - low) / timedelta(days=1)))
        seen: set[str] = set()
        for keyword in query.keywords:
            for zone, code in self._zones(query):
                params = {"what": keyword, "max_days_old": str(max_days_old), **zone}
                for raw in self._pages(params):
                    raw_id = str(raw.get("id", ""))
                    if not raw_id or raw_id in seen:
                        continue
                    try:
                        offer = offer_from_raw(raw, now)
                    except (KeyError, ValueError, ValidationError):
                        seen.add(raw_id)
                        self.skipped += 1
                        continue
                    # `where` est une recherche plein texte : « Loire » ramène aussi la
                    # Haute-Loire. On écarte les offres dont le département connu diffère.
                    if code and offer.department and offer.department != code:
                        continue
                    seen.add(raw_id)
                    yield offer
        if self.skipped:
            logger.warning("%d offre(s) Adzuna ignorée(s) (format inattendu).", self.skipped)
