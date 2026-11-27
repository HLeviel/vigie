"""Outils des tests de l'étage LLM : faux juge scripté et faux serveur OpenRouter (respx)."""

import json
from typing import Any

import httpx
from pydantic import SecretStr

from vigie.match.judge.base import (
    Assessment,
    JudgeInvalidResponse,
    JudgeTruncatedResponse,
    OfferView,
    Usage,
)
from vigie.match.judge.openrouter import OpenRouterJudge
from vigie.match.profile import Profile
from vigie.radar.http import SourceHttpClient

KEY = "sk-or-cle-de-test-secrete"
MODEL = "fournisseur-fictif/modele-un"
OTHER_MODEL = "fournisseur-fictif/modele-deux"


def assessment(score: int = 80, recommendation: str = "postuler") -> Assessment:
    return Assessment.model_validate(
        {
            "score": score,
            "recommendation": recommendation,
            "strengths": ["Pile Python et cloud proche du profil."],
            "gaps": ["Spark non mentionné dans le profil."],
            "risks": [],
        }
    )


class FakeJudge:
    """Réponses scriptées (évaluation ou exception, une par appel) ; compte les jetons."""

    def __init__(
        self,
        name: str = f"openrouter:{MODEL}",
        script: list[Assessment | Exception] | None = None,
        default: Assessment | None = None,
        tokens: tuple[int, int] = (500, 100),
    ) -> None:
        self.name = name
        self.script = list(script or [])
        self.default = default or assessment()
        self.tokens = tokens
        self.usage = Usage()
        self.last_usage = Usage()
        self.seen: list[OfferView] = []

    def assess(self, profile: Profile, offer: OfferView) -> Assessment:
        self.seen.append(offer)
        step = self.script.pop(0) if self.script else self.default
        unavailable = isinstance(step, Exception) and not isinstance(
            step, JudgeInvalidResponse | JudgeTruncatedResponse
        )
        used = Usage(0, 0, 0) if unavailable else Usage(1, *self.tokens)
        self.last_usage = used
        self.usage = Usage(
            self.usage.calls + used.calls,
            self.usage.prompt_tokens + used.prompt_tokens,
            self.usage.completion_tokens + used.completion_tokens,
        )
        if isinstance(step, Exception):
            raise step
        return step


def completion(
    content: Any, usage: dict[str, int] | None = None, finish_reason: str = "stop"
) -> dict[str, Any]:
    """Corps d'une réponse OpenRouter (format compatible OpenAI)."""
    body = content if isinstance(content, str) or content is None else json.dumps(content)
    return {
        "id": "gen-test",
        "model": MODEL,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": body},
            }
        ],
        "usage": usage or {"prompt_tokens": 420, "completion_tokens": 85, "total_tokens": 505},
    }


def valid_content() -> dict[str, Any]:
    return assessment().model_dump()


def openrouter_judge(model: str = MODEL, **kwargs: Any) -> OpenRouterJudge:
    client = SourceHttpClient("openrouter", min_interval=0, sleep=lambda _s: None)
    return OpenRouterJudge(model, SecretStr(KEY), client=client, **kwargs)


def ok(body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(200, json=body or completion(valid_content()))
