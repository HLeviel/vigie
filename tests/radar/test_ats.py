import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tests.radar.factories import NOW, make_config, make_settings
from vigie.radar.config import AtsCompany
from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.radar.models import ContractType, RemoteMode, SearchQuery
from vigie.radar.pipeline import plan_sources, run_pipeline
from vigie.radar.sources import available, build
from vigie.radar.sources.ats import ADAPTERS, SUPPORTED_PLATFORMS, AtsSource
from vigie.radar.sources.ats import smartrecruiters as sr
from vigie.radar.sources.ats.common import parse_epoch_ms, parse_utc_text, unescape_html_to_text
from vigie.radar.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
QUERY = SearchQuery(name="q", keywords=["a"], departments=["69"])
SINCE = NOW


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def make_source(*companies: tuple[str, str, str]) -> AtsSource:
    client = SourceHttpClient("ats", min_interval=0, sleep=lambda s: None)
    return AtsSource(
        [AtsCompany(name=n, platform=p, token=t) for n, p, t in companies],  # type: ignore[arg-type]
        client=client,
        now=lambda: NOW,
    )


def fetch(source: AtsSource) -> dict[str, Any]:
    return {o.source_id: o for o in source.fetch(QUERY, SINCE)}


GH = "https://boards-api.greenhouse.io/v1/boards/exempleia/jobs"
LV = "https://api.lever.co/v0/postings/startupdata"
AB = "https://api.ashbyhq.com/posting-api/job-board/exemple-ashby"
SRL = "https://api.smartrecruiters.com/v1/companies/exemplesr/postings"
RC = "https://exemplerec.recruitee.com/api/offers/"


def test_registry_and_platforms() -> None:
    assert "ats" in available()
    source = build("ats", make_settings(), make_config())
    assert source.name == "ats"
    assert SUPPORTED_PLATFORMS == {"greenhouse", "lever", "ashby", "smartrecruiters", "recruitee"}
    assert set(ADAPTERS) == SUPPORTED_PLATFORMS  # workable : pas encore vérifié


# --- Greenhouse -------------------------------------------------------------------------------


@respx.mock
def test_greenhouse() -> None:
    route = respx.get(GH).mock(return_value=httpx.Response(200, json=load("ats_greenhouse.json")))
    offers = fetch(make_source(("Exemple IA", "greenhouse", "exempleia")))
    assert route.calls.last.request.url.params["content"] == "true"
    # Paris x2, Remote - France, Remote (sans restriction) ; San Francisco et « Remote, US » exclus
    assert sorted(offers) == ["9000001", "9000003", "9000004", "9000005"]
    first = offers["9000001"]
    assert first.uid == "ats:greenhouse:9000001"
    assert first.company == "Exemple IA"  # nom de la configuration
    assert first.location_label == "Paris"
    assert first.title == "Data Engineer (H/F)"
    # contenu échappé en entités -> texte brut, sans balise ni entité
    assert (
        first.description == "Le poste\nNous construisons des pipelines Python & SQL.\nPython\ndbt"
    )
    assert str(first.url) == "https://exemple.example/jobs?gh_jid=9000001"
    assert first.published_at == datetime(2026, 11, 10, 14, 0, tzinfo=UTC)  # -05:00 converti en UTC
    assert offers["9000004"].contract_type is ContractType.STAGE  # « Stage » dans le titre
    assert first.contract_type is ContractType.AUTRE


def test_unescape_html_to_text() -> None:
    assert unescape_html_to_text("&lt;p&gt;Bonjour &amp;amp; &lt;b&gt;bye&lt;/b&gt;&lt;/p&gt;") == (
        "Bonjour & bye"
    )


# --- Lever ------------------------------------------------------------------------------------


