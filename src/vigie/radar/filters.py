"""Filtres (section 10), appliqués après dédoublonnage dans cet ordre :

ancienneté maximale, type de contrat, exclusions de titre, entreprises exclues, mots requis.
Une offre écartée garde son motif (``filtered_reason``) pour ajuster les règles.
"""

import re
from datetime import datetime, timedelta

from vigie.radar.config import FiltersConfig
from vigie.radar.models import ContractType, Offer, SearchQuery
from vigie.radar.normalize import normalize_for_key


def _word_pattern(term: str) -> re.Pattern[str] | None:
    normalized = normalize_for_key(term)
    if not normalized:
        return None
    return re.compile(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])")


def _compile(terms: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for term in terms:
        pattern = _word_pattern(term)
        if pattern is not None:
            compiled.append((term, pattern))
    return compiled


class FilterSet:
    def __init__(self, config: FiltersConfig) -> None:
        self._exclude_title = _compile(config.exclude_title)
        self._exclude_companies = _compile(config.exclude_companies)
        self._require_any = _compile(config.require_any)

    def reason(self, offer: Offer, query: SearchQuery, now: datetime) -> str | None:
        """Motif d'exclusion, ou ``None`` si l'offre est conservée."""
        if offer.published_at < now - timedelta(days=query.max_age_days):
            return "age"
        # Les offres au contrat inconnu (AUTRE) sont conservées : Adzuna le renseigne rarement.
        if (
            query.contract_types
            and offer.contract_type is not ContractType.AUTRE
            and offer.contract_type not in query.contract_types
        ):
            return "contract"
        title = normalize_for_key(offer.title)
        for term, pattern in self._exclude_title:
            if pattern.search(title):
                return f"title:{term}"
        company = normalize_for_key(offer.company or "")
        for term, pattern in self._exclude_companies:
            if pattern.search(company):
                return f"company:{term}"
        if self._require_any:
            text = normalize_for_key(f"{offer.title} {offer.description}")
            if not any(pattern.search(text) for _, pattern in self._require_any):
                return "require_any"
        return None
