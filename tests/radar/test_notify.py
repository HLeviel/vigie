import logging
import smtplib
import ssl
from typing import Any

import pytest

from tests.radar.factories import NOW, make_offer, make_settings
from vigie.radar.config import DigestConfig
from vigie.radar.digest import build_digest
from vigie.radar.notify import NotifyError, SmtpSender, build_message, parse_recipients
from vigie.radar.store import Store
from vigie.settings import MissingSettingsError, Settings

PASSWORD = "mot-de-passe-smtp-secret"
USER = "moi@example.org"
HOST = "smtp.example.org"


class FakeSmtp:
    events: list[str] = []
    sent: list[Any] = []
    kwargs: dict[str, Any] = {}
    fail_at: str | None = None

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        FakeSmtp.events.append("init")
        FakeSmtp.kwargs = {"host": host, "port": port, **kwargs}
        self.login_args: tuple[str, str] | None = None

    def _maybe_fail(self, step: str) -> None:
        if FakeSmtp.fail_at == step:
            raise {
                "starttls": smtplib.SMTPNotSupportedError(f"STARTTLS refusé par {HOST}"),
                "login": smtplib.SMTPAuthenticationError(
                    535, f"mauvais mot de passe {PASSWORD}".encode()
                ),
                "send": smtplib.SMTPRecipientsRefused({USER: (550, b"refus")}),
                "quit": smtplib.SMTPServerDisconnected(f"coupé {HOST}"),
            }[step]

    def starttls(self, context: ssl.SSLContext) -> None:
        FakeSmtp.events.append("starttls")
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED  # vérification du certificat
        self._maybe_fail("starttls")

    def login(self, user: str, password: str) -> None:
        FakeSmtp.events.append("login")
        self.login_args = (user, password)
        FakeSmtp.kwargs["login"] = (user, password)
        self._maybe_fail("login")

    def send_message(self, message: Any) -> None:
        FakeSmtp.events.append("send")
        self._maybe_fail("send")
        FakeSmtp.sent.append(message)

    def quit(self) -> None:
        FakeSmtp.events.append("quit")
        self._maybe_fail("quit")


@pytest.fixture(autouse=True)
def reset() -> None:
    FakeSmtp.events = []
    FakeSmtp.sent = []
    FakeSmtp.kwargs = {}
    FakeSmtp.fail_at = None


def smtp_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "smtp_host": HOST,
        "smtp_port": 587,
        "smtp_user": USER,
        "smtp_password": PASSWORD,
        "digest_to": "a@example.org, b@example.org",
    }
    values.update(overrides)
    return make_settings(**values)


def make_digest(**offer: Any) -> Any:
    store = Store.open(":memory:")
    store.insert_offer(make_offer(**offer), NOW)
    return build_digest(store, DigestConfig(), NOW)


def sender(settings: Settings | None = None) -> SmtpSender:
    return SmtpSender(settings or smtp_settings(), smtp_factory=FakeSmtp, ssl_factory=FakeSmtp)


def test_sends_over_starttls_before_login() -> None:
    sender().send(make_digest())
    assert FakeSmtp.events == ["init", "starttls", "login", "send", "quit"]
    assert FakeSmtp.kwargs["login"] == (USER, PASSWORD)
    assert FakeSmtp.kwargs["port"] == 587
    assert FakeSmtp.kwargs["timeout"] == 30


def test_message_content() -> None:
    sender().send(make_digest(title="Data Engineer"))
    [message] = FakeSmtp.sent
    assert message["Subject"] == "Vigie : 1 nouvelle offre"
    assert message["From"] == USER
    assert message["To"] == "a@example.org, b@example.org"
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert plain.startswith("Vigie : 1 nouvelle offre")
    assert "https://example.org/offre/1" in plain
    assert "Data Engineer" in plain
    assert html.startswith("<!doctype html>")
    assert "Data Engineer</a>" in html


def test_hostile_offer_text_is_escaped_in_html_and_plain_in_text() -> None:
    sender().send(make_digest(title="<script>alert(1)</script> [x](javascript:y)"))
    [message] = FakeSmtp.sent
    html = message.get_body(preferencelist=("html",)).get_content()
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert "<script" not in html
    assert "&lt;script&gt;" in html
    # la partie texte n'interprète rien : le texte y est lisible tel quel, sans échappements
    assert "<script>alert(1)</script> [x](javascript:y)" in plain
    assert "\\" not in plain


