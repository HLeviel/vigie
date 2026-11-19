"""Modèles de données de radar : Offer, SearchQuery, RunReport."""

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    HttpUrl,
    computed_field,
    field_validator,
    model_validator,
)

from vigie.radar.normalize import (
    compute_dedup_key,
    is_suspicious,
    normalize_spaces,
    parse_remote,
)

logger = logging.getLogger(__name__)


class ContractType(StrEnum):
    CDI = "CDI"
    CDD = "CDD"
    INTERIM = "INTERIM"
    FREELANCE = "FREELANCE"
    STAGE = "STAGE"
    ALTERNANCE = "ALTERNANCE"
    AUTRE = "AUTRE"


class RemoteMode(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class Offer(BaseModel):
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str
    company: str | None = None
    location_label: str | None = None
    postal_code: str | None = None
    department: str | None = None
    contract_type: ContractType = ContractType.AUTRE
    remote: RemoteMode = RemoteMode.UNKNOWN
    salary_min: float | None = None
    salary_max: float | None = None
    salary_label: str | None = None
    description: str = ""
    url: HttpUrl
    published_at: AwareDatetime
    fetched_at: AwareDatetime
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_required(cls, value: str) -> str:
        value = normalize_spaces(value)
        if not value:
            raise ValueError("le titre ne peut pas être vide")
        return value

    @field_validator("company", "location_label")
    @classmethod
    def _clean_optional(cls, value: str | None) -> str | None:
        return (normalize_spaces(value) or None) if value else None

    @field_validator("published_at", "fetched_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _to_utc(value)

    @model_validator(mode="after")
    def _infer_remote(self) -> Self:
        """Déduit ``remote`` du titre et de la description s'il n'est pas fourni."""
        if "remote" not in self.model_fields_set:
            self.remote = RemoteMode(parse_remote(self.title, self.description))
        return self

    @model_validator(mode="after")
    def _salary_order(self) -> Self:
        low, high = self.salary_min, self.salary_max
        if low is not None and high is not None and low > high:
            raise ValueError("salary_min doit être inférieur ou égal à salary_max")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uid(self) -> str:
        return f"{self.source}:{self.source_id}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dedup_key(self) -> str:
        return compute_dedup_key(self.company, self.title, self.department)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def suspicious(self) -> bool:
        """Titre ou description contenant des caractères invisibles ou un motif d'injection."""
        return is_suspicious(self.title) or is_suspicious(self.description)


class SearchQuery(BaseModel):
    """Une recherche : mots-clés, zone, types de contrat, ancienneté maximale."""

    name: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    commune: str | None = None
    distance_km: int | None = Field(default=None, gt=0)
    contract_types: list[ContractType] = Field(default_factory=list)
    max_age_days: int = Field(default=7, gt=0)

    @model_validator(mode="after")
    def _zone(self) -> Self:
        if self.departments and self.commune:
            raise ValueError("zone : départements ou commune + rayon, pas les deux")
        if self.commune and self.distance_km is None:
            raise ValueError("zone : une commune requiert distance_km")
        if self.distance_km is not None and not self.commune:
            raise ValueError("zone : distance_km requiert une commune")
        if not self.departments and not self.commune:
            if not self.keywords:
                raise ValueError("une recherche sans zone exige au moins un mot-clé")
            # Message volontairement sans nom ni critère : les logs Actions sont publics.
            logger.warning("Une recherche n'a pas de zone géographique : toute la France.")
        return self


class SourceReport(BaseModel):
    source: str
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    filtered: int = 0
    errors: int = 0
    duration_ms: int = 0
    error_message: str | None = None


class RunReport(BaseModel):
    run_id: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    sources: list[SourceReport] = Field(default_factory=list)
