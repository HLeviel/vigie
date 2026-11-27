"""Étage 2 du scoring (lot M3) : le LLM réévalue les meilleures offres.

- Seulement les ``N`` offres au meilleur score hybride dont la confiance n'est pas ``faible`` ;
  une offre ``suspicious`` n'est **jamais** envoyée (elle garde son score hybride).
- Une réponse invalide est rejetée (une nouvelle tentative au plus) : l'offre garde son score
  hybride. Score final = score LLM s'il existe, sinon score hybride ; les deux sont stockés.
- Cache : pas de nouvel appel si l'offre, le profil, le modèle et la version du prompt n'ont pas
  changé (clé de la table ``scores``).
- Budget : nombre maximal d'appels et de jetons par exécution ; au-delà, l'étage s'arrête
  proprement et le signale.

Compteurs seulement dans les rapports et les journaux : jamais d'intitulé, de texte d'offre ni de
réponse du modèle.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from vigie.match.config import LlmConfig
from vigie.match.judge.base import (
    Judge,
    JudgeInvalidResponse,
    JudgeUnavailable,
    OfferView,
)
from vigie.match.judge.prompt import PROMPT_VERSION
from vigie.match.profile import Profile
from vigie.match.scoring import ScoreDetails
from vigie.match.store import LlmCallStore, ScoreStore, ScoringKey
from vigie.radar.models import Offer
from vigie.radar.store import Store

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # un appel puis une nouvelle tentative au plus


@dataclass
class LlmReport:
    candidates: int = 0  # offres retenues pour l'étage (top N éligibles)
    cached: int = 0  # déjà évaluées (même offre, profil, modèle, prompt)
    assessed: int = 0  # évaluations valides obtenues pendant cette exécution
    invalid: int = 0  # offres dont la réponse est restée invalide
    calls: int = 0  # appels au juge, nouvelles tentatives comprises
    prompt_tokens: int = 0
    completion_tokens: int = 0
    budget_reached: bool = False
    failed: bool = False  # étage ignoré (clé refusée, paramètre non géré, crédits, réseau...)
    error_message: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def select_candidates(
    offers: list[Offer], hybrid: dict[str, tuple[float, str]], top_n: int
) -> list[tuple[Offer, float]]:
    """Les ``top_n`` meilleures offres hybrides éligibles : non exclues, non ``suspicious``,
    confiance normale. Le filtre précède le tri : une offre écartée ne prend pas une place."""
    eligible: list[tuple[Offer, float]] = []
    for offer in offers:
        found = hybrid.get(offer.uid)
        if found is None or offer.suspicious:
            continue
        details = ScoreDetails.model_validate_json(found[1])
        if details.excluded or details.suspicious or details.confidence == "faible":
            continue
        eligible.append((offer, found[0]))
    eligible.sort(key=lambda pair: (-pair[1], pair[0].uid))
    return eligible[:top_n]


def llm_key(config: LlmConfig, profile: Profile, min_score: float = 0.0) -> ScoringKey:
    """Clé de cache des évaluations : profil, modèle OpenRouter, version du prompt."""
    assert config.model is not None  # noqa: S101 (validé par la configuration)
    return ScoringKey(
        profile_hash=profile.profile_hash,
        model=config.model,
        signature=PROMPT_VERSION,
        min_score=min_score,
        stage="llm",
    )


def assess_candidates(
    candidates: list[tuple[Offer, float]],
    judge: Judge,
    profile: Profile,
    config: LlmConfig,
    key: ScoringKey,
    store: Store,
    run_id: str,
    now: datetime,
) -> LlmReport:
    """Évalue les candidats pas encore en cache, dans la limite du budget."""
    scores, calls = ScoreStore(store), LlmCallStore(store)
    report = LlmReport(candidates=len(candidates))
    known = scores.get_many([offer.uid for offer, _ in candidates], key)
    report.cached = len(known)

    def over_budget() -> bool:
        return (
            report.calls >= config.max_calls_per_run
            or report.total_tokens >= config.max_total_tokens_per_run
        )

    for offer, hybrid_score in candidates:
        if offer.uid in known:
            continue
        view = OfferView.from_offer(offer)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if over_budget():
                report.budget_reached = True
                return report
            report.calls += 1
            try:
                assessment = judge.assess(profile, view)
            except JudgeInvalidResponse as exc:
                _count(report, calls, run_id, offer, key, judge, "invalid", now)
                if attempt == MAX_ATTEMPTS or not exc.retryable:
                    report.invalid += 1
                    break
                continue
            except JudgeUnavailable as exc:
                _count(report, calls, run_id, offer, key, judge, "error", now)
                report.failed = True
                report.error_message = f"{type(exc).__name__}: {exc}"
                return report
            _count(report, calls, run_id, offer, key, judge, "ok", now)
            details = {
                "assessment": assessment.model_dump(),
                "hybrid_score": hybrid_score,
                "prompt_version": key.signature,
            }
            scores.put(offer.uid, key, float(assessment.score), _json(details), now)
            report.assessed += 1
            break
    return report


def _json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _count(
    report: LlmReport,
    calls: LlmCallStore,
    run_id: str,
    offer: Offer,
    key: ScoringKey,
    judge: Judge,
    outcome: str,
    now: datetime,
) -> None:
    usage = judge.last_usage
    report.prompt_tokens += usage.prompt_tokens
    report.completion_tokens += usage.completion_tokens
    calls.add(
        run_id,
        offer.uid,
        key.model,
        key.signature,
        usage.prompt_tokens,
        usage.completion_tokens,
        outcome,
        now,
    )


def run_llm_stage(
    config: LlmConfig,
    profile: Profile,
    store: Store,
    judge: Judge,
    hybrid_key: ScoringKey,
    run_id: str,
    now: datetime,
) -> LlmReport:
    """Étage LLM d'une exécution : parmi les offres à notifier."""
    offers = store.pending_offers()
    hybrid = ScoreStore(store).get_many([o.uid for o in offers], hybrid_key)
    candidates = select_candidates(offers, hybrid, config.top_n)
    report = assess_candidates(
        candidates, judge, profile, config, llm_key(config, profile, hybrid_key.min_score),
        store, run_id, now,
    )  # fmt: skip
    logger.info(
        "llm : candidates=%d en cache=%d évaluées=%d invalides=%d appels=%d jetons=%d%s%s",
        report.candidates,
        report.cached,
        report.assessed,
        report.invalid,
        report.calls,
        report.total_tokens,
        " BUDGET" if report.budget_reached else "",
        " ÉCHEC" if report.failed else "",
    )
    return report