@respx.mock
def test_lever() -> None:
    route = respx.get(LV).mock(return_value=httpx.Response(200, json=load("ats_lever.json")))
    offers = fetch(make_source(("Startup Data", "lever", "startupdata")))
    assert route.calls.last.request.url.params["mode"] == "json"
    ids = {k[-1] for k in offers}
    assert ids == {
        "1",
        "3",
        "4",
    }  # Lyon, Paris (stage), remote Europe ; New York et remote US exclus
    lyon = offers["a0000000-0000-0000-0000-000000000001"]
    assert lyon.remote is RemoteMode.PARTIAL  # hybrid
    assert lyon.location_label == "Lyon, France"
    assert lyon.contract_type is ContractType.AUTRE  # « Full-time » n'est pas un CDI
    assert "Nous construisons des produits d'IA." in lyon.description
    assert "Vos missions" in lyon.description
    assert "Concevoir des modèles" in lyon.description
    assert "Merci de postuler." in lyon.description
    assert str(lyon.url) == "https://jobs.lever.co/startupdata/a0000000-0000-0000-0000-000000000001"
    assert lyon.published_at == parse_epoch_ms(1794477600000)
    assert lyon.published_at.tzinfo is UTC
    assert offers["a0000000-0000-0000-0000-000000000003"].contract_type is ContractType.STAGE
    remote = offers["a0000000-0000-0000-0000-000000000004"]
    assert remote.contract_type is ContractType.CDD  # Fixed-Term
    assert remote.remote is RemoteMode.FULL


# --- Ashby ------------------------------------------------------------------------------------


@respx.mock
def test_ashby() -> None:
    respx.get(AB).mock(return_value=httpx.Response(200, json=load("ats_ashby.json")))
    offers = fetch(make_source(("Exemple Ashby", "ashby", "exemple-ashby")))
    ids = {k[-1] for k in offers}
    # Paris, Remote UE, Lyon (stage), Berlin avec lieu secondaire à Marseille ;
    # « Remote - US » et l'offre non listée exclues
    assert ids == {"1", "2", "5", "6"}
    paris = offers["b0000000-0000-0000-0000-000000000001"]
    assert paris.remote is RemoteMode.PARTIAL
    assert paris.description == "Description de Data Engineer."
    assert str(paris.url).endswith("/b0000000-0000-0000-0000-000000000001")
    assert paris.published_at == datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    assert offers["b0000000-0000-0000-0000-000000000002"].remote is RemoteMode.FULL
    assert offers["b0000000-0000-0000-0000-000000000005"].contract_type is ContractType.STAGE


# --- SmartRecruiters --------------------------------------------------------------------------


def sr_detail_route() -> respx.Route:
    return respx.get(f"{SRL}/744000100000001").mock(
        return_value=httpx.Response(200, json=load("ats_smartrecruiters_detail.json"))
    )


@respx.mock
def test_smartrecruiters_list_detail_and_mapping() -> None:
    listing = respx.get(SRL).mock(
        return_value=httpx.Response(200, json=load("ats_smartrecruiters.json"))
    )
    detail = sr_detail_route()
    respx.get(url__regex=rf"{SRL}/74400010000000[23]").mock(return_value=httpx.Response(404))
    offers = fetch(make_source(("Exemple SR", "smartrecruiters", "exemplesr")))
    params = dict(listing.calls.last.request.url.params)
    assert (params["country"], params["limit"], params["offset"]) == ("fr", "100", "0")
    assert detail.call_count == 1
    assert sorted(offers) == ["744000100000001", "744000100000002", "744000100000003"]
    lyon = offers["744000100000001"]
    assert (lyon.postal_code, lyon.department) == ("69003", "69")  # code postal de la liste
    assert lyon.location_label == "Lyon, Auvergne-Rhône-Alpes"
    assert lyon.contract_type is ContractType.CDI  # permanent
    assert lyon.remote is RemoteMode.PARTIAL  # hybrid
    assert "Python" in lyon.description
    assert "Bac +5" in lyon.description
    assert "<" not in lyon.description
    assert str(lyon.url).endswith("744000100000001-ingenieur-data-h-f")  # postingUrl du détail
    # « Apprenti(e) » dans le titre l'emporte sur le code « contract »
    apprenti = offers["744000100000002"]
    assert apprenti.contract_type is ContractType.ALTERNANCE
    assert apprenti.department == "93"
    # détail indisponible : description vide, URL publique reconstruite
    assert apprenti.description == ""
    assert str(apprenti.url) == "https://jobs.smartrecruiters.com/exemplesr/744000100000002"
    assert offers["744000100000003"].remote is RemoteMode.FULL


@respx.mock
def test_smartrecruiters_pagination() -> None:
    page1 = {
        "offset": 0,
        "limit": 100,
        "totalFound": 150,
        "content": load("ats_smartrecruiters.json")["content"],
    }
    page2 = {"offset": 100, "limit": 100, "totalFound": 150, "content": []}
    route = respx.get(SRL).mock(
        side_effect=lambda r: httpx.Response(
            200, json=page1 if r.url.params["offset"] == "0" else page2
        )
    )
    respx.get(url__regex=rf"{SRL}/\d+").mock(return_value=httpx.Response(404))
    fetch(make_source(("Exemple SR", "smartrecruiters", "exemplesr")))
    assert [c.request.url.params["offset"] for c in route.calls] == ["0", "100"]


