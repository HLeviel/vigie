"""Point d'entrée unique pour les secrets et la configuration.

Aucun autre module ne doit lire ``os.environ``. Les secrets sont typés
``SecretStr`` : on n'appelle ``get_secret_value()`` qu'au moment de l'usage.
Aucun secret n'a de valeur par défaut : une variable absente vaut ``None``.
"""

from collections.abc import Iterable
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Variables requises par source ou fonctionnalité activée (noms d'environnement).
REQUIRED_BY_FEATURE: dict[str, tuple[str, ...]] = {
    "france_travail": ("FT_CLIENT_ID", "FT_CLIENT_SECRET"),
    "adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"),
    "imap": ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "IMAP_FOLDER"),
    "email": ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO"),
    "state_s3": ("VIGIE_STATE_BUCKET",),
}


class MissingSettingsError(Exception):
    """Une variable requise est absente. Le message ne contient que des noms."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__("Variables d'environnement manquantes : " + ", ".join(names))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # France Travail (en attente d'accès : source livrée désactivée)
    ft_client_id: SecretStr | None = None
    ft_client_secret: SecretStr | None = None
    # Scope OAuth2 : non secret, à aligner sur la documentation de l'application francetravail.io
    ft_scope: str = "api_offresdemploiv2 o2dsoffre"

    # Adzuna (lot M1)
    adzuna_app_id: SecretStr | None = None
    adzuna_app_key: SecretStr | None = None

    # Lecture des alertes email (lot M1 ; lecture seule, mot de passe d'application)
    imap_host: str | None = None
    imap_user: SecretStr | None = None
    imap_password: SecretStr | None = None
    imap_folder: str | None = None

    # Envoi du digest (lot M2)
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: SecretStr | None = None
    smtp_password: SecretStr | None = None
    digest_to: SecretStr | None = None

    # Chemin de la base SQLite (non secret) ; défaut : ~/.local/share/vigie/radar.db
    vigie_db_path: Path | None = None

    # État persistant (lot M2)
    vigie_state_bucket: str | None = None

    def missing(self, features: Iterable[str]) -> list[str]:
        """Noms des variables absentes pour les fonctionnalités données."""
        names: list[str] = []
        for feature in features:
            for name in REQUIRED_BY_FEATURE[feature]:
                if getattr(self, name.lower()) is None and name not in names:
                    names.append(name)
        return names

    def require(self, features: Iterable[str]) -> None:
        """Arrête avec un message nommant les variables manquantes, sans aucune valeur."""
        names = self.missing(features)
        if names:
            raise MissingSettingsError(names)
