"""Étape de scoring de ``radar run`` (section 8 de la spec ``match``).

S'insère entre la collecte et le digest. Ne s'applique qu'aux offres nouvelles, non filtrées et
non notifiées. Tout ou rien : les scores ne sont écrits qu'une fois **tous** les embeddings
obtenus ; une panne laisse les offres sans score (le digest part alors trié par date).

Compteurs seulement dans les rapports et les journaux.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from vigie.match.config import LlmConfig, MatchConfig
from vigie.match.embedders.base import Embedder, build_embedder
from vigie.match.embedders.cache import embed_cached
from vigie.match.judge.base import Judge, build_judge
from vigie.match.llm_stage import LlmReport, llm_key, run_llm_stage
from vigie.match.profile import Profile, load_profile_file
from vigie.match.scoring import Scorer, scoring_key
from vigie.match.store import EmbeddingStore, ScoreStore, ScoringKey
from vigie.match.text import offer_owner, offer_text
from vigie.radar.models import SourceReport
from vigie.radar.store import Store
from vigie.settings import Settings

logger = logging.getLogger(__name__)

MATCH_SOURCE = "match"  # nom de la ligne de ``run_sources``
LLM_SOURCE = "match:llm"  # ligne de l'étage LLM

EmbedderFactory = Callable[[str, str], Embedder]


@dataclass
class MatchReport:
    pending: int = 0  # offres à scorer
    scored: int = 0
    excluded: int = 0  # exclues par les préférences
    failed: bool = False  # étage hybride en échec : le digest part trié par date
    error_message: str | None = None  # « Type: message » (message sûr) ou type seul
    llm: LlmReport | None = None  # étage LLM (None : désactivé ou jamais atteint)

    @property
    def llm_calls(self) -> int:
        return self.llm.calls if self.llm else 0

    @property
    def llm_failed(self) -> bool:
        """Étage LLM ignoré pour cette exécution (le score hybride est conservé)."""
        return self.llm is not None and self.llm.failed

    @property
    def any_failure(self) -> bool:
        return self.failed or self.llm_failed

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

    def llm_source_report(self, duration_ms: int = 0) -> SourceReport | None:
        """Ligne ``match:llm`` : candidates, évaluées, en cache, invalides, échec (compteurs)."""
        if self.llm is None:
            return None
        return SourceReport(
            source=LLM_SOURCE,
            fetched=self.llm.candidates,
            new=self.llm.assessed,
            duplicates=self.llm.cached,
            filtered=self.llm.invalid,
            errors=1 if self.llm.failed else 0,
            duration_ms=duration_ms,
            error_message=self.llm.error_message,
        )


def run_match(
    config: MatchConfig,
    store: Store,
    *,
    embedder_factory: EmbedderFactory = build_embedder,
    judge_factory: Callable[[LlmConfig], Judge] | None = None,
    run_id: str = "",
    now: datetime | None = None,
) -> MatchReport:
    """Score les offres en attente qui n'ont pas encore de score pour la clé courante, puis, si
    l'étage LLM est activé, réévalue les meilleures.

    Ne lève jamais : une panne (profil, fournisseur, bogue) est rapportée dans ``MatchReport``.
    Une panne de l'étage LLM laisse les scores hybrides intacts.
    """
    report = MatchReport()
    when = now or datetime.now(UTC)
    try:
        profile, hybrid_key = _run(config, store, embedder_factory, when, report)
    except Exception as exc:
        report.failed = True
        report.scored = 0
        report.excluded = 0
        report.error_message = f"{type(exc).__name__}: {exc}"
        logger.warning("match : échec (%s)", type(exc).__name__)
        return report
    logger.info(
        "match : à scorer=%d scorées=%d exclues=%d", report.pending, report.scored, report.excluded
    )
    if config.llm.enabled:
        report.llm = _run_llm(config, profile, hybrid_key, store, judge_factory, run_id, when)
    return report


def _default_judge_factory(config: LlmConfig) -> Judge:
    return build_judge(config, Settings())


def _run_llm(
    config: MatchConfig,
    profile: Profile,
    hybrid_key: ScoringKey,
    store: Store,
    judge_factory: Callable[[LlmConfig], Judge] | None,
    run_id: str,
    now: datetime,
) -> LlmReport:
    try:
        judge = (judge_factory or _default_judge_factory)(config.llm)
        return run_llm_stage(config.llm, profile, store, judge, hybrid_key, run_id, now)
    except Exception as exc:  # une panne de l'étage LLM n'invalide pas les scores hybrides
        logger.warning("llm : échec (%s)", type(exc).__name__)
        return LlmReport(failed=True, error_message=f"{type(exc).__name__}: {exc}")


def _run(
    config: MatchConfig,
    store: Store,
    embedder_factory: EmbedderFactory,
    now: datetime,
    report: MatchReport,
) -> tuple[Profile, ScoringKey]:
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
        return profile, key

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
    return profile, key


def key_for(config: MatchConfig, embedder_name: str | None = None) -> ScoringKey:
    """Clé de scoring d'après la configuration, sans appel réseau (profil lu, rien d'autre).

    Le nom d'un embedder est toujours ``<fournisseur>:<modèle>``.
    """
    profile = load_profile_file(config.profile)
    name = embedder_name or f"{config.embeddings.provider}:{config.embeddings.model}"
    key = scoring_key(config, profile, name)
    if config.llm.enabled and config.llm.model is not None:  # score final : LLM s'il existe
        return replace(key, llm=llm_key(config.llm, profile, key.min_score))
    return key
