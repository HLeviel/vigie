"""Envoi du digest par email (SMTP, TLS obligatoire).

Le message contient une version texte brute et une version HTML. Les identifiants ne sont lus
qu'au moment de la connexion ; aucun message d'erreur n'expose d'hôte, d'identifiant, d'adresse ni
de texte serveur. Une connexion qui ne peut pas passer en TLS est abandonnée avant toute
authentification.
"""

import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any, Protocol

from vigie.radar.digest import Digest, render_html, render_text
from vigie.settings import MissingSettingsError, Settings

TIMEOUT = 30
SSL_PORT = 465
_ADDRESS = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+$")


class NotifyError(Exception):
    """Échec de l'envoi. Le message ne contient ni hôte, ni identifiant, ni adresse."""


class DigestSender(Protocol):
    def send(self, digest: Digest) -> None: ...


def parse_recipients(value: str) -> list[str]:
    recipients = [part.strip() for part in value.split(",") if part.strip()]
    if not recipients or not all(_ADDRESS.match(r) for r in recipients):
        raise NotifyError("DIGEST_TO invalide : une ou plusieurs adresses valides attendues")
    return recipients


def build_message(digest: Digest, sender: str, recipients: list[str]) -> EmailMessage:
    message = EmailMessage()
    try:
        message["Subject"] = digest.title
        message["From"] = sender
        message["To"] = ", ".join(recipients)
    except ValueError:  # retour à la ligne dans un en-tête : tentative d'injection
        raise NotifyError("en-tête de message invalide") from None
    message.set_content(render_text(digest))
    message.add_alternative(render_html(digest), subtype="html")
    return message


class SmtpSender:
    def __init__(
        self,
        settings: Settings,
        *,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        settings.require(["email"])
        if (
            settings.smtp_host is None
            or settings.smtp_port is None
            or settings.smtp_user is None
            or settings.smtp_password is None
            or settings.digest_to is None
        ):  # pragma: no cover (déjà garanti par require)
            raise MissingSettingsError(settings.missing(["email"]))
        self._settings = settings
        self._smtp_factory = smtp_factory
        self._ssl_factory = ssl_factory

    def send(self, digest: Digest) -> None:
        s = self._settings
        assert s.smtp_host is not None and s.smtp_port is not None  # noqa: S101 (typage)
        assert s.smtp_user is not None and s.smtp_password is not None  # noqa: S101
        assert s.digest_to is not None  # noqa: S101
        user = s.smtp_user.get_secret_value()
        recipients = parse_recipients(s.digest_to.get_secret_value())
        # L'identifiant SMTP sert d'expéditeur s'il ressemble à une adresse (Gmail), sinon le
        # premier destinataire : le digest est envoyé à soi-même.
        sender = user if _ADDRESS.match(user) else recipients[0]
        message = build_message(digest, sender, recipients)
        context = ssl.create_default_context()
        connection: Any = None
        stage = "connexion au serveur SMTP"
        try:
            if s.smtp_port == SSL_PORT:
                connection = self._ssl_factory(
                    s.smtp_host, s.smtp_port, timeout=TIMEOUT, context=context
                )
            else:
                connection = self._smtp_factory(s.smtp_host, s.smtp_port, timeout=TIMEOUT)
                stage = "négociation TLS (STARTTLS)"
                connection.starttls(context=context)  # sans TLS, on n'envoie jamais le mot de passe
            stage = "authentification SMTP (identifiant ou mot de passe d'application refusé ?)"
            connection.login(user, s.smtp_password.get_secret_value())
            stage = "envoi du message (expéditeur ou destinataire refusé ?)"
            connection.send_message(message)
        except (smtplib.SMTPException, OSError, ssl.SSLError):
            # L'étape en cause suffit à corriger la configuration ; le texte du serveur, lui, peut
            # contenir l'hôte, l'identifiant ou une adresse : il n'est jamais repris.
            raise NotifyError(f"envoi du digest impossible : échec à l'étape « {stage} »") from None
        finally:
            if connection is not None:
                try:
                    connection.quit()
                except (smtplib.SMTPException, OSError):
                    pass
