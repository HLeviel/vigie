"""`vigie radar run` en mode M2 : envoi du digest, état S3, codes de sortie 4 et 5."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.radar.factories import FakeSource, factory, make_offer, make_settings
from tests.radar.test_state import BUCKET, FakeS3, client_error
from vigie.radar import cli
from vigie.radar import sources as source_registry
from vigie.radar.digest import Digest
from vigie.radar.http import SourceHttpError
from vigie.radar.notify import NotifyError
from vigie.radar.state import S3State

runner = CliRunner()
SMTP_PASSWORD = "mot-de-passe-smtp-secret"
SMTP = {
    "smtp_host": "smtp.example.org",
    "smtp_port": 587,
    "smtp_user": "moi@example.org",
    "smtp_password": SMTP_PASSWORD,
    "digest_to": "moi@example.org",
}

CONFIG = """
sources:
  adzuna: { enabled: true }
searches:
  - { name: demo, keywords: [data], departments: ['69'] }
filters:
  require_any: [data]
"""


class FakeSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.digests: list[Digest] = []

    def send(self, digest: Digest) -> None:
        if self.fail:
            raise NotifyError("envoi du digest impossible")
        self.digests.append(digest)


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.config = tmp / "radar.yaml"
        self.config.write_text(CONFIG)
        self.db = tmp / "radar.db"
        self.sender = FakeSender()
        self.s3 = FakeS3()
        self.source_calls = 0
        self.sender_built = 0

    def use_source(self, monkeypatch: pytest.MonkeyPatch, source: FakeSource) -> None:
        def build(settings: Any, config: Any) -> FakeSource:
            self.source_calls += 1
            return source

        monkeypatch.setitem(source_registry.REGISTRY, "adzuna", build)

    def run(self, *extra: str) -> Any:
        return runner.invoke(
            cli.root,
            ["radar", "run", "--config", str(self.config), "--db", str(self.db), *extra],
        )


def offers(n: int = 3) -> list[Any]:
    return [make_offer(source_id=str(i), title=f"Data Engineer {i}") for i in range(n)]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.chdir(tmp_path)
    e = Env(tmp_path)
    monkeypatch.setattr(cli, "_settings", lambda: make_settings(**SMTP))

    def build_sender(settings: Any) -> FakeSender:
        e.sender_built += 1
        return e.sender

    monkeypatch.setattr(cli, "_sender", build_sender)
    monkeypatch.setattr(cli, "_state", lambda settings: None)
    return e


def with_s3(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "_settings",
        lambda: make_settings(vigie_state_bucket=BUCKET, **SMTP),
    )
    monkeypatch.setattr(cli, "_state", lambda settings: S3State(BUCKET, client=env.s3))


# --- envoi du digest --------------------------------------------------------------------------


def test_digest_is_sent_and_offers_are_marked_notified(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", offers(3)))
    result = env.run()
    assert result.exit_code == 0, result.output
    assert "digest : envoyé (3 offre(s) affichée(s) sur 3 à notifier)" in result.output
    assert "offres à notifier : 0" in result.output
    [digest] = env.sender.digests
    assert [i.title for i in digest.items] == [
        "Data Engineer 0",
        "Data Engineer 1",
        "Data Engineer 2",
    ]


def test_second_run_sends_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", offers(3)))
    env.run()
    second = env.run()
    assert second.exit_code == 0
    assert "nouvelles=0" in second.output
    assert "rien à envoyer" in second.output
    assert len(env.sender.digests) == 1  # pas d'email sans nouvelle offre


def test_send_failure_exits_4_and_offers_stay_pending_for_next_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", offers(3)))
    env.sender.fail = True
    failed = env.run()
    assert failed.exit_code == 4
    assert "échec de l'envoi" in failed.output
    assert "les offres restent à notifier" in failed.output
    assert "offres à notifier : 3" in failed.output
    env.sender.fail = False
    retry = env.run()
    assert retry.exit_code == 0
    assert "nouvelles=0" in retry.output  # rien n'est recollecté...
    assert "envoyé (3 offre(s) affichée(s) sur 3" in retry.output  # ...mais tout est renvoyé
    assert len(env.sender.digests) == 1


def test_alert_email_when_a_source_fails_without_offers(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use_source(
        monkeypatch, FakeSource("adzuna", [], error=SourceHttpError("adzuna : HTTP 500", 500))
    )
    result = env.run()
    assert result.exit_code == 2  # toutes les sources ont échoué ; l'envoi, lui, a réussi
    [digest] = env.sender.digests
    assert digest.items == ()
    assert digest.has_failure
    assert "envoyé (0 offre(s) affichée(s) sur 0" in result.output


def test_delivery_failure_takes_precedence_over_source_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", [], error=RuntimeError("x")))
    env.sender.fail = True
    assert env.run().exit_code == 4


def test_unconfigured_smtp_skips_sending_silently(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_settings", make_settings)
    env.use_source(monkeypatch, FakeSource("adzuna", offers(2)))
    result = env.run()
    assert result.exit_code == 0
    assert "envoi non configuré (variables SMTP absentes)" in result.output
    assert env.sender_built == 0
    assert "offres à notifier : 2" in result.output


def test_partially_configured_smtp_stops_before_collecting(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "_settings", lambda: make_settings(smtp_host="smtp.example.org", smtp_user="u")
    )
    env.use_source(monkeypatch, FakeSource("adzuna", offers(1)))
    result = env.run()
    assert result.exit_code == 1
    assert "SMTP_PORT" in result.output
    assert "DIGEST_TO" in result.output
    assert "smtp.example.org" not in result.output
    assert env.source_calls == 0
    assert not env.db.exists()


def test_no_email_option(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", offers(1)))
    result = env.run("--no-email")
    assert "envoi désactivé (--no-email)" in result.output
    assert env.sender_built == 0
    assert "offres à notifier : 1" in result.output


def test_dry_run_sends_nothing_and_writes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", offers(2)))
    result = env.run("--dry-run")
    assert result.exit_code == 0
    assert "digest : non envoyé (dry-run)" in result.output
    assert env.sender_built == 0
    assert not env.db.exists()


def test_console_never_shows_titles_or_secrets(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", [make_offer(title="Titre-Confidentiel")]))
    result = env.run()
    for forbidden in ("Confidentiel", SMTP_PASSWORD, "smtp.example.org", "Acme"):
        assert forbidden not in result.output


# --- état S3 ----------------------------------------------------------------------------------


def test_state_is_pulled_then_pushed_and_survives_a_fresh_runner(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_s3(env, monkeypatch)
    env.use_source(monkeypatch, FakeSource("adzuna", offers(3)))
    first = env.run()
    assert first.exit_code == 0, first.output
    assert "état S3 : aucun état, première exécution" in first.output
    assert "état S3 : synchronisé" in first.output
    assert env.s3.data is not None
    assert env.s3.puts[0]["IfNoneMatch"] == "*"

    env.db.unlink()  # un nouveau runner GitHub n'a pas de disque persistant
    second = env.run()
    assert second.exit_code == 0, second.output
    assert "état S3 : téléchargé" in second.output
    assert "nouvelles=0" in second.output  # tout est déjà connu
    assert "rien à envoyer" in second.output  # ... et déjà notifié
    assert env.s3.puts[1]["IfMatch"] == '"v1"'
    assert len(env.sender.digests) == 1


def test_read_failure_stops_before_collecting_or_sending(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_s3(env, monkeypatch)
    env.s3.get_error = client_error("AccessDenied")
    env.use_source(monkeypatch, FakeSource("adzuna", offers(3)))
    result = env.run()
    assert result.exit_code == 5
    assert "lecture de l'état S3 impossible" in result.output
    assert BUCKET not in result.output
    assert env.source_calls == 0  # jamais repartir d'un état vide : doublons d'emails
    assert env.sender.digests == []


def test_push_conflict_exits_5(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    with_s3(env, monkeypatch)
    # un état créé par une autre exécution entre le téléchargement et l'écriture
    env.s3.put_error = client_error("PreconditionFailed", "PutObject")
    env.use_source(monkeypatch, FakeSource("adzuna", offers(1)))
    result = env.run()
    assert result.exit_code == 5
    assert "a changé pendant l'exécution" in result.output
    assert BUCKET not in result.output
    assert len(env.sender.digests) == 1  # préférable à la perte d'un digest : doublon possible


def test_delivery_failure_still_syncs_the_state(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    with_s3(env, monkeypatch)
    env.sender.fail = True
    env.use_source(monkeypatch, FakeSource("adzuna", offers(2)))
    result = env.run()
    assert result.exit_code == 4
    assert env.s3.data is not None  # offres connues, non notifiées : renvoyées demain
    env.db.unlink()
    env.sender.fail = False
    retry = env.run()
    assert "envoyé (2 offre(s) affichée(s) sur 2" in retry.output


def test_dry_run_reads_the_remote_state_into_a_scratch_folder_and_never_pushes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_s3(env, monkeypatch)
    env.use_source(monkeypatch, FakeSource("adzuna", offers(2)))
    env.run()  # crée l'état distant
    env.db.unlink()
    puts_before = len(env.s3.puts)
    result = env.run("--dry-run")
    assert result.exit_code == 0
    assert "état S3 : téléchargé" in result.output
    assert len(env.s3.puts) == puts_before  # aucune écriture distante
    assert not env.db.exists()  # aucun fichier local conservé


def test_source_failure_still_pushes_state_with_exit_code_3(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_s3(env, monkeypatch)
    config = env.config.read_text().replace(
        "adzuna: { enabled: true }",
        "adzuna: { enabled: true }\n  france_travail: { enabled: true }",
    )
    env.config.write_text(config)
    monkeypatch.setattr(
        cli,
        "_settings",
        lambda: make_settings(
            vigie_state_bucket=BUCKET, ft_client_id="i", ft_client_secret="s", **SMTP
        ),
    )
    env.use_source(monkeypatch, FakeSource("adzuna", offers(1)))
    monkeypatch.setitem(
        source_registry.REGISTRY,
        "france_travail",
        factory(FakeSource("ft", [], error=RuntimeError("x"))),
    )
    result = env.run()
    assert result.exit_code == 3
    assert env.s3.data is not None  # l'état est renvoyé : sinon doublons au prochain run


# --- check ------------------------------------------------------------------------------------


def check(env: Env, *extra: str) -> Any:
    return runner.invoke(cli.root, ["radar", "check", "--config", str(env.config), *extra])


def test_check_reports_delivery_and_state_status(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = check(env)
    assert "envoi du digest : configuré" in ok.output
    assert "état : fichier local seulement" in ok.output
    with_s3(env, monkeypatch)
    assert "état : S3" in check(env).output


def test_check_ci_requires_smtp_and_bucket(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_settings", make_settings)
    missing = check(env, "--ci")
    assert missing.exit_code == 1
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "DIGEST_TO"):
        assert name in missing.output
    monkeypatch.setattr(cli, "_settings", lambda: make_settings(**SMTP))
    no_bucket = check(env, "--ci")
    assert no_bucket.exit_code == 1
    assert "VIGIE_STATE_BUCKET" in no_bucket.output
    with_s3(env, monkeypatch)
    ok = check(env, "--ci")
    assert ok.exit_code == 0, ok.output
    assert SMTP_PASSWORD not in ok.output
    assert BUCKET not in ok.output


def test_check_without_ci_tolerates_a_missing_delivery_setup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_settings", make_settings)
    result = check(env)
    assert result.exit_code == 0
    assert "envoi du digest : non configuré" in result.output


def test_check_rejects_a_partially_configured_smtp(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_settings", lambda: make_settings(smtp_host="smtp.example.org"))
    result = check(env)
    assert result.exit_code == 1
    assert "SMTP_PASSWORD" in result.output
    assert "smtp.example.org" not in result.output


# --- raisons d'échec lisibles, sans donnée sensible ------------------------------------------------


def test_failed_source_shows_the_reason_of_safe_exceptions(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vigie.radar.sources.email_alerts.mailbox import MailboxError

    env.use_source(
        monkeypatch,
        FakeSource("adzuna", [], error=MailboxError("authentification IMAP refusée")),
    )
    result = env.run()
    assert "ÉCHEC (MailboxError : authentification IMAP refusée)" in result.output


def test_failed_source_hides_the_message_of_other_exceptions(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_url = "adzuna : HTTP 500 sur https://api.example.org/?what=critere-confidentiel"
    env.use_source(monkeypatch, FakeSource("adzuna", [], error=SourceHttpError(secret_url, 500)))
    result = env.run()
    assert "ÉCHEC (SourceHttpError)" in result.output
    assert "critere-confidentiel" not in result.output
    assert "HTTP 500" not in result.output


def test_delivery_failure_shows_the_failing_stage(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.use_source(monkeypatch, FakeSource("adzuna", offers(1)))
    env.sender.fail = True
    result = env.run()
    assert "digest : échec de l'envoi (envoi du digest impossible)" in result.output


def test_stats_shows_the_reason_too(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from vigie.radar.sources.email_alerts.mailbox import MailboxError

    env.use_source(
        monkeypatch, FakeSource("adzuna", [], error=MailboxError("dossier IMAP introuvable"))
    )
    env.run()
    result = runner.invoke(cli.root, ["radar", "stats", "--db", str(env.db)])
    assert "ÉCHEC (MailboxError : dossier IMAP introuvable)" in result.output


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (None, ""),
        ("", ""),
        (
            "MailboxError: connexion IMAP impossible",
            " (MailboxError : connexion IMAP impossible)",
        ),
        ("NotifyError: x", " (NotifyError : x)"),
        ("StateSyncError: y", " (StateSyncError : y)"),
        ("RuntimeError: contient un secret", " (RuntimeError)"),
        ("SourceHttpError: https://x?app_key=***", " (SourceHttpError)"),
        ("SansDeuxPoints", " (SansDeuxPoints)"),
    ],
)
def test_failure_reason(message: str | None, expected: str) -> None:
    assert cli.failure_reason(message) == expected
