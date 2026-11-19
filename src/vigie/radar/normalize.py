"""Nettoyage et normalisation : texte, clé de dédoublonnage, contrat, salaire, lieu.

Fonctions pures et déterministes, sans réseau ni LLM.
"""

import hashlib
import re
import unicodedata
from html import unescape
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
_SKIPPED_TAGS = {"script", "style"}


def normalize_spaces(text: str) -> str:
    """Remplace toute suite d'espaces (dont insécables) par un seul espace."""
    return re.sub(r"\s+", " ", text).strip()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Texte brut d'un fragment HTML : balises supprimées, entités décodées.

    Le contenu est traité comme une donnée : jamais interprété ni exécuté.
    Les caractères invisibles sont conservés (ils servent à repérer les textes suspects).
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = (normalize_spaces(line) for line in unescape("".join(parser.parts)).split("\n"))
    return "\n".join(line for line in lines if line)


# --- Dédoublonnage -------------------------------------------------------------------------

_GENDER_RE = re.compile(r"\(?\b[hfm]\s*/\s*[hfm](\s*/\s*[xd])?\b\)?", re.IGNORECASE)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_for_key(text: str) -> str:
    """Minuscules, sans accents, sans mention de genre (H/F, F/H), sans ponctuation."""
    text = _GENDER_RE.sub(" ", text)
    text = strip_accents(text).lower()
    text = re.sub(r"[^\w\s]|_", " ", text)
    return normalize_spaces(text)


