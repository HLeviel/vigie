"""Source France Travail (API Offres d'emploi v2).

ATTENTION : écrite sans accès à l'API ni à sa documentation officielle (accès suspendu).
Le mapping, les codes de contrat, les formats de dates et le comportement de pagination
viennent de la connaissance du schéma et n'ont PAS été vérifiés sur une réponse réelle.
Voir ``docs/specs/radar.md`` et l'issue #19 : tout ce qui est « non vérifié » est à confirmer.
"""

import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.radar.models import ContractType, Offer, SearchQuery
from vigie.radar.normalize import html_to_text, parse_contract_type, parse_location, parse_salary
from vigie.settings import MissingSettingsError, Settings

logger = logging.getLogger(__name__)

NAME = "france_travail"
TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"  # noqa: S105
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
DETAIL_URL = "https://candidat.francetravail.fr/offres/recherche/detail/{id}"  # non vérifié

PAGE_SIZE = 150
# Bornes de la spec : début <= 1000, fin <= 1149. La dernière page chevauche la précédente ;
# les doublons sont éliminés par identifiant.
PAGE_RANGES: list[tuple[int, int]] = [
    (start, start + PAGE_SIZE - 1) for start in range(0, 1000, 150)
] + [(1000, 1149)]
TOKEN_MARGIN_S = 60.0
MIN_WINDOW = timedelta(hours=1)

# Codes typeContrat de l'API (non vérifié) -> ContractType
CONTRACT_CODES: dict[str, ContractType] = {
    "CDI": ContractType.CDI,
    "CDD": ContractType.CDD,
    "MIS": ContractType.INTERIM,
    "LIB": ContractType.FREELANCE,
    "FRA": ContractType.FREELANCE,
}
# ContractType -> code utilisable en filtre `typeContrat` (stage et alternance : pas de code)
CONTRACT_FILTER_CODES: dict[ContractType, str] = {
    ContractType.CDI: "CDI",
    ContractType.CDD: "CDD",
    ContractType.INTERIM: "MIS",
    ContractType.FREELANCE: "LIB",
}


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def contract_from_raw(raw: dict[str, Any]) -> ContractType:
    """Type de contrat : l'alternance (indicateur ou nature du contrat) l'emporte."""
    nature = str(raw.get("natureContrat") or "")
    if raw.get("alternance") is True or parse_contract_type(nature) == "ALTERNANCE":
        return ContractType.ALTERNANCE
    code = str(raw.get("typeContrat") or "").upper()
    if code in CONTRACT_CODES:
        return CONTRACT_CODES[code]
    return ContractType(parse_contract_type(str(raw.get("typeContratLibelle") or code)))


def offer_from_raw(raw: dict[str, Any], fetched_at: datetime) -> Offer:
    """Convertit une offre brute en ``Offer`` (lève ``KeyError`` ou ``ValidationError``)."""
    source_id = str(raw["id"])
    place = raw.get("lieuTravail") or {}
    label, postal, department = parse_location(place.get("libelle"), place.get("codePostal"))
    salary_label = (raw.get("salaire") or {}).get("libelle") or None
    salary_min, salary_max = parse_salary(salary_label)
    origin = raw.get("origineOffre") or {}
    return Offer(
        source=NAME,
        source_id=source_id,
        title=raw["intitule"],
        company=(raw.get("entreprise") or {}).get("nom"),
        location_label=label,
        postal_code=postal,
        department=department,
        contract_type=contract_from_raw(raw),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_label=salary_label,
        description=html_to_text(str(raw.get("description") or "")),
        url=origin.get("urlOrigine") or DETAIL_URL.format(id=source_id),
        published_at=datetime.fromisoformat(raw["dateCreation"]),
        fetched_at=fetched_at,
        raw=raw,
    )


