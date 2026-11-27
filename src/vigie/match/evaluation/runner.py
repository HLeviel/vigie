"""Orchestration de l'évaluation : plonge, règle sur 30 % du jeu, mesure sur 70 %.

Aucun réglage n'est fait sur la partie de mesure. Le modèle d'embedding, les poids et la
normalisation du score hybride sont choisis sur la partie de réglage seulement.
"""

import itertools
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vigie.match.config import MatchConfig, WeightsConfig
from vigie.match.embedders.base import Embedder, Kind
from vigie.match.embedders.cache import embed_cached
from vigie.match.evaluation import variants as v
from vigie.match.evaluation.dataset import DatasetStats, EvalItem, split, stats
from vigie.match.evaluation.metrics import (
    PRIMARY,
    Bootstrap,
    evaluate_ranking,
    rank,
    verdict,
)
from vigie.match.judge.base import Assessment, Judge, OfferView
from vigie.match.llm_stage import assess_candidates, llm_key, select_candidates
from vigie.match.profile import Profile
from vigie.match.scoring import SEMANTIC_CEILING, SEMANTIC_FLOOR, Scorer, ScoreResult
from vigie.match.store import EmbeddingStore, ScoreStore
from vigie.match.text import ProfileText, offer_owner, offer_text, profile_texts
from vigie.radar.store import Store

# Grille de réglage du score hybride (la valeur par défaut de chaque paramètre est testée en
# premier : à égalité, elle est conservée).
WEIGHT_GRID = (0.7, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 0.9, 1.0)  # poids sémantique
FLOOR_GRID = (SEMANTIC_FLOOR, 0.1, 0.2, 0.3, 0.4)
CEILING_GRID = (SEMANTIC_CEILING, 0.9, 0.8, 0.7, 0.6)
TUNING_METRIC = "ndcg@10"


class TimedEmbedder:
    """Décore un fournisseur pour mesurer le temps passé et le nombre de textes plongés."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.name = inner.name
        self.dim = inner.dim
        self.seconds = 0.0
        self.texts = 0

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        start = time.perf_counter()
        try:
            return self._inner.embed(texts, kind)
        finally:
            self.seconds += time.perf_counter() - start
            self.texts += len(texts)


class TimedJudge:
    """Décore un juge pour mesurer le temps passé et le nombre d'appels."""

    def __init__(self, inner: Judge) -> None:
        self._inner = inner
        self.name = inner.name
        self.usage = inner.usage
        self.last_usage = inner.last_usage
        self.seconds = 0.0
        self.calls = 0

    def assess(self, profile: Profile, offer: OfferView) -> Assessment:
        start = time.perf_counter()
        try:
            return self._inner.assess(profile, offer)
        finally:
            self.seconds += time.perf_counter() - start
            self.calls += 1
            self.usage = self._inner.usage
            self.last_usage = self._inner.last_usage


@dataclass(frozen=True)
class Tuned:
    model: str  # nom du modèle retenu pour le variant hybride
    semantic_weight: float
    floor: float
    ceiling: float

    @property
    def lexical_weight(self) -> float:
        return round(1 - self.semantic_weight, 6)


@dataclass
class VariantResult:
    name: str
    metrics: dict[str, float]
    intervals: dict[str, tuple[float, float]]
    seconds_per_offer: float | None = None  # None : rien calculé pendant cette évaluation
    offers_embedded: int = 0
    # Variantes ``hybrid+llm`` : offres évaluées par le LLM, appels et jetons de cette évaluation.
    llm_candidates: int = 0
    llm_assessed: int = 0  # évaluations valides (cache compris)
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds_per_call: float | None = None


@dataclass(frozen=True)
class Comparison:
    a: str
    b: str
    metric: str
    mean: float
    low: float
    high: float

    @property
    def verdict(self) -> str:
        return verdict(self.low, self.high)


@dataclass
class EvalResult:
    generated_at: datetime
    profile_hash: str
    dataset: DatasetStats
    tuning: DatasetStats
    measure: DatasetStats
    variants: list[VariantResult]
    tuned: Tuned
    vs_date: dict[str, dict[str, Comparison]] = field(default_factory=dict)
    targeted: list[Comparison] = field(default_factory=list)
    draws: int = 1000
    notes: list[str] = field(default_factory=list)  # variantes ignorées, budget atteint...


