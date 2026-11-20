"""Lecture IMAP **en lecture seule** d'un dossier dédié.

Garanties : ``select(readonly=True)``, ``BODY.PEEK`` (ne marque rien comme lu), aucune commande
d'écriture (``STORE``, ``COPY``, ``EXPUNGE``, ``DELETE``…). Les en-têtes seuls sont lus d'abord : le
corps d'un email n'est téléchargé que s'il vient d'un expéditeur autorisé et n'a pas été traité.
Aucun identifiant ni contenu d'email n'est journalisé, et les erreurs n'exposent pas le texte du
serveur.
"""

import email
import email.policy
import email.utils
import hashlib
import imaplib
import logging
import re
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import SecretStr

logger = logging.getLogger(__name__)

PORT = 993
HEADER_BATCH = 100
MAX_MESSAGES = 300  # corps téléchargés par exécution ; le reste attend l'exécution suivante
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailboxError(Exception):
    """Échec de lecture de la boîte. Le message ne contient aucun identifiant ni texte serveur."""


@dataclass(frozen=True)
class RawMessage:
    message_id: str
    from_address: str
    date: datetime
    raw: bytes  # message complet (RFC 822)


class Mailbox(Protocol):
    def messages(self, since: datetime, skip: Callable[[str, str], bool]) -> Iterator[RawMessage]:
        """Messages reçus depuis ``since``, sauf ceux pour lesquels ``skip(message_id, from)``."""
        ...


def imap_date(moment: datetime) -> str:
    """Date au format IMAP (``20-Nov-2026``), indépendante de la langue du système."""
    moment = moment.astimezone(UTC)
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year}"


def _fetched(data: list[Any]) -> list[tuple[int, bytes]]:
    items = []
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            match = re.search(rb"UID (\d+)", item[0])
            if match:
                items.append((int(match.group(1)), bytes(item[1])))
    return items


def parse_headers(raw: bytes) -> tuple[str, str, datetime]:
    """(Message-ID, adresse d'expéditeur en minuscules, date UTC) d'un bloc d'en-têtes."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    sender = email.utils.parseaddr(str(message.get("From") or ""))[1].lower()
    date_text = str(message.get("Date") or "")
    try:
        date = email.utils.parsedate_to_datetime(date_text)
        date = date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)
    except (TypeError, ValueError):
        date = datetime.now(UTC)
    message_id = str(message.get("Message-ID") or "").strip()
    if not message_id:
        message_id = "synthetic:" + hashlib.sha256(f"{sender}|{date_text}".encode()).hexdigest()
    return message_id, sender, date


class ImapMailbox:
    def __init__(
        self,
        host: str,
        user: SecretStr,
        password: SecretStr,
        folder: str,
        *,
        port: int = PORT,
        imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
    ) -> None:
        self._host = host
        self._user = user
        self._password = password
        self._folder = folder
        self._port = port
        self._factory = imap_factory

    def _connect(self) -> Any:
        try:
            connection = self._factory(self._host, self._port)
        except (OSError, ssl.SSLError, imaplib.IMAP4.error):
            raise MailboxError("connexion IMAP impossible") from None
        try:
            connection.login(self._user.get_secret_value(), self._password.get_secret_value())
        except imaplib.IMAP4.error:
            raise MailboxError("authentification IMAP refusée") from None
        try:
            status, _ = connection.select(f'"{self._folder}"', readonly=True)
        except imaplib.IMAP4.error:
            status = "NO"
        if status != "OK":
            raise MailboxError("dossier IMAP introuvable ou inaccessible")
        return connection

    def messages(self, since: datetime, skip: Callable[[str, str], bool]) -> Iterator[RawMessage]:
        connection = self._connect()
        try:
            try:
                yield from self._read(connection, since, skip)
            except (OSError, ssl.SSLError, imaplib.IMAP4.error):
                raise MailboxError("lecture IMAP interrompue") from None
        finally:
            try:
                connection.logout()
            except (OSError, imaplib.IMAP4.error):  # pragma: no cover
                pass

    def _read(
        self, connection: Any, since: datetime, skip: Callable[[str, str], bool]
    ) -> Iterator[RawMessage]:
        status, data = connection.uid("SEARCH", None, "SINCE", imap_date(since))
        if status != "OK":
            raise MailboxError("recherche IMAP impossible")
        uids = [int(u) for u in (data[0] or b"").split()]
        wanted: list[int] = []
        for start in range(0, len(uids), HEADER_BATCH):
            batch = ",".join(str(u) for u in uids[start : start + HEADER_BATCH])
            status, rows = connection.uid(
                "FETCH", batch, "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID DATE)])"
            )
            if status != "OK":
                raise MailboxError("lecture des en-têtes impossible")
            for uid, header in _fetched(rows):
                message_id, sender, _ = parse_headers(header)
                if not skip(message_id, sender):
                    wanted.append(uid)
        if len(wanted) > MAX_MESSAGES:
            logger.warning(
                "%d messages à lire, %d traités maintenant, le reste à la prochaine exécution.",
                len(wanted),
                MAX_MESSAGES,
            )
            wanted = wanted[:MAX_MESSAGES]
        for uid in wanted:
            status, rows = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                raise MailboxError("lecture d'un message impossible")
            for _, body in _fetched(rows):
                message_id, sender, date = parse_headers(body)
                yield RawMessage(message_id, sender, date, body)
