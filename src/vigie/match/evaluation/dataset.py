"""Jeu de données annoté : chargement, découpage reproductible, échantillonnage des annotations.

Le jeu réel est **privé** (table ``labels`` de la base) : il n'est jamais commité. Le dépôt public
ne contient qu'un petit jeu fictif pour tester ce code.
"""

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vigie.match.store import LABEL_GAIN, LABELS, LabelStore, ScoreStore, ScoringKey
from vigie.radar.models import Offer
from vigie.radar.store import Store

# Graine documentée du découpage : ne jamais la changer en cours de route (les décisions tirées du
# rapport reposent sur ce découpage).
SPLIT_SEED = "vigie-match-eval-v1"
TUNING_SHARE = 0.3  # 30 % pour régler les poids et la normalisation, 70 % pour mesurer

MIN_LABELED = 150  # objectif de la spec
MIN_RELEVANT = 30


@dataclass(frozen=True)
class EvalItem:
    offer: Offer
    label: str

    @property
    def uid(self) -> str:
        return self.offer.uid

    @property
    def gain(self) -> int:
        return LABEL_GAIN[self.label]


@dataclass(frozen=True)
class DatasetStats:
    total: int
    relevant: int
    maybe: int
    irrelevant: int

    @property
    def meets_goal(self) -> bool:
        return self.total >= MIN_LABELED and self.relevant >= MIN_RELEVANT


def stats(items: list[EvalItem]) -> DatasetStats:
    count = {label: sum(1 for i in items if i.label == label) for label in LABELS}
    return DatasetStats(
        total=len(items),
        relevant=count["pertinent"],
        maybe=count["peut-être"],
        irrelevant=count["non pertinent"],
    )


def split(items: list[EvalItem]) -> tuple[list[EvalItem], list[EvalItem]]:
    """(réglage, mesure) : découpage fixe fondé sur l'empreinte de ``uid``, sans hasard.

    Le même jeu donne toujours le même découpage ; ajouter des annotations ne déplace pas celles
    qui existent déjà de part et d'autre, sauf aux frontières de la proportion.
    """

    def order(item: EvalItem) -> str:
        return hashlib.sha256(f"{SPLIT_SEED}:{item.uid}".encode()).hexdigest()

    ranked = sorted(items, key=order)
    cut = round(len(ranked) * TUNING_SHARE)
    return ranked[:cut], ranked[cut:]


def load_from_store(store: Store) -> list[EvalItem]:
    """Offres annotées (dernière étiquette de chacune, toutes versions du profil confondues)."""
    labels = LabelStore(store).latest()
    offers = {o.uid: o for o in store.offers_by_uid(list(labels))}
    return [EvalItem(offers[uid], label) for uid, label in sorted(labels.items()) if uid in offers]


def load_json(path: Path) -> list[EvalItem]:
    """Jeu fictif de test : ``{"offers": [{... champs d'une offre ..., "label": "pertinent"}]}``."""
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    items: list[EvalItem] = []
    for raw in data["offers"]:
        fields = dict(raw)
        label = fields.pop("label")
        if label not in LABELS:
            raise ValueError("étiquette inconnue dans le jeu de test")
        items.append(EvalItem(Offer(**fields), label))
    return items


# --- échantillon à annoter ------------------------------------------------------------------------

_BUCKETS = ((25, "0-25"), (50, "25-50"), (75, "50-75"), (101, "75-100"))


def score_bucket(score: float | None) -> str:
    if score is None:
        return "sans score"
    return next(name for limit, name in _BUCKETS if score < limit)


def sample_for_labeling(
    store: Store,
    key: ScoringKey,
    n: int,
    already_labeled: set[str],
    seed: str = SPLIT_SEED,
    only: set[str] | None = None,
) -> list[Offer]:
    """``n`` offres à annoter, tirées **à tour de rôle dans chaque strate** (source × niveau de
    score), pour ne pas n'annoter que les offres déjà bien classées. Déterministe pour une base
    et un jeu d'étiquettes donnés. ``only`` limite le tirage à ces offres (réannotation)."""
    candidates = [
        o
        for o in store.retained_offers()
        if o.uid not in already_labeled and (only is None or o.uid in only)
    ]
    scores = ScoreStore(store).get_many([o.uid for o in candidates], key)
    strata: dict[tuple[str, str], list[Offer]] = defaultdict(list)
    for offer in candidates:
        found = scores.get(offer.uid)
        strata[(offer.source, score_bucket(found[0] if found else None))].append(offer)
    rng = random.Random(f"{seed}:{len(already_labeled)}")  # noqa: S311 (pas de secret)
    queues = [rng.sample(offers, len(offers)) for _, offers in sorted(strata.items())]
    chosen: list[Offer] = []
    while len(chosen) < n and any(queues):
        for queue in queues:
            if queue and len(chosen) < n:
                chosen.append(queue.pop())
    return chosen
