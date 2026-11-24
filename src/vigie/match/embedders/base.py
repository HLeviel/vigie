"""Interface commune des fournisseurs d'embeddings (section 5 de la spec ``match``)."""

import math
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

Kind = Literal["query", "document"]


class EmbedderError(Exception):
    """Échec d'un fournisseur. Le message est fixe : il ne contient aucun texte d'offre ni de
    profil, aucun secret, aucun texte renvoyé par le fournisseur."""


class Embedder(Protocol):
    name: str  # ex. "local:<modèle>", "bedrock:<modèle>"
    dim: int

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]: ...


EmbedderFactory = Callable[[str], Embedder]

# provider -> fabrique(modèle). Un fournisseur s'inscrit ici (``local`` et ``bedrock`` : étape 4).
REGISTRY: dict[str, EmbedderFactory] = {}


def build_embedder(provider: str, model: str) -> Embedder:
    try:
        factory = REGISTRY[provider]
    except KeyError:
        raise EmbedderError(f"fournisseur d'embeddings indisponible : {provider}") from None
    return factory(model)


def normalize_vector(vector: Sequence[float]) -> list[float]:
    """Vecteur de norme 1. Refuse un vecteur nul ou non fini."""
    if not vector or not all(math.isfinite(x) for x in vector):
        raise EmbedderError("vecteur d'embedding invalide")
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise EmbedderError("vecteur d'embedding nul")
    return [x / norm for x in vector]
