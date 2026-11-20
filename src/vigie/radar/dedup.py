"""Dédoublonnage (section 9 de la spec).

1. Même source : ``uid`` unique ; une offre déjà vue n'est pas réinsérée.
2. Entre sources : même ``dedup_key`` qu'une offre d'une autre source -> alias de la première.
   Deux offres d'une même source restent distinctes même si leur clé est identique.
"""

from enum import StrEnum

from vigie.radar.models import Offer
from vigie.radar.store import Store


class Outcome(StrEnum):
    NEW = "new"
    SEEN = "seen"
    ALIAS = "alias"


def classify(store: Store, offer: Offer) -> tuple[Outcome, str | None]:
    """Retourne le résultat et, pour un alias, l'``uid`` de l'offre canonique."""
    if store.known(offer.uid):
        return Outcome.SEEN, None
    canonical = store.find_canonical(offer.dedup_key, exclude_source=offer.source)
    if canonical is not None:
        return Outcome.ALIAS, canonical
    return Outcome.NEW, None