def _embed_all(
    embedder: TimedEmbedder,
    store: EmbeddingStore,
    texts: list[ProfileText],
    items: list[EvalItem],
    now: datetime,
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    profile_vectors, _ = embed_cached(
        embedder, store, [(t.owner, t.text) for t in texts], "query", now
    )
    offer_vectors, _ = embed_cached(
        embedder,
        store,
        [(offer_owner(i.offer), offer_text(i.offer)) for i in items],
        "document",
        now,
    )
    return profile_vectors, {i.uid: offer_vectors[offer_owner(i.offer)] for i in items}


def _ndcg(scores: dict[str, float], items: list[EvalItem]) -> float:
    gains = {i.uid: i.gain for i in items}
    return evaluate_ranking(rank(scores, list(gains)), gains)[TUNING_METRIC]


def _hybrid(
    scorer: Scorer, items: list[EvalItem], vectors: dict[str, list[float]]
) -> dict[str, float]:
    return {i.uid: scorer.score(i.offer, vectors[i.uid]).score for i in items}


def evaluate(
    profile: Profile,
    config: MatchConfig,
    items: list[EvalItem],
    embedders: list[Embedder],
    store: EmbeddingStore,
    *,
    draws: int = 1000,
    now: datetime | None = None,
    judges: list[Judge] | None = None,
    db: Store | None = None,
) -> EvalResult:
    """``judges`` : juges LLM comparés (un variant ``hybrid+llm:<modèle>`` chacun) ; ``db`` est
    alors obligatoire (cache des évaluations et jetons consommés)."""
    when = now or datetime.now(UTC)
    if judges and db is None:
        raise ValueError("la base est requise pour évaluer l'étage LLM")
    tuning_items, measure_items = split(items)
    if not tuning_items or not measure_items:
        raise ValueError("jeu trop petit pour être découpé en réglage et mesure")
    texts = profile_texts(profile)
    timed = [TimedEmbedder(e) for e in embedders]
    embedded = {t.name: _embed_all(t, store, texts, items, when) for t in timed}

    def scorer_for(
        name: str,
        weights: WeightsConfig | None = None,
        floor: float = SEMANTIC_FLOOR,
        ceiling: float = SEMANTIC_CEILING,
    ) -> Scorer:
        cfg = config if weights is None else config.model_copy(update={"weights": weights})
        return Scorer(
            profile, cfg, texts, embedded[name][0], semantic_floor=floor, semantic_ceiling=ceiling
        )

    # --- réglage, sur la partie de réglage seulement ---------------------------------------
    def semantic(name: str, subset: list[EvalItem]) -> dict[str, float]:
        scorer = scorer_for(name)
        return {i.uid: scorer.semantic(embedded[name][1][i.uid]) for i in subset}

    best = max(timed, key=lambda t: (_ndcg(semantic(t.name, tuning_items), tuning_items), t.name))
    tuned = Tuned(best.name, 0.7, SEMANTIC_FLOOR, SEMANTIC_CEILING)
    best_score = -1.0
    for weight, floor, ceiling in itertools.product(WEIGHT_GRID, FLOOR_GRID, CEILING_GRID):
        scorer = scorer_for(
            best.name, WeightsConfig(semantic=weight, lexical=round(1 - weight, 6)), floor, ceiling
        )
        value = _ndcg(_hybrid(scorer, tuning_items, embedded[best.name][1]), tuning_items)
        if value > best_score + 1e-12:
            best_score, tuned = value, Tuned(best.name, weight, floor, ceiling)

    # --- mesure, sur la partie de mesure ---------------------------------------------------
    base_scorer = scorer_for(best.name)
    hybrid_scorer = scorer_for(
        best.name,
        WeightsConfig(semantic=tuned.semantic_weight, lexical=tuned.lexical_weight),
        tuned.floor,
        tuned.ceiling,
    )
    scores: dict[str, dict[str, float]] = {
        v.BASELINE_DATE: v.date_scores(measure_items),
        v.BASELINE_LEXICAL: {i.uid: base_scorer.lexical(i.offer) for i in measure_items},
        v.BASELINE_TFIDF: v.tfidf_scores(profile, measure_items),
    }
    for t in timed:
        sem = scorer_for(t.name)
        scores[f"emb:{t.name}"] = {
            i.uid: sem.semantic(embedded[t.name][1][i.uid]) for i in measure_items
        }
    hybrid_results = {
        i.uid: hybrid_scorer.score(i.offer, embedded[best.name][1][i.uid]) for i in measure_items
    }
    scores[f"hybrid:{best.name}"] = {uid: r.score for uid, r in hybrid_results.items()}
    notes: list[str] = []
    llm_stats: dict[str, VariantResult] = {}
    for judge in judges or []:
        _add_llm_variant(
            judge, profile, config, measure_items, hybrid_results, scores, llm_stats, notes,
            db, when,
        )  # fmt: skip

    gains = {i.uid: i.gain for i in measure_items}
    rankings = {name: rank(s, list(gains)) for name, s in scores.items()}
    boot = Bootstrap(rankings, gains, draws=draws)

    results: list[VariantResult] = []
    for name, ranking in rankings.items():
        embedder = next((t for t in timed if name == f"emb:{t.name}"), None)
        per_offer = embedder.seconds / embedder.texts if embedder and embedder.texts else None
        llm = llm_stats.get(name)
        results.append(
            VariantResult(
                name=name,
                metrics=evaluate_ranking(ranking, gains),
                intervals={m: boot.interval(name, m) for m in boot.samples[name]},
                seconds_per_offer=per_offer,
                offers_embedded=embedder.texts if embedder else 0,
                llm_candidates=llm.llm_candidates if llm else 0,
                llm_assessed=llm.llm_assessed if llm else 0,
                llm_calls=llm.llm_calls if llm else 0,
                prompt_tokens=llm.prompt_tokens if llm else 0,
                completion_tokens=llm.completion_tokens if llm else 0,
                seconds_per_call=llm.seconds_per_call if llm else None,
            )
        )

    def compare(a: str, b: str, metric: str) -> Comparison:
        mean, low, high = boot.difference(a, b, metric)
        return Comparison(a, b, metric, mean, low, high)

    vs_date = {
        r.name: {m: compare(r.name, v.BASELINE_DATE, m) for m in PRIMARY}
        for r in results
        if r.name != v.BASELINE_DATE
    }
    hybrid_name = f"hybrid:{best.name}"
    best_emb = f"emb:{best.name}"
    targeted = [
        compare(a, b, m)
        for a, b in (
            (best_emb, v.BASELINE_LEXICAL),
            (hybrid_name, v.BASELINE_LEXICAL),
            (hybrid_name, best_emb),
            (hybrid_name, v.BASELINE_DATE),
        )
        for m in PRIMARY
    ]
    targeted += [  # chaque étage doit prouver un gain sur le précédent : LLM contre hybride
        compare(name, hybrid_name, m) for name in llm_stats for m in PRIMARY
    ]
    return EvalResult(
        generated_at=when,
        profile_hash=profile.profile_hash,
        dataset=stats(items),
        tuning=stats(tuning_items),
        measure=stats(measure_items),
        variants=results,
        tuned=tuned,
        vs_date=vs_date,
        targeted=targeted,
        draws=draws,
        notes=notes,
    )


def _add_llm_variant(
    judge: Judge,
    profile: Profile,
    config: MatchConfig,
    measure_items: list[EvalItem],
    hybrid_results: dict[str, ScoreResult],
    scores: dict[str, dict[str, float]],
    llm_stats: dict[str, VariantResult],
    notes: list[str],
    db: Store | None,
    now: datetime,
) -> None:
    """Variante ``hybrid+llm:<modèle>`` : le LLM réévalue les ``top_n`` meilleures offres hybrides
    éligibles de la partie de mesure ; les autres gardent leur score hybride."""
    assert db is not None  # noqa: S101 (vérifié par l'appelant)
    model = judge.name.split(":", 1)[1]
    name = f"hybrid+llm:{model}"
    timed = TimedJudge(judge)
    offers = [i.offer for i in measure_items]
    hybrid = {uid: (r.score, r.details.model_dump_json()) for uid, r in hybrid_results.items()}
    candidates = select_candidates(offers, hybrid, config.llm.top_n)
    llm_cfg = config.llm.model_copy(update={"model": model})
    key = llm_key(llm_cfg, profile)
    report = assess_candidates(
        candidates, timed, profile, llm_cfg, key, db, f"eval-{now:%Y%m%d}", now
    )
    if report.failed:
        notes.append(f"{name} ignorée : {report.error_message}")
        return
    if report.budget_reached:
        notes.append(f"{name} : budget atteint, certaines offres gardent leur score hybride")
    final = dict(scores[next(n for n in scores if n.startswith("hybrid:"))])
    stored = ScoreStore(db).get_many([offer.uid for offer, _ in candidates], key)
    for uid, (score, _) in stored.items():
        final[uid] = score
    scores[name] = final
    llm_stats[name] = VariantResult(
        name=name,
        metrics={},
        intervals={},
        llm_candidates=report.candidates,
        llm_assessed=len(stored),
        llm_calls=report.calls,
        prompt_tokens=report.prompt_tokens,
        completion_tokens=report.completion_tokens,
        seconds_per_call=timed.seconds / timed.calls if timed.calls else None,
    )