@respx.mock
def test_smartrecruiters_detail_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "MAX_DETAILS", 1)
    respx.get(SRL).mock(return_value=httpx.Response(200, json=load("ats_smartrecruiters.json")))
    detail = respx.get(url__regex=rf"{SRL}/\d+").mock(
        return_value=httpx.Response(200, json=load("ats_smartrecruiters_detail.json"))
    )
    offers = fetch(make_source(("Exemple SR", "smartrecruiters", "exemplesr")))
    assert detail.call_count == 1
    assert len(offers) == 3  # les offres au-delà de la limite restent, sans description


@respx.mock
def test_smartrecruiters_unknown_company_is_an_empty_list() -> None:
    respx.get(SRL).mock(
        return_value=httpx.Response(
            200, json={"offset": 0, "limit": 100, "totalFound": 0, "content": []}
        )
    )
    assert fetch(make_source(("Exemple SR", "smartrecruiters", "exemplesr"))) == {}


# --- Recruitee --------------------------------------------------------------------------------


@respx.mock
def test_recruitee() -> None:
    respx.get(RC).mock(return_value=httpx.Response(200, json=load("ats_recruitee.json")))
    offers = fetch(make_source(("Exemple Rec", "recruitee", "exemplerec")))
    # Paris ; Amsterdam + Lyon (lieux multiples) ; NL seul, remote US et brouillon exclus
    assert sorted(offers) == ["9100001", "9100003"]
    paris = offers["9100001"]
    assert paris.contract_type is ContractType.CDI
    assert paris.remote is RemoteMode.PARTIAL
    assert (paris.salary_min, paris.salary_max) == (50000.0, 60000.0)
    assert (paris.postal_code, paris.department) == ("75001", "75")
    assert paris.description.startswith("Description de Data Engineer.")
    assert "Python" in paris.description
    assert paris.published_at == datetime(2026, 11, 18, 16, 23, 32, tzinfo=UTC)
    assert str(paris.url) == "https://exemplerec.example/o/9100001"
    multi = offers["9100003"]
    assert multi.contract_type is ContractType.CDD
    assert multi.remote is RemoteMode.NONE  # on_site
    assert multi.salary_min is None


def test_recruitee_date_and_salary_periods() -> None:
    from vigie.radar.sources.ats.recruitee import _contract, _salary

    assert parse_utc_text("2026-11-18 16:23:32 UTC") == datetime(
        2026, 11, 18, 16, 23, 32, tzinfo=UTC
    )
    assert _salary(
        {"salary": {"min": 3000, "max": 4000, "period": "month", "currency": "EUR"}}
    ) == (36000.0, 48000.0)
    assert _salary(
        {"salary": {"min": 3000, "max": None, "period": "month", "currency": "EUR"}}
    ) == (36000.0, None)
    assert _salary({"salary": {"min": 50, "max": 60, "period": "hour", "currency": "EUR"}}) == (
        None,
        None,
    )
    assert _salary({"salary": {"min": 50, "max": 60, "period": "year", "currency": "USD"}}) == (
        None,
        None,
    )
    assert _salary({}) == (None, None)
    assert _contract("fulltime_permanent") is ContractType.CDI
    assert _contract("parttime_fixed_term") is ContractType.CDD
    assert _contract("internship") is ContractType.STAGE
    assert _contract("freelance") is ContractType.FREELANCE
    assert _contract("autre") is None


# --- comportement commun ----------------------------------------------------------------------


@respx.mock
def test_unknown_token_404_is_counted_without_failing_the_source() -> None:
    respx.get(GH).mock(
        return_value=httpx.Response(404, json={"status": 404, "error": "Job not found"})
    )
    respx.get(LV).mock(return_value=httpx.Response(200, json=load("ats_lever.json")))
    source = make_source(("A", "greenhouse", "exempleia"), ("B", "lever", "startupdata"))
    offers = fetch(source)
    assert len(offers) == 3  # Lever a produit ses offres malgré le 404 de Greenhouse
    assert source.skipped == 1  # l'entreprise introuvable est comptée en erreur


