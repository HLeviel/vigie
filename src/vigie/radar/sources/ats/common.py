"""Outils communs aux adaptateurs ATS."""

import html
from datetime import UTC, datetime

from vigie.radar.models import ContractType
from vigie.radar.normalize import contract_from_title, html_to_text


def parse_iso(value: str) -> datetime:
    """Date ISO 8601 avec fuseau (``Z`` accepté)."""
    return datetime.fromisoformat(value)


def parse_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC)


def parse_utc_text(value: str) -> datetime:
    """Format ``2026-11-18 16:23:32 UTC`` (Recruitee)."""
    return datetime.fromisoformat(value.removesuffix(" UTC").strip()).replace(tzinfo=UTC)


def unescape_html_to_text(value: str) -> str:
    """HTML échappé en entités (Greenhouse : ``&lt;p&gt;``) vers texte brut."""
    return html_to_text(html.unescape(value))


def contract_or_title(code_value: ContractType | None, title: str) -> ContractType:
    """Un titre explicite (stage, alternance, intérim, freelance) l'emporte sur le code ATS."""
    from_title = contract_from_title(title)
    if from_title:
        return ContractType(from_title)
    return code_value or ContractType.AUTRE


def join_text(*parts: str) -> str:
    return "\n".join(part.strip() for part in parts if part and part.strip())
