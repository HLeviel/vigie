"""Registre des sources.

Ajouter une source = ajouter un fichier dans ce dossier et une entrée dans ``REGISTRY``,
sans toucher au pipeline.
"""

from collections.abc import Callable

from vigie.radar.config import RadarConfig
from vigie.radar.sources.adzuna import AdzunaSource
from vigie.radar.sources.ats import AtsSource
from vigie.radar.sources.base import Source
from vigie.radar.sources.france_travail import FranceTravailSource
from vigie.settings import Settings

SourceFactory = Callable[[Settings, RadarConfig], Source]

# nom de source -> fabrique. Une nouvelle source s'inscrit ici.
REGISTRY: dict[str, SourceFactory] = {
    "adzuna": lambda settings, config: AdzunaSource(settings),
    "ats": lambda settings, config: AtsSource(config.ats_companies),
    "france_travail": lambda settings, config: FranceTravailSource(settings),
}


class UnknownSourceError(KeyError):
    """Nom de source absent du registre."""


def available() -> list[str]:
    return sorted(REGISTRY)


def build(name: str, settings: Settings, config: RadarConfig) -> Source:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise UnknownSourceError(f"source inconnue : {name}") from None
    return factory(settings, config)
