"""Alertes France Travail (expéditeur ``nepasrepondre@offre.francetravail.fr``).

Gabarit **vérifié le 24/11/2026 sur trois vrais emails** (« Vous avez reçu N nouvelles offres », une
alerte nommée par l'utilisateur) : un seul HTML, un bloc ``<table>`` par offre :

- lien direct ``https://candidat.francetravail.fr/offres/recherche/detail/<id>?at_*=…`` (aucun
  domaine de suivi ; l'identifiant est alphanumérique, ex. ``216TNKC``, ou numérique) ;
- dans le lien, un ``<span>`` extérieur : titre (texte direct, suivi d'un commentaire HTML et d'un
  ``<br>``), puis deux ``<span>`` : « ENTREPRISE - » (**absent sur certaines offres**) et le lieu
  (« 38 - Corps », « 2B - BIGUGLIA » ou seulement le département « 35 - Ille-et-Vilaine ») ;
- puis le contrat (« CDI », « CDD », « Intérim »…), le temps de travail (« Temps plein », « Temps
  partiel », « Non renseigné »), un extrait de description tronqué par « ... » (parfois précédé de
  « Cette offre d'emploi est confiée à France Travail par … ») et « Offre n°<id> ».
"""

import re

from bs4.element import Comment, NavigableString, Tag

from vigie.radar.normalize import normalize_spaces
from vigie.radar.sources.email_alerts.parsing import Fields, SenderSpec

LINK = re.compile(
    r"(?:candidat\.francetravail\.fr|candidat\.pole-emploi\.fr)"
    r"/offres/recherche/detail/(?P<id>[0-9A-Za-z]+)"
)
_WORKING_TIME = re.compile(r"^(?:temps (?:plein|partiel)|non renseign[ée])$", re.IGNORECASE)
_PARTNER = re.compile(r"^cette offre d'emploi est confiée à france travail par\b", re.IGNORECASE)
_OFFER_NUMBER = re.compile(r"^offre n[°o]\s*\w+$", re.IGNORECASE)


def parse_card(anchor: Tag, card: Tag) -> Fields | None:
    outer = anchor.find("span")
    if not isinstance(outer, Tag):
        return None
    # Texte direct du span extérieur, sans les commentaires HTML (« Lieu offre ») ni les sous-spans.
    title = normalize_spaces(
        " ".join(
            str(node)
            for node in outer.children
            if isinstance(node, NavigableString) and not isinstance(node, Comment)
        )
    )
    spans = [normalize_spaces(s.get_text(" ")) for s in outer.find_all("span", recursive=False)]
    spans = [s for s in spans if s]
    if not title or not spans:
        return None
    # « ENTREPRISE - » puis le lieu ; sans entreprise, le lieu est seul.
    company = spans[0].removesuffix("-").strip() or None if len(spans) >= 2 else None
    location = spans[-1]

    # Lignes du lien exclues par égalité exacte : « CDI » est aussi un morceau du titre de certaines
    # offres (« … - CDI H/F »), un test de sous-chaîne le ferait disparaître.
    anchor_lines = {normalize_spaces(line) for line in anchor.get_text("\n").split("\n")}
    lines = [normalize_spaces(line) for line in card.get_text("\n").split("\n")]
    lines = [line for line in lines if line and line not in anchor_lines]
    contract = lines.pop(0) if lines else None
    if lines and _WORKING_TIME.match(lines[0]):
        lines.pop(0)
    description = [
        line for line in lines if not _PARTNER.match(line) and not _OFFER_NUMBER.match(line)
    ]
    return Fields(title, company, location, contract, "\n".join(description))


SPEC = SenderSpec(key="france_travail", link_regex=LINK, structured=parse_card)
