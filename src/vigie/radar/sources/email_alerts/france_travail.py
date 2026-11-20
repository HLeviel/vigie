"""Alertes France Travail. Motif de lien **non vérifié** (aucun email réel disponible)."""

import re

from vigie.radar.sources.email_alerts.parsing import SenderSpec

SPEC = SenderSpec(
    key="france_travail",
    link_regex=re.compile(
        r"(?:candidat\.francetravail\.fr|candidat\.pole-emploi\.fr)/offres/recherche/detail/(?P<id>[0-9A-Za-z]+)"
    ),
)
