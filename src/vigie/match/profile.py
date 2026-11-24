"""Profil de l'utilisateur (section 3 de la spec ``match``) : schéma, normalisation, empreinte.

Le vrai ``profile.yaml`` vit dans le dépôt privé d'exécution ; le dépôt public ne contient que
``config/profile.example.yaml`` (fictif). Les messages d'erreur ne reprennent jamais une valeur
saisie.
"""

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from vigie.radar.models import ContractType, RemoteMode
from vigie.radar.normalize import normalize_spaces, strip_accents

DEFAULT_PROFILE_PATH = Path("profile/profile.yaml")

# Table de synonymes (version 1) : chaque ligne donne une forme canonique, puis ses variantes.
# Elle sert à la correspondance lexicale ; toutes les formes sont déjà normalisées (minuscules,
# sans accents). Une modification de la table incrémente SYNONYMS_VERSION.
SYNONYMS_VERSION = 1
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("machine learning", "ml", "apprentissage automatique"),
    ("large language model", "llm", "llms"),
    ("amazon web services", "aws"),
    ("google cloud platform", "gcp"),
    ("intelligence artificielle", "ia", "ai"),
    ("natural language processing", "nlp", "traitement du langage naturel"),
    ("retrieval augmented generation", "rag"),
    ("kubernetes", "k8s"),
    ("javascript", "js"),
    ("typescript", "ts"),
    ("postgresql", "postgres"),
    ("continuous integration", "ci/cd", "ci cd"),
)
SYNONYMS: dict[str, str] = {variant: group[0] for group in _SYNONYM_GROUPS for variant in group}


class ProfileError(Exception):
    """Profil absent ou invalide. Le message ne contient jamais de valeur saisie."""


def normalize_skill(value: str) -> str:
    """Minuscules, sans accents, espaces normalisés (sans appliquer les synonymes)."""
    return normalize_spaces(strip_accents(value).lower())


def canonical_skill(value: str) -> str:
    """Forme normalisée puis forme canonique selon la table de synonymes."""
    normalized = normalize_skill(value)
    return SYNONYMS.get(normalized, normalized)


def _canonical_list(values: list[str]) -> list[str]:
    """Compétences canoniques, sans vide ni doublon, en conservant l'ordre."""
    seen: dict[str, None] = {}
    for value in values:
        canonical = canonical_skill(value)
        if not canonical:
            raise ValueError("une compétence ne peut pas être vide")
        seen.setdefault(canonical)
    return list(seen)


Skills = Annotated[list[str], AfterValidator(_canonical_list)]
_Month = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillsConfig(_Strict):
    must_have: Skills = Field(default_factory=list)
    nice_to_have: Skills = Field(default_factory=list)
    avoid: Skills = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_contradiction(self) -> Self:
        if set(self.avoid) & (set(self.must_have) | set(self.nice_to_have)):
            raise ValueError("une compétence ne peut pas être à la fois recherchée et à éviter")
        # Une compétence présente dans les deux listes recherchées compte comme must_have.
        self.nice_to_have = [s for s in self.nice_to_have if s not in set(self.must_have)]
        return self


def _month(value: object) -> str | None:
    if value is None:
        return None
    # YAML lit ``2021-01`` comme une chaîne, mais un entier ou une date complète sont refusés.
    if isinstance(value, date) or not isinstance(value, str) or not _Month.match(value):
        raise ValueError("mois attendu au format AAAA-MM")
    return value


def _required_month(value: str) -> str:
    return _month(value) or ""


class Experience(_Strict):
    title: str = Field(min_length=1)
    company: str | None = None
    start: Annotated[str, AfterValidator(_required_month)]
    end: Annotated[str | None, AfterValidator(_month)] = None
    description: str = ""
    skills: Skills = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError("end doit être postérieur ou égal à start")
        return self


class Preferences(_Strict):
    remote: list[RemoteMode] = Field(default_factory=list)
    contract_types: list[ContractType] = Field(default_factory=list)
    min_salary: float | None = Field(default=None, gt=0)
    languages: list[Annotated[str, Field(pattern=r"^[a-z]{2}$")]] = Field(default_factory=list)


class Profile(_Strict):
    summary: str = Field(min_length=1)
    target_roles: list[str] = Field(min_length=1)
    skills: SkillsConfig
    experiences: list[Experience] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)

    @model_validator(mode="after")
    def _clean_text(self) -> Self:
        self.summary = normalize_spaces(self.summary)
        self.target_roles = [normalize_spaces(r) for r in self.target_roles]
        if not self.summary or not all(self.target_roles):
            raise ValueError("summary et target_roles ne peuvent pas être vides")
        for exp in self.experiences:
            exp.title = normalize_spaces(exp.title)
            exp.company = normalize_spaces(exp.company) if exp.company else None
            exp.description = normalize_spaces(exp.description)
        return self

    def normalized_content(self) -> dict[str, Any]:
        """Contenu normalisé, indépendant de la mise en forme du YAML et de l'ordre des listes
        de compétences (l'ordre des expériences et des rôles, lui, est conservé)."""
        data = self.model_dump(mode="json")
        data["skills"] = {key: sorted(value) for key, value in data["skills"].items()}
        for exp in data["experiences"]:
            exp["skills"] = sorted(exp["skills"])
        prefs = data["preferences"]
        prefs["remote"] = sorted(prefs["remote"])
        prefs["contract_types"] = sorted(prefs["contract_types"])
        prefs["languages"] = sorted(prefs["languages"])
        return data

    @property
    def profile_hash(self) -> str:
        """SHA-256 du contenu normalisé : identifie la version du profil utilisée pour un score."""
        payload = json.dumps(
            self.normalized_content(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _format_errors(exc: ValidationError) -> str:
    lines = [
        f"  - {'.'.join(str(part) for part in err['loc']) or '(racine)'} : {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    ]
    return "Profil invalide :\n" + "\n".join(lines)


def load_profile_text(text: str) -> Profile:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (ligne {mark.line + 1}, colonne {mark.column + 1})" if mark else ""
        raise ProfileError(f"YAML invalide{where}") from None
    if not isinstance(data, dict):
        raise ProfileError("Profil invalide : le document doit être un dictionnaire YAML")
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(_format_errors(exc)) from None


def load_profile_file(path: Path) -> Profile:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"Fichier de profil illisible : {exc.strerror}") from None
    return load_profile_text(text)
