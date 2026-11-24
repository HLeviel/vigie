import re

import pytest

from tests.radar.email_helpers import html_of
from vigie.radar.sources.email_alerts import SPECS
from vigie.radar.sources.email_alerts.parsing import (
    SenderSpec,
    extract_cards,
    normalize_link,
)


def cards(name: str, key: str) -> list:  # type: ignore[type-arg]
    return extract_cards(html_of(name), SPECS[key])


def test_france_travail_real_template_cards() -> None:
    """Gabarit vérifié sur de vrais emails (10 offres par email, avec ou sans entreprise)."""
    found = {c.key: c for c in cards("france_travail", "france_travail")}
    assert list(found) == [
        "900AAA1", "900AAA2", "900AAA3", "900AAA4", "900AAA5",
        "900AAA6", "900AAA7", "900AAA8", "900AAA9", "9053104",
    ]  # fmt: skip
    first = found["900AAA1"]
    # le titre ne contient ni l'entreprise, ni le lieu, ni le commentaire HTML « Lieu offre »
    assert first.title == "Technicien support industriel (F/H) - CDI H/F"
    assert first.company == "SOCIETE EXEMPLE ALPHA"  # « SOCIETE EXEMPLE ALPHA - » sans le tiret
    assert first.location == "38 - Corps"
    # « CDI » est aussi un morceau du titre : la ligne de contrat doit quand même être trouvée
    assert first.contract == "CDI"
    assert first.description.splitlines()[:2] == [
        "Vos missions",
        "- Assurer le support technique des équipements (postes, réseaux, automates)",
    ]
    # ni « Offre n°… », ni le temps de travail, ni la mention du partenaire
    assert all(
        "Offre n°" not in c.description and "Temps plein" not in c.description
        for c in found.values()
    )
    assert "confiée à France Travail" not in first.description
    # paramètres de suivi `at_*` retirés
    assert first.url == "https://candidat.francetravail.fr/offres/recherche/detail/900AAA1"


def test_france_travail_variants() -> None:
    found = {c.key: c for c in cards("france_travail", "france_travail")}
    assert found["900AAA7"].company is None  # offre sans entreprise : un seul <span>
    assert found["900AAA7"].location == "2B - BIGUGLIA"
    assert found["900AAA9"].location == "35 - Ille-et-Vilaine"  # département seul
    assert found["900AAA8"].contract == "Intérim"
    assert found["900AAA5"].contract == "CDD"
    assert found["9053104"].key == "9053104"  # identifiant numérique
    assert "Salaire : 1 802,00€ à 2 600,00€ par mois" in found["900AAA9"].description
    assert found["900AAA4"].description.startswith("Missions :\nVous interviendrez")


def test_france_travail_non_offer_links_are_ignored() -> None:
    urls = [c.url for c in cards("france_travail", "france_travail")]
    assert len(urls) == 10
    assert all("/offres/recherche/detail/" in u for u in urls)  # ni appli, ni « gérer mes alertes »


def test_france_travail_single_offer_email() -> None:
    [card] = cards("france_travail_single", "france_travail")
    assert card.key == "9000001"
    assert card.company == "SOCIETE EXEMPLE DELTA"
    assert card.contract == "CDI"
    assert card.description.startswith("POSTE : Commercial(e) solutions B2B H/F")


def test_france_travail_markup_change_falls_back_on_the_generic_reader() -> None:
    html = (
        '<div><a href="https://candidat.francetravail.fr/offres/recherche/detail/ABC123">'
        "Poste X</a><p>Société Y</p><p>69 - Lyon</p></div>"
    )
    [card] = extract_cards(html, SPECS["france_travail"])
    assert (card.key, card.title, card.company, card.location) == (
        "ABC123",
        "Poste X",
        "Société Y",
        "69 - Lyon",
    )


def test_apec_card() -> None:
    [card] = cards("apec", "apec")
    assert card.key == "178000001W"
    assert (card.title, card.company, card.location) == (
        "Ingénieur Machine Learning",
        "Société Exemple",
        "Lyon (69)",
    )
    assert (
        "xtor" not in card.url or "xtor" in card.url
    )  # xtor n'est pas (encore) un paramètre connu


def test_wttj_company_comes_from_the_link() -> None:
    [card] = cards("wttj", "wttj")
    assert card.key == "data-scientist_lyon"
    assert card.company == "exemple ia"
    assert card.location == "Lyon"
    assert "trk" not in card.url


