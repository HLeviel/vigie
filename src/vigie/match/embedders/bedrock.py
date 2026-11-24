"""Fournisseur ``bedrock`` : embeddings AWS Bedrock, authentification par OIDC (aucun secret).

Deux familles de modèles, dont le format de requête vient de la documentation AWS :
- **Amazon Titan Text Embeddings V2** : ``inputText``, ``dimensions``, ``normalize`` ; réponse
  ``embedding`` ; un texte par appel ;
- **Cohere Embed Multilingual v3** : ``texts``, ``input_type`` (``search_query`` ou
  ``search_document``) ; réponse ``embeddings`` ; plusieurs textes par appel.
Les autres familles ont d'autres formats, non vérifiés : elles sont refusées plutôt que devinées.

Aucun nom de modèle, texte ou message d'erreur AWS dans les exceptions : le code d'erreur AWS
(``AccessDeniedException``…) seulement.
"""

import json
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from vigie.match.embedders.base import EmbedderError, Kind

TITAN_V2_PREFIX = "amazon.titan-embed-text-v2"
COHERE_V3_PREFIX = "cohere.embed-multilingual-v3"
# Dimensions : Titan, demandée explicitement (valeur de l'API, non vérifiée en réel) ; Cohere v3,
# 1024 selon des sources AWS (non vérifié en réel). La longueur de chaque réponse est contrôlée.
DIMENSIONS = 1024
COHERE_MAX_TEXTS = 96  # textes par appel (limite non vérifiée sur Bedrock) ; l'appelant envoie 64


class BedrockEmbedder:
    def __init__(self, model: str, client: Any | None = None) -> None:
        if model.startswith(TITAN_V2_PREFIX):
            self._family = "titan"
        elif model.startswith(COHERE_V3_PREFIX):
            self._family = "cohere"
        else:
            raise EmbedderError(
                "modèle Bedrock non pris en charge : seuls Amazon Titan Text Embeddings V2 "
                f"({TITAN_V2_PREFIX}…) et Cohere Embed Multilingual v3 ({COHERE_V3_PREFIX}…) "
                "le sont pour l'instant"
            )
        self._model = model
        self.name = f"bedrock:{model}"
        self.dim = DIMENSIONS
        self._client = client

    def _runtime(self) -> Any:
        if self._client is None:
            try:
                import boto3  # import tardif : inutile hors des exécutions avec Bedrock
                from botocore.config import Config

                self._client = boto3.client(
                    "bedrock-runtime",
                    config=Config(
                        retries={"max_attempts": 3, "mode": "standard"},
                        connect_timeout=10,
                        read_timeout=30,
                    ),
                )
            except BotoCoreError:
                raise EmbedderError("client Bedrock impossible à créer (région ?)") from None
        return self._client

    def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        if self._family == "titan":
            # Titan ne distingue pas requête et document : ``kind`` est sans effet.
            return [self._embed_titan(text) for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), COHERE_MAX_TEXTS):
            vectors.extend(self._embed_cohere(texts[start : start + COHERE_MAX_TEXTS], kind))
        return vectors

    def _embed_titan(self, text: str) -> list[float]:
        body = {"inputText": text, "dimensions": self.dim, "normalize": True}
        try:
            return [float(x) for x in self._invoke(body)["embedding"]]
        except (KeyError, TypeError, ValueError):
            raise EmbedderError("réponse Bedrock inattendue") from None

    def _embed_cohere(self, texts: list[str], kind: Kind) -> list[list[float]]:
        body = {
            "texts": texts,
            "input_type": "search_query" if kind == "query" else "search_document",
            # texte trop long : coupé en fin plutôt que refusé (paramètre non vérifié en réel)
            "truncate": "END",
        }
        try:
            embeddings = self._invoke(body)["embeddings"]
            if not isinstance(embeddings, list) or len(embeddings) != len(texts):
                raise ValueError
            return [[float(x) for x in vector] for vector in embeddings]
        except (KeyError, TypeError, ValueError):
            raise EmbedderError("réponse Bedrock inattendue") from None

    def _invoke(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._runtime().invoke_model(
                modelId=self._model,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(response["body"].read())
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "inconnu"))
            raise EmbedderError(f"appel Bedrock refusé ou en échec ({code})") from None
        except BotoCoreError:
            raise EmbedderError("appel Bedrock impossible (réseau ou configuration)") from None
        except (KeyError, TypeError, ValueError):
            raise EmbedderError("réponse Bedrock inattendue") from None
