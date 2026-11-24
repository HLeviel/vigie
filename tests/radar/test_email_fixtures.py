"""Garde-fous sur les .eml committés : ils doivent être anonymes (le dépôt est public)."""

import email
import email.policy
import re
from pathlib import Path

import pytest

from tests.radar.email_helpers import EMAILS

# En-têtes qui identifient l'utilisateur, son compte ou son infrastructure de réception.
FORBIDDEN_HEADERS = {
    "delivered-to", "received", "x-received", "return-path", "authentication-results",
    "received-spf", "dkim-signature", "x-pole-emploi-id", "x-cse-connectionguid",
    "x-cse-msgguid", "cc", "bcc", "x-gm-message-state",
}  # fmt: skip
FILES = sorted(EMAILS.glob("*.eml"))


def parsed(path: Path) -> email.message.EmailMessage:
    return email.message_from_bytes(path.read_bytes(), policy=email.policy.default)  # type: ignore[return-value]


def test_there_are_fixtures() -> None:
    assert len(FILES) >= 8


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_personal_headers(path: Path) -> None:
    keys = {k.lower() for k in parsed(path).keys()}
    assert not keys & FORBIDDEN_HEADERS, f"{path.name} : {keys & FORBIDDEN_HEADERS}"
    assert not any(k.startswith("arc-") for k in keys)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_recipient_is_fictitious(path: Path) -> None:
    message = parsed(path)
    assert str(message["To"]).endswith(("@example.org", "@example.com"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_real_mail_account_or_tracking_token_in_the_body(path: Path) -> None:
    text = path.read_bytes().decode("utf-8", errors="replace")
    body = parsed(path).get_body(preferencelist=("html", "plain"))
    assert body is not None
    content = str(body.get_content())
    for source in (text, content):
        assert not re.search(r"@(gmail|outlook|hotmail|yahoo)\.", source, re.IGNORECASE)
        assert "X-Pole-Emploi-ID" not in source
    # un lien de suivi personnel ressemble à un long jeton ; les gabarits réels n'en ont pas ici
    for url in re.findall(r'href="([^"]+)"', content):
        assert not re.search(r"[A-Za-z0-9_]{40,}", url.split("?")[0]), url


def test_message_ids_are_fictitious() -> None:
    for path in FILES:
        assert (
            str(parsed(path)["Message-ID"]).strip("<>").endswith(("example.org", "fixture.example"))
        )