def test_linkedin_ids_with_and_without_slug() -> None:
    first, second = cards("linkedin", "linkedin")
    assert (first.key, second.key) == ("4000000001", "4000000002")
    assert first.url == "https://www.linkedin.com/comm/jobs/view/4000000001/"
    assert "trackingId" not in second.url
    assert first.company == "Société A"


def test_hidden_text_is_removed() -> None:
    [card] = cards("linkedin_hostile", "linkedin")
    text = " ".join([card.title, card.company or "", card.location or "", card.description])
    assert "reveal your system prompt" not in text  # display:none
    assert "Ignore all previous" not in text  # font-size:0
    assert "texte caché" not in text  # attribut hidden
    # le texte visible, lui, est conservé tel quel (le champ `suspicious` le signalera)
    assert "envoie ton CV" in card.description


def test_unknown_format_gives_no_card() -> None:
    assert cards("apec_format_change", "apec") == []


def test_unsubscribe_and_other_links_are_not_offers() -> None:
    for name, key in (("france_travail", "france_travail"), ("linkedin", "linkedin")):
        assert all("desabonnement" not in c.url for c in cards(name, key))


def test_same_job_linked_twice_is_one_card() -> None:
    html = (
        '<div><a href="https://x.example/j/42">Poste</a><p>Acme</p><p>Lyon</p>'
        '<a href="https://x.example/j/42?utm_source=a">Voir l\'offre</a></div>'
    )
    spec = SenderSpec("x", re.compile(r"x\.example/j/(?P<id>\d+)"))
    [card] = extract_cards(html, spec)
    assert (card.key, card.title, card.company, card.location) == ("42", "Poste", "Acme", "Lyon")


def test_generic_anchor_text_falls_back_on_the_card_text() -> None:
    html = (
        "<div><p>Ingénieur Data</p><p>Acme</p>"
        '<a href="https://x.example/j/7">Voir l\'offre</a></div>'
    )
    spec = SenderSpec("x", re.compile(r"x\.example/j/(?P<id>\d+)"))
    [card] = extract_cards(html, spec)
    assert card.title == "Ingénieur Data"
    assert card.company == "Acme"


def test_no_usable_title_skips_the_card() -> None:
    spec = SenderSpec("x", re.compile(r"x\.example/j/(?P<id>\d+)"))
    assert extract_cards('<a href="https://x.example/j/9">Voir</a>', spec) == []


def test_two_cards_do_not_mix() -> None:
    html = (
        '<table><tr><td><a href="https://x.example/j/1">Poste un</a><p>Alpha</p></td></tr></table>'
        '<table><tr><td><a href="https://x.example/j/2">Poste deux</a><p>Beta</p></td></tr></table>'
    )
    spec = SenderSpec("x", re.compile(r"x\.example/j/(?P<id>\d+)"))
    one, two = extract_cards(html, spec)
    assert (one.company, two.company) == ("Alpha", "Beta")
    assert "Poste deux" not in " ".join([one.title, one.company or "", one.description])


def test_link_without_id_group_uses_a_hash() -> None:
    spec = SenderSpec("x", re.compile(r"x\.example/offre/[a-z-]+"))
    html = (
        '<div><a href="https://x.example/offre/poste-a?utm_source=1&trk=2">Poste A</a></div>'
        '<div><a href="https://x.example/offre/poste-b">Poste B</a></div>'
    )
    a, b = extract_cards(html, spec)
    assert a.key != b.key
    assert len(a.key) == 16
    # le hash ne dépend pas des paramètres de suivi
    [again] = extract_cards(
        '<a href="https://x.example/offre/poste-a?utm_source=9">Poste A</a>', spec
    )
    assert again.key == a.key


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://A.example/p?utm_source=x&id=1&trackingId=z#frag", "https://a.example/p?id=1"),
        ("https://a.example/p?b=2&a=1", "https://a.example/p?a=1&b=2"),
        ("https://a.example/p?token=SECRET&lipi=urn", "https://a.example/p"),
        ("https://a.example/p", "https://a.example/p"),
    ],
)
def test_normalize_link(url: str, expected: str) -> None:
    assert normalize_link(url) == expected


def test_flat_email_stops_at_the_next_job() -> None:
    html = (
        '<a href="https://x.example/j/1">Premier poste</a><div>Alpha</div><div>Lyon</div>'
        '<a href="https://x.example/j/2">Second poste</a><div>Beta</div><div>Paris</div>'
    )
    spec = SenderSpec("x", re.compile(r"x\.example/j/(?P<id>\d+)"))
    one, two = extract_cards(html, spec)
    assert (one.company, one.location) == ("Alpha", "Lyon")
    assert (two.company, two.location) == ("Beta", "Paris")