@respx.mock
def test_all_companies_failing_fails_the_source() -> None:
    respx.get(GH).mock(return_value=httpx.Response(404))
    respx.get(LV).mock(return_value=httpx.Response(404))
    source = make_source(("A", "greenhouse", "exempleia"), ("B", "lever", "startupdata"))
    with pytest.raises(SourceHttpError):
        fetch(source)


@respx.mock
def test_server_error_on_one_company_is_not_fatal() -> None:
    respx.get(GH).mock(return_value=httpx.Response(503))
    respx.get(LV).mock(return_value=httpx.Response(200, json=load("ats_lever.json")))
    source = make_source(("A", "greenhouse", "exempleia"), ("B", "lever", "startupdata"))
    assert len(fetch(source)) == 3
    assert source.skipped == 1


def test_unsupported_platform_is_counted_not_fatal() -> None:
    source = make_source(("W", "workable", "exemplewk"))
    with pytest.raises(SourceHttpError):  # seule entreprise : toutes ont échoué
        fetch(source)
    assert source.skipped == 1


@respx.mock
def test_each_company_is_fetched_once_per_run() -> None:
    route = respx.get(LV).mock(return_value=httpx.Response(200, json=load("ats_lever.json")))
    source = make_source(("B", "lever", "startupdata"))
    first = list(source.fetch(QUERY, SINCE))
    second = list(source.fetch(SearchQuery(name="autre", keywords=["b"]), SINCE))
    assert route.call_count == 1
    assert [o.uid for o in first] == [o.uid for o in second]


@respx.mock
def test_invalid_items_are_skipped() -> None:
    data = load("ats_lever.json")
    broken = {**data[0], "id": "x", "createdAt": "pas-un-nombre"}
    respx.get(LV).mock(return_value=httpx.Response(200, json=[data[0], broken]))
    source = make_source(("B", "lever", "startupdata"))
    assert len(fetch(source)) == 1
    assert source.skipped == 1


def test_no_companies_yields_nothing() -> None:
    assert fetch(make_source()) == {}


@respx.mock
def test_logs_never_contain_company_names(caplog: pytest.LogCaptureFixture) -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/token-confidentiel/jobs").mock(
        return_value=httpx.Response(404)
    )
    respx.get(LV).mock(return_value=httpx.Response(200, json=load("ats_lever.json")))
    source = make_source(
        ("Entreprise-Confidentielle", "greenhouse", "token-confidentiel"),
        ("B", "lever", "startupdata"),
    )
    with caplog.at_level(logging.DEBUG):
        fetch(source)
    assert "Confidentiel" not in caplog.text
    assert "confidentiel" not in caplog.text
    assert "1 entreprise(s) ATS en erreur dont 1 introuvable(s)" in caplog.text


# --- pipeline ---------------------------------------------------------------------------------

ATS_YAML = """
sources:
  ats: { enabled: true }
ats_companies:
  - { name: Exemple IA, platform: greenhouse, token: exempleia }
  - { name: Startup Data, platform: lever, token: startupdata }
  - { name: Introuvable, platform: ashby, token: exemple-ashby }
searches:
  - { name: demo, keywords: [data], departments: ['69'], max_age_days: 365 }
filters:
  exclude_title: [stage]
  require_any: [python, data, ia]
"""


@respx.mock
def test_pipeline_runs_twice_with_ats() -> None:
    respx.get(GH).mock(return_value=httpx.Response(200, json=load("ats_greenhouse.json")))
    respx.get(LV).mock(return_value=httpx.Response(200, json=load("ats_lever.json")))
    respx.get(AB).mock(return_value=httpx.Response(404, text="Not Found"))
    config = make_config(ATS_YAML)
    plan = plan_sources(config, make_settings())
    assert plan.active == ["ats"]  # aucun secret requis

    store = Store.open(":memory:")
    first = run_pipeline(config, make_settings(), store, plan, now=lambda: NOW)
    second = run_pipeline(config, make_settings(), store, plan, now=lambda: NOW)
    r1, r2 = first.report.sources[0], second.report.sources[0]
    assert r1.errors == 1  # Ashby introuvable (404), comptée mais non fatale
    assert first.exit_code == 0
    assert r1.fetched == 7  # 4 Greenhouse + 3 Lever
    assert r1.new + r1.filtered == 7
    assert (r2.new, r2.duplicates) == (0, 7)
    assert second.exit_code == 0
    # un stage est écarté (contrat inconnu pour « Stage - Data Scientist », filtre sur le titre)
    assert store.filtered_reason("ats:greenhouse:9000004") == "title:stage"
