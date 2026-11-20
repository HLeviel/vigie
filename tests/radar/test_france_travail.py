import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from vigie.radar.http import SourceHttpClient, SourceHttpError
from vigie.radar.models import ContractType, RemoteMode, SearchQuery
from vigie.radar.sources.france_travail import (
    SEARCH_URL,
    TOKEN_URL,
    FranceTravailSource,
    contract_from_raw,
    offer_from_raw,
)
from vigie.settings import MissingSettingsError, Settings

FIXTURE = Path(__file__).parent / "fixtures" / "france_travail_search.json"
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
SECRET = "s3cr3t-valeur-de-test"
CLIENT_ID = "id-valeur-de-test"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def fixture_results() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(FIXTURE.read_text())["resultats"]
    return data


def make_settings() -> Settings:
    return Settings(_env_file=None, ft_client_id=CLIENT_ID, ft_client_secret=SECRET)  # type: ignore[call-arg]


def make_source(clock: Clock | None = None) -> FranceTravailSource:
    clock = clock or Clock()
    client = SourceHttpClient("france_travail", min_interval=0, sleep=lambda s: None)
    return FranceTravailSource(make_settings(), client=client, now=lambda: NOW, monotonic=clock)


def query(**kw: Any) -> SearchQuery:
    data: dict[str, Any] = {"name": "q", "keywords": ["python"], "departments": ["69"]}
    data.update(kw)
    return SearchQuery(**data)


def token_route() -> respx.Route:
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1500})
    )


SINCE = NOW - timedelta(days=1)


def test_missing_secrets_are_named() -> None:
    with pytest.raises(MissingSettingsError) as exc:
        FranceTravailSource(Settings(_env_file=None))  # type: ignore[call-arg]
    assert "FT_CLIENT_ID" in str(exc.value)


# --- mapping ---------------------------------------------------------------------------------


def test_mapping_full_offer() -> None:
    offer = offer_from_raw(fixture_results()[0], NOW)
    assert offer.uid == "france_travail:190AAAA"
    assert offer.title == "Data Engineer (H/F)"
    assert offer.company == "Exemple SA"
    assert offer.location_label == "69 - LYON 03"
    assert offer.postal_code == "69003"
    assert offer.department == "69"
    assert offer.contract_type is ContractType.CDI
    assert (offer.salary_min, offer.salary_max) == (45000.0, 55000.0)
    assert offer.salary_label is not None
    assert offer.remote is RemoteMode.PARTIAL
    assert str(offer.url) == "https://candidat.francetravail.fr/offres/recherche/detail/190AAAA"
    assert offer.published_at == datetime(2026, 11, 18, 9, 30, tzinfo=UTC)
    assert offer.raw["id"] == "190AAAA"


def test_mapping_html_description_monthly_salary_partner_url() -> None:
    offer = offer_from_raw(fixture_results()[1], NOW)
    assert offer.description == "Mission de 6 mois.\nPas de teletravail."
    assert offer.remote is RemoteMode.NONE
    assert offer.contract_type is ContractType.CDD
    assert (offer.salary_min, offer.salary_max) == (39000.0, 39000.0)
    assert str(offer.url) == "https://partenaire.example.org/offre/42"


def test_mapping_missing_optional_fields() -> None:
    offer = offer_from_raw(fixture_results()[2], NOW)
    assert offer.company is None
    assert offer.contract_type is ContractType.INTERIM
    assert offer.salary_label is None
    assert offer.salary_min is None
    assert str(offer.url).endswith("/190CCCC")


def test_mapping_alternance_and_hourly_salary() -> None:
    offer = offer_from_raw(fixture_results()[3], NOW)
    assert offer.contract_type is ContractType.ALTERNANCE
    assert offer.salary_label == "Horaire de 12.0 Euros"
    assert offer.salary_min is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"typeContrat": "CDI"}, ContractType.CDI),
        ({"typeContrat": "MIS"}, ContractType.INTERIM),
        ({"typeContrat": "LIB"}, ContractType.FREELANCE),
        ({"typeContrat": "SAI"}, ContractType.AUTRE),
        ({"typeContrat": "CDD", "alternance": True}, ContractType.ALTERNANCE),
        (
            {"typeContrat": "CDD", "natureContrat": "Contrat de professionnalisation"},
            ContractType.ALTERNANCE,
        ),
        ({"typeContratLibelle": "Contrat à durée indéterminée"}, ContractType.CDI),
        ({}, ContractType.AUTRE),
    ],
)
def test_contract_from_raw(raw: dict[str, Any], expected: ContractType) -> None:
    assert contract_from_raw(raw) is expected


# --- authentification ------------------------------------------------------------------------


@respx.mock
def test_token_is_reused_then_renewed() -> None:
    clock = Clock()
    token = token_route()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(204))
    source = make_source(clock)
    list(source.fetch(query(keywords=["a", "b"]), SINCE))
    assert token.call_count == 1  # réutilisé pour les deux requêtes
    clock.t = 1500 - 59  # moins de 60 s avant expiration : renouvelé
    list(source.fetch(query(), SINCE))
    assert token.call_count == 2


@respx.mock
def test_token_request_and_search_headers() -> None:
    token = token_route()
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(204))
    list(make_source().fetch(query(), SINCE))
    body = token.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "scope=api_offresdemploiv2" in body
    assert search.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_401_renews_token_once(caplog: pytest.LogCaptureFixture) -> None:
    token = token_route()
    search = respx.get(SEARCH_URL).mock(side_effect=[httpx.Response(401), httpx.Response(204)])
    assert list(make_source().fetch(query(), SINCE)) == []
    assert token.call_count == 2
    assert search.call_count == 2


