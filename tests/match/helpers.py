"""Éléments communs aux tests d'intégration de ``match`` (profil fictif, configuration, offres)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.match.fakes import FakeEmbedder
from tests.radar.factories import make_offer
from vigie.match.config import MatchConfig
from vigie.radar.models import Offer

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
PROFILE_EXAMPLE = Path(__file__).parents[2] / "config" / "profile.example.yaml"

# Poids : lexical seul, pour des scores prévisibles (le faux fournisseur est pseudo-aléatoire).
LEXICAL_ONLY = "  weights: { semantic: 0, lexical: 1 }\n"


def match_config(profile: Path, extra: str = LEXICAL_ONLY) -> MatchConfig:
    from vigie.radar.config import load_config_text

    text = (
        "searches:\n  - { name: s, keywords: [a], departments: ['69'] }\n"
        f"match:\n  enabled: true\n  profile: {profile}\n"
        "  embeddings: { provider: local, model: fictif }\n" + extra
    )
    cfg = load_config_text(text).match
    assert cfg is not None
    return cfg


def _ago(days: int) -> datetime:
    """Date relative à l'horloge réelle (voir ``tests/radar/factories.py``)."""
    return datetime.now(UTC).replace(microsecond=0) - timedelta(days=days)


def make_offers() -> list[Offer]:
    """A : très pertinente ; B : un peu ; C : hors sujet ; D : contrat CDD, donc exclue."""
    return [
        make_offer(
            source_id="A",
            title="Ingénieur IA",
            description="python sql rag amazon web services langchain " * 10,
            published_at=_ago(4),
        ),
        make_offer(
            source_id="B",
            title="Développeur",
            description="python " * 60,
            published_at=_ago(2),
        ),
        make_offer(
            source_id="C",
            title="Comptable",
            description="comptabilité " * 40,
            published_at=_ago(1),
        ),
        make_offer(
            source_id="D",
            title="Data python",
            description="python sql " * 40,
            contract_type="CDD",
            published_at=_ago(3),
        ),
    ]


def uids(*letters: str) -> list[str]:
    return [f"adzuna:{letter}" for letter in letters]


def counting_factory(embedder: FakeEmbedder) -> Any:
    return lambda provider, model: embedder
