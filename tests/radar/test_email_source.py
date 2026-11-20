import logging
import socket
from datetime import UTC, datetime
from typing import Any

import pytest
import respx

from tests.radar.email_helpers import ALLOWED, FakeMailbox, config
from tests.radar.factories import NOW, make_config, make_settings
from vigie.radar.models import ContractType, SearchQuery
from vigie.radar.pipeline import plan_sources, run_pipeline
from vigie.radar.sources import available
from vigie.radar.sources.email_alerts import EmailAlertsSource, match_sender
from vigie.radar.store import Store
from vigie.settings import MissingSettingsError, Settings

QUERY = SearchQuery(name="q", keywords=["a"], departments=["69"])
SINCE = datetime(2026, 11, 1, tzinfo=UTC)
ALL = [
    "france_travail",
    "apec",
    "wttj",
    "linkedin",
    "linkedin_hostile",
    "apec_format_change",
    "unknown_sender",
]


def imap_settings() -> Settings:
    return make_settings(
        imap_host="imap.example.org",
        imap_user="moi",
        imap_password="pw",
        imap_folder="Vigie/Alertes",
    )


def make_source(
    names: list[str] = ALL, allowed: dict[str, list[str]] | None = None
) -> tuple[EmailAlertsSource, FakeMailbox]:
    mailbox = FakeMailbox(names)
    return EmailAlertsSource(
        imap_settings(), config(allowed), mailbox=mailbox, now=lambda: NOW
    ), mailbox


def run_all(source: EmailAlertsSource) -> dict[str, Any]:
    return {o.uid: o for o in source.fetch(QUERY, SINCE)}


def test_registered_and_requires_imap_variables() -> None:
    assert "email_alerts" in available()
    with pytest.raises(MissingSettingsError) as exc:
        EmailAlertsSource(Settings(_env_file=None), config())  # type: ignore[call-arg]
    assert "IMAP_HOST" in str(exc.value)
    assert "IMAP_PASSWORD" in str(exc.value)


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("alerte@francetravail.fr", "france_travail"),
        ("a@mail.welcometothejungle.com", "wttj"),  # sous-domaine
        ("A@LINKEDIN.COM", "linkedin"),
        ("x@evil-linkedin.com", None),  # pas un sous-domaine
        ("x@linkedin.com.evil.org", None),
        ("x@notapec.fr", None),
        ("", None),
    ],
)
def test_match_sender(address: str, expected: str | None) -> None:
    assert match_sender(address, ALLOWED) == expected


def test_match_sender_exact_address() -> None:
    allowed = {"apec": ["alertes@apec.fr"]}
    assert match_sender("alertes@apec.fr", allowed) == "apec"
    assert match_sender("autre@apec.fr", allowed) is None


def test_offers_are_extracted_per_sender() -> None:
    source, mailbox = make_source()
    offers = run_all(source)
    by_source: dict[str, int] = {}
    for offer in offers.values():
        by_source[offer.source] = by_source.get(offer.source, 0) + 1
    assert by_source == {
        "email:france_travail": 2,
        "email:apec": 1,
        "email:wttj": 1,
        "email:linkedin": 3,  # dont l'email piégé
    }
    assert "unknown_sender" not in mailbox.bodies_read  # expéditeur hors liste blanche
    assert "unknown_sender" in mailbox.skipped


def test_offer_fields() -> None:
    source, _ = make_source(["france_travail"])
    offer = run_all(source)["email:france_travail:190AAAA"]
    assert offer.title == "Data Engineer (H/F)"
    assert offer.company == "Exemple SA"
    assert offer.location_label == "69 - LYON 03"
    assert offer.department == "69"
    assert offer.description == "Pipelines Python et SQL."
    assert str(offer.url) == "https://candidat.francetravail.fr/offres/recherche/detail/190AAAA"
    assert offer.published_at == datetime(2026, 11, 19, 7, 30, tzinfo=UTC)  # date de l'email
    assert offer.contract_type is ContractType.AUTRE
    # raw : de quoi déboguer, jamais le corps ni l'adresse de l'utilisateur
    assert offer.raw == {
        "sender_key": "france_travail",
        "message_id": "<ft-1@example.org>",
        "link": str(offer.url),
    }
    assert "candidat@example.org" not in str(offer.model_dump())


def test_contract_type_from_title() -> None:
    source, _ = make_source(["linkedin"])
    # aucun titre de la fixture n'est un stage/alternance : AUTRE
    assert {o.contract_type for o in run_all(source).values()} == {ContractType.AUTRE}


def test_hostile_email_is_flagged_and_hidden_text_dropped() -> None:
    source, _ = make_source(["linkedin_hostile"])
    [offer] = run_all(source).values()
    assert offer.suspicious  # injection visible + caractère invisible dans le titre
    assert "system prompt" not in offer.description
    assert "Ignore all previous" not in offer.description


def test_no_offer_extracted_warns_with_sender_and_date_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, _ = make_source(["apec_format_change"])
    with caplog.at_level(logging.DEBUG):
        assert run_all(source) == {}
    assert source.skipped == 1  # compte une erreur pour la source
    assert "expéditeur=apec date=2026-11-19" in caplog.text
    for forbidden in ("Nouveau design", "click.apec", "noreply@apec.fr", "Voir mes offres"):
        assert forbidden not in caplog.text


