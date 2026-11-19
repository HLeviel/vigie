"""Registre des sources.

Ajouter une source = ajouter un fichier dans ce dossier et une entrée dans ``REGISTRY``,
sans toucher au pipeline.
"""

from collections.abc import Callable

from vigie.radar.sources.base import Source
from vigie.settings import Settings

SourceFactory = Callable[[Settings], Source]

# nom de source -> fabrique. Les sources (France Travail, Adzuna) s'y inscrivent ici.
REGISTRY: dict[str, SourceFactory] = {}


class UnknownSourceError(KeyError):
    """Nom de source absent du registre."""


def available() -> list[str]:
    return sorted(REGISTRY)


def build(name: str, settings: Settings) -> Source:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise UnknownSourceError(f"source inconnue : {name}") from None
    return factory(settings)
