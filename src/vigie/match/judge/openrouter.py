"""Juge OpenRouter : ``POST /api/v1/chat/completions`` (format compatible OpenAI).

Un seul fournisseur pour changer de modèle par simple configuration. Chaque requête exige un
fournisseur qui respecte le schéma (``provider.require_parameters``) et exclut ceux qui conservent
les données (``provider.data_collection: "deny"``). Aucun secret, aucune offre ni aucune réponse
du serveur dans les messages d'erreur ; la clé n'est jamais journalisée.
"""

import json
import logging
from typing import Any

from pydantic import SecretStr, ValidationError

from vigie.match.config import LlmConfig
from vigie.match.judge.base import (
    Assessment,
    JudgeInvalidResponse,
    JudgeTruncatedResponse,
    JudgeUnavailable,
    OfferView,
    Usage,
    assessment_schema,
)
from vigie.match.judge.prompt import messages
from vigie.match.profile import Profile
from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.settings import Settings

logger = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/v1/chat/completions"
SCHEMA_NAME = "assessment"
# Paramètres exigés à chaque requête : nommés dans le message si le modèle ne les permet pas.
REQUIRED_PARAMETERS = (
    "response_format (json_schema strict)",
    "provider.require_parameters",
    "provider.data_collection=deny",
)


class OpenRouterJudge:
    def __init__(
        self,
        model: str,
        api_key: SecretStr,
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
        client: SourceHttpClient | None = None,
    ) -> None:
        self._model = model
        self._key = api_key
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._client = client or SourceHttpClient("openrouter")
        self.name = f"openrouter:{model}"
        self.usage = Usage()
        self.last_usage = Usage()

    def _body(self, profile: Profile, offer: OfferView) -> dict[str, Any]:
        return {
            "model": self._model,
            "messages": messages(profile, offer),
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": assessment_schema()},
            },
            "provider": {"require_parameters": True, "data_collection": "deny"},
        }

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        try:
            response = self._client.request("POST", URL, json=body, headers=headers)
            payload = response.json()
        except SourceHttpError as exc:
            raise _unavailable(exc.status_code) from None
        except ValueError:
            raise JudgeInvalidResponse("réponse OpenRouter illisible (JSON attendu)") from None
        if not isinstance(payload, dict):
            raise JudgeInvalidResponse("réponse OpenRouter inattendue")
        return payload

    def _record_usage(self, payload: dict[str, Any]) -> None:
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        self.last_usage = Usage(
            calls=1,
            prompt_tokens=prompt if isinstance(prompt, int) and prompt >= 0 else 0,
            completion_tokens=completion if isinstance(completion, int) and completion >= 0 else 0,
        )
        self.usage.calls += 1
        self.usage.prompt_tokens += self.last_usage.prompt_tokens
        self.usage.completion_tokens += self.last_usage.completion_tokens

    def assess(self, profile: Profile, offer: OfferView) -> Assessment:
        self.last_usage = Usage()
        payload = self._post(self._body(profile, offer))
        self._record_usage(payload)
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise JudgeInvalidResponse("réponse OpenRouter inattendue (choices absent)") from None
        if choice.get("finish_reason") == "length":
            # Modèle de raisonnement ou réponse trop longue : un nouvel essai n'y changerait rien.
            raise JudgeTruncatedResponse("réponse tronquée : augmenter match.llm.max_tokens")
        if not isinstance(content, str) or not content.strip():
            raise JudgeInvalidResponse("réponse OpenRouter vide")
        try:
            return Assessment.model_validate(json.loads(content))
        except (ValueError, ValidationError):
            raise JudgeInvalidResponse("réponse non conforme au schéma") from None


def _unavailable(status: int | None) -> JudgeUnavailable:
    """Message fixe selon le code HTTP : jamais le corps de la réponse, jamais l'offre."""
    if status in (401, 403):
        return JudgeUnavailable(f"OpenRouter a refusé la clé OPENROUTER_API_KEY (HTTP {status})")
    if status == 402:
        return JudgeUnavailable("OpenRouter : crédits insuffisants sur le compte (HTTP 402)")
    if status == 429:
        return JudgeUnavailable("OpenRouter : limite de débit atteinte (HTTP 429)")
    if status is not None:
        return JudgeUnavailable(
            f"OpenRouter n'a pas pu traiter la requête (HTTP {status}) : le modèle ne permet "
            f"peut-être pas les paramètres exigés ({', '.join(REQUIRED_PARAMETERS)}) ; "
            "choisir un autre modèle"
        )
    return JudgeUnavailable("OpenRouter injoignable (erreur réseau)")


def create(config: LlmConfig, settings: Settings) -> OpenRouterJudge:
    if config.model is None:
        raise JudgeUnavailable("match.llm.model est obligatoire")
    if settings.openrouter_api_key is None:
        raise JudgeUnavailable("variable d'environnement manquante : OPENROUTER_API_KEY")
    return OpenRouterJudge(
        config.model,
        settings.openrouter_api_key,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
