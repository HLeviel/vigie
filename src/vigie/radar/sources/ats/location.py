"""Éligibilité géographique d'une offre ATS : en France, ou en télétravail compatible.

Les API ATS renvoient toutes les offres de l'entreprise dans le monde (section 6 de la spec).
Heuristique de lecture : un lieu texte, éventuellement un code pays, un indicateur de télétravail.
"""

import re
from dataclasses import dataclass

from vigie.radar.normalize import normalize_for_key

EUROPE_CODES = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE "
    "IS LI NO CH GB".split()
)
_EUROPE_NAMES = (
    "europe european union eu emea france french germany spain italy portugal belgium "
    "netherlands ireland united kingdom uk switzerland austria poland sweden denmark finland "
    "norway luxembourg"
)
_EUROPE_MARKERS = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(n) for n in _EUROPE_NAMES.split()) + r")(?![a-z0-9])"
)
# « united kingdom » contient deux mots : on les teste aussi en expression.
_EUROPE_PHRASES = ("united kingdom", "european union")
_FRENCH_CITIES = frozenset(
    "paris lyon marseille toulouse nice nantes montpellier strasbourg bordeaux lille rennes "
    "reims grenoble dijon angers nimes toulon brest tours amiens limoges metz besancon perpignan "
    "orleans rouen caen nancy clermont-ferrand saint-etienne villeurbanne boulogne-billancourt "
    "nanterre courbevoie puteaux la-defense issy-les-moulineaux levallois-perret "
    "sophia-antipolis aix-en-provence cannes antibes pau la-rochelle poitiers valence "
    "annecy chambery avignon le-havre le-mans saint-denis montrouge vincennes".split()
)
_REMOTE_WORDS = re.compile(
    r"(?<![a-z0-9])(?:remote|remotely|teletravail|distance|anywhere|worldwide|global|hybrid|"
    r"home|office|from|or|and|ou|et|work|working|only)(?![a-z0-9])"
)
_IS_REMOTE = re.compile(
    r"(?<![a-z0-9])(?:remote|remotely|teletravail|anywhere|worldwide)(?![a-z0-9])"
)


@dataclass(frozen=True)
class Place:
    """Lieu d'une offre telle que la décrit l'API."""

    text: str = ""  # libellé(s) de lieu bruts
    countries: tuple[str, ...] = ()  # codes pays ISO-2 s'ils sont fournis
    remote: bool = False  # indicateur structuré de télétravail


def _norm(text: str) -> str:
    return normalize_for_key(text.replace("/", " ").replace(",", " "))


def is_in_france(place: Place) -> bool:
    if any(code.upper() == "FR" for code in place.countries):
        return True
    text = _norm(place.text)
    if re.search(r"(?<![a-z0-9])france(?![a-z0-9])", text):
        return True
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(city.replace('-', ' '))}(?![a-z0-9])", text)
        for city in _FRENCH_CITIES
    )


def is_remote_compatible(place: Place) -> bool:
    """Télétravail, à moins que le lieu ne le réserve à une zone hors d'Europe."""
    text = _norm(place.text)
    if not (place.remote or _IS_REMOTE.search(text)):
        return False
    if place.countries and not any(code.upper() in EUROPE_CODES for code in place.countries):
        return False
    remainder = _REMOTE_WORDS.sub(" ", text).strip()
    if not remainder:
        return True  # « Remote » seul : aucune restriction géographique
    return bool(_EUROPE_MARKERS.search(text) or any(p in text for p in _EUROPE_PHRASES))


def is_eligible(place: Place) -> bool:
    return is_in_france(place) or is_remote_compatible(place)