def test_implicit_tls_on_port_465() -> None:
    sender(smtp_settings(smtp_port=465)).send(make_digest())
    assert FakeSmtp.events == ["init", "login", "send", "quit"]  # pas de starttls : déjà en TLS
    assert isinstance(FakeSmtp.kwargs["context"], ssl.SSLContext)


def test_no_login_if_tls_cannot_be_established(caplog: pytest.LogCaptureFixture) -> None:
    FakeSmtp.fail_at = "starttls"
    with caplog.at_level(logging.DEBUG), pytest.raises(NotifyError) as exc:
        sender().send(make_digest())
    assert "login" not in FakeSmtp.events  # le mot de passe n'est jamais envoyé en clair
    assert "quit" in FakeSmtp.events
    for text in (str(exc.value), repr(exc.value), caplog.text):
        assert HOST not in text
        assert PASSWORD not in text


def test_authentication_error_hides_server_text() -> None:
    FakeSmtp.fail_at = "login"
    with pytest.raises(NotifyError) as exc:
        sender().send(make_digest())
    for text in (str(exc.value), repr(exc.value)):
        assert PASSWORD not in text
        assert "535" not in text
    assert exc.value.__suppress_context__


def test_send_error_and_connection_error_are_generic() -> None:
    FakeSmtp.fail_at = "send"
    with pytest.raises(NotifyError) as exc:
        sender().send(make_digest())
    assert USER not in str(exc.value)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(f"connexion refusée vers {HOST}")

    with pytest.raises(NotifyError) as exc2:
        SmtpSender(smtp_settings(), smtp_factory=refuse).send(make_digest())
    assert HOST not in str(exc2.value)


def test_quit_failure_is_ignored() -> None:
    FakeSmtp.fail_at = "quit"
    sender().send(make_digest())
    assert len(FakeSmtp.sent) == 1


@pytest.mark.parametrize(
    "value", ["", "pas-une-adresse", "a@b.c; Bcc: x@y.z", "a@b.c\nBcc: x@y.z", "<a@b.c>", ","]
)
def test_invalid_recipients_never_open_a_connection(value: str) -> None:
    with pytest.raises(NotifyError) as exc:
        sender(smtp_settings(digest_to=value)).send(make_digest())
    assert "init" not in FakeSmtp.events
    assert value.strip() not in str(exc.value) or not value.strip()


def test_parse_recipients() -> None:
    assert parse_recipients(" a@b.fr ,c@d.fr,") == ["a@b.fr", "c@d.fr"]


def test_sender_falls_back_on_the_first_recipient_if_user_is_not_an_address() -> None:
    sender(smtp_settings(smtp_user="identifiant-sans-arobase")).send(make_digest())
    assert FakeSmtp.sent[0]["From"] == "a@example.org"


def test_header_injection_is_refused() -> None:
    digest = make_digest()
    with pytest.raises(NotifyError):
        build_message(digest, "moi@example.org\nBcc: x@y.z", ["a@example.org"])


def test_missing_variables_are_named() -> None:
    with pytest.raises(MissingSettingsError) as exc:
        SmtpSender(make_settings(smtp_host=HOST))
    assert "SMTP_PASSWORD" in str(exc.value)
    assert "DIGEST_TO" in str(exc.value)
    assert HOST not in str(exc.value)


def test_alert_digest_subject() -> None:
    store = Store.open(":memory:")
    digest = build_digest(store, DigestConfig(), NOW)
    assert build_message(digest, USER, [USER])["Subject"] == "Vigie : alerte, une source a échoué"


def error_message(fail_at: str | None, smtp_port: int = 587) -> str:
    FakeSmtp.fail_at = fail_at
    with pytest.raises(NotifyError) as exc:
        if fail_at == "connect":

            def refuse(*args: Any, **kwargs: Any) -> Any:
                raise OSError(f"connexion refusée vers {HOST}")

            SmtpSender(smtp_settings(smtp_port=smtp_port), smtp_factory=refuse).send(make_digest())
        else:
            sender(smtp_settings(smtp_port=smtp_port)).send(make_digest())
    text = str(exc.value)
    for secret in (HOST, USER, PASSWORD, "535", "550"):
        assert secret not in text
    return text


def test_the_failing_stage_is_named_without_any_server_text() -> None:
    assert "connexion au serveur SMTP" in error_message("connect")
    assert "négociation TLS" in error_message("starttls")
    assert "authentification SMTP" in error_message("login")
    assert "envoi du message" in error_message("send")


def test_implicit_tls_port_has_no_starttls_stage() -> None:
    assert "authentification SMTP" in error_message("login", smtp_port=465)
