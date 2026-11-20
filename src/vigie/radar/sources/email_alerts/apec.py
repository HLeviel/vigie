"""Alertes APEC. Motif de lien **non vérifié** (aucun email réel disponible)."""

import re

from vigie.radar.sources.email_alerts.parsing import SenderSpec

SPEC = SenderSpec(
    key="apec",
    link_regex=re.compile(
        r"apec\.fr/candidat/recherche-emploi\.html/emploi/detail-offre/(?P<id>[0-9A-Za-z]+)"
    ),
)