def compute_dedup_key(company: str | None, title: str, department: str | None) -> str:
    """SHA-256 de ``norm(company)|norm(title)|department`` (section 9 de la spec)."""
    raw = "|".join([normalize_for_key(company or ""), normalize_for_key(title), department or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --- Contrat -------------------------------------------------------------------------------

# Ordre significatif : le premier motif trouvé l'emporte.
_CONTRACT_PATTERNS: list[tuple[str, str]] = [
    (r"\balternance\b|\bapprentissage\b|\bprofessionnalisation\b", "ALTERNANCE"),
    (r"\bstage\b|\bstagiaire\b", "STAGE"),
    (r"\bfreelance\b|\bind[eé]pendant\b|\blib[eé]rale?\b|\bportage\b", "FREELANCE"),
    (r"\bint[eé]rim\b|\bmission int[eé]rimaire\b|\bmis\b", "INTERIM"),
    (r"\bcdi\b|\bdur[eé]e ind[eé]termin[eé]e\b", "CDI"),
    (r"\bcdd\b|\bdur[eé]e d[eé]termin[eé]e\b", "CDD"),
]


def parse_contract_type(label: str | None) -> str:
    """Valeur de ``ContractType`` correspondant à un libellé ; ``AUTRE`` si inconnu."""
    if not label:
        return "AUTRE"
    text = label.lower()
    for pattern, value in _CONTRACT_PATTERNS:
        if re.search(pattern, text):
            return value
    return "AUTRE"


# --- Salaire -------------------------------------------------------------------------------

_AMOUNT = r"(\d[\d\s  .,]*\d|\d)"
_SALARY_RE = re.compile(
    rf"(?P<period>annuel|mensuel)\s+de\s+{_AMOUNT}\s*(?:euros?|€)?"
    rf"(?:\s*(?:à|a|-)\s*{_AMOUNT}\s*(?:euros?|€)?)?",
    re.IGNORECASE,
)


def _to_float(raw: str) -> float | None:
    cleaned = re.sub(r"[\s  ]", "", raw).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


_MONTHS_RE = re.compile(r"sur\s+(\d+(?:[.,]\d+)?)\s*mois", re.IGNORECASE)


def parse_salary(label: str | None) -> tuple[float | None, float | None]:
    """Extrait (min, max) annuels bruts en euros d'un libellé de salaire.

    Formats reconnus : « Annuel de 45000 Euros à 55000 Euros », « Mensuel de 3000 Euros »
    (multiplié par 12, ou par N si le libellé indique « sur N mois », N entre 1 et 24).
    Tout autre format (horaire, texte libre, N hors bornes) donne ``(None, None)``.
    """
    if not label:
        return None, None
    match = _SALARY_RE.search(label)
    if not match:
        return None, None
    factor = 1.0
    if match.group("period").lower() == "mensuel":
        factor = 12.0
        months = _MONTHS_RE.search(label)
        if months:
            n = _to_float(months.group(1))
            if n is None or not 1 <= n <= 24:
                return None, None
            factor = n
    low = _to_float(match.group(2))
    high = _to_float(match.group(3)) if match.group(3) else low
    if low is None or high is None:
        return None, None
    low, high = sorted((low * factor, high * factor))
    return low, high


# --- Lieu ----------------------------------------------------------------------------------

_DEPARTMENT_PREFIX_RE = re.compile(r"^\s*(\d{2,3}|2[ABab])\s*-\s*\S")


def department_from_postal_code(postal_code: str | None) -> str | None:
    """Code département déduit d'un code postal (DOM sur 3 chiffres, Corse en 2A/2B)."""
    if not postal_code or not re.fullmatch(r"\d{5}", postal_code):
        return None
    if postal_code.startswith("20"):
        return "2A" if int(postal_code) < 20200 else "2B"
    if postal_code.startswith(("97", "98")):
        return postal_code[:3]
    return postal_code[:2]


def department_from_label(label: str | None) -> str | None:
    """Code département en tête d'un libellé du type « 69 - Lyon 3e »."""
    if not label:
        return None
    match = _DEPARTMENT_PREFIX_RE.match(label)
    return match.group(1).upper() if match else None


def parse_location(
    label: str | None, postal_code: str | None = None
) -> tuple[str | None, str | None, str | None]:
    """Retourne (libellé nettoyé, code postal, département).

    Le code postal prime sur le libellé pour déterminer le département.
    """
    clean_label = normalize_spaces(label) if label else None
    postal = postal_code.strip() if postal_code and postal_code.strip() else None
    department = department_from_postal_code(postal) or department_from_label(clean_label)
    return clean_label or None, postal, department


# --- Texte suspect -------------------------------------------------------------------------

_INVISIBLE_RE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")
_INJECTION_RE = re.compile(
    r"ignor(?:e|er|ez)\s+(?:all\s+|toutes\s+(?:les\s+)?|les\s+)?"
    r"(?:previous|prior|above|pr[eé]c[eé]dentes?)\s+(?:instructions?|consignes?)"
    r"|ignor(?:e|ez)\s+(?:toutes\s+)?(?:les\s+)?instructions?\s+pr[eé]c[eé]dentes?"
    r"|disregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?"
    r"|(?:reveal|show|print)\s+(?:your\s+)?system\s+prompt"
    r"|you\s+are\s+now\s+(?:a|an|in)\b",
    re.IGNORECASE,
)


def is_suspicious(text: str) -> bool:
    """Vrai si le texte contient des caractères invisibles ou un motif d'injection de prompt.

    Heuristique de signalement uniquement : le traitement complet relève de ``match``.
    """
    return bool(_INVISIBLE_RE.search(text) or _INJECTION_RE.search(text))


# --- Télétravail ---------------------------------------------------------------------------

_REMOTE_PATTERNS: dict[str, re.Pattern[str]] = {
    "FULL": re.compile(
        r"100\s*%\s*(?:en\s+)?(?:teletravail|remote)"
        r"|teletravail\s+(?:a\s+)?100\s*%"
        r"|(?<!pas de )(?<!sans )(?<!non )teletravail\s+complet"
        r"|full(?:y)?[\s-]?remote"
    ),
    "PARTIAL": re.compile(
        r"teletravail\s+partiel|\bhybrid(?:e|s)?\b"
        r"|\d+\s+jours?\s+(?:de|en|par\s+semaine\s+de)\s+teletravail"
    ),
    "NONE": re.compile(
        r"pas\s+de\s+teletravail(?!\s+(?:complet|total))"
        r"|sans\s+teletravail"
        r"|aucun\s+teletravail"
        r"|teletravail\s+(?:impossible|non\s+possible|non\s+autorise)"
    ),
}


def parse_remote(title: str, description: str = "") -> str:
    """Valeur de ``RemoteMode`` déduite du titre et de la description.

    FULL, PARTIAL ou NONE (explicite seulement) ; ``UNKNOWN`` si aucun signal ;
    ``PARTIAL`` en cas de signaux contradictoires (spec 1.1).
    """
    text = normalize_spaces(strip_accents(f"{title}\n{description}").lower())
    found = [value for value, pattern in _REMOTE_PATTERNS.items() if pattern.search(text)]
    if not found:
        return "UNKNOWN"
    return found[0] if len(found) == 1 else "PARTIAL"
