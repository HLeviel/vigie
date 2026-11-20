import email
import email.policy
import imaplib
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from tests.radar.email_helpers import load_raw
from vigie.radar.sources.email_alerts import mailbox as mb
from vigie.radar.sources.email_alerts.mailbox import (
    ImapMailbox,
    MailboxError,
    imap_date,
    parse_headers,
)

PASSWORD = "mot-de-passe-d-application"
USER = "utilisateur@example.org"
SINCE = datetime(2026, 11, 5, 8, 0, tzinfo=UTC)


def header_block(raw: bytes) -> bytes:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    lines = [f"{k}: {message[k]}" for k in ("From", "Message-ID", "Date") if message[k]]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class FakeImap:
    """Serveur IMAP minimal : enregistre toutes les commandes reçues."""

    instances: list["FakeImap"] = []
    messages: dict[int, bytes] = {}
    login_error: Exception | None = None
    select_status = "OK"

    def __init__(self, host: str, port: int = 993) -> None:
        self.host, self.port = host, port
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.logged_out = False
        FakeImap.instances.append(self)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", (user, password), {}))
        if FakeImap.login_error:
            raise FakeImap.login_error
        return "OK", [b"ok"]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", (folder,), {"readonly": readonly}))
        return FakeImap.select_status, [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid:" + command, args, {}))
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in FakeImap.messages).encode()]
        if command == "FETCH":
            wanted = [int(u) for u in str(args[0]).split(",")]
            body = "PEEK[]" in args[1]
            rows: list[Any] = []
            for uid in wanted:
                raw = FakeImap.messages[uid]
                payload = raw if body else header_block(raw)
                rows.append((f"{uid} (UID {uid} BODY[] {{{len(payload)}}}".encode(), payload))
                rows.append(b")")
            return "OK", rows
        raise AssertionError(f"commande inattendue : {command}")  # pragma: no cover

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", []


@pytest.fixture(autouse=True)
def fake_server() -> None:
    FakeImap.instances = []
    FakeImap.login_error = None
    FakeImap.select_status = "OK"
    FakeImap.messages = {
        1: load_raw("france_travail"),
        2: load_raw("unknown_sender"),
        3: load_raw("linkedin"),
    }


def mailbox(folder: str = "Vigie/Alertes") -> ImapMailbox:
    return ImapMailbox(
        "imap.example.org", SecretStr(USER), SecretStr(PASSWORD), folder, imap_factory=FakeImap
    )


def read(skip: Any = lambda mid, sender: False) -> list[mb.RawMessage]:
    return list(mailbox().messages(SINCE, skip))


def test_connection_is_read_only_and_never_writes() -> None:
    messages = read()
    server = FakeImap.instances[0]
    select = next(c for c in server.calls if c[0] == "select")
    assert select[2]["readonly"] is True
    assert select[1] == ('"Vigie/Alertes"',)  # nom quoté
    commands = {c[0] for c in server.calls}
    assert commands <= {"login", "select", "uid:SEARCH", "uid:FETCH"}
    # BODY.PEEK partout : rien n'est marqué comme lu
    for name, args, _ in server.calls:
        if name == "uid:FETCH":
            assert "PEEK" in args[1]
    assert server.logged_out
    assert server.port == 993
    assert [m.message_id for m in messages] == [
        "<ft-1@example.org>",
        "<promo-1@example.org>",
        "<li-1@example.org>",
    ]


def test_search_uses_an_imap_date() -> None:
    read()
    search = next(c for c in FakeImap.instances[0].calls if c[0] == "uid:SEARCH")
    assert search[1] == (None, "SINCE", "05-Nov-2026")


def test_skipped_messages_never_have_their_body_downloaded() -> None:
    read(skip=lambda mid, sender: not sender.endswith("@francetravail.fr"))
    fetches = [c for c in FakeImap.instances[0].calls if c[0] == "uid:FETCH"]
    bodies = [c for c in fetches if "PEEK[]" in c[1][1]]
    assert [c[1][0] for c in bodies] == ["1"]  # un seul corps téléchargé
    headers = [c for c in fetches if "HEADER.FIELDS" in c[1][1]]
    assert len(headers) == 1  # en-têtes lus en une seule requête groupée


