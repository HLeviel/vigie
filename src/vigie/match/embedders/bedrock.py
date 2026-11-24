"""Fournisseur ``bedrock`` : embeddings AWS Bedrock, authentification par OIDC (aucun secret).

Seul le format de requête de **Amazon Titan Text Embeddings V2** est pris en charge (documenté par
AWS : ``inputText``, ``dimensions``, ``normalize`` ; réponse ``embedding``). Les autres familles de
modèles ont d'autres formats, non vérifiés : elles sont refusées plutôt que devinées.

Aucun nom de modèle, texte ou message d'erreur AWS dans les exceptions : le code d'erreur AWS
(``AccessDeniedException``…) seulement.
"""

import json
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from vigie.match.embedders.base import EmbedderError, Kind

TITAN_V2_PREFIX = "amazon.titan-embed-text-v2"
# Dimension demandée explicitement : 1024 est une des valeurs de l'API, non vérifiée en réel.
DIMENSIONS = 1024


class BedrockEmbedder:
    def __init__(self, model: str, client: Any | None = None) -> None:
        if not model.startswith(TITAN_V2_PREFIX):
            raise EmbedderError(
                "modèle Bedrock non pris en charge : seul Amazon Titan Text Embeddings V2 "
                f"({TITAN_V2_PREFIX}…) l'est pour l'instant"
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
        # Titan ne distingue pas requête et document : ``kind`` est sans effet ici.
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text, "dimensions": self.dim, "normalize": True})
        try:
            response = self._runtime().invoke_model(
                modelId=self._model,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(response["body"].read())
            vector = payload["embedding"]
            return [float(x) for x in vector]
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "inconnu"))
            raise EmbedderError(f"appel Bedrock refusé ou en échec ({code})") from None
        except BotoCoreError:
            raise EmbedderError("appel Bedrock impossible (réseau ou configuration)") from None
        except (KeyError, TypeError, ValueError):
            raise EmbedderError("réponse Bedrock inattendue") from None
