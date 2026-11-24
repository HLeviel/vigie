"""Préparation des textes à plonger dans l'espace des embeddings (section 4 de la spec ``match``).

Le texte d'une offre est une entrée non fiable : il est seulement nettoyé et tronqué ici, jamais
interprété.
"""

import hashlib
from dataclasses import dataclass
from typing import Literal

from vigie.match.profile import Profile
from vigie.radar.models import Offer
from vigie.radar.normalize import normalize_spaces, strip_invisible

# Limite de longueur provisoire, en caractères : la vraie limite dépend du modèle d'embedding, qui
# sera choisi par l'évaluation (M2). Elle est passée explicitement quand elle est connue.
DEFAULT_MAX_CHARS = 2000

# Une description plus courte rend le score peu fiable (section 4 : confiance « faible »).
LOW_CONFIDENCE_CHARS = 300

Confidence = Literal["faible", "normale"]


def clean_text(text: str) -> str:
    """Sans caractères invisibles, bidirectionnels ni de contrôle ; espaces normalisés."""
    return normalize_spaces(strip_invisible(text))


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        raise ValueError("max_chars doit être strictement positif")
    return text[:max_chars].rstrip()


def text_hash(text: str) -> str:
    """Empreinte du texte exact envoyé au modèle : sert de clé de cache."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def offer_text(offer: Offer, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """``titre + entreprise + description``, nettoyé puis tronqué."""
    parts = [offer.title, offer.company or "", offer.description]
    text = clean_text(" . ".join(part for part in (clean_text(p) for p in parts) if part))
    return truncate(text, max_chars)


def offer_confidence(offer: Offer) -> Confidence:
    """``faible`` si la description fait moins de 300 caractères (une fois nettoyée)."""
    return "faible" if len(clean_text(offer.description)) < LOW_CONFIDENCE_CHARS else "normale"


def offer_owner(offer: Offer) -> str:
    return f"offer:{offer.uid}"


@dataclass(frozen=True)
class ProfileText:
    """Un texte du profil et son propriétaire pour le cache des embeddings."""

    owner: str
    text: str


def profile_texts(profile: Profile, max_chars: int = DEFAULT_MAX_CHARS) -> list[ProfileText]:
    """Textes du profil : ``summary``, chaque expérience, une ligne par rôle cible.

    Le propriétaire contient l'empreinte du profil : un changement de profil donne d'autres
    propriétaires, donc invalide les embeddings du profil sans toucher à ceux des offres.
    """
    texts = [profile.summary]
    for experience in profile.experiences:
        parts = [experience.title, experience.description, ", ".join(experience.skills)]
        texts.append(" . ".join(part for part in parts if part))
    texts.extend(profile.target_roles)
    prefix = f"profile:{profile.profile_hash}"
    return [
        ProfileText(owner=f"{prefix}:{n}", text=truncate(clean_text(text), max_chars))
        for n, text in enumerate(texts)
    ]
