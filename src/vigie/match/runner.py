"""Étape de scoring de ``radar run`` (section 8 de la spec ``match``).

S'insère entre la collecte et le digest. Ne s'applique qu'aux offres nouvelles, non filtrées et
non notifiées. Tout ou rien : les scores ne sont écrits qu'une fois **tous** les embeddings
obtenus ; une panne laisse les offres sans score (le digest part alors trié par date).

Compteurs seulement dans les rapports et les journaux.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from vigie.match.config import MatchConfig
from vigie.match.embedders.base import Embedder, build_embedder
from vigie.match.embedders.cache import embed_cached
from vigie.match.profile import load_profile_file
from vigie.match.scoring import Scorer, scoring_key
from vigie.match.store import EmbeddingStore, ScoreStore, ScoringKey
from vigie.match.text import offer_owner, offer_text
from vigie.radar.models import SourceReport
from vigie.radar.store import Store

logger = logging.getLogger(__name__)

MATCH_SOURCE = "match"  # nom de la ligne de ``run_sources``

EmbedderFactory = Callable[[str, str], Embedder]


@dataclass
class MatchReport:
    pending: int = 0  # offres à scorer
    scored: int = 0
    excluded: int = 0  # exclues par les préférences
    llm_calls: int = 0  # étage LLM : lot M3, pas encore implémenté
    failed: bool = False
    error_message: str | None = None  # « Type: message » (message sûr) ou type seul

    def as_source_report(self, duration_ms: int = 0) -> SourceReport:
        """Ligne ``match`` de ``run_sources`` : à scorer, scorées, exclues, échec."""
        return SourceReport(
            source=MATCH_SOURCE,
            fetched=self.pending,
            new=self.scored,
            filtered=self.excluded,
            errors=1 if self.failed else 0,
            duration_ms=duration_ms,
            error_message=self.error_message,
        )


def run_match(
    config: MatchConfig,
    store: Store,
    *,
    embedder_factory: EmbedderFactory = build_embedder,
    now: datetime | None = None,
) -> MatchReport:
    """Score les offres en attente qui n'ont pas encore de score pour la clé courante.

    Ne lève jamais : une panne (profil, fournisseur, bogue) est rapportée dans ``MatchReport``.
    """
    report = MatchReport()
    when = now or datetime.now(UTC)
    try:
        _run(config, store, embedder_factory, when, report)
    except Exception as exc:
        report.failed = True
        report.scored = 0
        report.excluded = 0
        report.error_message = f"{type(exc).__name__}: {exc}"
        logger.warning("match : échec (%s)", type(exc).__name__)
    else:
        logger.info(
            "match : à scorer=%d scorées=%d exclues=%d",
            report.pending,
            report.scored,
            report.excluded,
        )
    return report


def _run(
    config: MatchConfig,
    store: Store,
    embedder_factory: EmbedderFactory,
    now: datetime,
    report: MatchReport,
) -> None:
    profile = load_profile_file(config.profile)
    embedder = embedder_factory(config.embeddings.provider, config.embeddings.model)
    key = scoring_key(config, profile, embedder.name)
    scores = ScoreStore(store)
    embeddings = EmbeddingStore(store)

    offers = store.pending_offers()
    known = scores.get_many([o.uid for o in offers], key)
    todo = [o for o in offers if o.uid not in known]
    report.pending = len(todo)
    if not todo:
        return

    scorer = Scorer.build(profile, config, embedder, embeddings, now)
    needs = [o for o in todo if scorer.needs_embedding(o)]
    vectors, _ = embed_cached(
        embedder, embeddings, [(offer_owner(o), offer_text(o)) for o in needs], "document", now
    )
    results = []
    for offer in todo:
        vector = vectors.get(offer_owner(offer))
        results.append((offer.uid, scorer.score(offer, vector)))
    # Tout est calculé : on écrit (jamais de scores partiels).
    for uid, result in results:
        scores.put(uid, key, result.score, result.details.model_dump_json(), now)
        report.scored += 1
        report.excluded += int(result.excluded)


def key_for(config: MatchConfig, embedder_name: str | None = None) -> ScoringKey:
    """Clé de scoring d'après la configuration, sans appel réseau (profil lu, rien d'autre).

    Le nom d'un embedder est toujours ``<fournisseur>:<modèle>``.
    """
    profile = load_profile_file(config.profile)
    name = embedder_name or f"{config.embeddings.provider}:{config.embeddings.model}"
    return scoring_key(config, profile, name)