def test_processed_emails_are_remembered_and_not_reread() -> None:
    store = Store.open(":memory:")
    first, mailbox1 = make_source()
    first.attach_store(store)
    run_all(first)
    # tout est traité, sauf l'email au format inconnu et l'expéditeur hors liste blanche
    assert store.count_processed_emails() == 5
    assert store.email_processed("<ft-1@example.org>")
    assert not store.email_processed("<apec-2@example.org>")
    assert not store.email_processed("<promo-1@example.org>")

    second, mailbox2 = make_source()
    second.attach_store(store)
    assert len(run_all(second)) == 0
    # seul l'email « format inconnu » est relu (pour qu'un parseur corrigé puisse l'exploiter)
    assert mailbox2.bodies_read == ["apec_format_change"]
    assert second.skipped == 1


def test_email_is_marked_only_after_its_offers_were_consumed() -> None:
    store = Store.open(":memory:")
    source, _ = make_source(["france_travail"])
    source.attach_store(store)
    generator = source.fetch(QUERY, SINCE)
    next(generator)  # une seule offre consommée sur deux
    generator.close()
    assert store.count_processed_emails() == 0  # rien n'est perdu si le pipeline s'interrompt
    full = list(make_source(["france_travail"])[0].fetch(QUERY, SINCE))
    assert len(full) == 2


def test_mailbox_is_read_once_per_run() -> None:
    source, mailbox = make_source(["france_travail", "linkedin"])
    first = list(source.fetch(QUERY, SINCE))
    second = list(source.fetch(SearchQuery(name="autre", keywords=["b"]), SINCE))
    assert [o.uid for o in first] == [o.uid for o in second]
    assert len(mailbox.since) == 1


def test_since_is_passed_to_the_mailbox() -> None:
    source, mailbox = make_source(["france_travail"])
    run_all(source)
    assert mailbox.since == [SINCE]


def test_no_network_access_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("accès réseau interdit : les liens ne sont jamais ouverts")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with respx.mock(assert_all_called=False) as router:
        source, _ = make_source()
        assert run_all(source)
        assert router.calls.call_count == 0  # aucune requête HTTP


def test_without_store_nothing_is_marked() -> None:
    source, _ = make_source(["france_travail"])
    assert len(run_all(source)) == 2


def test_empty_whitelist_reads_nothing() -> None:
    source, mailbox = make_source(allowed={})
    assert run_all(source) == {}
    assert mailbox.bodies_read == []


def test_duplicate_links_in_one_email_give_one_offer() -> None:
    source, _ = make_source(["france_travail"])
    uids = [o.uid for o in source.fetch(QUERY, SINCE)]
    assert len(uids) == len(set(uids)) == 2  # chaque offre est liée deux fois dans l'email


# --- pipeline ---------------------------------------------------------------------------------

EMAIL_YAML = """
sources:
  email_alerts: { enabled: true }
email_alerts:
  allowed_senders:
    france_travail: ["@francetravail.fr"]
    apec: ["@apec.fr"]
    wttj: ["@welcometothejungle.com"]
    linkedin: ["@linkedin.com"]
searches:
  - { name: demo, keywords: [data], departments: ['69'], max_age_days: 30 }
filters:
  require_any: [data, ia, machine learning]
"""


def test_plan_requires_imap_variables() -> None:
    cfg = make_config(EMAIL_YAML)
    plan = plan_sources(cfg, make_settings())
    assert plan.active == []
    assert plan.missing_variables == ["IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "IMAP_FOLDER"]
    assert plan_sources(cfg, imap_settings()).active == ["email_alerts"]


def test_pipeline_two_runs_do_not_reread_or_duplicate() -> None:
    cfg = make_config(EMAIL_YAML)
    settings = imap_settings()
    mailboxes: list[FakeMailbox] = []

    def build(_: Settings, __: Any) -> EmailAlertsSource:
        mailbox = FakeMailbox(ALL)
        mailboxes.append(mailbox)
        return EmailAlertsSource(settings, cfg.email_alerts, mailbox=mailbox, now=lambda: NOW)

    registry: dict[str, Any] = {"email_alerts": build}
    plan = plan_sources(cfg, settings, registry)
    store = Store.open(":memory:")
    first = run_pipeline(cfg, settings, store, plan, registry=registry, now=lambda: NOW)
    second = run_pipeline(cfg, settings, store, plan, registry=registry, now=lambda: NOW)
    r1, r2 = first.report.sources[0], second.report.sources[0]
    assert r1.fetched == 7
    assert r1.new + r1.filtered == 7
    assert r1.errors == 1  # l'email au format inconnu
    assert first.exit_code == 0  # une erreur d'extraction n'est pas une panne de la source
    assert (r2.fetched, r2.new) == (0, 0)  # rien n'est relu ni réinséré
    assert mailboxes[1].bodies_read == ["apec_format_change"]
    assert store.count_pending() == r1.new


def test_pipeline_dry_run_marks_nothing_on_disk(tmp_path: Any) -> None:
    cfg = make_config(EMAIL_YAML)
    settings = imap_settings()
    registry: dict[str, Any] = {
        "email_alerts": lambda s, c: EmailAlertsSource(
            settings, cfg.email_alerts, mailbox=FakeMailbox(ALL), now=lambda: NOW
        )
    }
    plan = plan_sources(cfg, settings, registry)
    path = tmp_path / "radar.db"
    with Store.open(path):
        pass
    with Store.open_copy(path) as copy:
        run_pipeline(cfg, settings, copy, plan, registry=registry, now=lambda: NOW)
        assert copy.count_processed_emails() == 5
    with Store.open(path) as real:
        assert real.count_processed_emails() == 0
