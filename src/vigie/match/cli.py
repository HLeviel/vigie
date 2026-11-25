"""Interface en ligne de commande : ``vigie match check`` (étape 1 ; les autres commandes suivent).

Comme pour ``radar``, la sortie n'affiche que des compteurs et des noms de variables : jamais le
contenu du profil, ni un secret.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from vigie.match.config import MatchConfig
from vigie.match.embedders.base import Embedder, EmbedderError, build_embedder
from vigie.match.evaluation.dataset import (
    MIN_LABELED,
    MIN_RELEVANT,
    load_from_store,
    sample_for_labeling,
    stats,
)
from vigie.match.evaluation.report import render_markdown, render_svg
from vigie.match.evaluation.runner import evaluate
from vigie.match.profile import Profile, ProfileError, load_profile_file
from vigie.match.scoring import scoring_key
from vigie.match.store import EmbeddingStore, LabelStore
from vigie.match.text import clean_text, truncate
from vigie.radar.config import ConfigError, resolve_config
from vigie.radar.models import Offer
from vigie.radar.store import DEFAULT_DB_PATH, Store, StoreError
from vigie.settings import Settings

app = typer.Typer(help="match : score d'adéquation offre / profil.", no_args_is_help=True)

ConfigOption = Annotated[
    Path | None, typer.Option("--config", help="Fichier de configuration YAML.")
]
DbOption = Annotated[Path | None, typer.Option("--db", help="Chemin de radar.db.")]

EXIT_CONFIG = 1


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(EXIT_CONFIG)


@app.command()
def check(config: ConfigOption = None) -> None:
    """Vérifie le profil, la section ``match`` et les secrets, sans appel réseau."""
    try:
        cfg = resolve_config(config)
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    match = cfg.match
    if match is None:
        raise _fail("section match absente de la configuration")
    typer.echo("configuration match valide" + ("" if match.enabled else " (désactivée)"))
    try:
        profile = load_profile_file(match.profile)
    except ProfileError as exc:
        raise _fail(str(exc)) from None
    skills = profile.skills
    typer.echo(
        f"profil valide : {len(profile.target_roles)} rôle(s) cible, "
        f"{len(skills.must_have)} compétence(s) maîtrisée(s), "
        f"{len(skills.nice_to_have)} en acquisition, {len(skills.avoid)} à éviter, "
        f"{len(profile.experiences)} expérience(s)"
    )
    typer.echo(f"empreinte du profil : {profile.profile_hash[:12]}")
    typer.echo(f"embeddings : fournisseur {match.embeddings.provider}")
    if match.embeddings.provider == "local":
        # sans importer la bibliothèque (lourde) ni charger de modèle : aucun appel réseau
        if importlib.util.find_spec("sentence_transformers") is None:
            raise _fail("embeddings local : installer l'extra « local » (uv sync --extra local)")
        typer.echo("embeddings local : sentence-transformers installé (modèle non vérifié ici)")
    else:
        typer.echo("embeddings bedrock : authentification par OIDC (rien à vérifier ici)")

    settings = Settings()
    if match.llm.enabled:
        typer.echo("étage LLM : activé dans la configuration, pas encore implémenté (lot M3)")
        if match.llm.provider == "anthropic":
            missing = settings.missing(["llm_anthropic"])
            if missing:
                raise _fail("étage LLM : variables manquantes : " + ", ".join(missing))
            typer.echo("secrets : présents")
        else:
            typer.echo("étage LLM : authentification par OIDC (rien à vérifier ici)")
    else:
        typer.echo("étage LLM : désactivé")


# --- étiquetage et évaluation (lot M2) ---------------------------------------------------------

MIN_FOR_EVAL = 10  # en dessous, le découpage réglage/mesure n'a pas de sens
ANSWERS = {"p": "pertinent", "m": "peut-être", "n": "non pertinent"}


def _match_setup(config: Path | None) -> tuple[MatchConfig, Profile]:
    try:
        cfg = resolve_config(config)
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    if cfg.match is None:
        raise _fail("section match absente de la configuration")
    try:
        return cfg.match, load_profile_file(cfg.match.profile)
    except ProfileError as exc:
        raise _fail(str(exc)) from None


def _open_store(db: Path | None) -> Store:
    path = db or Settings().vigie_db_path or DEFAULT_DB_PATH
    if not path.exists():
        raise _fail("aucune base : lancer d'abord `vigie radar run`")
    try:
        return Store.open(path)
    except StoreError as exc:
        raise _fail(str(exc)) from None


def _show(offer: Offer, position: int, total: int) -> None:
    """Affiche une offre réelle (donnée privée, texte nettoyé : aucun caractère de contrôle)."""
    typer.echo(f"\n[{position}/{total}] {clean_text(offer.source)}")
    typer.echo(truncate(clean_text(offer.title), 200))
    where = " · ".join(
        part
        for part in (clean_text(offer.company or ""), clean_text(offer.location_label or ""))
        if part
    )
    if where:
        typer.echo(where)
    typer.echo(truncate(clean_text(offer.description), 400) or "(pas de description)")


@app.command()
def label(
    config: ConfigOption = None,
    db: DbOption = None,
    n: Annotated[int, typer.Option("--n", min=1, help="Nombre d'offres à annoter.")] = 20,
    relabel: Annotated[
        bool,
        typer.Option(
            "--relabel", help="Réannote des offres déjà annotées (la nouvelle étiquette remplace)."
        ),
    ] = False,
) -> None:
    """Annote des offres réelles (affiche des offres : à utiliser en local ou en privé).

    Les annotations ne dépendent pas de la version du profil : on ne réannote qu'à la demande.
    """
    match, profile = _match_setup(config)
    key = scoring_key(match, profile, f"{match.embeddings.provider}:{match.embeddings.model}")
    with _open_store(db) as store:
        labels = LabelStore(store)
        done = set(labels.latest())
        if relabel:
            seed = f"relabel:{len(done)}:{labels.newest()}"  # un autre tirage après chaque passe
            sample = sample_for_labeling(store, key, n, set(), seed, only=done)
        else:
            sample = sample_for_labeling(store, key, n, done)
        if not sample:
            typer.echo("aucune offre annotée à revoir" if relabel else "aucune offre à annoter")
            return
        added = 0
        for position, offer in enumerate(sample, start=1):
            _show(offer, position, len(sample))
            while True:
                answer = typer.prompt(
                    "p pertinent, m peut-être, n non pertinent, s passer, q quitter"
                )
                answer = answer.strip().lower()
                if answer in {*ANSWERS, "s", "q"}:
                    break
            if answer == "q":
                break
            if answer in ANSWERS:
                labels.put(offer.uid, profile.profile_hash, ANSWERS[answer], datetime.now(UTC))
                store.commit()
                added += 1
        total = stats(load_from_store(store))
    typer.echo(
        f"{added} étiquette(s) posée(s) ; total {total.total} dont {total.relevant} pertinente(s) "
        f"(objectif : {MIN_LABELED} dont {MIN_RELEVANT})"
    )


def _embedders(match: MatchConfig) -> list[Embedder]:
    """Modèle configuré puis candidats, sans doublon."""
    wanted = [match.embeddings, *match.evaluation.candidates]
    seen: set[tuple[str, str]] = set()
    built: list[Embedder] = []
    for entry in wanted:
        if (entry.provider, entry.model) in seen:
            continue
        seen.add((entry.provider, entry.model))
        try:
            built.append(build_embedder(entry.provider, entry.model))
        except EmbedderError as exc:
            raise _fail(f"embeddings : {exc}") from None
    return built


@app.command("eval")
def eval_(
    config: ConfigOption = None,
    db: DbOption = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Rapport Markdown (le SVG est écrit à côté)."),
    ] = None,
) -> None:
    """Évalue les variantes de classement sur le jeu annoté et écrit un rapport publiable."""
    match, profile = _match_setup(config)
    with _open_store(db) as store:
        items = load_from_store(store)
        other = LabelStore(store).count_other_profiles(profile.profile_hash)
        if other:
            typer.echo(
                f"{other} étiquette(s) posée(s) sous une autre version du profil : conservées"
            )
        if len(items) < MIN_FOR_EVAL:
            raise _fail(
                f"{len(items)} offre(s) annotée(s) : au moins {MIN_FOR_EVAL} sont nécessaires "
                f"(objectif : {MIN_LABELED}, dont {MIN_RELEVANT} pertinentes) ; "
                "utiliser `vigie match label`"
            )
        embedders = _embedders(match)
        try:
            result = evaluate(profile, match, items, embedders, EmbeddingStore(store))
        except EmbedderError as exc:
            raise _fail(f"embeddings : {exc}") from None
        store.commit()
    date = result.generated_at.strftime("%Y-%m-%d")
    target = output or Path(f"match-{date}.md")
    svg = target.with_suffix(".svg")
    target.write_text(render_markdown(result, svg.name), encoding="utf-8")
    svg.write_text(render_svg(result), encoding="utf-8")
    typer.echo(
        f"rapport écrit : {result.dataset.total} offres annotées, {len(result.variants)} variantes"
        + ("" if result.dataset.meets_goal else " (jeu insuffisant : voir l'avertissement)")
    )
