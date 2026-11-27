"""Scoring des offres : étage 0 (préférences) et étage 1 (score hybride) de la spec ``match``.

Déterministe, sans réseau : les vecteurs viennent d'un ``Embedder`` (réel ou faux). Le texte d'une
offre est une donnée : il est comparé, jamais interprété.
"""

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from vigie.match.config import MatchConfig
from vigie.match.embedders.base import Embedder
from vigie.match.embedders.cache import embed_cached
from vigie.match.profile import SYNONYMS, Profile, normalize_skill
from vigie.match.store import EmbeddingStore, ScoringKey
from vigie.match.text import (
    Confidence,
    ProfileText,
    clean_text,
    offer_confidence,
    profile_texts,
)
from vigie.radar.models import ContractType, Offer, RemoteMode

# Normalisation du score sémantique : cosinus -> 0-100, bornes FIXES (jamais calculées sur le lot du
# jour, pour que le score d'une offre ne dépende pas des autres). Valeurs provisoires : l'évaluation
# (M2) les fixera sur le jeu annoté.
SEMANTIC_FLOOR = 0.0
SEMANTIC_CEILING = 1.0

# Poids lexicaux : une compétence maîtrisée compte plus qu'une compétence en acquisition.
LEXICAL_WEIGHTS = {"must_have": 1.0, "nice_to_have": 0.5}

Stage = Literal["hybrid"]


class ClosestProfileText(BaseModel):
    kind: Literal["summary", "experience", "role"]
    index: int


class ScoreDetails(BaseModel):
    """Explication d'un score : données privées (compétences, rôles), jamais publiées."""

    stage: Stage = "hybrid"
    excluded: bool = False
    reasons: list[str] = []  # codes : pref:contract, pref:remote, pref:salary, pref:avoid:<skill>
    penalties: dict[str, float] = {}
    semantic: float | None = None
    lexical: float | None = None
    found_must_have: list[str] = []
    found_nice_to_have: list[str] = []
    missing_must_have: list[str] = []
    closest_profile_text: ClosestProfileText | None = None
    confidence: Confidence = "normale"
    suspicious: bool = False  # offre signalée par radar : jamais envoyée au LLM


class ScoreResult(BaseModel):
    score: float  # 0-100
    details: ScoreDetails

    @property
    def excluded(self) -> bool:
        return self.details.excluded


# --- correspondance lexicale ----------------------------------------------------------------------


def _variants(canonical: str) -> list[str]:
    return sorted({canonical, *(v for v, c in SYNONYMS.items() if c == canonical)})


def _contains(text: str, canonical: str) -> bool:
    """Mot (ou expression) entier, synonymes compris, dans un texte déjà normalisé."""
    for variant in _variants(canonical):
        if re.search(rf"(?<![a-z0-9]){re.escape(variant)}(?![a-z0-9])", text):
            return True
    return False


def _normalized(text: str) -> str:
    return normalize_skill(clean_text(text))


# --- calculs -------------------------------------------------------------------------------------


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def semantic_score(
    cosine: float, floor: float = SEMANTIC_FLOOR, ceiling: float = SEMANTIC_CEILING
) -> float:
    """Cosinus ramené sur 0-100 par la normalisation fixe (bornes ``floor`` et ``ceiling``)."""
    if ceiling <= floor:
        raise ValueError("le plafond doit dépasser le plancher")
    return min(100.0, max(0.0, (cosine - floor) / (ceiling - floor) * 100.0))


