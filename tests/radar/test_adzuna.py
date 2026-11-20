import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from vigie.radar.geo import department_code_from_name, department_name
from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.radar.models import ContractType, RemoteMode, SearchQuery
from vigie.radar.sources import available, build
from vigie.radar.sources.adzuna import (
    MAX_PAGES,
    PER_PAGE,
    AdzunaSource,
    contract_from_raw,
    offer_from_raw,
)
from vigie.settings import MissingSettingsError, Settings

FIXTURE = Path(__file__).parent / "fixtures" / "adzuna_search.json"
URL = "https://api.adzuna.com/v1/api/jobs/fr/search/1"
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
SINCE = NOW - timedelta(days=1)
APP_ID = "id-valeur-de-test"
APP_KEY = "s3cr3t-valeur-de-test"


def fixture_body() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def make_settings() -> Settings:
    return Settings(_env_file=None, adzuna_app_id=APP_ID, adzuna_app_key=APP_KEY)  # type: ignore[call-arg]


def make_source() -> AdzunaSource:
    client = SourceHttpClient("adzuna", min_interval=0, sleep=lambda s: None)
    return AdzunaSource(make_settings(), client=client, now=lambda: NOW)


def query(**kw: Any) -> SearchQuery:
    data: dict[str, Any] = {"name": "q", "keywords": ["python"], "departments": ["69"]}
    data.update(kw)
    return SearchQuery(**data)


def page_url(page: int) -> str:
    return f"https://api.adzuna.com/v1/api/jobs/fr/search/{page}"


def test_missing_secrets_are_named() -> None:
    with pytest.raises(MissingSettingsError) as exc:
        AdzunaSource(Settings(_env_file=None))  # type: ignore[call-arg]
    assert "ADZUNA_APP_ID" in str(exc.value)
    assert "ADZUNA_APP_KEY" in str(exc.value)


def test_registered() -> None:
    assert "adzuna" in available()
    assert build("adzuna", make_settings()).name == "adzuna"


# --- mapping ---------------------------------------------------------------------------------


def test_mapping_full_offer() -> None:
    offer = offer_from_raw(fixture_body()["results"][0], NOW)
    assert offer.uid == "adzuna:4400001"
    assert offer.title == "Data Engineer Python H/F"
    assert offer.company == "Exemple SA"
    assert offer.location_label == "Lyon, Rhône"
    assert offer.department == "69"
    assert offer.contract_type is ContractType.CDI
    assert (offer.salary_min, offer.salary_max) == (45000, 55000)
    assert offer.remote is RemoteMode.PARTIAL
    assert offer.published_at == datetime(2026, 11, 18, 8, 15, tzinfo=UTC)
    assert "utm_medium=api" in str(offer.url)
    assert offer.raw["id"] == "4400001"


def test_predicted_salary_is_ignored_and_html_stripped() -> None:
    offer = offer_from_raw(fixture_body()["results"][1], NOW)
    assert offer.salary_min is None
    assert offer.salary_max is None
    assert offer.description == "Mission de 6 mois.\nFull remote possible."
    assert offer.remote is RemoteMode.FULL
    assert offer.contract_type is ContractType.CDD
    assert offer.department == "75"


def test_mapping_minimal_offers() -> None:
    alt = offer_from_raw(fixture_body()["results"][2], NOW)
    assert alt.contract_type is ContractType.ALTERNANCE
    assert alt.description == ""
    assert alt.department is None
    bare = offer_from_raw(fixture_body()["results"][3], NOW)
    assert bare.company is None
    assert bare.location_label is None
    assert bare.contract_type is ContractType.AUTRE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"contract_type": "permanent"}, ContractType.CDI),
        ({"contract_type": "contract"}, ContractType.CDD),
        ({"contract_type": "autre"}, ContractType.AUTRE),
        ({"title": "Stage data", "contract_type": "permanent"}, ContractType.STAGE),
        ({"title": "Consultant freelance", "contract_type": "contract"}, ContractType.FREELANCE),
        ({"title": "Mission d'intérim"}, ContractType.INTERIM),
        ({}, ContractType.AUTRE),
    ],
)
def test_contract_from_raw(raw: dict[str, Any], expected: ContractType) -> None:
    assert contract_from_raw(raw) is expected


def test_geo_helpers() -> None:
    assert department_name("69") == "Rhône"
    assert department_name("2a") == "Corse-du-Sud"
    assert department_name("00") is None
    assert department_code_from_name("RHONE") == "69"
    assert department_code_from_name("Côte-d'Or") == "21"
    assert department_code_from_name("Atlantide") is None


# --- requêtes --------------------------------------------------------------------------------


@respx.mock
def test_search_params_and_headers() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=fixture_body()))
    offers = list(make_source().fetch(query(), SINCE))
    assert [o.source_id for o in offers] == ["4400001", "4400002", "4400003", "4400004"]
    request = route.calls.last.request
    params = dict(request.url.params)
    assert params["what"] == "python"
    assert params["where"] == "Rhône"
    assert params["max_days_old"] == "1"
    assert params["results_per_page"] == "50"
    assert params["app_id"] == APP_ID
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"].startswith("vigie-radar/")


