"""Métriques de classement et intervalles de confiance par bootstrap (section 9 de la spec).

Une liste de ``gains`` est donnée dans l'ordre du classement : 2 = pertinent, 1 = peut-être,
0 = non pertinent. Seul ``pertinent`` (gain 2) compte pour precision@k et recall@k ; nDCG@k utilise
les gains tels quels (gain linéaire, pas 2^gain − 1).
"""

import math
import random
from collections.abc import Callable, Sequence

RELEVANT_GAIN = 2

Metric = Callable[[Sequence[int]], float]


def precision_at_k(gains: Sequence[int], k: int) -> float:
    return sum(1 for g in gains[:k] if g == RELEVANT_GAIN) / k


def recall_at_k(gains: Sequence[int], k: int) -> float:
    total = sum(1 for g in gains if g == RELEVANT_GAIN)
    if total == 0:
        return 0.0
    return sum(1 for g in gains[:k] if g == RELEVANT_GAIN) / total


def _dcg(gains: Sequence[int], k: int) -> float:
    return sum(g / math.log2(rank + 2) for rank, g in enumerate(gains[:k]))


def ndcg_at_k(gains: Sequence[int], k: int) -> float:
    ideal = _dcg(sorted(gains, reverse=True), k)
    return 0.0 if ideal == 0 else _dcg(gains, k) / ideal


METRICS: dict[str, Metric] = {
    "precision@5": lambda g: precision_at_k(g, 5),
    "precision@10": lambda g: precision_at_k(g, 10),
    "ndcg@10": lambda g: ndcg_at_k(g, 10),
    "recall@20": lambda g: recall_at_k(g, 20),
}
PRIMARY = ("precision@10", "ndcg@10")  # métriques qui décident d'un gain


def rank(scores: dict[str, float], uids: Sequence[str]) -> list[str]:
    """Classement par score décroissant, égalités départagées par ``uid`` (déterministe)."""
    return sorted(uids, key=lambda uid: (-scores[uid], uid))


def evaluate_ranking(ranking: Sequence[str], gains: dict[str, int]) -> dict[str, float]:
    ordered = [gains[uid] for uid in ranking]
    return {name: metric(ordered) for name, metric in METRICS.items()}


def percentile_interval(values: Sequence[float], level: float = 0.95) -> tuple[float, float]:
    ordered = sorted(values)
    low = ordered[int(((1 - level) / 2) * (len(ordered) - 1))]
    high = ordered[int((1 - (1 - level) / 2) * (len(ordered) - 1))]
    return low, high


class Bootstrap:
    """Rééchantillonnages **appariés** : tous les variants voient les mêmes tirages, ce qui permet
    de tester l'écart entre deux variants et pas seulement chaque intervalle isolément."""

    def __init__(
        self,
        rankings: dict[str, list[str]],
        gains: dict[str, int],
        draws: int = 1000,
        seed: int = 20261124,
    ) -> None:
        uids = sorted(gains)
        if not uids:
            raise ValueError("jeu de mesure vide")
        position = {name: {uid: i for i, uid in enumerate(r)} for name, r in rankings.items()}
        rng = random.Random(seed)  # noqa: S311 (pas de secret)
        self.samples: dict[str, dict[str, list[float]]] = {
            name: {metric: [] for metric in METRICS} for name in rankings
        }
        for _ in range(draws):
            drawn = rng.choices(uids, k=len(uids))
            for name, pos in position.items():
                ordered = [gains[uid] for uid in sorted(drawn, key=pos.__getitem__)]
                for metric_name, metric in METRICS.items():
                    self.samples[name][metric_name].append(metric(ordered))

    def interval(self, name: str, metric: str) -> tuple[float, float]:
        return percentile_interval(self.samples[name][metric])

    def difference(self, a: str, b: str, metric: str) -> tuple[float, float, float]:
        """Moyenne et intervalle à 95 % de ``a − b`` sur les mêmes tirages."""
        diffs = [
            x - y for x, y in zip(self.samples[a][metric], self.samples[b][metric], strict=True)
        ]
        low, high = percentile_interval(diffs)
        return sum(diffs) / len(diffs), low, high


def verdict(low: float, high: float) -> str:
    """Un écart dont l'intervalle contient 0 n'est jamais présenté comme un gain."""
    if low > 0:
        return "gain"
    if high < 0:
        return "perte"
    return "écart non significatif"