class Scorer:
    def __init__(
        self,
        profile: Profile,
        config: MatchConfig,
        texts: list[ProfileText],
        vectors: dict[str, list[float]],
        *,
        semantic_floor: float = SEMANTIC_FLOOR,
        semantic_ceiling: float = SEMANTIC_CEILING,
    ) -> None:
        missing = [t.owner for t in texts if t.owner not in vectors]
        if missing:
            raise ValueError("vecteur de profil manquant")
        self._profile = profile
        self._config = config
        self._texts = texts
        self._vectors = [vectors[t.owner] for t in texts]
        self._floor = semantic_floor
        self._ceiling = semantic_ceiling

    @classmethod
    def build(
        cls,
        profile: Profile,
        config: MatchConfig,
        embedder: Embedder,
        store: EmbeddingStore,
        now: datetime | None = None,
    ) -> "Scorer":
        """Plonge les textes du profil (avec cache) et retire ceux des anciennes versions."""
        texts = profile_texts(profile)
        vectors, _ = embed_cached(embedder, store, [(t.owner, t.text) for t in texts], "query", now)
        store.prune_profiles(profile.profile_hash)
        return cls(profile, config, texts, vectors)

    # -- étage 0 ---------------------------------------------------------------------------------

    def preferences(self, offer: Offer) -> tuple[list[str], dict[str, float]]:
        """Exclusions (codes) et pénalités (code -> points) selon les préférences du profil."""
        prefs = self._profile.preferences
        excluded: list[str] = []
        penalties: dict[str, float] = {}
        if (
            prefs.contract_types
            and offer.contract_type != ContractType.AUTRE
            and offer.contract_type not in prefs.contract_types
        ):
            excluded.append("pref:contract")
        if prefs.remote and offer.remote != RemoteMode.UNKNOWN and offer.remote not in prefs.remote:
            penalties["pref:remote"] = self._config.penalties.remote
        if (
            prefs.min_salary is not None
            and offer.salary_max is not None
            and offer.salary_max < prefs.min_salary
        ):
            penalties["pref:salary"] = self._config.penalties.salary
        title = _normalized(offer.title)
        for skill in self._profile.skills.avoid:
            if _contains(title, skill):
                penalties[f"pref:avoid:{skill}"] = self._config.penalties.avoid
        return excluded, penalties

    def needs_embedding(self, offer: Offer) -> bool:
        """Faux si l'offre est exclue : inutile de payer son embedding."""
        return not self.preferences(offer)[0]

    # -- étage 1 ---------------------------------------------------------------------------------

    def _lexical(self, offer: Offer) -> tuple[float | None, list[str], list[str], list[str]]:
        skills = self._profile.skills
        text = _normalized(f"{offer.title} . {offer.description}")
        found_must = [s for s in skills.must_have if _contains(text, s)]
        found_nice = [s for s in skills.nice_to_have if _contains(text, s)]
        missing = [s for s in skills.must_have if s not in found_must]
        total = LEXICAL_WEIGHTS["must_have"] * len(skills.must_have) + LEXICAL_WEIGHTS[
            "nice_to_have"
        ] * len(skills.nice_to_have)
        if total == 0:
            return None, found_must, found_nice, missing  # profil sans compétence listée
        got = LEXICAL_WEIGHTS["must_have"] * len(found_must) + LEXICAL_WEIGHTS[
            "nice_to_have"
        ] * len(found_nice)
        return got / total * 100.0, found_must, found_nice, missing

    def _semantic(self, vector: list[float]) -> tuple[float, ClosestProfileText]:
        sims = [_dot(vector, v) for v in self._vectors]
        k = min(self._config.top_k_profile, len(sims))
        best = sorted(sims, reverse=True)[:k]
        closest = max(range(len(sims)), key=lambda i: sims[i])
        text = self._texts[closest]
        score = semantic_score(sum(best) / k, self._floor, self._ceiling)
        return score, ClosestProfileText(kind=text.kind, index=text.index)

    def semantic(self, vector: list[float]) -> float:
        """Score sémantique seul (0-100), sans préférences ni lexical : variante d'évaluation."""
        return self._semantic(vector)[0]

    def lexical(self, offer: Offer) -> float:
        """Score lexical seul (0-100) ; 0 si le profil ne liste aucune compétence."""
        value = self._lexical(offer)[0]
        return 0.0 if value is None else value

    def score(self, offer: Offer, vector: list[float] | None = None) -> ScoreResult:
        """Score 0-100 et explication. ``vector`` est requis sauf si l'offre est exclue."""
        confidence = offer_confidence(offer)
        excluded, penalties = self.preferences(offer)
        if excluded:
            return ScoreResult(
                score=0.0,
                details=ScoreDetails(
                    excluded=True,
                    reasons=excluded,
                    penalties={},
                    confidence=confidence,
                    suspicious=offer.suspicious,
                ),
            )
        if vector is None or len(vector) != len(self._vectors[0]):
            raise ValueError("vecteur d'offre absent ou de mauvaise dimension")
        if not all(math.isfinite(x) for x in vector):
            raise ValueError("vecteur d'offre invalide")

        semantic, closest = self._semantic(vector)
        lexical, found_must, found_nice, missing = self._lexical(offer)
        weights = self._config.weights
        if lexical is None:  # rien à comparer côté lexical : le sémantique porte tout le score
            base = semantic
        else:
            base = weights.semantic * semantic + weights.lexical * lexical
        score = min(100.0, max(0.0, base - sum(penalties.values())))
        return ScoreResult(
            score=round(score, 2),
            details=ScoreDetails(
                reasons=list(penalties),
                penalties=penalties,
                semantic=round(semantic, 2),
                lexical=None if lexical is None else round(lexical, 2),
                found_must_have=found_must,
                found_nice_to_have=found_nice,
                missing_must_have=missing,
                closest_profile_text=closest,
                confidence=confidence,
                suspicious=offer.suspicious,
            ),
        )


# À incrémenter quand le calcul du score change : les anciens scores deviennent obsolètes.
SCORING_VERSION = 1


def scoring_signature(config: MatchConfig) -> str:
    """Empreinte courte du réglage qui influe sur le score (poids, pénalités, k, bornes)."""
    payload = json.dumps(
        {
            "version": SCORING_VERSION,
            "weights": config.weights.model_dump(),
            "penalties": config.penalties.model_dump(),
            "top_k_profile": config.top_k_profile,
            "semantic": [SEMANTIC_FLOOR, SEMANTIC_CEILING],
            "lexical": LEXICAL_WEIGHTS,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def scoring_key(config: MatchConfig, profile: Profile, embedder_name: str) -> ScoringKey:
    return ScoringKey(
        profile_hash=profile.profile_hash,
        model=embedder_name,
        signature=scoring_signature(config),
        min_score=config.digest.min_score,
    )
