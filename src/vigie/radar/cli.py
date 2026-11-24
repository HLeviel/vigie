"""Interface en ligne de commande : ``vigie radar run | check | stats | digest``.

Les sorties n'affichent que des compteurs et des noms de variables, jamais de secret, d'intitulé
d'offre ni de critère de recherche. Le digest, lui, contient les offres : il va dans un fichier
(créé en mode 0600), pas sur la sortie standard sauf demande explicite (``--output -``).
"""

import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from vigie.radar.config import ConfigError, RadarConfig, resolve_config
from vigie.radar.digest import build_digest, render
from vigie.radar.notify import DigestSender, NotifyError, SmtpSender
from vigie.radar.pipeline import (
    EXIT_CONFIG,
    EXIT_DELIVERY,
    EXIT_STATE,
    SourcePlan,
    UnknownSourceSelectionError,
    plan_sources,
    run_pipeline,
)
from vigie.radar.sources.ats import SUPPORTED_PLATFORMS
from vigie.radar.state import S3State, StateSyncError
from vigie.radar.store import DEFAULT_DB_PATH, Store, StoreError
from vigie.settings import REQUIRED_BY_FEATURE, Settings

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


def _sender(settings: Settings) -> DigestSender:
    return SmtpSender(settings)


def _state(settings: Settings) -> S3State | None:
    return S3State(settings.vigie_state_bucket) if settings.vigie_state_bucket else None


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(EXIT_CONFIG)


def _load(config_path: Path | None) -> RadarConfig:
    try:
        return resolve_config(config_path)
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
def check(
    config: ConfigOption = None,
    ci: Annotated[
        bool,
        typer.Option(
            "--ci", help="Exige aussi l'envoi du digest et l'état S3 (workflow planifié)."
        ),
    ] = False,
) -> None:
    """Vérifie la configuration et la présence des secrets, sans appel réseau."""
    settings = _settings()
    cfg = _load(config)
    typer.echo(f"configuration valide : {len(cfg.searches)} recherche(s)")
    try:
        plan = plan_sources(cfg, settings)
    except UnknownSourceSelectionError as exc:  # pragma: no cover (sans --source)
        raise _fail(str(exc)) from None
    _describe_plan(plan)
    _describe_ats(cfg)
    if plan.missing_variables:
        raise _fail("variables d'environnement manquantes : " + ", ".join(plan.missing_variables))
    email_missing = settings.missing(["email"])
    total = len(REQUIRED_BY_FEATURE["email"])
    if ci and email_missing or 0 < len(email_missing) < total:
        raise _fail("envoi du digest : variables manquantes : " + ", ".join(email_missing))
    typer.echo(
        "envoi du digest : configuré" if not email_missing else "envoi du digest : non configuré"
    )
    state_missing = settings.missing(["state_s3"])
    if ci and state_missing:
        raise _fail("état S3 : variables manquantes : " + ", ".join(state_missing))
    typer.echo("état : S3" if not state_missing else "état : fichier local seulement")
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
    send_email: Annotated[
        bool,
        typer.Option(
            "--email/--no-email", help="Envoie le digest par email si les variables SMTP existent."
        ),
    ] = True,
) -> None:
    """Collecte, dédoublonne, filtre, stocke, puis envoie le digest et synchronise l'état S3."""
    settings = _settings()
    cfg = _load(config)
    try:
        plan = plan_sources(cfg, settings, only=source)
    except UnknownSourceSelectionError as exc:
        raise _fail(str(exc)) from None
    _describe_plan(plan)
    if plan.missing_variables:
        raise _fail("variables d'environnement manquantes : " + ", ".join(plan.missing_variables))

    email_missing = settings.missing(["email"])
    email_total = len(REQUIRED_BY_FEATURE["email"])
    if send_email and not dry_run and 0 < len(email_missing) < email_total:
        raise _fail("envoi du digest : variables manquantes : " + ", ".join(email_missing))
    can_send = send_email and not email_missing

    state = _state(settings)
    path = _db_path(db, settings)
    scratch: tempfile.TemporaryDirectory[str] | None = None
    if dry_run and state:  # l'état distant est lu dans un dossier jetable : rien n'est conservé
        scratch = tempfile.TemporaryDirectory()
        path = Path(scratch.name) / "radar.db"
    try:
        remote = None
        if state:
            try:
                remote = state.pull(path)
            except StateSyncError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(EXIT_STATE) from None
            typer.echo(
                "état S3 : " + ("téléchargé" if remote.etag else "aucun état, première exécution")
            )
        try:
            store = Store.open_copy(path) if dry_run else Store.open(path)
        except StoreError as exc:
            raise _fail(str(exc)) from None
        since_utc = since.replace(tzinfo=UTC) if since else None
        now = datetime.now(UTC)
        with store:
            result = run_pipeline(cfg, settings, store, plan, since=since_utc)
            delivery = "disabled"
            if not dry_run and can_send:
                digest = build_digest(store, cfg.digest, now)
                if not digest.should_send:
                    delivery = "nothing"
                else:
                    try:
                        _sender(settings).send(digest)
                    except NotifyError:
                        delivery = "failed"
                    else:
                        store.mark_notified([item.uid for item in digest.items], now)
                        delivery = f"sent:{len(digest.items)}/{digest.total}"
            pending = store.count_pending()

        state_failed = False
        if state and remote is not None and not dry_run:
            try:
                state.push(path, remote)
                typer.echo("état S3 : synchronisé")
            except StateSyncError as exc:
                typer.echo(str(exc), err=True)
                state_failed = True
    finally:
        if scratch is not None:
            scratch.cleanup()

    for report in result.report.sources:
        flag = " ÉCHEC" if report.error_message else ""
        typer.echo(
            f"{report.source} : récupérées={report.fetched} nouvelles={report.new} "
            f"filtrées={report.filtered} doublons={report.duplicates} "
            f"erreurs={report.errors} ({report.duration_ms} ms){flag}"
        )
    typer.echo(f"offres à notifier : {pending}")
    typer.echo(_describe_delivery(delivery, dry_run, send_email, bool(email_missing)))
    if dry_run:
        typer.echo("dry-run : rien n'a été écrit, aucun email envoyé")
    code = result.exit_code
    if delivery == "failed":
        code = EXIT_DELIVERY
    if state_failed:
        code = EXIT_STATE
    raise typer.Exit(code)


