"""Interface en ligne de commande : ``vigie match check`` (étape 1 ; les autres commandes suivent).

Comme pour ``radar``, la sortie n'affiche que des compteurs et des noms de variables : jamais le
contenu du profil, ni un secret.
"""

from pathlib import Path
from typing import Annotated

import typer

from vigie.match.profile import ProfileError, load_profile_file
from vigie.radar.config import ConfigError, resolve_config
from vigie.settings import Settings

app = typer.Typer(help="match : score d'adéquation offre / profil.", no_args_is_help=True)

ConfigOption = Annotated[
    Path | None, typer.Option("--config", help="Fichier de configuration YAML.")
]

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
