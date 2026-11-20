import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.radar.factories import (
    NOW,
    FakeSource,
    factory,
    make_config,
    make_offer,
    make_settings,
)
from vigie.radar.config import load_config_text
from vigie.radar.http import SourceHttpError
from vigie.radar.pipeline import (
    UnknownSourceSelectionError,
    default_since,
    plan_sources,
    run_pipeline,
)
from vigie.radar.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "adzuna_search.json"


def run(store: Store, sources: dict[str, FakeSource], config_text: str | None = None) -> Any:
    config = make_config(config_text) if config_text else make_config()
    registry = {name: factory(src) for name, src in sources.items()}
    plan = plan_sources(config, make_settings(), registry)
    plan.active = list(sources)
    return run_pipeline(config, make_settings(), store, plan, registry=registry, now=lambda: NOW)


def offers(n: int, source: str = "adzuna", **kw: Any) -> list[Any]:
    return [
        make_offer(source=source, source_id=str(i), title=f"Data Engineer {i}", **kw)
        for i in range(n)
    ]


def test_new_offers_are_stored_and_pending() -> None:
    store = Store.open(":memory:")
    result = run(store, {"adzuna": FakeSource("adzuna", offers(3))})
    [report] = result.report.sources
    assert (report.fetched, report.new, report.duplicates, report.filtered) == (3, 3, 0, 0)
    assert result.pending == 3
    assert result.exit_code == 0
    assert result.status == "success"


def test_second_run_adds_nothing() -> None:
    """Critère d'acceptation : la seconde exécution n'ajoute aucun doublon."""
    store = Store.open(":memory:")
    source = FakeSource("adzuna", offers(3))
    run(store, {"adzuna": source})
    second = run(store, {"adzuna": source})
    [report] = second.report.sources
    assert (report.fetched, report.new, report.duplicates) == (3, 0, 3)
    assert second.pending == 3  # toujours à notifier (rien n'a été notifié)
    assert store.count_pending() == 3


def test_notified_offers_are_not_returned_again() -> None:
    store = Store.open(":memory:")
    source = FakeSource("adzuna", offers(2))
    run(store, {"adzuna": source})
    store.mark_notified([o.uid for o in store.pending_offers()], NOW)
    second = run(store, {"adzuna": source})
    assert second.pending == 0
    assert store.pending_offers() == []


def test_filtered_offers_are_kept_with_reason_and_not_pending() -> None:
    store = Store.open(":memory:")
    mixed = [
        make_offer(source_id="ok"),
        make_offer(source_id="stage", title="Stage Data Engineer"),
        make_offer(source_id="hors-sujet", title="Comptable", description="Compta"),
    ]
    result = run(store, {"adzuna": FakeSource("adzuna", mixed)})
    [report] = result.report.sources
    assert (report.new, report.filtered) == (1, 2)
    assert store.filtered_reason("adzuna:stage") == "title:stage"
    assert store.filtered_reason("adzuna:hors-sujet") == "require_any"
    assert [o.source_id for o in store.pending_offers()] == ["ok"]


def test_cross_source_duplicate_becomes_alias() -> None:
    store = Store.open(":memory:")
    a = FakeSource("adzuna", [make_offer(source="adzuna", source_id="1")])
    b = FakeSource("france_travail", [make_offer(source="france_travail", source_id="77")])
    result = run(store, {"adzuna": a, "france_travail": b})
    adzuna, ft = result.report.sources
    assert (adzuna.new, ft.new, ft.duplicates) == (1, 0, 1)
    assert store.aliases_of("adzuna:1") == ["france_travail:77"]
    assert store.count_pending() == 1
    # exécution suivante : l'alias est reconnu, pas réinséré
    again = run(store, {"adzuna": a, "france_travail": b})
    assert again.report.sources[1].duplicates == 1
    assert store.count_pending() == 1


def test_source_failure_does_not_stop_others_and_exit_code_is_3() -> None:
    store = Store.open(":memory:")
    broken = FakeSource("adzuna", [], error=SourceHttpError("adzuna : HTTP 500", 500))
    healthy = FakeSource("france_travail", offers(2, source="france_travail"))
    result = run(store, {"adzuna": broken, "france_travail": healthy})
    bad, good = result.report.sources
    assert bad.errors == 1
    assert bad.error_message is not None
    assert bad.error_message.startswith("SourceHttpError")
    assert good.new == 2
    assert result.failed == ["adzuna"]
    assert result.status == "partial"
    assert result.exit_code == 3
    assert store.count_pending() == 2


def test_all_sources_failing_gives_exit_code_2() -> None:
    store = Store.open(":memory:")
    boom = RuntimeError("boom")
    result = run(
        store,
        {
            "adzuna": FakeSource("adzuna", [], error=boom),
            "france_travail": FakeSource("x", [], error=boom),
        },
    )
    assert result.status == "failed"
    assert result.exit_code == 2


def test_offers_before_a_mid_stream_failure_are_kept() -> None:
    store = Store.open(":memory:")
    source = FakeSource("adzuna", offers(2), error=RuntimeError("coupure"))
    result = run(store, {"adzuna": source})
    assert result.report.sources[0].new == 2
    assert result.exit_code == 2
    assert store.count_pending() == 2


def test_skipped_offers_count_as_errors_but_not_as_failure() -> None:
    store = Store.open(":memory:")
    result = run(store, {"adzuna": FakeSource("adzuna", offers(1), skipped=2)})
    assert result.report.sources[0].errors == 2
    assert result.exit_code == 0