@respx.mock
def test_repeated_401_raises() -> None:
    token_route()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(SourceHttpError) as exc:
        list(make_source().fetch(query(), SINCE))
    assert exc.value.status_code == 401


@respx.mock
def test_no_secret_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    token_route()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceHttpError) as exc:
        list(make_source().fetch(query(), SINCE))
    for text in (caplog.text, str(exc.value), repr(exc.value)):
        assert SECRET not in text
        assert CLIENT_ID not in text
        assert "tok" not in text.replace("token", "")


# --- recherche et pagination -----------------------------------------------------------------


@respx.mock
def test_fetch_from_fixture_and_params() -> None:
    token_route()
    search = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"resultats": fixture_results()})
    )
    offers = list(
        make_source().fetch(query(contract_types=[ContractType.CDI, ContractType.FREELANCE]), SINCE)
    )
    assert [o.source_id for o in offers] == ["190AAAA", "190BBBB", "190CCCC", "190DDDD"]
    params = dict(search.calls.last.request.url.params)
    assert params["motsCles"] == "python"
    assert params["departement"] == "69"
    assert params["typeContrat"] == "CDI,LIB"
    assert params["range"] == "0-149"
    assert params["minCreationDate"] == "2026-11-19T06:00:00Z"
    assert params["maxCreationDate"] == "2026-11-20T06:00:00Z"


@respx.mock
def test_since_is_bounded_by_max_age() -> None:
    token_route()
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(204))
    list(make_source().fetch(query(max_age_days=2), NOW - timedelta(days=30)))
    assert search.calls.last.request.url.params["minCreationDate"] == "2026-11-18T06:00:00Z"


@respx.mock
def test_one_request_per_keyword_and_department() -> None:
    token_route()
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(204))
    list(make_source().fetch(query(keywords=["a", "b"], departments=["69", "75"]), SINCE))
    pairs = {
        (c.request.url.params["motsCles"], c.request.url.params["departement"])
        for c in search.calls
    }
    assert pairs == {("a", "69"), ("a", "75"), ("b", "69"), ("b", "75")}


@respx.mock
def test_commune_zone_and_unmapped_contracts_omit_filter() -> None:
    token_route()
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(204))
    q = SearchQuery(
        name="q",
        keywords=["python"],
        commune="69123",
        distance_km=20,
        contract_types=[ContractType.STAGE],
    )
    list(make_source().fetch(q, SINCE))
    params = dict(search.calls.last.request.url.params)
    assert (params["commune"], params["distance"]) == ("69123", "20")
    assert "typeContrat" not in params
    assert "departement" not in params


@respx.mock
def test_empty_response_204() -> None:
    token_route()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(204))
    assert list(make_source().fetch(query(), SINCE)) == []


def make_items(prefix: str, count: int) -> list[dict[str, Any]]:
    template = fixture_results()[0]
    return [{**template, "id": f"{prefix}{i}"} for i in range(count)]


@respx.mock
def test_pagination_stops_on_short_page() -> None:
    token_route()
    pages = {"0-149": make_items("a", 150), "150-299": make_items("b", 20)}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultats": pages[request.url.params["range"]]})

    search = respx.get(SEARCH_URL).mock(side_effect=handler)
    offers = list(make_source().fetch(query(), SINCE))
    assert len(offers) == 170
    assert [c.request.url.params["range"] for c in search.calls] == ["0-149", "150-299"]


@respx.mock
def test_cap_triggers_window_split_and_warning(caplog: pytest.LogCaptureFixture) -> None:
    token_route()

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        low = datetime.fromisoformat(p["minCreationDate"])
        high = datetime.fromisoformat(p["maxCreationDate"])
        if high - low > timedelta(hours=12):
            count = 150
        else:
            count = 5
        prefix = f"{p['minCreationDate']}-{p['range']}-"
        return httpx.Response(200, json={"resultats": make_items(prefix, count)})

    search = respx.get(SEARCH_URL).mock(side_effect=handler)
    with caplog.at_level(logging.WARNING):
        offers = list(make_source().fetch(query(), SINCE))
    ranges = [c.request.url.params["range"] for c in search.calls]
    assert ranges[:8] == [
        "0-149",
        "150-299",
        "300-449",
        "450-599",
        "600-749",
        "750-899",
        "900-1049",
        "1000-1149",
    ]
    assert len(search.calls) == 10  # 8 pages pour la fenêtre complète, 1 par moitié
    # les résultats de la fenêtre plafonnée sont écartés : les deux moitiés la couvrent
    assert len(offers) == 10
    assert "Plafond de 1150 offres atteint" in caplog.text
    assert "python" not in caplog.text


@respx.mock
def test_minimal_window_warns_truncated(caplog: pytest.LogCaptureFixture) -> None:
    token_route()
    respx.get(SEARCH_URL).mock(
        side_effect=lambda request: httpx.Response(
            200, json={"resultats": make_items(request.url.params["range"], 150)}
        )
    )
    with caplog.at_level(logging.WARNING):
        list(make_source().fetch(query(max_age_days=1), NOW - timedelta(minutes=30)))
    assert "tronqués" in caplog.text


@respx.mock
def test_duplicates_and_invalid_items_are_skipped() -> None:
    token_route()
    good = fixture_results()[0]
    bad = {**good, "id": "BAD", "intitule": "   "}
    no_date = {k: v for k, v in good.items() if k != "dateCreation"} | {"id": "NODATE"}
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"resultats": [good, good, bad, no_date, {"x": 1}]})
    )
    source = make_source()
    offers = list(source.fetch(query(), SINCE))
    assert [o.source_id for o in offers] == ["190AAAA"]
    assert source.skipped == 2


@respx.mock
def test_server_error_propagates() -> None:
    token_route()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(SourceHttpError):
        list(make_source().fetch(query(), SINCE))
