"""Calcul des embeddings avec cache : jamais de recalcul si le texte n'a pas changé."""

from dataclasses import dataclass
from datetime import UTC, datetime

from vigie.match.embedders.base import Embedder, EmbedderError, Kind, normalize_vector
from vigie.match.store import EmbeddingStore, decode_vector, encode_vector
from vigie.match.text import text_hash

BATCH_SIZE = 64  # textes par appel au fournisseur


@dataclass
class EmbedStats:
    """Compteurs seulement."""

    cached: int = 0
    computed: int = 0


def embed_cached(
    embedder: Embedder,
    store: EmbeddingStore,
    items: list[tuple[str, str]],
    kind: Kind,
    now: datetime | None = None,
) -> tuple[dict[str, list[float]], EmbedStats]:
    """Vecteurs (norme 1) de ``items`` = ``[(owner, texte), ...]``, par propriétaire.

    Un vecteur déjà stocké pour (propriétaire, modèle, empreinte du texte) est relu ; les autres
    sont demandés au fournisseur par lots, normalisés, puis stockés. Les vecteurs rendus sont ceux
    qui sont stockés (``float32``), qu'ils viennent du cache ou d'un calcul.
    """
    when = now or datetime.now(UTC)
    stats = EmbedStats()
    result: dict[str, list[float]] = {}
    missing: list[tuple[str, str, str]] = []  # (owner, texte, empreinte)
    for owner, text in items:
        digest = text_hash(text)
        cached = store.get(owner, embedder.name, digest)
        if cached is not None and len(cached) == embedder.dim:
            result[owner] = cached
            stats.cached += 1
        else:
            missing.append((owner, text, digest))

    for start in range(0, len(missing), BATCH_SIZE):
        batch = missing[start : start + BATCH_SIZE]
        vectors = embedder.embed([text for _, text, _ in batch], kind)
        if len(vectors) != len(batch):
            raise EmbedderError("nombre de vecteurs inattendu")
        for (owner, _, digest), raw in zip(batch, vectors, strict=True):
            if len(raw) != embedder.dim:
                raise EmbedderError("dimension de vecteur inattendue")
            vector = normalize_vector(raw)
            store.put(owner, embedder.name, digest, vector, when)
            # on rend la valeur stockée : même résultat qu'un passage par le cache
            result[owner] = decode_vector(encode_vector(vector), embedder.dim)
            stats.computed += 1
    return result, stats
