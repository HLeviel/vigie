"""Digest des nouveautés (section 12 de la spec), en HTML ou en Markdown.

Contient uniquement les offres nouvelles, non filtrées et non encore notifiées, de la plus récente
à la plus ancienne, limitées à ``max_items``. Les textes issus des offres sont une entrée non
fiable : échappés en HTML (autoescape Jinja), échappés en Markdown, débarrassés des caractères
invisibles et bidirectionnels, tronqués. Les liens sont ceux d'``Offer.url`` (http ou https).

Ce module ne marque rien comme notifié : c'est l'envoi réussi (étape 8) ou l'option explicite
``--mark-notified`` de la CLI qui le fait.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError

from vigie.match.scoring import ScoreDetails
from vigie.match.store import LlmCallStore, ScoreStore, ScoringKey
from vigie.radar.config import DigestConfig
from vigie.radar.models import ContractType, Offer
from vigie.radar.store import Store

PARIS = ZoneInfo("Europe/Paris")


def _template_dir() -> Path:
    """Paquet installé (``vigie/templates``) d'abord, sinon dépôt source (``templates/``)."""
    here = Path(__file__).resolve()
    for candidate in (here.parents[1] / "templates", here.parents[3] / "templates"):
        if (candidate / "digest.html.j2").is_file():
            return candidate
    raise FileNotFoundError("modèle du digest introuvable (templates/digest.html.j2)")


TEMPLATE_DIR = _template_dir()
HTML_TEMPLATE = "digest.html.j2"
NBSP = " "
MAX_TITLE = 200
MAX_FIELD = 100

_CONTRACT_LABELS = {
    ContractType.CDI: "CDI",
    ContractType.CDD: "CDD",
    ContractType.INTERIM: "Intérim",
    ContractType.FREELANCE: "Freelance",
    ContractType.STAGE: "Stage",
    ContractType.ALTERNANCE: "Alternance",
}
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿­]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#|~&!$])")


