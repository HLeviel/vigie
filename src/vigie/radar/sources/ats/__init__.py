"""Source ATS : pages carrières des entreprises ciblées (``ats_companies`` de la configuration).

Un adaptateur par plateforme. Les API renvoient toutes les offres de l'entreprise, partout dans le
monde : on garde celles situées en France ou en télétravail compatible, avant les filtres de la
section 10. Une entreprise introuvable (404) ou en erreur est comptée sans faire échouer la source,
sauf si toutes les entreprises échouent. Aucun nom d'entreprise n'est journalisé.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from pydantic import ValidationError

from vigie.radar.config import AtsCompany
from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.radar.models import Offer, SearchQuery
from vigie.radar.sources.ats.ashby import AshbyAdapter
from vigie.radar.sources.ats.base import AtsAdapter
from vigie.radar.sources.ats.greenhouse import GreenhouseAdapter
from vigie.radar.sources.ats.lever import LeverAdapter
from vigie.radar.sources.ats.location import is_eligible
from vigie.radar.sources.ats.recruitee import RecruiteeAdapter
from vigie.radar.sources.ats.smartrecruiters import SmartRecruitersAdapter

logger = logging.getLogger(__name__)

NAME = "ats"
ADAPTERS: dict[str, Callable[[], AtsAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "smartrecruiters": SmartRecruitersAdapter,
    "recruitee": RecruiteeAdapter,
}
SUPPORTED_PLATFORMS = frozenset(ADAPTERS)


class AtsSource:
    name = NAME

    def __init__(
        self,
        companies: list[AtsCompany],
        *,
        client: SourceHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._companies = companies
        self._client = client or SourceHttpClient(NAME)
        self._now = now
        self._adapters: dict[str, AtsAdapter] = {}
        self._cache: dict[tuple[str, str], list[Offer]] = {}
        self.skipped = 0

    def _adapter(self, platform: str) -> AtsAdapter | None:
        if platform not in ADAPTERS:
            return None
        if platform not in self._adapters:
            self._adapters[platform] = ADAPTERS[platform]()
        return self._adapters[platform]

    def _collect(self, company: AtsCompany) -> list[Offer]:
        """Offres éligibles d'une entreprise ; ``SourceHttpError`` si elle est injoignable."""
        adapter = self._adapter(company.platform)
        if adapter is None:
            raise SourceHttpError("plateforme ATS non prise en charge")
        fetched_at = self._now()
        offers: list[Offer] = []
        for raw in adapter.fetch_jobs(self._client, company.token):
            if not is_eligible(adapter.place(raw)):
                continue
            try:
                offers.append(
                    adapter.to_offer(raw, company.name, company.token, fetched_at, self._client)
                )
            except (KeyError, TypeError, ValueError, ValidationError):
                self.skipped += 1
        return offers

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]:
        """Les offres ne dépendent pas de la recherche ni de ``since`` : chaque entreprise n'est
        interrogée qu'une fois par exécution (les appels suivants relisent le cache)."""
        failures = 0
        not_found = 0
        for company in self._companies:
            key = (company.platform, company.token)
            if key not in self._cache:
                try:
                    self._cache[key] = self._collect(company)
                except SourceHttpError as exc:
                    self._cache[key] = []
                    self.skipped += 1
                    failures += 1
                    not_found += int(exc.status_code == 404)
            yield from self._cache[key]
        if failures:
            logger.warning(
                "%d entreprise(s) ATS en erreur dont %d introuvable(s) (404).", failures, not_found
            )
        if self._companies and failures == len(self._companies):
            raise SourceHttpError("toutes les entreprises ATS ont échoué")