def _describe_delivery(delivery: str, dry_run: bool, send_email: bool, not_configured: bool) -> str:
    if dry_run:
        return "digest : non envoyé (dry-run)"
    if not send_email:
        return "digest : envoi désactivé (--no-email)"
    if not_configured:
        return "digest : envoi non configuré (variables SMTP absentes)"
    if delivery == "failed":
        return "digest : échec de l'envoi, les offres restent à notifier"
    if delivery == "nothing":
        return "digest : aucune nouvelle offre et aucune source en échec, rien à envoyer"
    shown, _, total = delivery.removeprefix("sent:").partition("/")
    return f"digest : envoyé ({shown} offre(s) affichée(s) sur {total} à notifier)"


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


@app.command()
def digest(
    config: ConfigOption = None,
    db: DbOption = None,
    output: Annotated[
        str | None,
        typer.Option("--output", help="Fichier de sortie ; `-` pour la sortie standard."),
    ] = None,
    mark_notified: Annotated[
        bool,
        typer.Option(
            "--mark-notified", help="Marque les offres affichées comme notifiées après l'écriture."
        ),
    ] = False,
) -> None:
    """Génère le digest des nouveautés depuis la base, sans collecter."""
    settings = _settings()
    cfg = _load(config)
    path = _db_path(db, settings)
    if not path.exists():
        raise _fail("aucune base : lancer d'abord `vigie radar run`")
    now = datetime.now(UTC)
    try:
        store = Store.open(path)
    except StoreError as exc:
        raise _fail(str(exc)) from None
    with store:
        result = build_digest(store, cfg.digest, now)
        if not result.should_send:
            typer.echo("aucune nouvelle offre et aucune source en échec : rien à écrire")
            return
        content = render(result)
        target = output or f"digest.{'html' if cfg.digest.format == 'html' else 'md'}"
        if target == "-":
            typer.echo(content, nl=False)
        else:
            _write_private(Path(target), content)
        if mark_notified and result.items:
            store.mark_notified([item.uid for item in result.items], now)
    typer.echo(
        f"digest : {len(result.items)} offre(s) affichée(s) sur {result.total} à notifier"
        + (" (alerte : une source a échoué)" if result.has_failure else ""),
        err=target == "-",
    )


def _write_private(path: Path, content: str) -> None:
    """Écrit le fichier en mode 0600 : il contient les offres retenues, donc des données privées."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # un fichier déjà présent garderait ses anciens droits
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


@root.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(message)s")
