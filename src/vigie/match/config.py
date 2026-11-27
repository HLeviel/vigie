"""Section ``match`` de ``config/radar.yaml`` (section 6 de la spec ``match``).

Schéma strict comme le reste de la configuration. Aucun modèle d'embedding par défaut : tant que
l'évaluation (M2) n'en a pas fixé un, ``embeddings.model`` est obligatoire.
"""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmbeddingsConfig(_Strict):
    provider: Literal["local", "bedrock"]
    model: str = Field(min_length=1)


class WeightsConfig(_Strict):
    semantic: float = Field(default=0.7, ge=0)
    lexical: float = Field(default=0.3, ge=0)

    @model_validator(mode="after")
    def _not_all_zero(self) -> Self:
        if self.semantic + self.lexical <= 0:
            raise ValueError("au moins un poids doit être strictement positif")
        return self


class PenaltiesConfig(_Strict):
    remote: float = Field(default=15, ge=0, le=100)
    salary: float = Field(default=15, ge=0, le=100)
    avoid: float = Field(default=20, ge=0, le=100)


# Identifiant OpenRouter : « fournisseur/modèle » (par exemple « anthropic/… » ou « openai/… »).
# Aucun identifiant n'est écrit dans le code : il se change dans la configuration.
_MODEL_ID = r"^[^/\s]+/\S+$"


class LlmConfig(_Strict):
    """Étage LLM (lot M3) : OpenRouter, modèle choisi par la configuration."""

    enabled: bool = False
    provider: Literal["openrouter"] = "openrouter"
    model: str | None = Field(default=None, pattern=_MODEL_ID)
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int = Field(default=800, gt=0)  # par appel
    top_n: int = Field(default=10, gt=0)
    max_calls_per_run: int = Field(default=15, gt=0)
    # Budget de jetons (entrée + sortie) par exécution : l'étage s'arrête proprement au-delà.
    max_total_tokens_per_run: int = Field(default=60_000, gt=0)

    @model_validator(mode="after")
    def _model_required_when_enabled(self) -> Self:
        if self.enabled and self.model is None:
            raise ValueError("model est obligatoire quand llm.enabled est vrai")
        return self


class MatchDigestConfig(_Strict):
    min_score: float = Field(default=40, ge=0, le=100)


class EvaluationConfig(_Strict):
    """Modèles d'embedding candidats comparés par ``vigie match eval`` (2 à 3 recommandés).

    Le modèle de ``embeddings`` est toujours comparé ; cette liste s'y ajoute.
    """

    candidates: list[EmbeddingsConfig] = Field(default_factory=list, max_length=5)
    # Modèles OpenRouter comparés pour les variantes `hybrid+llm:<modèle>` (au moins deux
    # recommandés), en plus de `llm.model`.
    llm_models: list[Annotated[str, Field(pattern=_MODEL_ID)]] = Field(
        default_factory=list, max_length=5
    )


class MatchConfig(_Strict):
    enabled: bool = False
    profile: Path = Path("profile/profile.yaml")
    embeddings: EmbeddingsConfig
    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    penalties: PenaltiesConfig = Field(default_factory=PenaltiesConfig)
    top_k_profile: int = Field(default=3, gt=0)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    digest: MatchDigestConfig = Field(default_factory=MatchDigestConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
