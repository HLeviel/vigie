from collections.abc import Iterator
from datetime import datetime

import pytest

from tests.radar.factories import make_config
from vigie.radar import sources
from vigie.radar.models import Offer, SearchQuery
from vigie.settings import Settings


class Fake:
    name = "fake"

    def fetch(self, query: SearchQuery, since: datetime) -> Iterator[Offer]:
        return iter(())


def test_registry_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sources.REGISTRY, "fake", lambda settings, config: Fake())
    assert "fake" in sources.available()
    source = sources.build("fake", Settings(_env_file=None), make_config())  # type: ignore[call-arg]
    assert source.name == "fake"


def test_unknown_source() -> None:
    with pytest.raises(sources.UnknownSourceError):
        sources.build("nope", Settings(_env_file=None), make_config())  # type: ignore[call-arg]


def test_france_travail_is_registered() -> None:
    assert "france_travail" in sources.available()
