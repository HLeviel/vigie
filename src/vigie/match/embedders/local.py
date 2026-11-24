"""Fournisseur ``local`` : ``sentence-transformers`` sur CPU (extra optionnel ``vigie[local]``).

Le modèle est téléchargé une fois puis mis en cache par la bibliothèque. Aucun texte n'est envoyé
à un service tiers. Importation tardive : sans ``vigie[local]``, seul ce fournisseur manque.
"""

import importlib
from collections.abc import Callable
from typing import Any

from vigie.match.embedders.base import EmbedderError, Kind

ModelLoader = Callable[[str], Any]

# Les modèles de la famille E5 attendent « query: » / « passage: » devant chaque texte (fiche du
# modèle). Les autres familles (BGE…) ont leurs propres règles, non vérifiées : aucun préfixe.
_E5_PREFIX: dict[Kind, str] = {"query": "query: ", "document": "passage: "}


def load_sentence_transformer(model: str) -> Any:
    try:
        module = importlib.import_module("sentence_transformers")
    except ImportError:
        raise EmbedderError(
            "fournisseur local indisponible : installer l'extra « local » (uv sync --extra local)"
        ) from None
    try:
        return module.SentenceTransformer(model)
    except Exception:
        raise EmbedderError("modèle local introuvable ou illisible") from None


class LocalEmbedder:
    def __init__(self, model: str, loader: ModelLoader = load_sentence_transformer) -> None:
        self._model = loader(model)
        self.name = f"local:{model}"
        dim = self._model.get_sentence_embedding_dimension()
        if not isinstance(dim, int) or dim <= 0:
            raise EmbedderError("dimension du modèle local inconnue")
        self.dim = dim
        self._prefixes = _E5_PREFIX if "e5" in model.lower() else {"query": "", "document": ""}

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        prefix = self._prefixes[kind]
        try:
            raw = self._model.encode(
                [prefix + text for text in texts],
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [[float(x) for x in vector] for vector in raw]
        except Exception:
            raise EmbedderError("échec du calcul d'embeddings local") from None