def test_run_is_recorded_and_failed_run_is_not_a_success() -> None:
    store = Store.open(":memory:")
    run(store, {"adzuna": FakeSource("adzuna", [], error=RuntimeError("x"))})
    assert store.last_success_started_at() is None
    run(store, {"adzuna": FakeSource("adzuna", offers(1))})
    assert store.last_success_started_at() == NOW
    runs = store.recent_runs(5)
    assert [r["status"] for r in runs] == ["success", "failed"] or {r["status"] for r in runs} == {
        "success",
        "failed",
    }


def test_default_since() -> None:
    store = Store.open(":memory:")
    config = make_config()
    assert default_since(config, store, NOW) == NOW - timedelta(days=7)
    run(store, {"adzuna": FakeSource("adzuna", [])})
    assert default_since(config, store, NOW + timedelta(days=5)) == NOW - timedelta(days=1)


def test_explicit_since_is_passed_to_sources() -> None:
    seen: list[Any] = []

    class Spy(FakeSource):
        def fetch(self, query: Any, since: Any) -> Any:
            seen.append(since)
            return iter(())

    config = make_config()
    registry = {"adzuna": factory(Spy("adzuna", []))}
    plan = plan_sources(config, make_settings(), registry)
    run_pipeline(
        config,
        make_settings(),
        Store.open(":memory:"),
        plan,
        since=NOW - timedelta(days=2),
        registry=registry,
        now=lambda: NOW,
    )
    assert seen == [NOW - timedelta(days=2)]


def test_logs_contain_only_counters(caplog: pytest.LogCaptureFixture) -> None:
    store = Store.open(":memory:")
    secret_offers = [make_offer(title="Titre-Confidentiel", company="Entreprise-Confidentielle")]
    with caplog.at_level(logging.DEBUG):
        run(store, {"adzuna": FakeSource("adzuna", secret_offers)})
    assert "fetched=1" in caplog.text
    assert "Confidentiel" not in caplog.text
    assert "python" not in caplog.text  # mot-clé de recherche


# --- plan_sources ------------------------------------------------------------------------------

PLAN_YAML = """
sources:
  adzuna: { enabled: true }
  ats: { enabled: true }
  email_alerts: { enabled: false }
  france_travail: { enabled: true }
searches:
  - { name: x, keywords: [a], departments: ['69'] }
"""


def test_plan_sources() -> None:
    config = load_config_text(PLAN_YAML)
    registry = {
        "adzuna": factory(FakeSource("adzuna", [])),
        "france_travail": factory(FakeSource("f", [])),
    }
    plan = plan_sources(config, make_settings(), registry)
    assert plan.active == ["adzuna"]
    assert plan.not_implemented == ["ats"]
    assert plan.pending_access == ["france_travail"]  # identifiants absents : en attente d'accès
    assert plan.missing_variables == []

    with_ft = make_settings(ft_client_id="i", ft_client_secret="s")
    assert plan_sources(config, with_ft, registry).active == ["adzuna", "france_travail"]


def test_plan_reports_missing_variables_by_name_only() -> None:
    config = load_config_text(PLAN_YAML)
    registry = {"adzuna": factory(FakeSource("adzuna", []))}
    plan = plan_sources(config, make_settings(adzuna_app_key=None), registry)
    assert plan.missing_variables == ["ADZUNA_APP_KEY"]
    assert plan.active == []


def test_plan_only() -> None:
    config = load_config_text(PLAN_YAML)
    registry = {"adzuna": factory(FakeSource("adzuna", []))}
    assert plan_sources(config, make_settings(), registry, only="adzuna").active == ["adzuna"]
    with pytest.raises(UnknownSourceSelectionError):
        plan_sources(config, make_settings(), registry, only="email_alerts")  # désactivée
    with pytest.raises(UnknownSourceSelectionError):
        plan_sources(config, make_settings(), registry, only="inconnue")


# --- intégration avec la vraie source Adzuna (réponse réelle anonymisée) ---

ADZUNA_YAML = """
sources:
  adzuna: { enabled: true }
searches:
  - name: demo
    keywords: ["ingenieur ia"]
    departments: ["69"]
    contract_types: [CDI]
    max_age_days: 90
filters:
  exclude_title: ["stage"]
  require_any: ["ia"]
"""


@respx.mock
def test_integration_adzuna_two_runs_on_a_temp_database(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    respx.get(url__regex=r"https://api\.adzuna\.com/v1/api/jobs/fr/search/\d+").mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text()))
    )
    config = make_config(ADZUNA_YAML)
    settings = make_settings()
    plan = plan_sources(config, settings)
    assert plan.active == ["adzuna"]

    path = tmp_path / "radar.db"
    with caplog.at_level(logging.DEBUG), Store.open(path) as store:
        first = run_pipeline(config, settings, store, plan, now=lambda: NOW)
        second = run_pipeline(config, settings, store, plan, now=lambda: NOW)
        r1, r2 = first.report.sources[0], second.report.sources[0]
        assert (r1.fetched, r1.new, r1.filtered, r1.duplicates) == (10, 9, 1, 0)
        # « Stage - … » est détecté comme STAGE : le filtre de contrat passe avant celui du titre
        assert store.filtered_reason("adzuna:7100000008") == "contract"
        assert (r2.fetched, r2.new, r2.filtered, r2.duplicates) == (10, 0, 0, 10)
        assert first.exit_code == second.exit_code == 0
        assert store.count_pending() == 9
    # aucun intitulé, entreprise ni secret dans les logs
    for forbidden in ("Société", "Ingénieur", "s3cr3t-valeur-de-test", "id-valeur-de-test"):
        assert forbidden not in caplog.text
