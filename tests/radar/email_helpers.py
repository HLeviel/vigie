"""Boîte factice et chargement des .eml pour les tests des alertes email."""

import email
import email.policy
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from vigie.radar.config import EmailAlertsConfig
from vigie.radar.sources.email_alerts.mailbox import RawMessage, parse_headers

EMAILS = Path(__file__).parent / "fixtures" / "emails"

ALLOWED = {
    "france_travail": ["@francetravail.fr"],
    "apec": ["@apec.fr"],
    "wttj": ["@welcometothejungle.com"],
    "linkedin": ["@linkedin.com"],
}


def config(allowed: dict[str, list[str]] | None = None) -> EmailAlertsConfig:
    return EmailAlertsConfig(allowed_senders=ALLOWED if allowed is None else allowed)


def load_raw(name: str) -> bytes:
    return (EMAILS / f"{name}.eml").read_bytes()


def load_message(name: str) -> RawMessage:
    raw = load_raw(name)
    message_id, sender, date = parse_headers(raw)
    return RawMessage(message_id, sender, date, raw)


class FakeMailbox:
    """Rejoue des .eml ; mémorise ce qui a été lu et ce qui a été ignoré."""

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.bodies_read: list[str] = []
        self.skipped: list[str] = []
        self.since: list[datetime] = []

    def messages(self, since: datetime, skip: Callable[[str, str], bool]) -> Iterator[RawMessage]:
        self.since.append(since)
        for name in self._names:
            message = load_message(name)
            if skip(message.message_id, message.from_address):
                self.skipped.append(name)
                continue
            self.bodies_read.append(name)
            yield message


def html_of(name: str) -> str:
    message = email.message_from_bytes(load_raw(name), policy=email.policy.default)
    body = message.get_body(preferencelist=("html",))
    assert body is not None
    return str(body.get_content())
