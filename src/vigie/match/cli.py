"""Interface en ligne de commande : ``vigie match check`` (étape 1 ; les autres commandes suivent).

Comme pour ``radar``, la sortie n'affiche que des compteurs et des noms de variables : jamais le
contenu du profil, ni un secret.
"""

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from vigie.match.config import MatchConfig
from vigie.match.embedders.base import Embedder, EmbedderError, build_embedder
from vigie.match.evaluation.csvio import CsvError, export_csv, read_import
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
from vigie.radar.state import S3State, StateSyncError
from vigie.radar.store import DEFAULT_DB_PATH, Store, StoreError
from vigie.settings import Settings

app = typer.Typer(help="match : score d'adéquation offre / profil.", no_args_is_help=True)

ConfigOption = Annotated[
    Path | None, typer.Option("--config", help="Fichier de configuration YAML.")
]
DbOption = Annotated[Path | None, typer.Option("--db", help="Chemin de radar.db.")]

EXIT_CONFIG = 1
EXIT_STATE = 5  # état S3 inaccessible ou non synchronisé (comme `radar run`)


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


def _state(settings: Settings) -> S3State | None:
    return S3State(settings.vigie_state_bucket) if settings.vigie_state_bucket else None


@contextmanager
def _database(db: Path | None, *, write: bool) -> Iterator[Store]:
    """Ouvre la base ; avec ``VIGIE_STATE_BUCKET``, l'état est lu depuis S3 d'abord et, si
    ``write``, réécrit après une commande réussie (écriture conditionnelle, comme ``radar run``).
    Une commande qui échoue ne renvoie rien."""
    settings = Settings()
    path = db or settings.vigie_db_path or DEFAULT_DB_PATH
    state = _state(settings)
    remote = None
    if state is not None:
        try:
            remote = state.pull(path)
        except StateSyncError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(EXIT_STATE) from None
        typer.echo("état S3 : " + ("téléchargé" if remote.etag else "aucun état"))
    if not path.exists():
        raise _fail("aucune base : lancer d'abord `vigie radar run`")
    try:
        store = Store.open(path)
    except StoreError as exc:
        raise _fail(str(exc)) from None
    try:
        yield store
    except BaseException:
        store.close()
        raise
    store.close()
    if state is not None and remote is not None and write:
        try:
            state.push(path, remote)
        except StateSyncError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(EXIT_STATE) from None
        typer.echo("état S3 : synchronisé")


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
    n: Annotated[
        int | None,
        typer.Option("--n", min=1, help="Nombre d'offres (20 en interactif, 50 en export)."),
    ] = None,
    relabel: Annotated[
        bool,
        typer.Option(
            "--relabel", help="Réannote des offres déjà annotées (la nouvelle étiquette remplace)."
        ),
    ] = False,
    export: Annotated[
        Path | None, typer.Option("--export", help="Écrit un CSV à annoter dans un tableur.")
    ] = None,
    import_: Annotated[
        list[Path] | None,
        typer.Option("--import", help="Importe un CSV annoté (option répétable)."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Avec --import : compteurs seulement, rien d'écrit.")
    ] = False,
) -> None:
    """Annote des offres réelles : en interactif, par CSV (--export puis --import).

    Affiche ou exporte des offres réelles : à utiliser en local ou dans le dépôt privé. Les
    annotations ne dépendent pas de la version du profil : on ne réannote qu'à la demande.
    """
    if export is not None and import_:
        raise _fail("--export et --import sont exclusifs")
    if dry_run and not import_:
        raise _fail("--dry-run s'utilise avec --import")
    if import_ and (n is not None or relabel):
        raise _fail("--n et --relabel ne s'appliquent pas à --import")
    match, profile = _match_setup(config)
    key = scoring_key(match, profile, f"{match.embeddings.provider}:{match.embeddings.model}")
    if import_:
        _import_labels(db, profile, import_, dry_run)
        return
    with _database(db, write=False) as store:
        labels = LabelStore(store)
        done = set(labels.latest())
        count = n if n is not None else (50 if export is not None else 20)
        if relabel:
            seed = f"relabel:{len(done)}:{labels.newest()}"  # un autre tirage après chaque passe
            sample = sample_for_labeling(store, key, count, set(), seed, only=done)
        else:
            sample = sample_for_labeling(store, key, count, done)
        if not sample:
            typer.echo("aucune offre annotée à revoir" if relabel else "aucune offre à annoter")
            return
        if export is not None:
            written = export_csv(sample, export)
            typer.echo(f"{written} offre(s) exportée(s) dans {export.name} (UTF-8, séparateur ;)")
            return
    _label_interactively(db, sample, profile)


def _label_interactively(db: Path | None, sample: list[Offer], profile: Profile) -> None:
    with _database(db, write=True) as store:
        labels = LabelStore(store)
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
    typer.echo(_summary_line(added, total.total, total.relevant))


def _summary_line(added: int, total: int, relevant: int) -> str:
    return (
        f"{added} étiquette(s) posée(s) ; total {total} dont {relevant} pertinente(s) "
        f"(objectif : {MIN_LABELED} dont {MIN_RELEVANT})"
    )


def _import_labels(db: Path | None, profile: Profile, files: list[Path], dry_run: bool) -> None:
    """Importe un ou plusieurs CSV annotés ; les erreurs sont signalées par ligne et motif, jamais
    par contenu, et n'empêchent pas l'import des autres lignes."""
    with _database(db, write=not dry_run) as store:
        labels = LabelStore(store)
        now = datetime.now(UTC)
        read = annotated = skipped = failed = 0
        for file in files:
            try:
                parsed = read_import(file)
            except CsvError as exc:
                raise _fail(f"{file.name} : {exc}") from None
            known = store.existing_uids(list({row.uid for row in parsed.rows}))
            errors = list(parsed.errors)
            for row in parsed.rows:
                if row.uid not in known:
                    errors.append((row.line, "uid inconnu"))
                    continue
                annotated += 1
                if not dry_run:
                    labels.put(row.uid, profile.profile_hash, row.label, now)
            for line, reason in sorted(errors):
                typer.echo(f"{file.name} ligne {line} : {reason}", err=True)
            read += parsed.read
            skipped += parsed.skipped
            failed += len(errors)
        total = stats(load_from_store(store))
    typer.echo(
        f"lignes lues : {read}, annotées : {annotated}, ignorées (non annotées) : {skipped}, "
        f"en erreur : {failed}" + (" (dry-run : rien d'écrit)" if dry_run else "")
    )
    typer.echo(_summary_line(annotated if not dry_run else 0, total.total, total.relevant))


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
    with _database(db, write=True) as store:
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
