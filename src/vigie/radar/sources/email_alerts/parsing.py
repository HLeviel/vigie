"""Extraction d'offres depuis le HTML d'un email d'alerte.

ATTENTION : heuristique générique, écrite **sans exemple réel** d'alerte (voir
``docs/decisions.md``). Elle cherche les liens d'offres propres à chaque expéditeur et lit le texte
voisin ; l'ordre « entreprise, puis lieu » et les motifs de liens sont des hypothèses à ajuster sur
de vrais emails.

Le contenu d'un email est une entrée non fiable : on n'ouvre **jamais** un lien, on ne charge aucune
image, et le texte caché (``display:none``, taille 0…) est supprimé, car c'est un vecteur courant
d'injection de prompt.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from vigie.radar.normalize import normalize_spaces

TRACKING_PARAMS = frozenset(
    "trk trkemail trkinfo trackingid tracking tracking_id trackid refid eid lipi midtoken midsig "
    "otptoken gclid fbclid mc_cid mc_eid click_id clickid token sig signature ref source "
    "campaign".split()
)
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|max-height\s*:\s*0|mso-hide\s*:\s*all"
    r"|font-size\s*:\s*0(?:px|pt|em|%)?\s*(?:;|$)|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)",
    re.IGNORECASE,
)
_GENERIC_TEXT = re.compile(
    r"^(?:voir|consulter|d[ée]couvrir|postuler|afficher|candidater|see|view|apply|learn more|"
    r"en savoir plus|plus d'infos|more)\b",
    re.IGNORECASE,
)
MAX_CLIMB = 6
MAX_CARD_LINES = 40
MAX_CARD_CHARS = 2000


@dataclass(frozen=True)
class Fields:
    """Champs d'une offre lus par un parseur propre à un expéditeur."""

    title: str
    company: str | None
    location: str | None
    contract: str | None  # libellé brut (« CDI », « Intérim »…)
    description: str


# (lien de l'offre, bloc de l'offre) -> champs, ou ``None`` pour retomber sur l'heuristique
StructuredParser = Callable[[Tag, Tag], Fields | None]


@dataclass(frozen=True)
class SenderSpec:
    """Ce qui identifie les offres d'un expéditeur : une expression sur les liens.

    ``structured`` est un parseur propre au gabarit réel de l'expéditeur, quand on en connaît un.
    """

    key: str
    link_regex: re.Pattern[str]  # groupes nommés facultatifs : ``id``, ``company``
    structured: StructuredParser | None = None


@dataclass(frozen=True)
class Card:
    key: str  # identifiant d'offre : id du lien, sinon hash du lien normalisé
    url: str  # lien normalisé (paramètres de suivi retirés)
    title: str
    company: str | None
    location: str | None
    description: str
    contract: str | None = None  # libellé brut de contrat, s'il est connu


def normalize_link(url: str) -> str:
    """Lien sans fragment ni paramètres de suivi (``utm_*``, ``trackingId``, jetons…)."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(("utm_", "at_"))
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(query)), "")
    )


def _is_generic(text: str) -> bool:
    return len(text) < 2 or bool(_GENERIC_TEXT.match(text))


def _has_other_job(element: Tag, own_key: str, key_of: Callable[[Tag], str | None]) -> bool:
    """Vrai si l'élément (ou un de ses liens) appartient à une autre offre que ``own_key``."""
    anchors = (
        [element, *element.find_all("a", href=True)]
        if element.name == "a"
        else (element.find_all("a", href=True))
    )
    return any((key := key_of(a)) is not None and key != own_key for a in anchors)


def _clean(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "head", "title", "noscript", "template"]):
        element.decompose()
    hidden = [
        el
        for el in soup.find_all(True)
        if el.has_attr("hidden") or _HIDDEN_STYLE.search(str(el.get("style") or ""))
    ]
    for element in hidden:
        if not element.decomposed:
            element.decompose()
    return soup


def _lines(node: Tag) -> list[str]:
    lines = [normalize_spaces(line) for line in node.get_text("\n").split("\n")]
    return [line for line in lines if line][:MAX_CARD_LINES]


MAX_FLAT_SIBLINGS = 4


def _flat_lines(anchor: Tag, is_other_job: Callable[[Tag], bool]) -> list[str]:
    """Email « à plat » (pas de bloc autour de l'offre) : le lien puis ses frères suivants."""
    lines = _lines(anchor)
    for sibling in list(anchor.next_siblings)[:MAX_FLAT_SIBLINGS]:
        if isinstance(sibling, Tag):
            if is_other_job(sibling):
                break
            lines += _lines(sibling)
        else:
            text = normalize_spaces(str(sibling))
            if text:
                lines.append(text)
    return lines


def extract_cards(html: str, spec: SenderSpec) -> list[Card]:
    soup = _clean(html)
    grouped: dict[str, list[tuple[Tag, str, re.Match[str]]]] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        match = spec.link_regex.search(href)
        if not match:
            continue
        url = normalize_link(href)
        groups = match.groupdict()
        key = groups.get("id") or hashlib.sha256(url.encode()).hexdigest()[:16]
        grouped.setdefault(key, []).append((anchor, url, match))

    def key_of(anchor: Tag) -> str | None:
        for key, entries in grouped.items():
            if any(entry[0] is anchor for entry in entries):
                return key
        return None

    cards: list[Card] = []
    for key, entries in grouped.items():
        first_anchor, url, match = entries[0]
        # Le plus grand bloc parent qui ne contient que les liens de cette offre.
        node: Tag = first_anchor
        for _ in range(MAX_CLIMB):
            parent = node.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            if _has_other_job(parent, key, key_of):
                break
            node = parent
        if spec.structured is not None:
            fields = spec.structured(first_anchor, node)
            if fields is not None:
                cards.append(
                    Card(
                        key,
                        url,
                        fields.title,
                        fields.company,
                        fields.location,
                        fields.description[:MAX_CARD_CHARS],
                        fields.contract,
                    )
                )
                continue
        texts = [normalize_spaces(a.get_text(" ")) for a, _, _ in entries]
        if node is first_anchor:

            def is_other(el: Tag, own: str = key) -> bool:
                return _has_other_job(el, own, key_of)

            lines = _flat_lines(first_anchor, is_other)
        else:
            lines = _lines(node)
        title = next((t for t in texts if not _is_generic(t)), None)
        if title is None:
            title = next((line for line in lines if not _is_generic(line)), None)
        if title is None:
            continue
        rest = [
            line for line in lines if line != title and line not in texts and not _is_generic(line)
        ]
        slug = match.groupdict().get("company")
        if slug:
            # L'entreprise est dans le lien (WTTJ) : plus fiable que l'ordre des lignes.
            company: str | None = slug.replace("-", " ").strip() or None
            location = rest[0] if rest else None
            description = "\n".join(rest[1:])[:MAX_CARD_CHARS]
        else:
            company = rest[0] if rest else None
            location = rest[1] if len(rest) > 1 else None
            description = "\n".join(rest[2:])[:MAX_CARD_CHARS]
        cards.append(Card(key, url, title, company, location, description))
    return cards
