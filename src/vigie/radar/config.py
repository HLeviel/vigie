"""Chargement et validation de la configuration YAML de radar (section 8 de la spec)."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vigie.radar.models import SearchQuery
from vigie.settings import Settings

DEFAULT_CONFIG_PATH = Path("config/radar.yaml")


class ConfigError(Exception):
    """Configuration absente ou invalide. Le message ne contient jamais de valeur saisie."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceToggle(_Strict):
    enabled: bool = False


class SourcesConfig(_Strict):
    adzuna: SourceToggle = Field(default_factory=SourceToggle)
    ats: SourceToggle = Field(default_factory=SourceToggle)
    email_alerts: SourceToggle = Field(default_factory=SourceToggle)
    france_travail: SourceToggle = Field(default_factory=SourceToggle)

    def enabled(self) -> list[str]:
        return [name for name, toggle in self if toggle.enabled]


class AtsCompany(_Strict):
    name: str = Field(min_length=1)
    platform: Literal["greenhouse", "lever", "ashby", "recruitee", "smartrecruiters", "workable"]
    token: str = Field(min_length=1)


class EmailAlertsConfig(_Strict):
    allowed_senders: dict[str, list[str]] = Field(default_factory=dict)


class FiltersConfig(_Strict):
    exclude_title: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    require_any: list[str] = Field(default_factory=list)


class DigestConfig(_Strict):
    max_items: int = Field(default=50, gt=0)
    format: Literal["html", "markdown"] = "html"


class RadarConfig(_Strict):
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    ats_companies: list[AtsCompany] = Field(default_factory=list)
    email_alerts: EmailAlertsConfig = Field(default_factory=EmailAlertsConfig)
    searches: list[SearchQuery] = Field(min_length=1)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)


def _format_errors(exc: ValidationError) -> str:
    # Chemin et message seulement : jamais la valeur saisie (les critères sont confidentiels).
    lines = [
        f"  - {'.'.join(str(part) for part in err['loc']) or '(racine)'} : {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    ]
    return "Configuration invalide :\n" + "\n".join(lines)


def load_config_text(text: str) -> RadarConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (ligne {mark.line + 1}, colonne {mark.column + 1})" if mark else ""
        raise ConfigError(f"YAML invalide{where}") from None
    if not isinstance(data, dict):
        raise ConfigError("Configuration invalide : le document doit être un dictionnaire YAML")
    try:
        return RadarConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None


def load_config_file(path: Path) -> RadarConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Fichier de configuration illisible : {exc.strerror}") from None
    return load_config_text(text)


def resolve_config(path: Path | None, settings: Settings) -> RadarConfig:
    """Ordre : ``--config``, secret ``VIGIE_RADAR_CONFIG``, puis ``config/radar.yaml``."""
    if path is not None:
        return load_config_file(path)
    if settings.vigie_radar_config is not None:
        return load_config_text(settings.vigie_radar_config.get_secret_value())
    if DEFAULT_CONFIG_PATH.exists():
        return load_config_file(DEFAULT_CONFIG_PATH)
    raise ConfigError(
        "Aucune configuration : utiliser --config, la variable VIGIE_RADAR_CONFIG "
        f"ou le fichier {DEFAULT_CONFIG_PATH}"
    )
