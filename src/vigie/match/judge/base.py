"""Interface commune des juges LLM (section 5 de la spec ``match``) et sortie structurée.

Le texte d'une offre est une entrée non fiable : il ne peut jamais modifier le comportement du
LLM. Le juge ne reçoit que des **données** (profil et offre nettoyés) et ne peut répondre qu'au
schéma ``Assessment`` : aucun outil, aucune navigation, aucune URL suivie.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vigie.match.config import LlmConfig
from vigie.match.profile import Profile
from vigie.match.text import clean_text, truncate
from vigie.radar.models import Offer
from vigie.settings import Settings

Recommendation = Literal["postuler", "à voir", "passer"]
Sentence = Annotated[str, Field(min_length=1, max_length=300)]

# Longueurs bornées avant envoi (caractères).
MAX_TITLE = 300
MAX_FIELD = 200
MAX_DESCRIPTION = 6000
MAX_PROFILE_TEXT = 1500


class Assessment(BaseModel):
    """Évaluation d'une offre par le LLM : sortie structurée et validée localement."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    recommendation: Recommendation
    strengths: list[Sentence] = Field(max_length=3)
    gaps: list[Sentence] = Field(max_length=3)
    risks: list[Sentence] = Field(max_length=3)


# Mots-clés du schéma que les modes stricts des fournisseurs ne gèrent pas tous : ils sont retirés
# de la requête ; les bornes sont vérifiées localement par ``Assessment``.
_UNSUPPORTED = {
    "title", "default", "description", "minimum", "maximum", "minItems", "maxItems",
    "minLength", "maxLength", "format", "pattern", "$defs",
}  # fmt: skip


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned = {k: _strictify(v) for k, v in node.items() if k not in _UNSUPPORTED}
        if cleaned.get("type") == "object" and "properties" in cleaned:
            cleaned["required"] = list(cleaned["properties"])
            cleaned["additionalProperties"] = False
        return cleaned
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    return node


def assessment_schema() -> dict[str, Any]:
    """Schéma JSON strict généré depuis le modèle pydantic ``Assessment``."""
    return dict(_strictify(Assessment.model_json_schema()))


@dataclass
class Usage:
    """Jetons consommés (entrée et sortie) et nombre d'appels : compteurs seulement."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class JudgeError(Exception):
    """Échec du juge. Le message est fixe : jamais un texte d'offre, de profil ni de réponse."""


class JudgeUnavailable(JudgeError):
    """Échec qui vaut pour toutes les offres de l'exécution (clé refusée, paramètre non géré,
    crédits, réseau) : l'étage LLM est ignoré, le score hybride est conservé."""


class JudgeInvalidResponse(JudgeError):
    """Réponse invalide pour une offre : elle garde son score hybride."""

    retryable = True  # une nouvelle tentative au plus (section 4)


class JudgeTruncatedResponse(JudgeInvalidResponse):
    """Réponse coupée par ``max_tokens`` : un nouvel essai donnerait la même chose."""

    retryable = False


@dataclass(frozen=True)
class OfferView:
    """Ce que le juge voit d'une offre : texte nettoyé et borné, sans lien ni identifiant."""

    title: str
    company: str
    location: str
    contract: str
    remote: str
    salary: str
    description: str

    @classmethod
    def from_offer(cls, offer: Offer) -> "OfferView":
        return cls(
            title=truncate(clean_text(offer.title), MAX_TITLE),
            company=truncate(clean_text(offer.company or ""), MAX_FIELD),
            location=truncate(clean_text(offer.location_label or ""), MAX_FIELD),
            contract=offer.contract_type.value,
            remote=offer.remote.value,
            salary=truncate(clean_text(offer.salary_label or ""), MAX_FIELD),
            description=truncate(clean_text(offer.description), MAX_DESCRIPTION),
        )


class Judge(Protocol):
    name: str  # ex. « openrouter:<modèle> »
    usage: Usage  # cumul des appels de ce juge (réponses invalides comprises)
    last_usage: Usage  # dernier appel seulement

    def assess(self, profile: Profile, offer: OfferView) -> Assessment: ...


JudgeFactory = Callable[[LlmConfig, Settings], Judge]
# provider -> fabrique(configuration de l'étage, réglages). Inscription : ``judge/__init__.py``.
REGISTRY: dict[str, JudgeFactory] = {}


def build_judge(config: LlmConfig, settings: Settings, model: str | None = None) -> Judge:
    """Juge du fournisseur configuré ; ``model`` remplace ``config.model`` (évaluation)."""
    import vigie.match.judge  # noqa: F401  (inscrit les fournisseurs)

    effective = config.model_copy(update={"model": model}) if model else config
    try:
        factory = REGISTRY[effective.provider]
    except KeyError:
        raise JudgeUnavailable(f"fournisseur LLM indisponible : {effective.provider}") from None
    return factory(effective, settings)