class FranceTravailSource:
    name = NAME

    def __init__(
        self,
        settings: Settings,
        *,
        client: SourceHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        settings.require(["france_travail"])
        self._settings = settings
        self._client = client or SourceHttpClient(NAME)
        self._now = now
        self._monotonic = monotonic
        # Le token reste en mémoire : jamais écrit sur disque ni dans les logs.
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.skipped = 0

    # --- authentification ---------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token and self._monotonic() < self._token_expires_at:
            return self._token
        client_id, client_secret = self._settings.ft_client_id, self._settings.ft_client_secret
        if client_id is None or client_secret is None:
            raise MissingSettingsError(self._settings.missing(["france_travail"]))
        response = self._client.request(
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id.get_secret_value(),
                "client_secret": client_secret.get_secret_value(),
                "scope": self._settings.ft_scope,
            },
        )
        body = response.json()
        self._token = str(body["access_token"])
        self._token_expires_at = self._monotonic() + float(body["expires_in"]) - TOKEN_MARGIN_S
        return self._token

    def _search(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """Une page de résultats ; renouvelle le token une fois en cas de 401."""
        for attempt in (1, 2):
            try:
                response = self._client.request(
                    "GET",
                    SEARCH_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {self._get_token()}"},
                )
            except SourceHttpError as exc:
                if exc.status_code == 401 and attempt == 1:
                    self._token = None
                    continue
                raise
            if response.status_code == 204 or not response.content:
                return []
            results = response.json().get("resultats", [])
            return list(results)
        return []  # pragma: no cover

    # --- pagination et découpage ----------------------------------------------------------

    def _pages(self, params: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
        """Toutes les pages d'une requête ; vrai si le plafond de 1 150 offres est atteint."""
        items: list[dict[str, Any]] = []
        for start, end in PAGE_RANGES:
            page = self._search({**params, "range": f"{start}-{end}"})
            items.extend(page)
            if len(page) < PAGE_SIZE:
                return items, False
        return items, True

    def _window(
        self, params: dict[str, str], low: datetime, high: datetime
    ) -> Iterator[dict[str, Any]]:
        items, capped = self._pages(
            {**params, "minCreationDate": _fmt(low), "maxCreationDate": _fmt(high)}
        )
        if capped and high - low > MIN_WINDOW:
            logger.warning(
                "Plafond de 1150 offres atteint : requête découpée par fenêtre de dates."
            )
            middle = low + (high - low) / 2
            yield from self._window(params, low, middle)
            yield from self._window(params, middle, high)
            return
        if capped:
            logger.warning(
                "Plafond de 1150 offres atteint sur une fenêtre minimale : résultats tronqués."
            )
        yield from items

    # --- interface Source -----------------------------------------------------------------

    @staticmethod
    def _base_params(query: SearchQuery) -> list[dict[str, str]]:
        """Une requête par (mot-clé, département) : les mots-clés sont des alternatives."""
        codes = [
            CONTRACT_FILTER_CODES[c] for c in query.contract_types if c in CONTRACT_FILTER_CODES
        ]
        zones: list[dict[str, str]]
        if query.commune and query.distance_km:
            zones = [{"commune": query.commune, "distance": str(query.distance_km)}]
        elif query.departments:
            zones = [{"departement": d} for d in query.departments]
        else:
            zones = [{}]
        requests = []
        for keyword in query.keywords:
            for zone in zones:
                params = {"motsCles": keyword, **zone}
                if codes:
                    params["typeContrat"] = ",".join(codes)
                requests.append(params)
        return requests

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]:
        now = self._now()
        low = max(since, now - timedelta(days=query.max_age_days))
        seen: set[str] = set()
        for params in self._base_params(query):
            for raw in self._window(params, low, now):
                raw_id = str(raw.get("id", ""))
                if not raw_id or raw_id in seen:
                    continue
                seen.add(raw_id)
                try:
                    yield offer_from_raw(raw, now)
                except (KeyError, ValueError, ValidationError):
                    self.skipped += 1
        if self.skipped:
            logger.warning(
                "%d offre(s) France Travail ignorée(s) (format inattendu).", self.skipped
            )
