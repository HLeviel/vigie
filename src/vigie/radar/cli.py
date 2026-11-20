"""Interface en ligne de commande : ``vigie radar run | check | stats``.

La commande ``digest`` arrive avec le module ``digest.py`` (étape 7 de la spec).
Les sorties n'affichent que des compteurs et des noms de variables, jamais de secret,
d'intitulé d'offre ni de critère de recherche.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from vigie.radar.config import ConfigError, RadarConfig, resolve_config
from vigie.radar.pipeline import (
    EXIT_CONFIG,
    SourcePlan,
    UnknownSourceSelectionError,
    plan_sources,
    run_pipeline,
)
from vigie.radar.sources.ats import SUPPORTED_PLATFORMS
from vigie.radar.store import DEFAULT_DB_PATH, Store, StoreError
from vigie.settings import Settings

root = typer.Typer(
    name="vigie", help="Vigie : copilote de recherche d'emploi.", no_args_is_help=True
)
app = typer.Typer(help="radar : veille quotidienne des offres.", no_args_is_help=True)
root.add_typer(app, name="radar")

ConfigOption = Annotated[
    Path | None, typer.Option("--config", help="Fichier de configuration YAML.")
]
DbOption = Annotated[Path | None, typer.Option("--db", help="Chemin de radar.db.")]


def _settings() -> Settings:
    return Settings()


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(EXIT_CONFIG)


def _load(config_path: Path | None, settings: Settings) -> RadarConfig:
    try:
        return resolve_config(config_path, settings)
    except ConfigError as exc:
        raise _fail(str(exc)) from None


def _db_path(db: Path | None, settings: Settings) -> Path:
    return db or settings.vigie_db_path or DEFAULT_DB_PATH


def _describe_ats(cfg: RadarConfig) -> None:
    """Compteurs seulement : les entreprises ciblées sont confidentielles."""
    if not cfg.sources.ats.enabled:
        return
    if not cfg.ats_companies:
        typer.echo("source ats : aucune entreprise dans ats_companies")
        return
    unsupported = sorted(
        {c.platform for c in cfg.ats_companies if c.platform not in SUPPORTED_PLATFORMS}
    )
    count = sum(c.platform in unsupported for c in cfg.ats_companies)
    if unsupported:
        typer.echo(
            f"source ats : {count} entreprise(s) sur une plateforme non prise en charge "
            f"({', '.join(unsupported)}), ignorée(s)"
        )


def _describe_plan(plan: SourcePlan) -> None:
    for name in plan.active:
        typer.echo(f"source {name} : active")
    for name in plan.pending_access:
        typer.echo(f"source {name} : en attente d'accès (identifiants absents), ignorée")
    for name in plan.not_implemented:
        typer.echo(f"source {name} : pas encore implémentée, ignorée")


@app.command()
def check(config: ConfigOption = None) -> None:
    """Vérifie la configuration et la présence des secrets, sans appel réseau."""
    settings = _settings()
    cfg = _load(config, settings)
    typer.echo(f"configuration valide : {len(cfg.searches)} recherche(s)")
    try:
        plan = plan_sources(cfg, settings)
    except UnknownSourceSelectionError as exc:  # pragma: no cover (sans --source)
        raise _fail(str(exc)) from None
    _describe_plan(plan)
    _describe_ats(cfg)
    if plan.missing_variables:
        raise _fail("variables d'environnement manquantes : " + ", ".join(plan.missing_variables))
    typer.echo("secrets : présents")


@app.command()
def run(
    config: ConfigOption = None,
    db: DbOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Collecte sans rien écrire en base.")
    ] = False,
    source: Annotated[str | None, typer.Option("--source", help="Une seule source.")] = None,
    since: Annotated[
        datetime | None,
        typer.Option("--since", formats=["%Y-%m-%d"], help="Date de début (AAAA-MM-JJ)."),
    ] = None,
) -> None:
    """Collecte, dédoublonne, filtre et stocke les nouvelles offres."""
    settings = _settings()
    cfg = _load(config, settings)
    try:
        plan = plan_sources(cfg, settings, only=source)
    except UnknownSourceSelectionError as exc:
        raise _fail(str(exc)) from None
    _describe_plan(plan)
    if plan.missing_variables:
        raise _fail("variables d'environnement manquantes : " + ", ".join(plan.missing_variables))

    path = _db_path(db, settings)
    try:
        store = Store.open_copy(path) if dry_run else Store.open(path)
    except StoreError as exc:
        raise _fail(str(exc)) from None
    since_utc = since.replace(tzinfo=UTC) if since else None
    with store:
        result = run_pipeline(cfg, settings, store, plan, since=since_utc)

    for report in result.report.sources:
        flag = " ÉCHEC" if report.error_message else ""
        typer.echo(
            f"{report.source} : récupérées={report.fetched} nouvelles={report.new} "
            f"filtrées={report.filtered} doublons={report.duplicates} "
            f"erreurs={report.errors} ({report.duration_ms} ms){flag}"
        )
    typer.echo(f"offres à notifier : {result.pending}")
    if dry_run:
        typer.echo("dry-run : rien n'a été écrit")
    raise typer.Exit(result.exit_code)


@app.command()
def stats(
    db: DbOption = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Nombre d'exécutions.")] = 5,
) -> None:
    """Derniers runs et compteurs par source."""
    settings = _settings()
    path = _db_path(db, settings)
    if not path.exists():
        typer.echo("aucune base : lancer d'abord `vigie radar run`")
        return
    with Store.open(path) as store:
        runs = store.recent_runs(limit)
        pending = store.count_pending()
    if not runs:
        typer.echo("aucune exécution enregistrée")
    for run_info in runs:
        typer.echo(f"{run_info['started_at']} : {run_info['status']}")
        for src in run_info["sources"]:
            flag = " ÉCHEC" if src["error_message"] else ""
            typer.echo(
                f"  {src['source']} : récupérées={src['fetched']} nouvelles={src['new']} "
                f"filtrées={src['filtered']} doublons={src['duplicates']} "
                f"erreurs={src['errors']}{flag}"
            )
    typer.echo(f"offres à notifier : {pending}")


@root.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(message)s")
