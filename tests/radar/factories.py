"""Fabriques et sources factices partagées par les tests."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

from vigie.radar.config import RadarConfig, load_config_text
from vigie.radar.models import Offer, SearchQuery
from vigie.settings import Settings

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
APP_ID = "id-valeur-de-test"
APP_KEY = "s3cr3t-valeur-de-test"


def make_offer(**overrides: Any) -> Offer:
    data: dict[str, Any] = {
        "source": "adzuna",
        "source_id": "1",
        "title": "Data Engineer Python",
        "company": "Acme",
        "location_label": "Lyon, Rhône",
        "department": "69",
        "description": "Pipelines Python et data.",
        "url": "https://example.org/offre/1",
        "published_at": datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        "fetched_at": NOW,
    }
    data.update(overrides)
    return Offer(**data)


def make_settings(**overrides: Any) -> Settings:
    data: dict[str, Any] = {"adzuna_app_id": APP_ID, "adzuna_app_key": APP_KEY}
    data.update(overrides)
    return Settings(_env_file=None, **data)  # type: ignore[call-arg]


CONFIG_YAML = """
sources:
  adzuna: { enabled: true }
searches:
  - name: demo
    keywords: ["python"]
    departments: ["69"]
filters:
  exclude_title: ["stage"]
  require_any: ["python", "data"]
"""


def make_config(text: str = CONFIG_YAML) -> RadarConfig:
    return load_config_text(text)


class FakeSource:
    """Source factice : renvoie des offres, puis lève éventuellement une erreur."""

    def __init__(
        self,
        name: str,
        offers: list[Offer],
        error: Exception | None = None,
        skipped: int = 0,
    ) -> None:
        self.name = name
        self._offers = offers
        self._error = error
        self.skipped = skipped

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]:
        yield from self._offers
        if self._error is not None:
            raise self._error


def factory(source: FakeSource) -> Callable[[Settings, RadarConfig], FakeSource]:
    return lambda settings, config: source
