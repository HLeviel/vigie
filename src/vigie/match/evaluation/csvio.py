"""Annotation par fichier CSV : export d'un échantillon, import des étiquettes (spec ``match`` 9).

Le fichier exporté contient des offres réelles (donnée privée) : jamais dans le dépôt public.
Il ne contient **aucun score ni rang** : l'annotation ne doit pas être influencée par le
classement à évaluer. Les textes issus des offres sont une entrée non fiable : un tableur
interprète une cellule qui commence par ``=``, ``+``, ``-`` ou ``@`` comme une formule.
"""

import csv
import io
import os
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from vigie.match.evaluation.dataset import SPLIT_SEED
from vigie.match.profile import normalize_skill
from vigie.match.text import clean_text, truncate
from vigie.radar.digest import format_date
from vigie.radar.models import Offer

BOM = "﻿"
DELIMITER = ";"
COLUMNS = (
    "uid",
    "label",
    "titre",
    "entreprise",
    "lieu",
    "contrat",
    "publiée",
    "source",
    "extrait",
    "lien",
)
EXCERPT_CHARS = 500
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Valeurs acceptées (sans casse ni accents) ; une cellule vide est une offre non annotée.
_ACCEPTED = {
    "p": "pertinent",
    "pertinent": "pertinent",
    "m": "peut-être",
    "peut-etre": "peut-être",
    "n": "non pertinent",
    "non": "non pertinent",
}


class CsvError(Exception):
    """Fichier inutilisable (colonne absente, encodage, syntaxe). Message fixe, sans contenu."""


def neutralize(value: str) -> str:
    """Cellule issue d'une offre : caractères invisibles et de contrôle retirés, puis apostrophe en
    tête si un tableur y verrait une formule (début par ``=``, ``+``, ``-``, ``@``, tabulation ou
    retour chariot, avant ou après nettoyage)."""
    text = clean_text(value)
    if value.startswith(_FORMULA_START) or text.startswith(_FORMULA_START):
        return "'" + text
    return text


def _row(offer: Offer) -> list[str]:
    return [
        offer.uid,
        "",
        neutralize(offer.title),
        neutralize(offer.company or ""),
        neutralize(offer.location_label or ""),
        neutralize(offer.contract_type.value),
        format_date(offer.published_at),
        neutralize(offer.source),
        neutralize(truncate(clean_text(offer.description), EXCERPT_CHARS)),
        neutralize(str(offer.url)),
    ]


def export_csv(offers: Iterable[Offer], path: Path, seed: str = SPLIT_SEED) -> int:
    """Écrit les offres en CSV (UTF-8 avec BOM, ``;``, RFC 4180), **dans un ordre aléatoire** pour
    que la position dans le fichier ne révèle rien du classement. Le fichier est créé en 0600."""
    shuffled = list(offers)
    random.Random(f"{seed}:{len(shuffled)}").shuffle(shuffled)  # noqa: S311 (pas de secret)
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer, delimiter=DELIMITER, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n"
    )
    writer.writerow(COLUMNS)
    for offer in shuffled:
        writer.writerow(_row(offer))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(BOM + buffer.getvalue())
    return len(shuffled)


def parse_label(cell: str) -> str | None:
    """Étiquette normalisée, ``None`` si la cellule est invalide (jamais pour une cellule vide :
    l'appelant traite le vide à part)."""
    return _ACCEPTED.get(normalize_skill(cell))


@dataclass
class ImportRow:
    line: int
    uid: str
    label: str  # étiquette normalisée


@dataclass
class ParsedImport:
    rows: list[ImportRow] = field(default_factory=list)
    read: int = 0
    skipped: int = 0  # cellule vide : non annotée
    errors: list[tuple[int, str]] = field(default_factory=list)  # (ligne, motif)


def _delimiter(header: str) -> str:
    return ";" if header.count(";") >= header.count(",") else ","


def read_import(path: Path) -> ParsedImport:
    """Lit un CSV annoté (avec ou sans BOM, ``;`` ou ``,`` détecté). Seules les colonnes ``uid`` et
    ``label`` comptent ; les autres sont ignorées. Les erreurs de ligne n'arrêtent pas la lecture
    et ne reprennent jamais le contenu."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise CsvError(f"fichier illisible : {exc.strerror}") from None
    except UnicodeDecodeError:
        raise CsvError("encodage illisible (UTF-8 attendu)") from None
    if not text.strip():
        raise CsvError("fichier vide")
    first_line = text.splitlines()[0]
    parsed = ParsedImport()
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(first_line))
        header = next(reader)
        names = [normalize_skill(name) for name in header]
        if "uid" not in names or "label" not in names:
            raise CsvError("colonne uid ou label absente de l'en-tête")
        uid_at, label_at = names.index("uid"), names.index("label")
        for row in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in row):
                continue  # ligne vide : ni lue ni comptée
            parsed.read += 1
            uid = row[uid_at].strip() if len(row) > uid_at else ""
            cell = row[label_at].strip() if len(row) > label_at else ""
            if not cell:
                parsed.skipped += 1
                continue
            if not uid:
                parsed.errors.append((line, "uid manquant"))
                continue
            label = parse_label(cell)
            if label is None:
                parsed.errors.append((line, "étiquette invalide"))
                continue
            parsed.rows.append(ImportRow(line, uid, label))
    except csv.Error:
        raise CsvError("CSV illisible (syntaxe ou cellule trop longue)") from None
    return parsed
