"""Alertes LinkedIn. Motif de lien **non vérifié** (aucun email réel disponible)."""

import re

from vigie.radar.sources.email_alerts.parsing import SenderSpec

SPEC = SenderSpec(
    key="linkedin",
    link_regex=re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(?P<id>\d+)"),
)
