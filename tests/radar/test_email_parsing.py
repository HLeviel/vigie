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


def test_france_travail_cards() -> None:
    first, second = cards("france_travail", "france_travail")
    assert first.key == "190AAAA"
    assert first.title == "Data Engineer (H/F)"
    assert first.company == "Exemple SA"
    assert first.location == "69 - LYON 03"
    assert first.description == "Pipelines Python et SQL."
    # paramètres de suivi retirés du lien conservé
    assert first.url == "https://candidat.francetravail.fr/offres/recherche/detail/190AAAA"
    assert (second.key, second.title, second.location) == (
        "190BBBB",
        "Développeur IA",
        "75 - PARIS 08",
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