def clean_text(text: str | None, limit: int = MAX_FIELD) -> str:
    """Texte sur une ligne, sans caractère invisible, bidirectionnel ni de contrôle, tronqué."""
    if not text:
        return ""
    text = _CONTROL.sub(" ", _INVISIBLE.sub("", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def md_escape(text: str) -> str:
    """Échappe ce que Markdown interpréterait (liens, HTML, titres, tableaux, emphase)."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def md_url(url: str) -> str:
    """URL utilisable entre parenthèses d'un lien Markdown."""
    return re.sub(r"[()\s<>\\]", lambda m: f"%{ord(m.group()):02X}", url)


def format_amount(value: float) -> str:
    return f"{round(value):,}".replace(",", NBSP)


def format_salary(low: float | None, high: float | None) -> str | None:
    """Fourchette annuelle brute en euros, ou ``None`` si aucun montant."""
    if low is None and high is None:
        return None
    if low is not None and high is not None and low != high:
        return f"{format_amount(low)} – {format_amount(high)}{NBSP}€ brut/an"
    amount = low if low is not None else high
    if low is None:
        return f"jusqu'à {format_amount(high or 0)}{NBSP}€ brut/an"
    if high is None:
        return f"à partir de {format_amount(low)}{NBSP}€ brut/an"
    return f"{format_amount(amount or 0)}{NBSP}€ brut/an"


def format_date(moment: datetime) -> str:
    return moment.astimezone(PARIS).strftime("%d/%m/%Y")


@dataclass(frozen=True)
class DigestItem:
    uid: str
    title: str
    company: str
    location: str
    contract: str
    salary: str
    date: str
    source: str
    url: str
    suspicious: bool
    # Scoring (module match) : absents tant que l'offre n'est pas scorée.
    score: int | None = None
    found_skills: tuple[str, ...] = ()
    missing_skills: tuple[str, ...] = ()
    low_confidence: bool = False
    recommendation: str = ""  # étage LLM : postuler, à voir, passer

    @classmethod
    def from_offer(cls, offer: Offer) -> "DigestItem":
        return cls(
            uid=offer.uid,
            title=clean_text(offer.title, MAX_TITLE),
            company=clean_text(offer.company),
            location=clean_text(offer.location_label),
            contract=_CONTRACT_LABELS.get(offer.contract_type, ""),
            salary=format_salary(offer.salary_min, offer.salary_max) or "",
            date=format_date(offer.published_at),
            source=clean_text(offer.source, 40),
            url=str(offer.url),
            suspicious=offer.suspicious,
        )

    def with_score(
        self, score: float, details: ScoreDetails, recommendation: str = ""
    ) -> "DigestItem":
        """Ajoute le score et l'explication (2 compétences trouvées et 1 manquante au plus)."""
        found = [*details.found_must_have, *details.found_nice_to_have][:2]
        return replace(
            self,
            score=round(score),
            found_skills=tuple(clean_text(s, 40) for s in found),
            missing_skills=tuple(clean_text(s, 40) for s in details.missing_must_have[:1]),
            low_confidence=details.confidence == "faible",
            recommendation=clean_text(recommendation, 20),
        )

    def meta_parts(self, escape: Callable[[str], str] = lambda text: text) -> list[str]:
        """Détails de l'offre ; ``escape`` s'applique aux seuls textes venant de l'offre."""
        parts = [escape(self.company), escape(self.location), self.contract, self.salary]
        parts = [p for p in parts if p]
        scoring = [f"score {self.score}/100"] if self.score is not None else []
        if self.recommendation:
            scoring.append(f"recommandation : {self.recommendation}")
        if self.low_confidence:
            scoring.append("confiance faible")
        return [*scoring, *parts, f"publiée le {self.date}", f"source : {escape(self.source)}"]

    def explanation(self, escape: Callable[[str], str] = lambda text: text) -> str:
        """Une ligne : compétences trouvées et manquante ; vide sans scoring."""
        pieces = []
        if self.found_skills:
            pieces.append("trouvées : " + ", ".join(escape(s) for s in self.found_skills))
        if self.missing_skills:
            pieces.append("manque : " + ", ".join(escape(s) for s in self.missing_skills))
        return " · ".join(pieces)

    @property
    def meta(self) -> str:
        return " · ".join(self.meta_parts())


@dataclass(frozen=True)
class SourceSummary:
    source: str
    fetched: int
    new: int
    filtered: int
    duplicates: int
    errors: int
    failed: bool
    error_type: str | None = None
    llm_usage: tuple[int, int, int] | None = None  # (appels, jetons d'entrée, jetons de sortie)

    def render(self, escape: Callable[[str], str] = lambda text: text) -> str:
        if self.source == "match:llm":  # étage LLM : compteurs et jetons seulement
            line = (
                f"match:llm : candidates {self.fetched}, évaluées {self.new}, "
                f"en cache {self.duplicates}, invalides {self.filtered}"
            )
            if self.llm_usage is not None:
                calls, tokens_in, tokens_out = self.llm_usage
                line += f", appels {calls}, jetons {tokens_in} en entrée et {tokens_out} en sortie"
        elif self.source == "match":  # ligne du scoring : mêmes colonnes, autre sens
            line = (
                f"match : à scorer {self.fetched}, scorées {self.new}, "
                f"exclues par préférence {self.filtered}"
            )
        else:
            line = (
                f"{escape(self.source)} : récupérées {self.fetched}, nouvelles {self.new}, "
                f"filtrées {self.filtered}, doublons {self.duplicates}, erreurs {self.errors}"
            )
        if self.failed:
            line += f" (ÉCHEC : {escape(self.error_type or 'erreur')})"
        return line

    @property
    def text(self) -> str:
        return self.render()


@dataclass(frozen=True)
class RunSummary:
    started_at: datetime
    status: str
    sources: tuple[SourceSummary, ...]


@dataclass(frozen=True)
class RunLine:
    text: str
    failed: bool


@dataclass(frozen=True)
class Digest:
    items: tuple[DigestItem, ...]
    total: int  # offres à notifier, y compris celles qui dépassent ``max_items``
    generated_at: datetime
    run: RunSummary | None = None
    format: str = "html"
    below_threshold: int = 0  # offres scorées sous ``min_score`` : comptées, pas listées
    below_uids: tuple[str, ...] = ()
    scored: bool = False  # vrai si les offres sont triées par score
    note: str = ""  # mention du pied (ex. scoring indisponible)

    @property
    def has_failure(self) -> bool:
        return self.run is not None and any(s.failed for s in self.run.sources)

    @property
    def should_send(self) -> bool:
        """Spec : pas de digest sans nouvelle offre, sauf si une source a échoué (alerte courte)."""
        return bool(self.items) or self.has_failure

    @property
    def title(self) -> str:
        if not self.items:
            if self.run is not None and not any(
                s.failed for s in self.run.sources if not s.source.startswith("match")
            ):
                return "Vigie : alerte, le scoring a échoué"
            return "Vigie : alerte, une source a échoué"
        n = len(self.items)
        return f"Vigie : {n} nouvelle{'s' if n > 1 else ''} offre{'s' if n > 1 else ''}"

    @property
    def subtitle(self) -> str:
        stamp = self.generated_at.astimezone(PARIS).strftime("%d/%m/%Y à %H:%M")
        text = f"Généré le {stamp} (heure de Paris)."
        if self.total > len(self.items):
            text += f" {len(self.items)} offres affichées sur {self.total} à notifier."
        if self.below_threshold:
            plural = "s" if self.below_threshold > 1 else ""
            text += (
                f" {self.below_threshold} offre{plural} sous le seuil de pertinence "
                f"(non listée{plural})."
            )
        if self.scored:
            text += " Triées par score décroissant."
        return text

    @property
    def run_label(self) -> str:
        if self.run is None:
            return ""
        stamp = self.run.started_at.astimezone(PARIS).strftime("%d/%m/%Y à %H:%M")
        status = {"success": "réussie", "partial": "partielle", "failed": "en échec"}.get(
            self.run.status, self.run.status
        )
        return f"Exécution du {stamp} : {status}."

    @property
    def run_lines(self) -> list[RunLine]:
        return [RunLine(s.text, s.failed) for s in (self.run.sources if self.run else ())]

    def run_lines_markdown(self) -> list[str]:
        return [s.render(md_escape) for s in (self.run.sources if self.run else ())]


def _error_type(message: str | None) -> str | None:
    """Type d'exception seulement (le message, même masqué, n'a pas sa place dans un digest)."""
    return str(message).split(":", 1)[0] if message else None


def _run_summary(store: Store) -> RunSummary | None:
    runs = store.recent_runs(1)
    if not runs:
        return None
    last = runs[0]
    return RunSummary(
        started_at=datetime.fromisoformat(last["started_at"]),
        status=str(last["status"]),
        sources=tuple(
            SourceSummary(
                source=str(s["source"]),
                fetched=int(s["fetched"]),
                new=int(s["new"]),
                filtered=int(s["filtered"]),
                duplicates=int(s["duplicates"]),
                errors=int(s["errors"]),
                failed=s["error_message"] is not None,
                error_type=str(s["error_message"]).split(":", 1)[0] if s["error_message"] else None,
                llm_usage=LlmCallStore(store).totals(str(last["run_id"]))
                if s["source"] == "match:llm"
                else None,
            )
            for s in last["sources"]
        ),
    )


SCORING_UNAVAILABLE = "Scoring indisponible ou incomplet : offres triées par date, sans score."


def _llm_verdict(details_json: str) -> str | None:
    """Recommandation d'une évaluation LLM stockée ; ``None`` si elle est illisible (l'offre garde
    alors son score hybride)."""
    try:
        data = json.loads(details_json)
        recommendation = data["assessment"]["recommendation"]
    except (ValueError, KeyError, TypeError):
        return None
    return recommendation if recommendation in {"postuler", "à voir", "passer"} else None


def _scored_items(
    offers: list[Offer], scoring: ScoringKey, store: Store
) -> tuple[list[DigestItem], list[str]] | None:
    """Offres triées par score décroissant (celles sous le seuil à part) ; ``None`` si une offre
    n'a pas de score valide : le digest retombe alors sur l'ordre par date."""
    uids = [o.uid for o in offers]
    stored = ScoreStore(store).get_many(uids, scoring)
    if len(stored) != len(offers):
        return None
    llm = ScoreStore(store).get_many(uids, scoring.llm) if scoring.llm else {}
    ranked: list[tuple[float, Offer, DigestItem]] = []
    below: list[str] = []
    for offer in offers:
        score, details_json = stored[offer.uid]
        try:
            details = ScoreDetails.model_validate_json(details_json)
        except ValidationError:
            return None
        recommendation = ""
        if offer.uid in llm and not details.excluded:  # score final : celui du LLM s'il existe
            verdict = _llm_verdict(llm[offer.uid][1])
            if verdict is not None:
                score, recommendation = llm[offer.uid][0], verdict
        if details.excluded or score < scoring.min_score:
            below.append(offer.uid)
        else:
            item = DigestItem.from_offer(offer).with_score(score, details, recommendation)
            ranked.append((score, offer, item))
    ranked.sort(key=lambda r: (-r[0], -r[1].published_at.timestamp(), r[1].uid))
    return [item for _, _, item in ranked], below


def build_digest(
    store: Store,
    config: DigestConfig,
    now: datetime,
    scoring: ScoringKey | None = None,
    note: str | None = None,
    llm_note: str | None = None,
) -> Digest:
    """Digest des offres à notifier.

    Sans ``scoring`` (module match absent ou désactivé) : de la plus récente à la plus ancienne.
    Avec ``scoring`` : triées par score ; si une offre n'a pas de score, retour à l'ordre par date
    avec une mention (une panne du scoring ne bloque jamais le digest). ``llm_note`` : mention du
    pied quand seul l'étage LLM a été ignoré (les offres restent triées par score hybride).
    """
    run = _run_summary(store)
    if scoring is not None and note is None:
        offers = store.pending_offers()
        ranked = _scored_items(offers, scoring, store)
        if ranked is not None:
            listed, below = ranked
            return Digest(
                items=tuple(listed[: config.max_items]),
                total=len(listed),
                generated_at=now,
                run=run,
                format=config.format,
                below_threshold=len(below),
                below_uids=tuple(below),
                scored=True,
                note=llm_note or "",
            )
        note = SCORING_UNAVAILABLE
    offers = store.pending_offers(limit=config.max_items)
    return Digest(
        items=tuple(DigestItem.from_offer(o) for o in offers),
        total=store.count_pending(),
        generated_at=now,
        run=run,
        format=config.format,
        note=note or "",
    )


def _environment() -> Environment:
    # autoescape toujours actif : les textes des offres ne sont jamais interprétés comme du HTML.
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(digest: Digest) -> str:
    return _environment().get_template(HTML_TEMPLATE).render(digest=digest)


def render_markdown(digest: Digest) -> str:
    # Titre, date et compteurs sont produits par Vigie ; seuls les textes des offres sont échappés.
    lines = [f"# {digest.title}", "", digest.subtitle, ""]
    if digest.items:
        for item in digest.items:
            lines.append(f"- [{md_escape(item.title)}]({md_url(item.url)})")
            lines.append("  " + " · ".join(item.meta_parts(md_escape)))
            if item.explanation():
                lines.append("  " + item.explanation(md_escape))
            if item.suspicious:
                lines.append(
                    "  ⚠ Contenu suspect (instructions ou caractères invisibles) : "
                    "lire avec prudence."
                )
        lines.append("")
    else:
        lines += ["Aucune nouvelle offre.", ""]
    lines += ["## Dernière exécution", ""]
    if digest.run:
        lines += [digest.run_label, ""]
        lines += [f"- {line}" for line in digest.run_lines_markdown()]
    else:
        lines.append("Aucune exécution enregistrée.")
    if digest.note:
        lines += ["", f"_{digest.note}_"]
    return "\n".join(lines) + "\n"


def render_text(digest: Digest) -> str:
    """Texte brut (partie ``text/plain`` d'un email) : rien n'y est interprété ni échappé."""
    lines = [digest.title, digest.subtitle, ""]
    if digest.items:
        for number, item in enumerate(digest.items, start=1):
            lines.append(f"{number}. {item.title}")
            lines.append("   " + " · ".join(item.meta_parts()))
            if item.explanation():
                lines.append("   " + item.explanation())
            lines.append(f"   {item.url}")
            if item.suspicious:
                lines.append("   ⚠ Contenu suspect : lire avec prudence.")
            lines.append("")
    else:
        lines += ["Aucune nouvelle offre.", ""]
    lines += ["Dernière exécution", digest.run_label or "Aucune exécution enregistrée."]
    lines += [f"- {line.text}" for line in digest.run_lines]
    if digest.note:
        lines += ["", digest.note]
    return "\n".join(lines) + "\n"


def render(digest: Digest) -> str:
    return render_html(digest) if digest.format == "html" else render_markdown(digest)
