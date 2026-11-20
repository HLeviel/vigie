from datetime import timedelta

from tests.radar.factories import NOW, make_offer
from vigie.radar.config import FiltersConfig
from vigie.radar.filters import FilterSet
from vigie.radar.models import ContractType, SearchQuery

QUERY = SearchQuery(name="q", keywords=["a"], departments=["69"], max_age_days=7)


def reason(
    filters: FiltersConfig | None = None, query: SearchQuery = QUERY, **offer: object
) -> str | None:
    return FilterSet(filters or FiltersConfig()).reason(make_offer(**offer), query, NOW)


def test_offer_passes_without_rules() -> None:
    assert reason() is None


def test_age() -> None:
    old = NOW - timedelta(days=8)
    assert reason(published_at=old) == "age"
    assert reason(published_at=NOW - timedelta(days=6)) is None


def test_contract_type() -> None:
    query = SearchQuery(name="q", keywords=["a"], contract_types=[ContractType.CDI])
    assert reason(query=query, contract_type=ContractType.CDD) == "contract"
    assert reason(query=query, contract_type=ContractType.CDI) is None
    # contrat inconnu : conservé
    assert reason(query=query, contract_type=ContractType.AUTRE) is None
    # aucun type demandé : aucun filtre
    assert reason(contract_type=ContractType.STAGE) is None


def test_exclude_title_whole_words_ignoring_case_and_accents() -> None:
    f = FiltersConfig(exclude_title=["stage", "Commercial", "data analyst"])
    assert reason(f, title="Stage - Data Engineer") == "title:stage"
    assert reason(f, title="Ingénieur Commercial") == "title:Commercial"
    assert reason(f, title="Senior Data Analyst H/F") == "title:data analyst"
    assert reason(f, title="Stagiaire data") is None  # « stage » n'est pas un mot entier ici
    assert reason(f, title="Backstage engineer") is None
    assert reason(f, title="Data Engineer") is None


def test_exclude_companies() -> None:
    f = FiltersConfig(exclude_companies=["Infotel"])
    assert reason(f, company="Infotel Corp.") == "company:Infotel"
    assert reason(f, company="INFOTEL") == "company:Infotel"
    assert reason(f, company="Autre") is None
    assert reason(f, company=None) is None


def test_require_any_on_title_and_description() -> None:
    f = FiltersConfig(require_any=["python", "ia"])
    assert reason(f, title="Poste", description="Du python partout") is None
    assert reason(f, title="Ingénieur IA", description="") is None
    assert (
        reason(f, title="Poste", description="Gestion sociale") == "require_any"
    )  # « ia » != « social »


def test_order_age_before_contract_before_title_before_company_before_require() -> None:
    f = FiltersConfig(exclude_title=["stage"], exclude_companies=["Acme"], require_any=["rust"])
    query = SearchQuery(name="q", keywords=["a"], contract_types=[ContractType.CDI])
    everything = {
        "published_at": NOW - timedelta(days=30),
        "contract_type": ContractType.CDD,
        "title": "Stage",
    }
    assert reason(f, query, **everything) == "age"
    everything.pop("published_at")
    assert reason(f, query, **everything) == "contract"
    everything.pop("contract_type")
    assert reason(f, query, **everything) == "title:stage"
    assert reason(f, query, title="Dev") == "company:Acme"
    assert reason(f, query, title="Dev", company="Autre") == "require_any"


def test_blank_terms_are_ignored() -> None:
    assert reason(FiltersConfig(exclude_title=["", "   "], require_any=[" "])) is None
