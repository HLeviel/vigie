"""Alertes Welcome to the Jungle. Motif de lien **non vérifié** (aucun email réel disponible)."""

import re

from vigie.radar.sources.email_alerts.parsing import SenderSpec

SPEC = SenderSpec(
    key="wttj",
    link_regex=re.compile(
        r"welcometothejungle\.com/(?:fr|en)/companies/(?P<company>[^/?#]+)/jobs/(?P<id>[^/?#]+)"
    ),
)