@respx.mock
def test_max_days_old_follows_since_and_max_age() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    list(make_source().fetch(query(max_age_days=7), NOW - timedelta(days=30)))
    assert route.calls.last.request.url.params["max_days_old"] == "7"
    list(make_source().fetch(query(max_age_days=7), NOW - timedelta(hours=30)))
    assert route.calls.last.request.url.params["max_days_old"] == "2"


@respx.mock
def test_one_request_per_keyword_and_department() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    list(make_source().fetch(query(keywords=["a", "b"], departments=["69", "75"]), SINCE))
    pairs = {(c.request.url.params["what"], c.request.url.params["where"]) for c in route.calls}
    assert pairs == {("a", "Rhône"), ("a", "Paris"), ("b", "Rhône"), ("b", "Paris")}


@respx.mock
def test_commune_zone_and_no_zone() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    list(
        make_source().fetch(
            SearchQuery(name="q", keywords=["a"], commune="Lyon", distance_km=15), SINCE
        )
    )
    params = dict(route.calls.last.request.url.params)
    assert (params["where"], params["distance"]) == ("Lyon", "15")
    list(make_source().fetch(SearchQuery(name="q", keywords=["a"]), SINCE))
    params = dict(route.calls.last.request.url.params)
    assert "where" not in params
    assert "distance" not in params


def test_unknown_department_error_hides_code() -> None:
    with pytest.raises(ValueError) as exc:
        list(make_source().fetch(query(departments=["00"]), SINCE))
    assert "00" not in str(exc.value)


def make_page(prefix: str, count: int) -> list[dict[str, Any]]:
    template = fixture_body()["results"][0]
    return [{**template, "id": f"{prefix}{i}"} for i in range(count)]


@respx.mock
def test_pagination_until_short_page() -> None:
    respx.get(page_url(1)).mock(
        return_value=httpx.Response(200, json={"count": 120, "results": make_page("a", PER_PAGE)})
    )
    respx.get(page_url(2)).mock(
        return_value=httpx.Response(200, json={"count": 120, "results": make_page("b", PER_PAGE)})
    )
    page3 = respx.get(page_url(3)).mock(
        return_value=httpx.Response(200, json={"count": 120, "results": make_page("c", 20)})
    )
    assert len(list(make_source().fetch(query(), SINCE))) == 120
    assert page3.call_count == 1


@respx.mock
def test_pagination_stops_when_count_reached() -> None:
    respx.get(page_url(1)).mock(
        return_value=httpx.Response(200, json={"count": 50, "results": make_page("a", PER_PAGE)})
    )
    page2 = respx.get(page_url(2)).mock(return_value=httpx.Response(200, json={"results": []}))
    assert len(list(make_source().fetch(query(), SINCE))) == 50
    assert page2.call_count == 0


@respx.mock
def test_page_cap_warns(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"results": make_page(f"p{page}-", PER_PAGE)})

    route = respx.get(url__regex=r"https://api\.adzuna\.com/v1/api/jobs/fr/search/\d+").mock(
        side_effect=handler
    )
    with caplog.at_level(logging.WARNING):
        offers = list(make_source().fetch(query(), SINCE))
    assert route.call_count == MAX_PAGES
    assert len(offers) == MAX_PAGES * PER_PAGE
    assert "tronqués" in caplog.text
    assert "python" not in caplog.text


@respx.mock
def test_duplicates_and_invalid_items_are_skipped() -> None:
    good = fixture_body()["results"][0]
    bad = {**good, "id": "BAD", "title": "  "}
    no_date = {k: v for k, v in good.items() if k != "created"} | {"id": "NODATE"}
    respx.get(URL).mock(
        return_value=httpx.Response(200, json={"results": [good, good, bad, no_date, {"x": 1}]})
    )
    source = make_source()
    assert [o.source_id for o in source.fetch(query(), SINCE)] == ["4400001"]
    assert source.skipped == 2


@respx.mock
def test_non_json_response_raises() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="callback({})"))
    with pytest.raises(SourceHttpError):
        list(make_source().fetch(query(), SINCE))


# --- erreurs et secrets ----------------------------------------------------------------------


@respx.mock
def test_retry_on_429_then_success() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    assert list(make_source().fetch(query(), SINCE)) == []
    assert route.call_count == 2


@respx.mock
def test_empty_results() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"count": 0, "results": []}))
    assert list(make_source().fetch(query(), SINCE)) == []


@respx.mock
def test_secrets_never_appear_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(URL).mock(return_value=httpx.Response(401))
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceHttpError) as exc:
        list(make_source().fetch(query(), SINCE))
    for text in (caplog.text, str(exc.value), repr(exc.value)):
        assert APP_KEY not in text
        assert APP_ID not in text
    assert "app_key=***" in str(exc.value)
    assert exc.value.status_code == 401


@respx.mock
def test_secrets_masked_after_server_error(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(URL).mock(return_value=httpx.Response(503))
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceHttpError) as exc:
        list(make_source().fetch(query(), SINCE))
    assert APP_KEY not in caplog.text + str(exc.value)


def test_fixture_contains_no_secret_or_personal_data() -> None:
    text = FIXTURE.read_text()
    assert APP_KEY not in text
    assert "@" not in text