def test_message_content_matches_the_source() -> None:
    [message] = read(skip=lambda mid, sender: "francetravail" not in sender)
    assert message.from_address == "alerte@francetravail.fr"
    assert message.date == datetime(2026, 11, 19, 7, 30, tzinfo=UTC)
    assert message.raw == load_raw("france_travail")


def test_login_error_hides_credentials(caplog: pytest.LogCaptureFixture) -> None:
    FakeImap.login_error = imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] {USER} {PASSWORD}")
    with caplog.at_level(logging.DEBUG), pytest.raises(MailboxError) as exc:
        read()
    for text in (str(exc.value), repr(exc.value), caplog.text):
        assert PASSWORD not in text
        assert USER not in text
    assert exc.value.__suppress_context__  # la cause d'origine n'est pas affichée


def test_connection_error_is_generic() -> None:
    def refuse(host: str, port: int) -> Any:
        raise OSError(f"Connection refused to {host}")

    box = ImapMailbox("h", SecretStr(USER), SecretStr(PASSWORD), "F", imap_factory=refuse)
    with pytest.raises(MailboxError) as exc:
        list(box.messages(SINCE, lambda m, s: False))
    assert "h" not in str(exc.value).replace("connexion", "")  # aucun nom d'hôte
    assert str(exc.value) == "connexion IMAP impossible"


def test_unknown_folder() -> None:
    FakeImap.select_status = "NO"
    with pytest.raises(MailboxError, match="dossier"):
        read()
    assert FakeImap.instances[0].logged_out is False  # jamais connecté à un dossier


def test_logout_even_if_the_consumer_stops_early() -> None:
    generator = mailbox().messages(SINCE, lambda m, s: False)
    next(generator)
    generator.close()
    assert FakeImap.instances[0].logged_out


def test_read_error_in_the_middle_is_generic() -> None:
    class Breaking(FakeImap):
        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            if command == "FETCH" and "PEEK[]" in args[1]:
                raise imaplib.IMAP4.abort(f"socket error {PASSWORD}")
            return super().uid(command, *args)

    box = ImapMailbox("h", SecretStr(USER), SecretStr(PASSWORD), "F", imap_factory=Breaking)
    with pytest.raises(MailboxError) as exc:
        list(box.messages(SINCE, lambda m, s: False))
    assert PASSWORD not in str(exc.value)


def test_cap_on_downloaded_messages(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mb, "MAX_MESSAGES", 2)
    FakeImap.messages = {i: load_raw("france_travail") for i in range(1, 6)}
    with caplog.at_level(logging.WARNING):
        assert len(read()) == 2
    assert "à la prochaine exécution" in caplog.text


def test_imap_date_is_locale_independent() -> None:
    assert imap_date(datetime(2026, 11, 5, tzinfo=UTC)) == "05-Nov-2026"
    assert imap_date(datetime(2026, 1, 31, 23, 0, tzinfo=UTC)) == "31-Jan-2026"
    assert imap_date(datetime(2026, 12, 1, 0, 0, tzinfo=UTC)) == "01-Dec-2026"


def test_parse_headers_edge_cases() -> None:
    message_id, sender, date = parse_headers(
        b"From: Quelqu'un <Alerte@Example.ORG>\r\nDate: Thu, 19 Nov 2026 07:30:00 +0100\r\n\r\n"
    )
    assert sender == "alerte@example.org"
    assert message_id.startswith("synthetic:")  # pas de Message-ID : identifiant stable
    assert date == datetime(2026, 11, 19, 6, 30, tzinfo=UTC)
    again, _, _ = parse_headers(
        b"From: Quelqu'un <Alerte@Example.ORG>\r\nDate: Thu, 19 Nov 2026 07:30:00 +0100\r\n\r\n"
    )
    assert again == message_id
    # date absente ou invalide : l'heure courante, sans erreur
    _, _, fallback = parse_headers(b"From: a@b.c\r\nDate: pas une date\r\n\r\n")
    assert fallback.tzinfo is UTC
    # date sans fuseau : interprétée en UTC
    _, _, naive = parse_headers(b"From: a@b.c\r\nDate: Thu, 19 Nov 2026 07:30:00 -0000\r\n\r\n")
    assert naive == datetime(2026, 11, 19, 7, 30, tzinfo=UTC)
